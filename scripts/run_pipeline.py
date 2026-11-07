#!/usr/bin/env python
"""端到端跑一份招标文件：解析 → 抽取 → 能力矩阵 → 生成 → docx。

    uv run python scripts/run_pipeline.py evals/datasets/tender_jiangsu.pdf
    uv run python scripts/run_pipeline.py evals/datasets/tender_jiangsu.pdf --no-generate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.config import get_settings  # noqa: E402
from bidpilot.pipeline import EXIT_UNREADABLE, format_degradation_report, run_pipeline  # noqa: E402
from bidpilot.sections import TenderParseError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="BidPilot P0 pipeline")
    ap.add_argument("pdf", help="招标文件 PDF 路径")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 out/）")
    ap.add_argument("--max-chars", type=int, default=None, help="每块最大字符数（默认 3200）")
    ap.add_argument("--concurrency", type=int, default=3, help="并发 LLM 请求数")
    ap.add_argument("--no-generate", action="store_true", help="只做抽取与能力矩阵，不生成应答")
    ap.add_argument("--no-db", action="store_true", help="不写 SQLite")
    ap.add_argument("--json-out", default=None, help="把完整统计写成 JSON")
    ap.add_argument("--extract-json", default=None, help="复用已落盘的抽取结果（跳过 LLM 抽取）")
    ap.add_argument("--extract-out", default=None, help="抽取完成后立即把结果落盘为 JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印逐块抽取进度（默认按 BIDPILOT_LOG 环境变量）")
    args = ap.parse_args()

    level = os.getenv("BIDPILOT_LOG", "INFO" if args.verbose else "WARNING").upper()
    logging.basicConfig(level=getattr(logging, level, logging.WARNING), format="%(asctime)s %(levelname)s %(message)s")

    s = get_settings()
    try:
        res = asyncio.run(
            run_pipeline(
                args.pdf,
                settings=s,
                concurrency=args.concurrency,
                max_chars=args.max_chars,
                generate=not args.no_generate,
                save_db=not args.no_db,
                out_dir=args.out_dir,
                extract_json=args.extract_json,
                extract_out=args.extract_out,
            )
        )
    except TenderParseError as exc:
        # 无文本层 / 空文档：不产出交付物，也不假装成功
        print("=" * 68)
        print(f"× {exc}", file=sys.stderr)
        print(f"退出码 {EXIT_UNREADABLE}（tender_unreadable）", file=sys.stderr)
        return EXIT_UNREADABLE
    stats = res.stats()
    ex = stats["extraction"]

    print("=" * 68)
    print(f"招标文件：{res.tender.title[:60]}")
    print(f"页数：{res.tender.page_count}    文档 id：{res.doc_id}")
    print(f"识别章节：{len(res.tender.sections)} 个（其中废标关键词命中 {len(res.tender.disq_hits)} 段）")
    for sec in res.tender.sections:
        print(f"  - [{sec.label}] {sec.title[:40]}  p{sec.page_start}-{sec.page_end}")
    print("-" * 68)
    print(f"评分项      ：{ex['scoring_items']['total']:3d} 条，needs_review {ex['scoring_items']['needs_review']}")
    print(f"废标条款    ：{ex['disqualifications']['total']:3d} 条，needs_review {ex['disqualifications']['needs_review']}")
    print(f"技术要求    ：{ex['tech_requirements']['total']:3d} 条，needs_review {ex['tech_requirements']['needs_review']}")
    print(f"抽取分块    ：{ex['chunks']} 块，重试后仍失败 {len(ex['chunks_failed'])} 块")
    for cid in ex["chunks_failed"]:
        print(f"    ! 未完全通过校验：{cid}")
    print("-" * 68)
    m = stats["capability_matrix"]
    print(f"需求-能力矩阵：共 {m['total']} 条  满足 {m.get('满足', 0)} / 部分满足 {m.get('部分满足', 0)} / 不满足 {m.get('不满足', 0)}")
    w = stats["workflow"]
    print(f"生成章节    ：{w['sections']} 节，输出段落 {w['paragraphs']} 段，因缺来源被拒 {w['rejected_paragraphs']} 段，mocked={w['mocked']}")
    print(f"Word        ：{stats['docx']['path']}  ({stats['docx']['bytes']} bytes)")
    u = stats["llm_usage"]
    print(
        f"LLM 调用    ：共 {u['calls']} 次（抽取 {u['extract_calls']} / 生成 {u['generate_calls']}），"
        f"prompt {u['prompt_tokens']} tok / completion {u['completion_tokens']} tok"
    )
    print(f"耗时        ：解析+切章 {stats['timing']['parse_s']}s，抽取 {u['extract_seconds']}s，"
          f"生成 {u['generate_seconds']}s，抽取+矩阵+导出总计 {u['total_seconds']}s（不含解析阶段）")
    flag = "**示例数据（不可作为投标依据）**" if stats["capability_library"]["is_example"] else "已配置"
    print(f"能力库      ：{flag} —— {stats['capability_library_note'][:50]}...")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stats": stats,
            "scoring_items": [i.model_dump(mode="json") for i in res.extraction.scoring_items],
            "disqualifications": [i.model_dump(mode="json") for i in res.extraction.disqualifications],
            "tech_requirements": [i.model_dump(mode="json") for i in res.extraction.tech_requirements],
            "matrix": [m.to_row() for m in res.matches],
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON        ：{args.json_out}")

    report = format_degradation_report(res)
    if report:
        print("=" * 68)
        for line in report:
            print(line)
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())