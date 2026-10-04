#!/usr/bin/env python3
"""定時タスク登録簿→ tasks.json ＋ launchd plist を生成・登録する。

使い方:
  gen_plists.py                 tasks.json と plist を生成（登録はしない）
  gen_plists.py --install       生成＋launchctl bootstrap（既存は入れ替え）
  gen_plists.py --uninstall     全 plist を bootout して削除
  gen_plists.py --list          登録状況を表示

入力: registry.json（登録簿のコピー。id/cronExpression/cwd/enabled）＋ overrides.json（model/chrome/timeout_min/skill/net_retry_window_min）
cron → StartCalendarInterval: リスト・範囲・ステップは全展開（launchd は */3 を解釈しないため）。曜日 7 は 0 に正規化。

--install の安全弁（2026-09-14 の中断事故 改善項目）:
  launchctl bootout は「実行中のジョブ」も SIGTERM で落とす。従来は --install のたびに全ラベルを
  bootout→bootstrap し直していたため、新しいタスクを1本足すだけで、その瞬間走っていた別のタスクが
  道連れで死んだ（新規タスク登録時に実行中の別タスクが中断）。そこで --install は
    ① plist の内容が変わっておらず、すでに登録済みのラベル → bootout も bootstrap もしない
    ② 実行中（run_task.py のロックが握られている）ラベル → 今回は見送り、完走後の再実行を促す
  とする。
"""
import fcntl
import itertools
import json
import pathlib
import plistlib
import subprocess
import sys

HOME = pathlib.Path.home()
HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HOME / "Library/LaunchAgents"
PY_APP = "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
LABEL = "com.example.routine."
LOGS = HOME / ".config/routines/logs"
LOCKS = HOME / ".config/routines/locks"


def expand(field: str, lo: int, hi: int):
    if field == "*":
        return [None]
    out = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/"); step = int(s)
        if part == "*":
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-"); rng = range(int(a), int(b) + 1)
        else:
            rng = range(int(part), int(part) + 1)
        out.update(rng[::step] if step > 1 else rng)
    return sorted(out)


def cron_to_intervals(expr: str):
    m, h, dom, mon, dow = expr.split()
    combos = []
    for mi, hi, di, moi, wi in itertools.product(
        expand(m, 0, 59), expand(h, 0, 23), expand(dom, 1, 31), expand(mon, 1, 12), expand(dow, 0, 7)
    ):
        d = {}
        if mi is not None: d["Minute"] = mi
        if hi is not None: d["Hour"] = hi
        if di is not None: d["Day"] = di
        if moi is not None: d["Month"] = moi
        if wi is not None: d["Weekday"] = 0 if wi == 7 else wi
        combos.append(d)
    # 重複除去
    uniq = []
    for c in combos:
        if c not in uniq: uniq.append(c)
    return uniq


def uid():
    return subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()


def running(name: str) -> bool:
    """そのタスクが今まさに実行中か（run_task.py が握る実行ロックで判定）。"""
    path = LOCKS / f"{name}.lock"
    if not path.exists():
        return False
    try:
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def registered_labels() -> set:
    """launchctl list に今いるラベル一覧。"""
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    return {ln.split("\t")[-1].strip() for ln in r.splitlines()[1:] if ln.strip()}


