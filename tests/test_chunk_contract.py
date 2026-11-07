"""切块契约：块长上限与"相邻块不重叠"是**承诺**；具体分布随语料变，见 docs/CHUNKING.md。

实测背景（Q3 审计对 documind 的 ADR-0005 提的反例是「声称 512 字滑窗 / 50 字重叠，
实测 71/72 块 >512、重叠中位 143」）。本仓的切块是**按页装箱、零重叠**，
所以这里钉三件事，防止将来改成滑窗却不改文档：

1. 块长 ≤ max_chars —— 唯一的例外是**单页本身超长**（此时该块只有一页，无从再切）；
2. 同一章节的相邻块不共享页面、字符重叠为 0；
3. 章节边界是硬边界：块不会跨章节取文本。

分布数字（P50/P95）不写进断言：那是语料属性，换语料就该变。
"""

from __future__ import annotations

from pathlib import Path

from bidpilot.extract import build_chunks

JIANGSU = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "tender_jiangsu.pdf"


def _overlap(prev: str, nxt: str) -> int:
    """prev 的后缀 == nxt 的前缀 的最长长度，即相邻块真实重复的字数。"""
    for size in range(min(len(prev), len(nxt)), 0, -1):
        if prev[-size:] == nxt[:size]:
            return size
    return 0


def _assert_no_overlap(chunks) -> None:
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        if prev.section_id != nxt.section_id:
            continue
        assert not set(range(prev.page_start, prev.page_end + 1)) & set(
            range(nxt.page_start, nxt.page_end + 1)
        ), f"{prev.id} 与 {nxt.id} 共享页面（同一页被送进模型两次）"
        assert _overlap(prev.body, nxt.body) == 0, f"{prev.id} 与 {nxt.id} 存在字面重叠"


def _assert_within_cap(chunks, max_chars: int) -> None:
    for c in chunks:
        if len(c.body) <= max_chars:
            continue
        # 唯一合法例外：这一块只有一页，且那页本身就超长（无从再切）
        assert c.page_start == c.page_end, f"{c.id} 超长（{len(c.body)} > {max_chars}）且不止一页"


def test_synthetic_doc_chunks_respect_cap_and_do_not_overlap(synthetic_doc):
    chunks = build_chunks(synthetic_doc, max_chars=1200)
    assert chunks, "合成语料应当切出块"
    _assert_within_cap(chunks, 1200)
    _assert_no_overlap(chunks)


def test_real_corpus_chunks_respect_cap_and_do_not_overlap(real_doc):
    chunks = build_chunks(real_doc, max_chars=2000)
    assert len(chunks) >= 5
    _assert_within_cap(chunks, 2000)
    _assert_no_overlap(chunks)
    # 默认参数（Settings.chunk_max_chars）与 build_chunks 的默认值必须是同一个数，
    # 否则 README / docs/CHUNKING.md 里的"2000"会只对一半路径成立。
    from bidpilot.config import Settings

    assert Settings().chunk_max_chars == 2000
    assert len(build_chunks(real_doc)) == len(chunks)


def test_single_oversized_page_is_the_only_way_to_exceed_cap(real_doc):
    """把上限压到 1 个字：每块都只能装一页，且这套实现不会为了凑上限去切页内文本。"""
    chunks = build_chunks(real_doc, max_chars=1)
    assert chunks
    assert all(c.page_start == c.page_end for c in chunks), "单页超长时不得跨页凑块"
    assert all(len(c.body) > 1 for c in chunks)


def test_chunk_pages_never_cross_section_boundaries(real_doc):
    """块只会落在自己章节的页区间内（章节边界即块边界，不允许跨章取文本）。"""
    by_id = {s.id: s for s in real_doc.sections}
    for c in build_chunks(real_doc):
        section = by_id[c.section_id]
        assert section.page_start <= c.page_start <= c.page_end <= section.page_end


def test_real_corpus_file_exists():
    assert JIANGSU.exists(), "真实语料缺失时本文件的断言会静默失效"