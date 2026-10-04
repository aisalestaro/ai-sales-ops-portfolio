#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LP・記事コピーの「説明どまり」を機械的に洗い出す監査ツール。

背景（AIドリブンスクール第10回）:
  「説明は頭に届く。でも心には来ない。」
  非効率・改善・課題・効率化のような抽象語が出たら、それは"説明"のサイン。
  裏にある具体的な場面（絵）に戻すべき箇所を、人間の目より先に機械で列挙する。

  ❌ 説明：「営業業務を効率化します」
  ⭕ 絵  ：「毎週金曜22時まで残っていた集計が、ボタン1つで10秒で終わります」

判定は「この語があるから悪い」ではなく **「絵の材料（数字・時間・場面）が同居しているか」** で見る。
抽象語があっても、同じ文に具体的な数字や場面があれば減点しない。

使い方:
  python3 copy_guard.py HP/src/app/'(corporate)'/sales-ai/page.tsx
  python3 copy_guard.py --dir HP/src/app                 … 配下を一括監査
  python3 copy_guard.py --firstview HP/.../page.tsx      … ファーストビューだけ「ズボラ社長テスト」

終了コード: 0=指摘なし / 50=指摘あり / 2=読めない
"""
import argparse
import glob
import os
import re
import sys

# 「説明」のサインになる抽象語（第10回の例示を核に、自社コピーで頻出するものを追加）
ABSTRACT = [
    "効率化", "改善", "最適化", "非効率", "課題", "向上", "強化", "推進", "活性化",
    "生産性", "高度化", "円滑", "スムーズ", "適切", "柔軟", "幅広い", "さまざま",
    "様々", "ワンストップ", "徹底的", "大幅", "飛躍的", "抜本的", "抜本",
    "DX推進", "業務改革", "変革", "シナジー", "ソリューション", "最大化",
]
# 絵の材料＝これがあれば「説明」で終わっていない
CONCRETE_PATTERNS = [
    (r"\d+\s*(時間|分|秒|日|週|ヶ月|か月|カ月|年|件|名|人|社|回|倍|%|％|万|千|円)", "数字"),
    (r"(月|火|水|木|金|土|日)曜", "曜日"),
    (r"\d{1,2}\s*時", "時刻"),
    (r"(朝|昼|夜|深夜|残業|定時|退勤|通勤|移動中|商談前|商談後|会議中)", "場面"),
]
# 言ってはいけない約束・訴求（SEO代行の絶対ルール等）
FORBIDDEN = [
    (r"順位(を)?保証", "順位保証は約束しない"),
    (r"必ず(上位|1位|成果|売れ)", "断定的な成果保証"),
    (r"(AIで|AIを使って).{0,12}(速い|早い|安い|簡単|量産)", "AIで速い/安い/簡単/量産は訴求に使わない"),
    (r"(速さ|安さ|簡単さ|量産).{0,10}(が|の)(強み|売り|ウリ)", "速さ/安さを強みとして訴求しない"),
]
# 否定・批判の文脈で出た禁止語は違反ではない（むしろ正しい訴求）。
# 例：「AIで量産したら順位が上がらない」「順位保証はしません」は自社の正しい立場。
# 初版はこれを見ておらず、SEO LPの正しいコピー3件を全部違反と誤検知した。
NEGATION_RE = re.compile(
    r"(しません|いたしません|ません|ない|ぬ|避け|やめ|禁止|注意|ご注意|"
    r"上がらない|効かない|失敗|うたう業者|リスク|ではなく|じゃなく|わけではあり)"
)
# 抽象語だが実質は固有名詞・技術用語なので減点しないもの
COMPOUND_ALLOW = ["AI検索最適化", "検索最適化", "AIO", "生産性の高い", "生産性向上の実績"]

JP_STR_RE = re.compile(r'["\'`]([^"\'`\n]{10,140})["\'`]')
HAS_JP_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")
# コード側の文字列（クラス名・パス・import）を除外
CODE_NOISE_RE = re.compile(
    r"(^@/|^\.{1,2}/|^https?://|^/[a-z0-9\-/]*$|[a-z\-]+:[a-z0-9\[\]#.\-]+|"
    r"^[a-zA-Z0-9 \-_/#.:%\[\]()]+$)"
)


def extract_strings(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        for m in JP_STR_RE.finditer(line):
            s = m.group(1).strip()
            if not HAS_JP_RE.search(s):
                continue
            if CODE_NOISE_RE.match(s):
                continue
            out.append((i, s))
    return out


def concrete_hits(text):
    return [label for rx, label in CONCRETE_PATTERNS if re.search(rx, text)]


def audit(path, quiet=False):
    try:
        strings = extract_strings(path)
    except OSError as e:
        print(f"[guard] 読めません: {path} ({e})", file=sys.stderr)
        return 2, []
    findings = []
    for lineno, s in strings:
        # 固有名詞・技術用語として使われている箇所は判定対象から外す
        cleaned = s
        for allow in COMPOUND_ALLOW:
            cleaned = cleaned.replace(allow, "")
        for word in ABSTRACT:
            if word in cleaned and not concrete_hits(s):
                findings.append(("説明どまり", lineno, word, s))
                break
        for rx, why in FORBIDDEN:
            m = re.search(rx, s)
            if not m:
                continue
            # ヒット位置の前後40字に否定・批判があれば違反としない
            lo, hi = max(0, m.start() - 40), min(len(s), m.end() + 40)
            if NEGATION_RE.search(s[lo:hi]):
                continue
            findings.append(("禁止表現", lineno, why, s))
    if not quiet:
        rel = os.path.relpath(path)
        if findings:
            print(f"\n### {rel}  🔴 {len(findings)}件")
            for kind, lineno, word, s in findings:
                mark = "🚫" if kind == "禁止表現" else "💬"
                print(f"  {mark} L{lineno} [{word}] {s[:88]}")
        else:
            print(f"\n### {rel}  ✅ 指摘なし")
    return (50 if findings else 0), findings


def firstview(path):
    """ファーストビュー相当（先頭120行）に絵の材料があるかを見る＝ズボラ社長テスト"""
    strings = [(l, s) for l, s in extract_strings(path) if l <= 120]
    rel = os.path.relpath(path)
    print(f"\n### {rel} のファーストビュー（先頭120行）")
    if not strings:
        print("  （日本語コピーを検出できませんでした）")
        return 0
    all_text = " ".join(s for _, s in strings)
    hits = concrete_hits(all_text)
    for l, s in strings[:6]:
        print(f"  L{l}: {s[:90]}")
    if hits:
        print(f"  ✅ 絵の材料あり: {', '.join(sorted(set(hits)))}")
        return 0
    print("  🔴 絵の材料（数字・時刻・場面）がゼロ＝説明だけでファーストビューが構成されている")
    print("     → ズボラ社長は「それで？」で離脱する。具体的な数字か場面を1つ入れる")
    return 50


def main():
    ap = argparse.ArgumentParser(description="LPコピーの説明どまり／禁止表現の監査")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dir", help="このディレクトリ配下の page.tsx を一括監査")
    ap.add_argument("--firstview", action="store_true", help="ファーストビューのズボラ社長テスト")
    ap.add_argument("--summary", action="store_true", help="件数サマリーのみ")
    args = ap.parse_args()

    targets = list(args.paths)
    if args.dir:
        targets += sorted(glob.glob(os.path.join(args.dir, "**", "page.tsx"), recursive=True))
    if not targets:
        ap.error("ファイルか --dir を指定してください")

    codes, table = [], []
    for t in targets:
        if args.firstview:
            codes.append(firstview(t))
        else:
            c, f = audit(t, quiet=args.summary)
            codes.append(c)
            table.append((len(f), sum(1 for x in f if x[0] == "禁止表現"), os.path.relpath(t)))
    if args.summary and table:
        table.sort(reverse=True)
        print(f"{'指摘':>4} {'禁止':>4}  ファイル")
        print("-" * 72)
        for n, ng, rel in table:
            if n:
                print(f"{n:>4} {ng:>4}  {rel}")
        print(f"\n合計 {sum(n for n,_,_ in table)}件（うち禁止表現 {sum(g for _,g,_ in table)}件）"
              f" / {len(table)}ファイル")
    return 50 if 50 in codes else (2 if 2 in codes else 0)


if __name__ == "__main__":
    sys.exit(main())