def main():
    args = sys.argv[1:]
    reg = json.loads((HERE / "registry.json").read_text(encoding="utf-8"))
    ov = json.loads((HERE / "overrides.json").read_text(encoding="utf-8")) if (HERE / "overrides.json").exists() else {}
    tasks = {}
    for t in reg:
        if not t.get("enabled") or not t.get("cronExpression"):
            continue
        name = t["id"]
        o = ov.get(name, {})
        tasks[name] = {
            "cron": t["cronExpression"], "cwd": t["cwd"],
            "model": o.get("model", ov.get("_default", {}).get("model", "claude-opus-5-5")),
            "chrome": bool(o.get("chrome", False)),
            "timeout_min": o.get("timeout_min", ov.get("_default", {}).get("timeout_min", 180)),
        }
        if o.get("skill"): tasks[name]["skill"] = o["skill"]
        # ブラウザ別名（chrome_device_map.py の alias）。run_task.py が起動前に pair する（2026-09-12 v3）
        if o.get("browser"): tasks[name]["browser"] = o["browser"]
        # SNS の表示アカウント。run_task.py が起動前に合わせる
        if o.get("account_handle"): tasks[name]["account_handle"] = o["account_handle"]
        if o.get("config_dir"): tasks[name]["config_dir"] = o["config_dir"]
        # 利用上限で起動直後に落ちたとき resets 時刻まで待って再実行する上限（分）。週次など「次の枠」が遠いタスク用
        if o.get("limit_retry_max_min"): tasks[name]["limit_retry_max_min"] = int(o["limit_retry_max_min"])
        # ネット断で起動直後に落ちたとき復旧を待つ窓（分）。既定120＝1〜2時間の断は遅れて実行、それ以上は見送り。
        # 週次など「次の枠」が遠いタスクは overrides で長め（360）にしてある。0 で待たない
        nw = o.get("net_retry_window_min", ov.get("_default", {}).get("net_retry_window_min"))
        if nw is not None: tasks[name]["net_retry_window_min"] = int(nw)
    for name, e in ov.get("_extra", {}).items():  # 登録簿に無い補助タスク（selftest等）
        tasks[name] = {"cron": e["cron"], "cwd": e["cwd"], "model": e.get("model", "claude-opus-5-5"),
                       "chrome": bool(e.get("chrome", False)), "timeout_min": e.get("timeout_min", 60), "skill": e["skill"]}
        if e.get("config_dir"): tasks[name]["config_dir"] = e["config_dir"]
        if e.get("net_retry_window_min") is not None: tasks[name]["net_retry_window_min"] = int(e["net_retry_window_min"])
    (HERE / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")

    if "--list" in args:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for name in sorted(tasks):
            print(("REG " if LABEL + name in r else "--- "), name, tasks[name]["cron"])
        return 0

    if "--uninstall" in args:
        for p in AGENTS.glob(LABEL + "*.plist"):
            subprocess.run(["launchctl", "bootout", f"gui/{uid()}/{p.stem}"], capture_output=True)
            p.unlink(); print("removed", p.name)
        return 0

    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    reg_now = registered_labels() if "--install" in args else set()
    skipped_running = []
    wanted = set()
    for name, t in tasks.items():
        label = LABEL + name
        wanted.add(label)
        pl = {
            "Label": label,
            "ProgramArguments": [PY_APP, str(HERE / "run_task.py"), name],
            "StartCalendarInterval": cron_to_intervals(t["cron"]),
            "StandardOutPath": str(LOGS / f"launchd_{name}.log"),
            "StandardErrorPath": str(LOGS / f"launchd_{name}.log"),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TZ": "Asia/Tokyo"},
            "ProcessType": "Background",
        }
        path = AGENTS / f"{label}.plist"
        # 内容が同じなら書き直さない。mtime を「予定が変わった時刻」として保つため（watchdog の未発火判定が使う）
        blob = plistlib.dumps(pl)
        changed = not path.exists() or path.read_bytes() != blob
        if changed:
            path.write_bytes(blob)
        print(f"{'wrote' if changed else 'kept '} {path.name}  ({len(pl['StartCalendarInterval'])} slots) cron={t['cron']}")
        if "--install" in args:
            # 変更が無く登録済みなら触らない（bootout は実行中のジョブを SIGTERM で殺すため）
            if not changed and label in reg_now:
                print("  bootstrap: skip（plist変更なし＆登録済み＝実行中タスクを殺さない）")
            elif running(name):
                skipped_running.append(name)
                print("  bootstrap: SKIP（このタスクは実行中。完走後にもう一度 --install すること）")
            else:
                subprocess.run(["launchctl", "bootout", f"gui/{uid()}/{label}"], capture_output=True)
                r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid()}", str(path)], capture_output=True, text=True)
                print("  bootstrap:", "ok" if r.returncode == 0 else r.stderr.strip())
    if "--install" in args:
        for p in AGENTS.glob(LABEL + "*.plist"):
            if p.stem not in wanted:
                nm = p.stem[len(LABEL):]
                if running(nm):  # 実行中のものは道連れにしない（次回の --install で消える）
                    skipped_running.append(nm)
                    print("kept stale (実行中のため削除見送り)", p.name)
                    continue
                subprocess.run(["launchctl", "bootout", f"gui/{uid()}/{p.stem}"], capture_output=True)
                p.unlink(); print("removed stale", p.name)
        if skipped_running:
            print("!! 実行中のため未反映: " + ", ".join(sorted(set(skipped_running)))
                  + "（完走後に `gen_plists.py --install` を再実行して登録を合わせること）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
