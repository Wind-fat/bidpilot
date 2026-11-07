#!/usr/bin/env python
"""章节标签「修正前 / 修正后」对比：页首归属 vs quote 落点归属。

    ./.venv/Scripts/python.exe scripts/locate_report.py \\
        --pdf evals/datasets/tender_jiangsu.pdf \\
        --extract-json out/jiangsu_extract.json

修正前 = `section_at(page, 0)`（整页算页首那一章，同页跟章的页尾条目会落到上一章标题上）；
修正后 = `section_at(quote_span[0], quote_span[1])`（按引用真正落点）。
输出章节表、改归明细与计数，直接贴进 docs/LOCATE.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.extract import load_extract  # noqa: E402
from bidpilot.sections import load_tender  # noqa: E402

KIND_LABEL = {"scoring_items": "评分项", "disqualifications": "废标", "tech_requirements": "技术要求"}


def label_of(doc, page: int, offset: int) -> str:
    sec = doc.section_at(page, offset)
    return sec.title if sec else "（未归属任何章）"


def main() -> int:
    ap = argparse.ArgumentParser(description="BidPilot 章节标签定位对比")
    ap.add_argument("--pdf", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"))
    ap.add_argument("--extract-json", default=str(ROOT / "out" / "jiangsu_extract.json"))
    args = ap.parse_args()

    doc = load_tender(args.pdf)
    res = load_extract(json.loads(Path(args.extract_json).read_text(encoding="utf-8")))

    print(f"文档：{args.pdf}（{doc.page_count} 页 / {len(doc.sections)} 章）")
    print("\n## 章节表（页 + 页内起止字符偏移）\n")
    print("| # | kind | 标题 | start (页+偏移) | end (页+偏移) | 段落 |")
    print("|---|---|---|---|---|---|")
    for i, s in enumerate(doc.sections, 1):
        print(f"| {i} | {s.kind} | {s.title} | p{s.page_start}+{s.char_start} | p{s.page_end}+{s.char_end} | {len(s.paragraphs)} |")

    moved: list[tuple[str, str, int, int, int, str, str]] = []
    total = 0
    for kind in KIND_LABEL:
        for item in getattr(res, kind):
            total += 1
            span = item.quote_span
            if span is None:
                continue
            page, start, _ = span
            before = label_of(doc, page, 0)
            after = label_of(doc, page, start)
            if before != after:
                moved.append((kind, item.id, page, start, span[2], before, after))

    print("\n## 改归统计\n")
    print(f"- 条目总数：**{total}**")
    print(f"- 改归：**{len(moved)}** 条（页首归属 → 落点归属）")
    by_kind = Counter(m[0] for m in moved)
    print("- 按类型：" + "，".join(f"{KIND_LABEL[k]} {v}" for k, v in by_kind.items()))
    by_page = Counter(m[2] for m in moved)
    print("- 按页：" + "，".join(f"p{p} {n}" for p, n in sorted(by_page.items())))
    pairs = Counter((m[5], m[6]) for m in moved)
    for (before, after), n in pairs.most_common():
        print(f"- {n} 条：{before} → **{after}**")

    print("\n## 改归明细\n")
    print("| 类型 | id | 页 | 页内起 | 止 | 修正前 | 修正后 |")
    print("|---|---|---|---|---|---|---|")
    for kind, iid, page, start, end, before, after in moved:
        print(f"| {KIND_LABEL[kind]} | {iid} | {page} | {start} | {end} | {before} | {after} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())