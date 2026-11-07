"""解析与章节识别：合成样本 + 真实 41 页招标文件。"""

from __future__ import annotations


def test_synthetic_sections(synthetic_doc):
    kinds = {s.kind for s in synthetic_doc.sections}
    assert {"bidder_instructions", "evaluation", "technical", "contract"} <= kinds
    ev = synthetic_doc.by_kind("evaluation")[0]
    assert ev.page_start == 1 and ev.page_end == 1
    assert "评标办法" in ev.title


def test_real_pdf_sections(real_doc):
    """真实文件：投标人须知 / 评标办法 / 技术规格 三类必须被定位到。"""
    assert real_doc.page_count == 41
    kinds = {s.kind for s in real_doc.sections}
    assert "bidder_instructions" in kinds, [s.kind for s in real_doc.sections]
    assert "evaluation" in kinds
    assert "technical" in kinds

    tech = real_doc.by_kind("technical")[0]
    assert "技术" in tech.title
    assert tech.page_start == 13 and tech.page_end >= 20

    ev = real_doc.by_kind("evaluation")[0]
    assert ev.page_start == 11
    # 章节页码范围必须递增且不越界
    for s in real_doc.sections:
        assert 1 <= s.page_start <= s.page_end <= real_doc.page_count


def test_disqualification_hits(real_doc):
    """废标条款没有独立章节，靠关键词定位：真实文件里必须命中，并且带页码。"""
    assert real_doc.disq_hits, "没有定位到任何废标/无效投标条款"
    pages = {h.page for h in real_doc.disq_hits}
    assert all(1 <= p <= real_doc.page_count for p in pages)
    assert any("无效投标" in h.text or "废标" in h.text for h in real_doc.disq_hits)


def test_outline_available(real_doc):
    """章节树来自 documind.parsed.outline()，BidPilot 不重复实现解析。"""
    tree = real_doc.outline()
    assert tree
    assert {"title", "page", "level", "children"} <= set(tree[0])


def test_synthetic_layout_is_produced_by_the_parser(synthetic_doc):
    """夹具的"第 1 页两章 / 第 2 页两章"必须是解析器分出来的，不是夹具写死的（P1-06）。"""
    assert synthetic_doc.page_count == 2
    assert synthetic_doc.parsed.page_count == 2
    assert synthetic_doc.parsed.filename == "synthetic_tender.md"


def test_page_text_equals_slice_text(synthetic_doc, real_doc):
    """页文本必须等于 documind slice_text 的切片结果 —— 唯一被上游钉住的强契约（P1-06）。"""
    for doc in (synthetic_doc, real_doc):
        for page in range(1, doc.page_count + 1):
            expected = doc.parsed.slice_text(page, 0, page, len(doc.parsed.pages[page - 1]))
            assert doc.page_text(page) == expected, f"p{page} 页文本与 slice_text 不一致"
        # 越界页不抛异常，返回空串
        assert doc.page_text(0) == ""
        assert doc.page_text(doc.page_count + 1) == ""


def test_paragraph_spans_match_slice_text(synthetic_doc):
    """段落文本 === slice_text(page, start, page, end)：页码/偏移与上游 span 语义同源。"""
    for p in synthetic_doc.parsed.paragraphs:
        assert synthetic_doc.parsed.slice_text(p.page, p.start, p.page, p.end) == p.text


def test_single_page_two_chapters_have_disjoint_spans(synthetic_doc):
    """同一页起两章：字符区间不得塌缩/重叠，后一章不能吞掉前一章的开头（P1-07）。"""
    first = synthetic_doc.by_kind("bidder_instructions")[0]
    second = synthetic_doc.by_kind("evaluation")[0]

    # 两章确实在同一页
    assert (first.page_start, first.page_end) == (1, 1)
    assert (second.page_start, second.page_end) == (1, 1)

    # 区间有序且不相交（前闭后闭，比较 (page, char) 元组）
    assert first.start <= first.end < second.start <= second.end
    assert second.char_start >= first.char_end + 1, "后一章的起点必须在前一章终点之后"
    assert not (first.contains(second.page_start, second.char_start))
    assert not (second.contains(first.page_start, first.char_end))

    # 段落不共享：前一章只拿到自己的两段，后一章从自己的标题开始
    assert first.paragraphs and second.paragraphs
    assert first.paragraphs[-1].text.startswith("1.1")
    assert second.paragraphs[0].text.startswith("第二章")
    assert not set(id(p) for p in first.paragraphs) & set(id(p) for p in second.paragraphs)

    # 整页长度大于两章之和 → 说明确实不是"两章各拿整页"
    assert first.char_end - first.char_start + second.char_end - second.char_start <= len(
        synthetic_doc.page_text(1)
    )


def test_same_page_chapters_do_not_overlap_in_paragraphs(synthetic_doc):
    """段落归属按 (page, start) 判定：前一章的段落不能落在后一章的区间里。"""
    first = synthetic_doc.by_kind("bidder_instructions")[0]
    second = synthetic_doc.by_kind("evaluation")[0]
    for p in first.paragraphs:
        assert (p.page, p.start) < second.start
    for p in second.paragraphs:
        assert (p.page, p.start) >= second.start


def test_section_at_uses_char_offset(synthetic_doc):
    """section_at(page, offset) 能在同一页上区分两章；section_of_page 仍按页首定位。"""
    second = synthetic_doc.by_kind("evaluation")[0]
    assert synthetic_doc.section_at(1, 0).kind == "bidder_instructions"
    assert synthetic_doc.section_at(1, second.char_start).kind == "evaluation"
    assert synthetic_doc.section_at(1, second.char_start - 1).kind == "bidder_instructions"
    assert synthetic_doc.section_of_page(1).kind == "bidder_instructions"