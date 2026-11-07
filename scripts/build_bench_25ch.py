#!/usr/bin/env python
"""造一份 **25+ 章 / 200 页级** 的长语料，供 200 页性能复测用（离线、无新依赖）。

为什么需要它：上一轮 200 页是「同一份 41 页文档循环 5 次」，章标题被 `_dedupe_headings`
去重后只剩 5 章，切章成本被低估了一个数量级。这里把**真实语料的条款正文**重新组织到
26 个互不相同的章标题下，得到一份「页数 200 级、章节数 26」的语料：

    ./.venv/Scripts/python.exe scripts/build_bench_25ch.py

产物：out/perf/tender_26ch_200p.md（Markdown 是 documind 的另一种一等输入，
分页由解析器按 4000 字/页切出，和 tests/conftest.py 的合成夹具同一套机制）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.sections import classify_title, load_tender  # noqa: E402

# 26 个互不相同的章标题，且都能被 KIND_RULES 归类（否则不会成为章节、也不构成切章边界）
CHAPTERS = [
    "第一章 投标人须知",
    "第二章 供应商须知",
    "第三章 投标须知",
    "第四章 磋商须知",
    "第五章 谈判须知",
    "第六章 响应人须知",
    "第七章 评标办法",
    "第八章 评标方法",
    "第九章 评审办法",
    "第十章 评分标准",
    "第十一章 评分办法",
    "第十二章 评标标准",
    "第十三章 评审标准",
    "第十四章 技术规格",
    "第十五章 技术服务需求",
    "第十六章 技术需求",
    "第十七章 技术要求",
    "第十八章 采购需求",
    "第十九章 项目需求",
    "第二十章 设备需求",
    "第二十一章 资格审查",
    "第二十二章 资格预审",
    "第二十三章 合同主要条款",
    "第二十四章 合同草案",
    "第二十五章 投标文件格式",
    "第二十六章 响应文件格式",
]

RE_HEADING = re.compile(r"^\s*#")
RE_CHAPTER_LINE = re.compile(r"^第[一二三四五六七八九十百零〇两]+章")


def clause_pool(pdf: Path) -> list[str]:
    """从真实招标文件里取条款正文（去掉标题行/页码行/过短行），保留原始语域。"""
    doc = load_tender(pdf)
    pool: list[str] = []
    for p in doc.parsed.paragraphs:
        text = re.sub(r"\s+", " ", p.text).strip()
        if len(text) < 24 or len(text) > 300:
            continue
        if RE_HEADING.match(text) or RE_CHAPTER_LINE.match(text):
            continue
        if "…" in text or "....." in text:
            continue
        pool.append(text)
    if not pool:
        raise SystemExit(f"没能从 {pdf} 取到条款正文")
    return pool


def build(out: Path, pool: list[str], pages: int, chars_per_page: int) -> Path:
    per_chapter = max(1, (pages * chars_per_page) // len(CHAPTERS))
    blocks: list[str] = []
    cursor = 0
    for c, title in enumerate(CHAPTERS, 1):
        lines = [f"# {title}", f"{c}.1 本章适用于本项目的相关条款，投标人须逐条响应并承担相应责任。"]
        size = len(lines[1])
        n = 2
        while size < per_chapter:
            body = pool[cursor % len(pool)]
            cursor += 1
            lines.append(f"{c}.{n} {body}")
            size += len(lines[-1])
            n += 1
        # 每个条款各自成块（空行分隔）：MarkdownParser 按「块」累计到 4000 字才翻页，
        # 整章挤成一个块的话 26 章只会切出 26 页。
        blocks.append("\n\n".join(lines))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="造 25+ 章 / 200 页级基准语料")
    ap.add_argument("--pdf", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"))
    ap.add_argument("--out", default=str(ROOT / "out" / "perf" / "tender_26ch_200p.md"))
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--chars-per-page", type=int, default=4000, help="documind MarkdownParser 的分页阈值")
    args = ap.parse_args()

    unclassified = [t for t in CHAPTERS if not classify_title(t)]
    if unclassified:
        raise SystemExit(f"这些标题不会被识别为章节，先改标题：{unclassified}")

    pool = clause_pool(Path(args.pdf))
    out = build(Path(args.out), pool, args.pages, args.chars_per_page)

    doc = load_tender(out)
    print(f"语料       ：{out}")
    print(f"文件大小   ：{out.stat().st_size} bytes")
    print(f"条款池     ：{len(pool)} 条来自 {args.pdf}")
    print(f"页数       ：{doc.page_count}（目标 {args.pages}）")
    print(f"段落数     ：{len(doc.parsed.paragraphs)}")
    print(f"章节数     ：{len(doc.sections)}（标题 {len(CHAPTERS)} 个）")
    for s in doc.sections[:5]:
        print(f"  - p{s.page_start}+{s.char_start} {s.kind:20s} {s.title}")
    print("  ……")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())