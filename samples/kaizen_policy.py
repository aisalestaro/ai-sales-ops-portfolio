#!/usr/bin/env python3
"""改善キュー（kaizen queue）の「自動実行か、人の判断か」を機械的に決めるポリシー部分の抜粋。

思想：AI に「判断に迷ったら提案にしておく」を許すと、改善が 1 週間止まる。
      そこで判断の分かれ目を AI の裁量から外し、**種別（kind）から機械的に auto / ask を決める**。
      直す・減らす・書き換えるは自動。増やす・払う・送る・消すは人に聞く。

本番では ~950 行の CLI（add / next / snapshot / done / fail / rollback / approve / verdict …）だが、
ここでは「分類の正本」と「判定日の丸め」だけを取り出している。

    python3 kaizen_policy.py classify fix_broken --files SKILL.md
    python3 kaizen_policy.py classify edit_rule --files .env        # 保護パス → ask に落ちる
    python3 kaizen_policy.py due 2026-10-06                          # → 直近の木曜（週次PDCAの判定日）
"""
import argparse
import datetime
import re
import sys

# ── kind → class の対応表（このテーブルが「判断が要るかどうか」の唯一の正本） ──
KINDS = {
    # 自動実行（既定）：小さく・可逆・内向き
    "fix_broken":    ("auto", "壊れている自動化の修復（未発動・沈黙failure・幽霊データ）"),
    "edit_rule":     ("auto", "SKILL.md／運用ルールの追記・修正"),
    "edit_ledger":   ("auto", "台帳・キーワード・記事指定の追加更新"),
    "schedule_time": ("auto", "定時タスクの時刻・頻度の調整"),
    "decrease":      ("auto", "件数・枠の削減／一時停止（安全側への変更）"),
    "content_type":  ("auto", "投稿・記事の型／構成／キーワードの変更"),
    "backlog_state": ("auto", "施策バックログの状態更新・優先順の入替"),
    "experiment":    ("auto", "判定ライン付きの小実験（測れるものだけ）"),
    "investigate":   ("auto", "調査・実態確認（読むだけ／結果を台帳に書く）"),
    # 人の判断（例外）：外向き・お金・増枠・不可逆
    "increase":      ("ask",  "件数・頻度の増枠（アカウント制限リスク）"),
    "money":         ("ask",  "費用が発生する（広告・有料契約・課金API）"),
    "external_send": ("ask",  "新規の対外送信・一斉配信・DM送付"),
    "public_change": ("ask",  "公開ページの料金／サービス／名称の変更"),
    "irreversible":  ("ask",  "削除・アカウント作成／改名など元に戻せない"),
    "strategy":      ("ask",  "事業の開始・撤退・料金方針"),
}

# 保護パス：auto でもここに触る項目は強制的に ask へ落とす
PROTECTED = [
    r"\.env", r"settings\.json", r"settings\.local\.json", r"(^|/)\.git/",
    r"credential", r"secret", r"src/lib/constants\.ts", r"policy-limits\.json",
]

# 「人にしかできない作業」の語彙。これに該当しない理由で人へ戻すのは禁止（AI 側の経路問題として扱う）
OWNER_ONLY = {
    "認証":   r"(パスワード|認証コード|2段階|ワンタイム|OTP)",
    "支払い": r"(課金|支払|決済|カード|請求)",
    "権限":   r"(アカウント作成|権限付与|招待|Connect|接続許可)",
    "本人":   r"(本人確認|署名|捺印)",
    "物理":   r"(電話|対面|郵送|現地)",
    "最終":   r"(送信ボタン|公開ボタン|最終承認)",
}

PDCA_WEEKDAY = 3  # 判定するのは木曜の週次PDCAだけ。判定日はその木曜に揃える


def classify(kind: str, files=None):
    """(auto|ask, 理由) を返す。未知の kind は experiment 扱い、保護パスに触るなら ask。"""
    cls, desc = KINDS.get(kind, KINDS["experiment"])
    for p in files or []:
        for pat in PROTECTED:
            if re.search(pat, p):
                return "ask", f"保護パスに触れる（{p} ~ /{pat}/）"
    return cls, desc


def owner_reason_of(text: str) -> str:
    """人へ戻す理由文が『人にしかできない作業』の語彙に該当すればその分類名、しなければ空文字。"""
    for name, rx in OWNER_ONLY.items():
        if re.search(rx, text or ""):
            return name
    return ""


def align_due(s: str, after_today=False) -> str:
    """YYYY-MM-DD を「その日以降で最初の木曜」に丸める。木曜ならそのまま。"""
    try:
        d = datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return s
    if after_today:
        d = max(d, datetime.date.today() + datetime.timedelta(days=1))
    d += datetime.timedelta(days=(PDCA_WEEKDAY - d.weekday()) % 7)
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify"); c.add_argument("kind"); c.add_argument("--files", nargs="*")
    d = sub.add_parser("due"); d.add_argument("date")
    o = sub.add_parser("owner"); o.add_argument("text")
    a = ap.parse_args()
    if a.cmd == "classify":
        cls, why = classify(a.kind, a.files)
        print(f"{a.kind} → {cls}  ({why})")
        return 0 if cls == "auto" else 4
    if a.cmd == "due":
        print(align_due(a.date)); return 0
    if a.cmd == "owner":
        r = owner_reason_of(a.text)
        print(r or "（該当なし＝AI側で解決すべき作業）"); return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main())
