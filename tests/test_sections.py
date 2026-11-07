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