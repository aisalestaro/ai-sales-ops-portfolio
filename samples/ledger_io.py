#!/usr/bin/env python3
"""台帳 JSON（crm/data/deals.json 等）を複数のルーティン・セッションが同時に書き換えても
更新が消えないようにする共通 I/O（2026-09-14）。

背景:
  取り込みバッチ（毎時）・ダッシュボード同期（10分毎）・提案生成・ネクストアクション正規化など
  複数のルーティンが同じ deals.json を「全体を読む → 書き換える → 全体を書く」。
  2本が重なると、後から書いた方が先の更新を丸ごと消していた（しかも警告が出ない）。

使い方（置き換えは2行だけ）:
    import ledger_io
    data = ledger_io.load(DEALS, {"deals": []})   # 読んだ時点の内容を「基準」として覚える
    ...data を書き換える...
    ledger_io.save(DEALS, data)                    # 基準からの自分の変更だけを最新ファイルへ重ねて書く

save の中身:
  1) ロック（fcntl.flock・プロセスが死ねば自動で外れる）
  2) 最新ファイルを読み直す。load 以降に誰も書いていなければそのまま書く
  3) 誰かが書いていたら 3-way マージ（基準→自分の差分を最新に適用）
     - dict はキー単位、id を持つ要素のリスト（deals / next_actions 等）は id 単位で合流
     - 同じ値を双方が別々に書き換えた場合だけ「自分（後から保存した側）」を採用し、crm/ledger_merge.log に記録
  4) 一時ファイル → os.replace で原子的に置き換え（読み手が書きかけの JSON を掴まない）
  5) 呼び出し側の data をマージ結果に「その場で」合わせる（deal 等の参照を保ったまま）＝続けて save しても安全

load を通さずに作ったオブジェクトを save した場合は、ロック＋原子的書き込みだけ行う（＝従来どおり上書き）。
"""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MERGE_LOG = ROOT / "crm" / "ledger_merge.log"
LOCK_DIR = Path.home() / "Library" / "Caches" / "ledger-locks"
LOCK_WAIT_SEC = 60

_MISSING = object()
# id(obj) -> (obj, 正規化パス, 基準スナップショット)。obj を強参照で持つので id の再利用は起きない
_BASES = {}


# ── ロック ─────────────────────────────────────────────
class _Lock:
    def __init__(self, path):
        p = str(Path(path).resolve())
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(p).name + "-" + hashlib.sha1(p.encode()).hexdigest()[:10] + ".lock"
        self.lock_path = LOCK_DIR / name
        self.fh = None

    def __enter__(self):
        self.fh = open(self.lock_path, "a+")
        deadline = time.time() + LOCK_WAIT_SEC
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    # 保持時間はミリ秒単位のはず。60秒取れない＝固まったプロセス。ルーティンは止めずに進む
                    _log(f"⚠ ロック待ち{LOCK_WAIT_SEC}秒超過のため無ロックで続行: {self.lock_path.name}")
                    return self
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def locked(path):
    """with ledger_io.locked(path): ... ＝読み書きの間ずっと排他したいとき用（短時間に限る）"""
    return _Lock(path)


# ── 読み書き ───────────────────────────────────────────
def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except json.JSONDecodeError:
        if default is _MISSING:
            raise
        return copy.deepcopy(default)


def write_json_atomic(path, obj, indent=2, trailing_newline=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=indent) + ("\n" if trailing_newline else "")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def load(path, default=_MISSING):
    """読み込み＋基準の記憶。default 指定時はファイル無し・壊れたJSONで default を返す"""
    with _Lock(path):
        data = _read(path, default)
    if isinstance(data, (dict, list)):
        _BASES[id(data)] = (data, str(Path(path).resolve()), copy.deepcopy(data))
    return data


