"""引用定位精度：quote_span + 按落点回填章节 + 独立可回查复核。

三件事都钉死，防止「标签变准」靠放宽校验换来：
1. quote_span 来自定位（页, 起, 止），不是模型自报；
2. 章节标签按 quote 的**页内偏移**反查，同页跟章不再把页尾条目算到上一章；
3. citations.check_all 是独立复核：故意改坏 quote / 偏移 / 标签时必须报失败。
"""

from __future__ import annotations

import json

import pytest

from bidpilot.citations import check_all, check_item
from bidpilot.extract import (
    build_chunks,
    dump_extract,
    extract,
    extract_chunk,
    load_extract,
    locate_quote_span,
    relocate,
)
from bidpilot.llm import FakeLLM
from bidpilot.schemas import TechRequirement

PRICE_QUOTE = "2.1 价格分 40 分，采用低价优先法计算。"
CONTRACT_QUOTE = "4.1 合同签订后 30 日内交货，逾期按日万分之五计违约金。"


def _chunk(doc, kind: str):
    return next(c for c in build_chunks(doc, max_chars=8000) if c.section_kind == kind)


async def test_quote_span_is_the_located_span(synthetic_doc):
    """评分项的 quote_span 必须等于 (页, 该页里真实偏移, 结束)，且 quote 是原文切片。"""
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "低价优先法计算价格分", "max_score": 40,
             "page": 1, "quote": PRICE_QUOTE},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)])
    batch, _ = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=0)

    item = batch.scoring_items[0]
    text = synthetic_doc.page_text(1)
    start, end = locate_quote_span(text, PRICE_QUOTE)
    assert item.quote_span == (1, start, end)
    assert item.quote == text[start:end] == PRICE_QUOTE
    assert item.char_offset == start


async def test_section_backfill_uses_quote_offset_not_page_start(synthetic_doc):
    """第一章/第二章同在第 1 页：评分项落在第二章区间里，标签就必须是第二章。"""
    assert synthetic_doc.section_at(1, 0).kind == "bidder_instructions", "夹具前提：页首是第一章"
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "低价优先法计算价格分", "max_score": 40,
             "page": 1, "quote": PRICE_QUOTE},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)])
    batch, _ = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=0)

    item = batch.scoring_items[0]
    assert item.section == "第二章 评标办法", item.section
    assert synthetic_doc.section_at(item.page, 0).title != item.section, "按页首回填会得到第一章"


async def test_model_supplied_span_is_overwritten(synthetic_doc):
    """模型自报的 quote_span 不可信：定位之后一律覆盖，不采信 1,0,1 这种编造值。"""
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "x", "max_score": 40, "page": 1,
             "quote": PRICE_QUOTE, "quote_span": [1, 0, 1], "section": "模型编的章"},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)])
    batch, _ = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=0)

    item = batch.scoring_items[0]
    assert item.quote_span[1] > 0
    assert item.section == "第二章 评标办法"


def test_relocate_labels_by_offset_and_marks_unlocatable(synthetic_doc):
    """已落盘条目重定位：第四章的条文在 p2 落在第三章之后，标签必须跟着落点走。"""
    from bidpilot.extract import ExtractResult

    page2 = synthetic_doc.page_text(2)
    assert CONTRACT_QUOTE in page2
    assert synthetic_doc.section_at(2, 0).kind == "technical", "夹具前提：p2 页首是第三章"

    res = ExtractResult(title="t", source="s")
    res.tech_requirements = [
        TechRequirement(id="T001", no="4.1", text="交货期", page=2, page_end=2, quote=CONTRACT_QUOTE),
        TechRequirement(id="T002", no="9.9", text="不存在", page=2, page_end=2, quote="这句话在原文里不存在。"),
    ]
    stats = relocate(synthetic_doc, res)

    good, bad = res.tech_requirements
    assert stats["located"] == 1 and len(stats["unlocated"]) == 1
    assert good.quote_span == (2, page2.find(CONTRACT_QUOTE), page2.find(CONTRACT_QUOTE) + len(CONTRACT_QUOTE))
    assert good.section == "第四章 合同主要条款"
    assert good.needs_review is False
    assert bad.needs_review is True
    assert any("未能在第 2 页" in r for r in bad.review_reasons)


async def test_span_survives_dump_roundtrip(synthetic_doc):
    """quote_span / section 落盘再读回要保真（分阶段跑依赖它）。"""
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "x", "max_score": 40, "page": 1, "quote": PRICE_QUOTE},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)], repeat_last=True)
    res = await extract(synthetic_doc, llm, max_chars=8000, max_retries=0, concurrency=1)
    again = load_extract(json.loads(json.dumps(dump_extract(res), ensure_ascii=False)))

    assert again.scoring_items[0].quote_span == res.scoring_items[0].quote_span
    assert again.scoring_items[0].section == res.scoring_items[0].section == "第二章 评标办法"


