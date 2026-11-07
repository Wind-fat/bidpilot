"""抽取：schema 校验、重试、quote 未命中 → needs_review（全部用假 LLM，不碰网络）。"""

from __future__ import annotations

import json

import pytest

from bidpilot.extract import build_chunks, dump_extract, extract, extract_chunk, load_extract
from bidpilot.llm import FakeLLM
from bidpilot.schemas import DisqualificationClause, ScoringItem, TechRequirement

VALID_SCORING = {
    "scoring_items": [
        {
            "no": "2.1",
            "item": "价格",
            "criteria_text": "低价优先法计算价格分",
            "max_score": 40,
            "page": 1,
            "quote": "2.1 价格分 40 分，采用低价优先法计算。",
        }
    ]
}

VALID_DISQ = {
    "disqualifications": [
        {
            "clause_text": "投标文件未按要求密封的作无效投标处理",
            "page": 1,
            "quote": "1.1 投标文件未按招标文件要求密封的，作无效投标处理。",
            "category": "形式",
        }
    ]
}


def _chunk(doc, kind: str):
    return next(c for c in build_chunks(doc, max_chars=8000) if c.section_kind == kind)


async def test_valid_batch_passes(synthetic_doc):
    llm = FakeLLM([json.dumps(VALID_SCORING, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)

    assert report.ok and report.attempts == 1
    assert len(batch.scoring_items) == 1
    item = batch.scoring_items[0]
    assert item.needs_review is False
    assert item.page == 1
    assert item.max_score == 40.0
    assert item.quote in synthetic_doc.page_text(1)


async def test_bad_quote_kept_as_needs_review_after_retries(synthetic_doc):
    """quote 找不到 → 重试 2 次仍失败 → 条目保留并标 needs_review，不静默丢弃。"""
    bad = {
        "scoring_items": [
            {
                "no": "2.1",
                "item": "价格",
                "criteria_text": "低价优先法",
                "max_score": 40,
                "page": 1,
                "quote": "这句话在原文里不存在。",
            }
        ]
    }
    llm = FakeLLM([json.dumps(bad, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)

    assert report.ok is False
    assert report.attempts == 3, "首次 + 最多 2 次重试"
    assert len(llm.calls) == 3
    assert len(batch.scoring_items) == 1, "条目不能被丢弃"
    assert batch.scoring_items[0].needs_review is True
    assert any("quote" in r for r in batch.scoring_items[0].review_reasons)


async def test_retry_then_success(synthetic_doc):
    """第一次页码/quote 不对，第二次修正 → 最终合规，且只多花一次调用。"""
    bad = {"scoring_items": [{"no": "2.1", "item": "价格", "criteria_text": "x", "max_score": 40, "page": 1, "quote": "不存在的内容"}]}
    llm = FakeLLM([json.dumps(bad, ensure_ascii=False), json.dumps(VALID_SCORING, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)

    assert report.ok and report.attempts == 2
    assert batch.scoring_items[0].needs_review is False


async def test_page_out_of_chunk_range_is_rejected(synthetic_doc):
    """页码不在本块范围 → 校验失败（防止模型编造页码）。"""
    bad = {
        "tech_requirements": [
            {"no": "9.9", "text": "x", "page": 99, "page_end": 99, "quote": "3.1 设备应支持 4K 全景视频播放"}
        ]
    }
    llm = FakeLLM([json.dumps(bad, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "technical"), max_retries=0)
    assert report.ok is False
    assert batch.tech_requirements[0].needs_review is True
    assert any("页范围" in r for r in batch.tech_requirements[0].review_reasons)


async def test_invalid_json_is_retried_not_crashed(synthetic_doc):
    llm = FakeLLM(["这不是 JSON", json.dumps(VALID_SCORING, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)
    assert report.ok is True
    assert len(llm.calls) == 2
    assert len(batch.scoring_items) == 1


async def test_unparseable_chunk_reports_failure_without_dropping_silently(synthetic_doc):
    llm = FakeLLM(["完全不是 JSON"], repeat_last=True)
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)
    assert report.ok is False
    assert report.errors and "schema" in report.errors[0]
    assert batch.scoring_items == []


async def test_category_enum_and_fallback(synthetic_doc):
    payload = {
        "disqualifications": [
            {**VALID_DISQ["disqualifications"][0], "category": "商务"},
            {**VALID_DISQ["disqualifications"][0], "category": "说不清的分类"},
        ]
    }
    llm = FakeLLM([json.dumps(payload, ensure_ascii=False)])
    batch, _ = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=0)
    assert all(isinstance(d.category.value, str) for d in batch.disqualifications)
    assert batch.disqualifications[0].category.value == "商务"
    # 非法分类不抛异常，退回关键词猜测（密封/递交 → 形式）
    assert batch.disqualifications[1].category.value == "形式"


async def test_merge_dedupes_across_chunks(synthetic_doc):
    llm = FakeLLM([json.dumps(VALID_SCORING, ensure_ascii=False)], repeat_last=True)
    res = await extract(synthetic_doc, llm, max_chars=8000, max_retries=0, concurrency=1)
    quotes = [s.quote for s in res.scoring_items]
    assert len(quotes) == len(set(quotes)), "同一 quote 不应重复入库"
    assert all(s.id.startswith("S") for s in res.scoring_items)


def test_schema_rejects_missing_required_fields():
    with pytest.raises(Exception):
        ScoringItem(no="1", item="价格", criteria_text="x", page=1)  # 缺 quote
    with pytest.raises(Exception):
        TechRequirement(no="1", text="x", page=1)  # 缺 quote
    with pytest.raises(Exception):
        DisqualificationClause(clause_text="x", page=0, quote="y")  # 页码必须 >=1


async def test_empty_result_is_retried_not_accepted(synthetic_doc):
    """模型对一页返回 {} 时不能当"合法空结果"放过：先重试一次，拿到内容才算过。"""
    llm = FakeLLM(["{}", json.dumps(VALID_SCORING, ensure_ascii=False)])
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=2)
    assert report.ok is True
    assert report.attempts == 2
    assert len(batch.scoring_items) == 1


async def test_empty_result_exhausts_retries_as_failure(synthetic_doc):
    """一直返回空 → 判失败（块会进 chunks_failed），不静默当成功。"""
    llm = FakeLLM(["{}"], repeat_last=True)
    batch, report = await extract_chunk(llm, synthetic_doc, _chunk(synthetic_doc, "evaluation"), max_retries=1)
    assert report.ok is False
    assert any("空结果" in e for e in report.errors)
    assert batch.scoring_items == []


async def test_extract_dump_roundtrip(synthetic_doc):
    """落盘再读回：条目 id / needs_review / 逐块报告都要保真（分阶段跑依赖这个）。"""
    llm = FakeLLM([json.dumps(VALID_SCORING, ensure_ascii=False)], repeat_last=True)
    res = await extract(synthetic_doc, llm, max_chars=8000, max_retries=0, concurrency=1)
    again = load_extract(json.loads(json.dumps(dump_extract(res), ensure_ascii=False)))

    assert [s.id for s in again.scoring_items] == [s.id for s in res.scoring_items]
    assert [s.quote for s in again.scoring_items] == [s.quote for s in res.scoring_items]
    assert len(again.reports) == len(res.reports)
    assert again.reports[0].pages == res.reports[0].pages
    assert again.stats()["scoring_items"]["total"] == res.stats()["scoring_items"]["total"]