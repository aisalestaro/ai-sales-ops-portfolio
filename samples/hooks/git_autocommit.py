#!/usr/bin/env python3
"""未コミットの変更を機械的にコミットする（全プロジェクト共通・2026-09-14）。

用途:
  1) Claude Code の Stop フック（~/.claude/settings.json）
       stdin の JSON から cwd を取り、その git リポジトリの残り変更をコミットする
  2) launchd の巡回（例: com.example.git-autocommit・10分毎）
       --repo <path> --trigger launchd --min-age 300
       ルーティンが書き換えた台帳をセッションが無い時間帯にも拾う

方針:
  - commit のあと、remote(origin) があれば push まで行う（2026-09-14 責任者指示・未 push の積み残しも拾う）
    --no-push で commit のみ。HP は push＝Vercel 本番デプロイだが、ビルド失敗時は Vercel が前の版を残す
  - .gitignore はそのまま尊重（git add -A）
  - マージ/リベース途中・ルート直下に .no-autocommit がある repo は触らない
  - --min-age N: 直近 N 秒以内に更新されたファイルは対象外（編集途中の混入を減らす）
  - Stop フック（2026-09-14 追記）: 並行する他セッションの「編集途中」を巻き込まないよう、
    このセッション（とそのサブエージェント）が Edit/Write したファイルは即コミット、
    それ以外は --others-min-age 秒（既定300）触られていないものだけコミットする。transcript が読めなければ従来どおり
  - push が拒否されたら（2026-09-14 追記）: fetch → GitHub 側の変更と手元の未コミット変更が同じファイルでなければ
    rebase --autostash で積み直して再 push。重なる／rebase が衝突する場合は中止して手元を元に戻し、ログ＋macOS 通知
  - launchd 巡回では手元が GitHub より遅れていて手元に未 push が無ければ fast-forward で取り込む
    （クラウドセッションや GitHub 上でマージした分を、ルーティンが使う手元へ10分以内に反映）
  - 入れ子の独立 repo（親から ignore されていて .git を持つ直下ディレクトリ・例 HP/）も同じ規則で別コミット
    --no-nested で親だけ／--nested-push-only で入れ子は push だけ（launchd 巡回＝HP を編集途中で半端に commit しないが、
    セッションが commit 済みで push し忘れた分は10分以内に届く）
  - 失敗しても必ず exit 0（フック/ルーティン本体を止めない）。記録は ~/.claude/hooks/git_autocommit.log
"""
import glob, json, os, subprocess, sys, time, argparse
from datetime import datetime

