#!/usr/bin/env python
"""切块参数实测：在真实语料上量出 chunk 长度分布与相邻块重叠，和文档里的数字对账。

    ./.venv/Scripts/python.exe scripts/measure_chunks.py evals/datasets/tender_jiangsu.pdf

输出（markdown，可直接贴进 docs/CHUNKING.md）：

1. 块数、块长 min/P50/P95/max、超过 max_chars 的块数；
2. 相邻块（同一章节内）的最长公共后缀/前缀，即"实际重叠字数"的 min/P50/P95/max；
3. 相邻块共享页面的情况（跨块是否出现同页重复送模型）。

为什么单独写脚本而不写进 pytest：这里的数字是**语料相关**的（41 页江苏样本），
变的是语料不是契约；契约（不重叠、不超 max_chars，除非单页本身超长）由
tests/test_chunk_contract.py 钉住。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.config import get_settings  # noqa: E402
from bidpilot.extract import Chunk, build_chunks  # noqa: E402
from bidpilot.sections import load_tender  # noqa: E402


def _pct(values: list[int], q: float) -> int:
    """线性插值分位数（不依赖 numpy；样本少时中位数与 statistics.median 一致）。"""
    if not values:
        return 0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def _overlap(prev: str, nxt: str) -> int:
    """prev 的后缀与 nxt 的前缀的最长公共长度 —— 即相邻块真实重复的字符数。"""
    best = 0
    limit = min(len(prev), len(nxt))
    # 只在有希望超过当前最优的长度上做比较
    for size in range(limit, best, -1):
        if prev[-size:] == nxt[:size]:
            best = size
            break
    return best


def measure(chunks: list[Chunk], max_chars: int) -> dict:
    lens = [len(c.body) for c in chunks]
    overlaps: list[int] = []
    page_hits: list[int] = []
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        if prev.section_id != nxt.section_id:
            continue
        overlaps.append(_overlap(prev.body, nxt.body))
        shared = set(range(prev.page_start, prev.page_end + 1)) & set(range(nxt.page_start, nxt.page_end + 1))
        page_hits.append(len(shared))
    return {
        "max_chars": max_chars,
        "n": len(chunks),
        "min": min(lens) if lens else 0,
        "p50": _pct(lens, 0.50),
        "p95": _pct(lens, 0.95),
        "max": max(lens) if lens else 0,
        "median_exact": statistics.median(lens) if lens else 0,
        "over_max": sum(1 for n in lens if n > max_chars),
        "overlap_n": len(overlaps),
        "overlap_min": min(overlaps) if overlaps else 0,
        "overlap_p50": _pct(overlaps, 0.50),
        "overlap_p95": _pct(overlaps, 0.95),
        "overlap_max": max(overlaps) if overlaps else 0,
        "page_shared_max": max(page_hits) if page_hits else 0,
        "per_section": _per_section(chunks),
    }


def _per_section(chunks: list[Chunk]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for c in chunks:
        counts[c.section_title or c.section_id] = counts.get(c.section_title or c.section_id, 0) + 1
    return list(counts.items())


def main() -> int:
    ap = argparse.ArgumentParser(description="实测切块分布")
    ap.add_argument("pdf", nargs="?", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"))
    ap.add_argument("--max-chars", type=int, default=None, help="默认取 Settings.chunk_max_chars")
    args = ap.parse_args()

    max_chars = args.max_chars or get_settings().chunk_max_chars
    doc = load_tender(args.pdf)
    chunks = build_chunks(doc, max_chars=max_chars)
    m = measure(chunks, max_chars)

    print(f"# 切块实测 · {Path(args.pdf).name}（{doc.page_count} 页，max_chars={max_chars}）\n")
    print("| 指标 | 值 |")
    print("|---|---|")
    print(f"| 块数 | {m['n']} |")
    print(f"| 块长 min / P50 / P95 / max | {m['min']} / {m['p50']} / {m['p95']} / {m['max']} |")
    print(f"| 块长 > max_chars 的块数 | {m['over_max']} / {m['n']} |")
    print(f"| 相邻块重叠 min / P50 / P95 / max（n={m['overlap_n']}） | "
          f"{m['overlap_min']} / {m['overlap_p50']} / {m['overlap_p95']} / {m['overlap_max']} |")
    print(f"| 相邻块共享页面（最多页数） | {m['page_shared_max']} |")
    print("\n每章节块数：")
    for title, n in m["per_section"]:
        print(f"- {title}: {n}")
    print("\n逐块：")
    for c in chunks:
        print(f"- {c.section_id} p{c.page_start}-{c.page_end} len={len(c.body)} tasks={','.join(c.tasks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())