#!/usr/bin/env python
"""引用可回查率：对输出 JSON 的每一条 requirement 做机械复核。

    ./.venv/Scripts/python.exe scripts/verify_citations.py \\
        --pdf evals/datasets/tender_jiangsu.pdf \\
        --extract-json out/jiangsu_extract.json

检查项（详见 src/bidpilot/citations.py）：
page 在文档范围内 → quote_span 存在且在该页范围内 → slice_text 与 quote 逐字一致 →
quote 是该页原文子串 → section 标签与区间起点反查的章节一致。

有失败明细时退出码 1（CI/脚本可直接判红）。加 --json 输出机器可读结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.citations import check_all  # noqa: E402
from bidpilot.extract import load_extract  # noqa: E402
from bidpilot.sections import TenderParseError, load_tender  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="BidPilot 引用可回查复核")
    ap.add_argument("--pdf", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"))
    ap.add_argument("--extract-json", default=str(ROOT / "out" / "jiangsu_extract.json"))
    ap.add_argument("--detail", type=int, default=20, help="最多打印几条失败明细（0 = 全打）")
    ap.add_argument("--json", action="store_true", help="额外输出一行 JSON 结果")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    payload = Path(args.extract_json)
    if not pdf.exists():
        raise SystemExit(f"缺少招标文件：{pdf}")
    if not payload.exists():
        raise SystemExit(f"缺少抽取结果 JSON：{payload}")

    try:
        doc = load_tender(pdf)
    except TenderParseError as exc:
        raise SystemExit(str(exc)) from exc

    res = load_extract(json.loads(payload.read_text(encoding="utf-8")))
    report = check_all(doc, res)

    print("=" * 68)
    print(f"文档     ：{pdf}（{doc.page_count} 页 / {len(doc.sections)} 章）")
    print(f"抽取结果 ：{payload}")
    print(f"条目     ：{report.total}（{report.kind_summary()}）")
    print("-" * 68)
    print(f"**{report.summary()}**")
    missing_span = sum(1 for p in report.problems if any("缺少 quote_span" in r for r in p.reasons))
    if missing_span:
        print(f"其中因缺少 quote_span 无法回查：{missing_span}")
    if report.problems:
        print(f"失败 {len(report.problems)} 条：")
        detail = report.problems if args.detail <= 0 else report.problems[: args.detail]
        for p in detail:
            print(f"  - {p.line()}")
        if len(detail) < len(report.problems):
            print(f"  …… 另有 {len(report.problems) - len(detail)} 条")
    else:
        print("失败 0 条：每条都能按 page + quote_span 在原文里逐字找回。")
    print("=" * 68)
    if args.json:
        print(
            json.dumps(
                {
                    "total": report.total,
                    "ok": report.ok,
                    "per_kind": report.per_kind,
                    "failures": [
                        {"kind": p.kind, "id": p.item_id, "page": p.page, "reasons": p.reasons}
                        for p in report.problems
                    ],
                },
                ensure_ascii=False,
            )
        )
    return 0 if not report.problems else 1


if __name__ == "__main__":
    raise SystemExit(main())