LOG = os.environ.get("GIT_AUTOCOMMIT_LOG") or os.path.expanduser("~/.claude/hooks/git_autocommit.log")
GIT = "/usr/bin/git"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def notify(msg):
    """人の判断が要る失敗だけ macOS 通知を出す（画面ロック中でも通知センターに残る）"""
    if os.environ.get("GIT_AUTOCOMMIT_NO_NOTIFY"):
        return
    try:
        m = msg.replace("\\", "\\\\").replace('"', '\\"')[:220]
        subprocess.run(["osascript", "-e", f'display notification "{m}" with title "git 自動コミット：要確認"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def run(args, cwd, timeout=60):
    # 認証が無い remoteでパスワード入力待ちにならず即失敗させる（2026-09-14）
    # rebase 等がエディタを開いて固まらないよう GIT_EDITOR=true
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    return subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def repo_root(path):
    r = run(["rev-parse", "--show-toplevel"], path)
    return r.stdout.strip() if r.returncode == 0 else None


def in_progress(root):
    g = os.path.join(root, ".git")
    if os.path.isfile(g):  # worktree
        try:
            g = open(g).read().split("gitdir:", 1)[1].strip()
        except Exception:
            return False
    return any(os.path.exists(os.path.join(g, n)) for n in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def session_edited_files(transcript_path):
    """このセッション（＋サブエージェント）が Edit/Write したファイルの絶対パス集合。読めなければ None"""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    files = [transcript_path]
    files += glob.glob(os.path.join(os.path.splitext(transcript_path)[0], "subagents", "*.jsonl"))
    out = set()
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"tool_use"' not in line:
                        continue
                    try:
                        content = (json.loads(line).get("message") or {}).get("content")
                    except Exception:
                        continue
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in EDIT_TOOLS:
                            inp = c.get("input") or {}
                            p = inp.get("file_path") or inp.get("notebook_path")
                            if p:
                                out.add(os.path.realpath(p))
        except OSError:
            continue
    return out


def status_entries(root):
    r = run(["status", "--porcelain=v1", "--untracked-files=all", "-z"], root)
    if r.returncode != 0:
        return None, r.stderr.strip()
    items = [x for x in r.stdout.split("\0") if x]
    out, i = [], 0
    while i < len(items):
        ent = items[i]; i += 1
        st, p = ent[:2], ent[3:]
        src = None
        if "R" in st or "C" in st:  # rename/copy: next item is the source path
            src = items[i] if i < len(items) else None
            i += 1
        out.append((p, src))
    return out, None


def changed_paths(root, min_age, own=None, others_min_age=0):
    ents, err = status_entries(root)
    if err:
        return None, err
    paths, skipped = [], 0
    now = time.time()
    for p, _src in ents:
        full = os.path.join(root, p)
        limit = min_age
        if own is not None:
            limit = max(min_age, 0) if os.path.realpath(full) in own else max(min_age, others_min_age)
        if limit > 0 and os.path.exists(full):
            try:
                if now - os.path.getmtime(full) < limit:
                    skipped += 1
                    continue
            except OSError:
                pass
        paths.append(p)
    return (paths, skipped), None


def commit(root, paths, trigger, label):
    for attempt in range(5):
        if os.path.exists(os.path.join(root, ".git", "index.lock")):
            time.sleep(1.5); continue
        a = run(["add", "-A", "--", *paths], root)
        if a.returncode != 0:
            if "index.lock" in a.stderr:
                time.sleep(1.5); continue
            return None, f"add失敗: {a.stderr.strip()[:200]}"
        break
    tops = sorted({p.split("/", 1)[0] for p in paths})
    head = f"chore(auto): {label}の未コミット変更を自動コミット（{len(paths)}件・{'/'.join(tops)[:80]}）"
    body = "\n".join(f"- {p}" for p in paths[:40]) + ("\n- …" if len(paths) > 40 else "")
    msg = f"{head}\n\ntrigger={trigger}\n{body}\n"
    # --only: 他セッションが index に積んだ別パスは巻き込まない
    c = run(["commit", "-q", "--only", "-m", msg, "--", *paths], root)
    if c.returncode != 0:
        return None, f"commit失敗: {c.stderr.strip()[:200]}"
    h = run(["rev-parse", "--short", "HEAD"], root).stdout.strip()
    return h, None


def current_branch(root):
    br = run(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
    return None if not br or br == "HEAD" else br


def dirty_set(root):
    ents, err = status_entries(root)
    if err:
        return set()
    return {p for e in ents for p in e if p}


def incoming_paths(root, br):
    """merge-base から origin/<br> までに GitHub 側で変わったパス"""
    r = run(["diff", "--name-only", "-z", f"HEAD...origin/{br}"], root)
    return {x for x in r.stdout.split("\0") if x} if r.returncode == 0 else set()


def ahead_behind(root, br):
    r = run(["rev-list", "--left-right", "--count", f"HEAD...origin/{br}"], root)
    if r.returncode != 0:
        return None, None
    a, b = (r.stdout.split() + ["0", "0"])[:2]
    return int(a), int(b)


def sync_down(root):
    """launchd 用: 手元に未 push が無く GitHub が先に進んでいたら fast-forward で取り込む"""
    br = current_branch(root)
    if not br or in_progress(root) or run(["remote", "get-url", "origin"], root).returncode != 0:
        return None
    if run(["fetch", "-q", "origin", br], root, timeout=120).returncode != 0:
        return None
    ahead, behind = ahead_behind(root, br)
    if not behind or ahead:
        return None  # 遅れていない／手元にも未 push がある（→ push 時に rebase で合流）
    overlap = incoming_paths(root, br) & dirty_set(root)
    if overlap:
        m = f"取り込み保留 {root}: GitHub側の変更と手元の未コミット変更が同じファイル（{', '.join(sorted(overlap)[:5])}）"
        notify(m)
        return "⚠ " + m
    r = run(["merge", "--ff-only", "-q", f"origin/{br}"], root, timeout=120)
    if r.returncode != 0:
        return f"⚠ 取り込み失敗 {root}: {r.stderr.strip()[:160]}"
    return f"取り込み {root} ← origin/{br} {behind}件（fast-forward）"


def recover_rejected_push(root, br):
    """push 拒否時: fetch → 重なり確認 → rebase --autostash。戻り値=(ok, msg)"""
    if run(["fetch", "-q", "origin", br], root, timeout=120).returncode != 0:
        return False, "fetch失敗"
    ahead, behind = ahead_behind(root, br)
    if not behind:
        return False, None  # 遅れていない＝拒否の理由は別（認証など）
    if in_progress(root):
        return False, "merge/rebase中のため積み直し保留"
    overlap = incoming_paths(root, br) & dirty_set(root)
    if overlap:
        m = f"GitHub側の変更と手元の未コミット変更が同じファイル（{', '.join(sorted(overlap)[:5])}）＝積み直し中止"
        notify(f"{os.path.basename(root)}: {m}")
        return False, m
    r = run(["rebase", "--autostash", f"origin/{br}"], root, timeout=300)
    if r.returncode != 0:
        u = run(["diff", "--name-only", "--diff-filter=U"], root).stdout.split()
        run(["rebase", "--abort"], root, timeout=120)
        m = f"コミット同士が衝突（{', '.join(u[:5]) or '詳細不明'}）＝rebase中止・手元は元のまま"
        notify(f"{os.path.basename(root)}: {m}")
        return False, m
    return True, f"GitHub側の{behind}件の上に積み直し"


def push_if_needed(root):
    """origin があり、未 push のコミットがあれば push する。戻り値=('pushed', n, note) | ('none',0,None) | ('error', msg, None)"""
    if run(["remote", "get-url", "origin"], root).returncode != 0:
        return ("none", 0, None)
    br = current_branch(root)
    if not br:
        return ("none", 0, None)
    up = run(["rev-parse", "--abbrev-ref", "@{u}"], root)
    if up.returncode != 0:
        # upstream 未設定＝初回。origin/<br> があればそれと比較、無ければ push -u
        ahead = None
    else:
        a = run(["rev-list", "--count", "@{u}..HEAD"], root)
        ahead = int(a.stdout.strip() or 0) if a.returncode == 0 else None
        if ahead == 0:
            return ("none", 0, None)
    note = None
    r = None
    for attempt in range(3):
        r = run(["push", "-q", "-u", "origin", br], root, timeout=300)
        if r.returncode == 0:
            return ("pushed", ahead if ahead is not None else -1, note)
        err = r.stderr or ""
        if attempt < 2 and ("rejected" in err or "fetch first" in err or "non-fast-forward" in err):
            ok, m = recover_rejected_push(root, br)
            if ok:
                note = m
                continue
            if m:
                return ("error", f"push拒否: {m}", None)
        time.sleep(3)
    return ("error", r.stderr.strip().splitlines()[-1][:160] if r is not None and r.stderr.strip() else "push失敗", None)


def nested_repos(root):
    """親から ignore されている直下ディレクトリのうち .git を持つもの（例 HP/）"""
    out = []
    r = run(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"], root)
    for d in r.stdout.split("\0"):
        d = d.rstrip("/")
        if d and "/" not in d and os.path.isdir(os.path.join(root, d, ".git")):
            out.append(os.path.join(root, d))
    return out


def push_msg(root, msg, res):
    st, n, note = res
    if st == "pushed":
        return (msg or f"push {root}") + f" → push済み({n if n >= 0 else '初回'}件" + (f"・{note}" if note else "") + ")"
    if st == "error":
        return (msg or f"push {root}") + f" ⚠ push失敗: {n}"
    return msg


def process(root, trigger, min_age, label, do_push=True, own=None, others_min_age=0):
    if os.path.exists(os.path.join(root, ".no-autocommit")):
        return f"skip(.no-autocommit) {root}"
    if in_progress(root):
        return f"skip(merge/rebase中) {root}"
    res, err = changed_paths(root, min_age, own, others_min_age)
    if err:
        return f"status失敗 {root}: {err}"
    paths, skipped = res
    msg = None
    wait = "他セッションが編集中の可能性" if own is not None else "編集中"
    if paths:
        h, err = commit(root, paths, trigger, label)
        if err:
            return f"失敗 {root}: {err}"
        msg = f"commit {h} {root} {len(paths)}件" + (f"（{skipped}件は{wait}のため次回）" if skipped else "")
    elif skipped:
        msg = f"待機 {root}: {wait}の更新{skipped}件"
    if do_push:
        msg = push_msg(root, msg, push_if_needed(root))
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="対象パス（省略時は stdin JSON の cwd → カレント）")
    ap.add_argument("--trigger", default="stop", help="stop | launchd | manual")
    ap.add_argument("--min-age", type=int, default=0, help="この秒数以内に更新されたファイルは対象外")
    ap.add_argument("--others-min-age", type=int, default=300,
                    help="Stop フック: このセッションが編集していないファイルはこの秒数触られていなければコミット")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-push", action="store_true", help="commit のみ（push しない）")
    ap.add_argument("--no-nested", action="store_true", help="入れ子の独立 repo（HP/ 等）は触らない")
    ap.add_argument("--nested-push-only", action="store_true", help="入れ子の独立 repo は commit せず未 push 分の push だけ")
    args = ap.parse_args()

    path = args.repo
    data = {}
    if not args.repo and not sys.stdin.isatty():  # --repo 指定（launchd・手動）では stdin を待たない
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
    path = path or data.get("cwd") or os.getcwd()
    own = session_edited_files(data.get("transcript_path")) if args.trigger == "stop" else None

    root = repo_root(path)
    if not root:
        # 2026-09-16: launchd 経路が /usr/bin/python3 起動だと ~/Desktop 配下が macOS TCC で読めず、ここで無音終了していた
        # （9/14 15:38 以降 [launchd] 行がゼロ）。黙って帰らず理由を残す。plist は Python.app 起動に変更済み。
        if os.path.isdir(path):
            r = run(["rev-parse", "--show-toplevel"], path)
            log(f"[{args.trigger}] repo_root 取得失敗 {path}: {(r.stderr or r.stdout).strip()[:160]}（TCC でディレクトリが読めない可能性）")
        return 0
    label = {"stop": "セッション終了時", "launchd": "定時巡回"}.get(args.trigger, args.trigger)
    results = []
    for r in [root, *([] if args.no_nested else nested_repos(root))]:
        try:
            if args.trigger == "launchd" and not args.no_push:
                m = sync_down(r)
                if m:
                    results.append(m); log(f"[{args.trigger}] {m}")
            if r != root and args.nested_push_only:
                m = push_msg(r, None, push_if_needed(r))
            else:
                m = process(r, args.trigger, args.min_age, label, do_push=not args.no_push,
                            own=own, others_min_age=args.others_min_age)
        except Exception as e:
            m = f"例外 {r}: {e}"
        if m:
            results.append(m); log(f"[{args.trigger}] {m}")
    done = [m for m in results if m.startswith(("commit ", "push ", "取り込み ", "⚠ ")) or "push失敗" in m]
    if done and args.trigger == "stop" and not args.quiet:
        # Stop フックの JSON 出力＝画面に1行だけ出す
        line = "🧾 自動コミット: " + " / ".join(m.split(" ", 1)[1].replace(root, os.path.basename(root)) for m in done)
        print(json.dumps({"systemMessage": line, "suppressOutput": True}, ensure_ascii=False))
    elif done and not args.quiet:
        print("\n".join(done))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(0)