async def test_citations_pass_on_fresh_extraction(synthetic_doc):
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "x", "max_score": 40, "page": 1, "quote": PRICE_QUOTE},
        ],
        "disqualifications": [
            {"clause_text": "未密封作无效投标", "page": 1, "quote": "1.1 投标文件未按招标文件要求密封的，作无效投标处理。",
             "category": "形式"},
        ],
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)], repeat_last=True)
    res = await extract(synthetic_doc, llm, max_chars=8000, max_retries=0, concurrency=1)
    relocate(synthetic_doc, res)

    report = check_all(synthetic_doc, res)
    assert report.total == 2 and report.ok == 2, [p.line() for p in report.problems]
    assert report.summary() == "可回查 2/2"


async def test_citations_detect_broken_quote_span_and_label(synthetic_doc):
    """复核必须真的会红：改坏偏移 / 改坏 quote / 改坏标签，三种都要报出来。"""
    payload = {
        "scoring_items": [
            {"no": "2.1", "item": "价格", "criteria_text": "x", "max_score": 40, "page": 1, "quote": PRICE_QUOTE},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)], repeat_last=True)
    res = await extract(synthetic_doc, llm, max_chars=8000, max_retries=0, concurrency=1)
    relocate(synthetic_doc, res)
    item = res.scoring_items[0]
    page, start, end = item.quote_span

    item.quote_span = (page, start + 1, end + 1)
    assert any("不一致" in r for r in check_item(synthetic_doc, item)), "偏移错了必须报"

    item.quote_span = (page, start, end)
    item.quote = PRICE_QUOTE[:-1]
    assert any("不一致" in r for r in check_item(synthetic_doc, item)), "quote 少了字必须报"

    item.quote = PRICE_QUOTE
    item.section = "第一章 投标人须知"
    assert any("section" in r for r in check_item(synthetic_doc, item)), "标签与落点不符必须报"

    item.section = "第二章 评标办法"
    item.quote_span = None
    assert any("缺少 quote_span" in r for r in check_item(synthetic_doc, item)), "没偏移必须报"


async def test_cross_page_quote_keeps_evidence_page(synthetic_doc):
    """跨页条目的 quote 只落在结束页：quote_span 要记结束页，不能悄悄算到起始页上。"""
    payload = {
        "tech_requirements": [
            {"no": "3.2", "text": "热线服务", "page": 1, "page_end": 2,
             "quote": "3.2 提供 7×12 小时热线服务，2 小时响应。"},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)])
    batch, _ = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "technical"), max_retries=0)

    item = batch.tech_requirements[0]
    assert item.page == 1 and item.page_end == 2
    assert item.quote_span[0] == 2, "证据在 p2，就必须记 p2"
    page2 = synthetic_doc.page_text(2)
    assert page2[item.quote_span[1] : item.quote_span[2]] == item.quote
    assert item.needs_review is False


@pytest.mark.parametrize("kind", ["scoring_items", "disqualifications", "tech_requirements"])
def test_real_corpus_every_item_is_recheckable(real_doc, kind):
    """真实 41 页语料：重定位之后每一条都要能回查（失败明细直接进断言消息）。"""
    from pathlib import Path

    # 优先用入仓的测试夹具（CI 里 out/ 不存在，若只认 out/ 这条测试会静默 skip，等于没测）
    root = Path(__file__).resolve().parents[1]
    candidates = [root / "evals" / "fixtures" / "jiangsu_extract.json", root / "out" / "jiangsu_extract.json"]
    payload = candidates[0] if candidates[0].exists() else candidates[1]
    if not payload.exists():
        pytest.skip("缺少抽取结果：evals/fixtures/jiangsu_extract.json 未入仓，且 out/ 下没有本次抽取")
    res = load_extract(json.loads(payload.read_text(encoding="utf-8")))
    relocate(real_doc, res)

    report = check_all(real_doc, res)
    assert report.ok == report.total, [p.line() for p in report.problems]
    assert getattr(res, kind), f"{kind} 为空，断言没有意义"


def test_source_mismatch_catches_cross_document_verification(synthetic_doc):
    """拿 A 的引用去 B 里定位：逐条能命中也不许算通过（P1-10 后续项）。"""
    from bidpilot.citations import source_mismatch
    from bidpilot.extract import ExtractResult

    name = synthetic_doc.path.name
    # 同一份文件的不同写法（裸文件名 / 相对路径 / Windows 分隔符 / 大小写）不算不一致
    for src in (name, f"evals/datasets/{name}", f"evals\\datasets\\{name}", name.upper()):
        assert source_mismatch(synthetic_doc, ExtractResult(title="t", source=src)) is None, src

    why = source_mismatch(synthetic_doc, ExtractResult(title="t", source="evals/datasets/tender_other.pdf"))
    assert why and "tender_other.pdf" in why and name in why, why

    # 旧 JSON 没记来源：这里不做断言，由 CLI 明说「跳过」
    assert source_mismatch(synthetic_doc, ExtractResult(title="t", source="")) is None