def save(path, data, indent=2, trailing_newline=True):
    """基準からの自分の変更を最新ファイルへ重ねて原子的に書く。戻り値=実際に書いた内容（data と同値）"""
    rp = str(Path(path).resolve())
    ent = _BASES.get(id(data))
    with _Lock(path):
        if ent is None or ent[0] is not data or ent[1] != rp:
            merged = data  # 基準なし＝従来どおりの上書き
        else:
            base = ent[2]
            current = _read(path, None)
            if current is None or current == base:
                merged = data
            else:
                conflicts = []
                merged = _merge3(base, data, current, conflicts, "")
                _log(f"🔀 {Path(path).name}: 読み込み後に他の処理が更新していたためマージ"
                     + (f"（双方が同じ値を変更＝後から保存した側を採用: {', '.join(conflicts[:8])}"
                        + (" …" if len(conflicts) > 8 else "") + "）" if conflicts else "（衝突なし）"))
        write_json_atomic(path, merged, indent=indent, trailing_newline=trailing_newline)
    if merged is not data:
        _assign_inplace(data, merged)
    if isinstance(data, (dict, list)):
        _BASES[id(data)] = (data, rp, copy.deepcopy(data))
    return data


# ── 3-way マージ ───────────────────────────────────────
def _is_id_list(*lists):
    seen = False
    for lst in lists:
        if not isinstance(lst, list):
            return False
        for x in lst:
            if not (isinstance(x, dict) and "id" in x):
                return False
            seen = True
    return seen


def _merge3(base, mine, cur, conflicts, where):
    if mine == base:
        return cur
    if cur == base or mine == cur:
        return mine
    if isinstance(mine, dict) and isinstance(cur, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        keys = list(cur.keys()) + [k for k in mine.keys() if k not in cur]
        for k in keys:
            bv, mv, cv = b.get(k, _MISSING), mine.get(k, _MISSING), cur.get(k, _MISSING)
            v = _merge_val(bv, mv, cv, conflicts, f"{where}.{k}" if where else str(k))
            if v is not _MISSING:
                out[k] = v
        return out
    if _is_id_list(mine, cur) and (base is _MISSING or base is None or isinstance(base, list)):
        b = base if isinstance(base, list) and _is_id_list(base) or base == [] else []
        bm = {x["id"]: x for x in b}
        mm = {x["id"]: x for x in mine}
        out = []
        for x in cur:
            i = x["id"]
            v = _merge_val(bm.get(i, _MISSING), mm.get(i, _MISSING), x, conflicts, f"{where}[{i}]")
            if v is not _MISSING:
                out.append(v)
        for x in mine:
            i = x["id"]
            if i not in {y["id"] for y in cur}:
                v = _merge_val(bm.get(i, _MISSING), x, _MISSING, conflicts, f"{where}[{i}]")
                if v is not _MISSING:
                    out.append(v)
        return out
    conflicts.append(where or "(root)")
    return mine


def _merge_val(b, m, c, conflicts, where):
    if m == b:          # 自分は触っていない（双方欠落も含む）
        return c
    if c == b:          # 相手は触っていない
        return m
    if m == c:
        return m
    if m is _MISSING:   # 自分が消した × 相手が変更 → 変更を残す
        conflicts.append(f"{where}(削除vs変更)")
        return c
    if c is _MISSING:   # 相手が消した × 自分が変更 → 変更を残す
        conflicts.append(f"{where}(変更vs削除)")
        return m
    return _merge3(b, m, c, conflicts, where)


def _assign_inplace(target, src):
    """target を src と同値にする。id が同じ要素・同じキーの dict/list は同一オブジェクトを再利用する"""
    if isinstance(target, dict) and isinstance(src, dict):
        for k in [k for k in target if k not in src]:
            del target[k]
        for k, v in src.items():
            tv = target.get(k, _MISSING)
            if isinstance(tv, (dict, list)) and type(tv) is type(v) and tv is not v:
                _assign_inplace(tv, v)
            else:
                target[k] = v
        # キー順も src に揃える（書き出し順の安定）
        items = [(k, target[k]) for k in src]
        target.clear()
        target.update(items)
        return
    if isinstance(target, list) and isinstance(src, list):
        if _is_id_list(target) and _is_id_list(src):
            old = {x["id"]: x for x in target}
            new = []
            for x in src:
                o = old.get(x["id"])
                if o is not None and o is not x:
                    _assign_inplace(o, x)
                    new.append(o)
                else:
                    new.append(x)
            target[:] = new
        else:
            target[:] = src
        return
    raise TypeError("root の型が違うため in-place 更新できない")


def _log(msg):
    try:
        with open(MERGE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} {msg}\n")
    except Exception:
        pass
