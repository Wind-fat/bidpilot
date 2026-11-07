"""生成 workflow：来源闸门（无来源段落不得输出）+ 状态机可局部重跑。"""

from __future__ import annotations

import json

from bidpilot.capabilities import CapabilityIndex, load_capabilities
from bidpilot.extract import ExtractResult
from bidpilot.llm import FakeLLM
from bidpilot.schemas import ScoringItem, TechRequirement
from bidpilot.workflow import MemoryStore, Stage, WorkflowEngine, verify_citations


def _result() -> ExtractResult:
    res = ExtractResult(title="测试招标文件", source="synthetic.pdf")
    res.tech_requirements = [
        TechRequirement(id="T001", no="3.1", text="设备应支持 4K 全景视频播放", page=2, page_end=2,
                        quote="3.1 设备应支持 4K 全景视频播放", section="第三章 采购需求")
    ]
    res.scoring_items = [
        ScoringItem(id="S001", no="2.1", item="价格", criteria_text="低价优先法计算价格分", max_score=40, page=1,
                    quote="2.1 价格分 40 分，采用低价优先法计算。")
    ]
    return res


def _engine(llm) -> tuple[WorkflowEngine, MemoryStore]:
    caps, _ = load_capabilities()
    store = MemoryStore()
    return WorkflowEngine("doc-test", _result(), CapabilityIndex(caps), llm, store), store


def test_verify_citations_drops_sourceless_and_unknown():
    allowed = {"T001", "cap-product-vr-classroom"}
    accepted, rejected = verify_citations(
        [
            {"text": "有来源的段落", "sources": ["T001"]},
            {"text": "没有来源的段落", "sources": []},
            {"text": "来源 id 不存在", "sources": ["T999"]},
            {"text": "  ", "sources": ["T001"]},
            {"text": "能力来源", "sources": ["cap-product-vr-classroom"]},
        ],
        allowed,
    )
    assert [p.text for p in accepted] == ["有来源的段落", "能力来源"]
    reasons = [r["reason"] for r in rejected]
    assert any("缺少来源" in r for r in reasons)
    assert any("来源 id 不存在" in r for r in reasons)
    assert any("空段落" in r for r in reasons)


async def test_workflow_blocks_paragraph_without_source():
    """第一节给合规段落，第二节给无来源段落 → 无来源的必须被拒且该节标 failed。"""
    llm = FakeLLM(
        [
            json.dumps({"paragraphs": [{"text": "我方 VR 智慧教室方案满足 4K 全景播放要求。",
                                        "sources": ["T001", "cap-product-vr-classroom"], "kind": "技术响应"}]},
                       ensure_ascii=False),
            json.dumps({"paragraphs": [{"text": "价格分应答要点：按低价优先法。", "sources": []}]}, ensure_ascii=False),
        ]
    )
    engine, store = _engine(llm)
    await engine.plan()
    res = await engine.run()

    assert len(res.sections) == 2
    first, second = res.sections
    assert first.state == Stage.CITATION_VERIFIED.value
    assert len(first.paragraphs) == 1
    assert first.paragraphs[0].sources == ["T001", "cap-product-vr-classroom"]

    assert second.state == Stage.FAILED.value
    assert second.paragraphs == []
    assert second.rejected and "缺少来源" in second.rejected[0]["reason"]

    # 状态真的落到了 store 里
    assert await store.get_state("doc-test", first.section_id) == Stage.CITATION_VERIFIED.value
    assert await store.get_state("doc-test", second.section_id) == Stage.FAILED.value


async def test_rerun_single_section_only_touches_that_section():
    llm = FakeLLM(
        [
            json.dumps({"paragraphs": [{"text": "a", "sources": ["T001"]}]}, ensure_ascii=False),
            json.dumps({"paragraphs": [{"text": "b", "sources": ["S001"]}]}, ensure_ascii=False),
        ]
    )
    engine, store = _engine(llm)
    await engine.plan()
    full = await engine.run()
    target = full.sections[0].section_id
    calls_before = len(llm.calls)

    rerun = await engine.run(only_section=target, restart=True)
    assert len(rerun.sections) == 1 and rerun.sections[0].section_id == target
    assert len(llm.calls) == calls_before + 1, "只应重跑一节，不应重跑全部"


async def test_mock_fallback_when_llm_unavailable():
    """LLM 不可用 → 模板应答，但仍必须带来源，且结果标 mocked。"""
    class Down:
        async def complete(self, *a, **kw):
            from bidpilot.llm import LLMError

            raise LLMError("connection refused")

    engine, _ = _engine(Down())
    await engine.plan()
    res = await engine.run()
    assert res.mocked is True
    assert all(p.sources for s in res.sections for p in s.paragraphs)
    assert all(s.state == Stage.CITATION_VERIFIED.value for s in res.sections)


async def test_aggregate_only_includes_verified_sections():
    llm = FakeLLM(
        [
            json.dumps({"paragraphs": [{"text": "有来源", "sources": ["T001"]}]}, ensure_ascii=False),
            json.dumps({"paragraphs": [{"text": "没来源", "sources": []}]}, ensure_ascii=False),
        ]
    )
    engine, _ = _engine(llm)
    await engine.plan()
    await engine.run()
    agg = await engine.aggregate()
    assert len(agg["sections"]) == 1
    assert agg["sections"][0]["paragraphs"][0]["text"] == "有来源"


async def test_matrix_rows_have_required_fields():
    engine, _ = _engine(FakeLLM(["{}"]))
    rows = engine.matrix_rows()
    assert rows and {"requirement", "matched_capability", "status", "confidence", "evidence"} <= set(rows[0])