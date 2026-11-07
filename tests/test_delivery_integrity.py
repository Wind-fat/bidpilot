"""交付物污染的验收测试：模板降级 / 示例能力库必须在 docx、文件名、退出码三处同时可见。

背景（arch review P0-05 / P1-11）：LLM 网关挂掉时旧实现产出的 .docx 结构完整、每段都带来源 id，
与真实生成不可区分；能力库为空时还会把整份标书判成"全部不满足"。这里逐条钉住修复点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from bidpilot.capabilities import (
    EMPTY_LIBRARY_EVIDENCE,
    MISSING,
    NEEDS_REVIEW,
    NO_MATCH_EVIDENCE,
    Capability,
    CapabilityIndex,
    Requirement,
    load_capabilities,
)
from bidpilot.config import Settings
from bidpilot.export import BANNER_EXAMPLE_CAPS, BANNER_FAILED, BANNER_MOCK, BANNER_STAGED, build_docx
from bidpilot.extract import ExtractResult, dump_extract
from bidpilot.llm import FakeLLM, LLMError
from bidpilot.pipeline import (
    EXIT_EXAMPLE_CAPABILITIES,
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_PRIORITY,
    EXIT_TEMPLATE_FALLBACK,
    FAILED_SUFFIX,
    MOCK_SUFFIX,
    PipelineResult,
    artifact_suffix,
    exit_code_for,
    format_degradation_report,
    run_pipeline,
)
from bidpilot.schemas import TechRequirement
from bidpilot.workflow import SectionRun, WorkflowResult

ROOT = Path(__file__).resolve().parents[1]
JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"


class DownLLM:
    """网关挂掉：任何调用都抛 LLMError（不联网）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *a, **kw):
        self.calls += 1
        raise LLMError("connection refused")


def _extract_file(tmp_path: Path) -> Path:
    res = ExtractResult(title="测试招标文件", source=str(JIANGSU))
    res.tech_requirements = [
        TechRequirement(
            id="T001", no="3.1", text="设备应支持 4K 全景视频播放，单眼分辨率不低于 1440x1600",
            page=13, page_end=13, quote="设备应支持 4K 全景视频播放", section="第三章 采购需求",
        ),
        TechRequirement(
            id="T002", no="3.2", text="提供 7×12 小时热线服务，2 小时响应",
            page=14, page_end=14, quote="提供 7×12 小时热线服务", section="第三章 采购需求",
        ),
    ]
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(dump_extract(res), ensure_ascii=False), encoding="utf-8")
    return p


def _docx_texts(path: Path) -> list[str]:
    return [p.text for p in Document(str(path)).paragraphs if p.text.strip()]


@pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")
async def test_mock_degradation_marks_docx_filename_and_exit_code(tmp_path):
    """mock 场景：首页横幅 + _MOCK 文件名后缀 + kind 尾注 + 非零退出码 + CLI 清单。"""
    settings = Settings(allow_mock=True, out_dir=tmp_path)  # 模板降级必须显式开启
    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=DownLLM(),
        generate=True,
        save_db=False,
        out_dir=tmp_path,
        extract_json=_extract_file(tmp_path),
    )

    # 1) 工作流确实降级了，且能点名被降级的章节
    assert res.workflow.mocked is True
    degraded = res.workflow.degraded_sections()
    assert degraded and all(d["severity"] == "mocked" for d in degraded)

    # 2) 文件名后缀
    assert res.docx_path.name.endswith("_MOCK.docx"), res.docx_path.name

    # 3) 首页横幅（加粗、正文可见）
    doc = Document(str(res.docx_path))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert BANNER_MOCK in texts, "首页缺少模板应答横幅"
    banner = next(p for p in doc.paragraphs if p.text.strip() == BANNER_MOCK)
    assert banner.runs and all(r.bold for r in banner.runs if r.text.strip()), "横幅必须加粗"
    assert texts.index(BANNER_MOCK) <= 2, "横幅必须在首页最前面"

    # 4) 被降级段落的 kind 尾注落进了文档
    assert any("类型：技术响应（模板，LLM 不可用）" in t for t in texts), "kind 尾注未落到 docx"

    # 5) 退出码 + CLI 清单
    assert res.exit_code == EXIT_TEMPLATE_FALLBACK != 0
    report = "\n".join(format_degradation_report(res))
    assert "模板应答" in report
    for d in degraded:
        assert d["title"] in report


@pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")
async def test_clean_run_exits_zero_without_banners(tmp_path):
    """没有降级、能力库非示例时：无横幅、无 _MOCK 后缀、退出码 0（告警不能变成噪声）。"""
    caps_file = tmp_path / "caps.json"
    caps_file.write_text(
        json.dumps(
            {"capabilities": [
                {"id": "cap-vr", "type": "产品", "name": "VR 智慧教室", "summary": "VR 头盔与全景播放", "tags": ["VR"]}
            ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake = FakeLLM(
        [json.dumps({"paragraphs": [{"text": "我方以 VR 教室方案响应。", "sources": ["T001"], "kind": "技术响应"}]},
                    ensure_ascii=False)],
        repeat_last=True,
    )
    settings = Settings(allow_mock=False, out_dir=tmp_path, capability_file=caps_file)
    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=fake,
        generate=True,
        save_db=False,
        out_dir=tmp_path,
        extract_json=_extract_file(tmp_path),
    )

    assert res.workflow.mocked is False
    assert res.workflow.problems() == []
    assert res.example_capabilities is False
    assert res.exit_code == 0
    assert "技术应答初稿.docx" in res.docx_path.name and not res.docx_path.name.endswith("_MOCK.docx")
    texts = _docx_texts(res.docx_path)
    assert BANNER_MOCK not in texts and BANNER_EXAMPLE_CAPS not in texts


@pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")
async def test_llm_down_without_explicit_mock_fails_loud(tmp_path):
    """默认（未设 BIDPILOT_ALLOW_MOCK）时不许静默降级：章节 failed、交付物自证是废稿、退出码非零。"""
    settings = Settings(allow_mock=False, out_dir=tmp_path)
    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=DownLLM(),
        generate=True,
        save_db=False,
        out_dir=tmp_path,
        extract_json=_extract_file(tmp_path),
    )

    assert res.workflow.mocked is False
    assert res.workflow.sections, "应当有章节被规划"
    assert all(s.state == "failed" for s in res.workflow.sections)
    assert "未开启模板降级" in res.workflow.sections[0].error
    assert res.empty_delivery is True
    assert res.docx_path.name.endswith("_FAILED.docx"), res.docx_path.name
    assert not res.docx_path.name.endswith("_MOCK.docx")
    assert res.exit_code == EXIT_INCOMPLETE != 0


@pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")
async def test_whole_section_failure_never_writes_an_anonymous_shell(tmp_path):
    """整节失败时不许落"只有封面+目录、看起来完整"的空壳：文件名带 _FAILED 且首行自证（本轮修复）。"""
    settings = Settings(allow_mock=False, out_dir=tmp_path)
    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=DownLLM(),
        generate=True,
        save_db=False,
        out_dir=tmp_path,
        extract_json=_extract_file(tmp_path),
    )

    # 1) 正常交付物名下的文件**不存在**（旧行为会落一份 35KB 空壳）
    normal = tmp_path / "tender_jiangsu_技术应答初稿.docx"
    assert not normal.exists(), "整节失败时不得落正常文件名的交付物"

    # 2) 落地的文件必须在第一段就声明自己不可用
    texts = _docx_texts(res.docx_path)
    assert texts, "docx 不应为空文件"
    assert texts[0] == BANNER_FAILED, f"首行必须是废稿声明，实际：{texts[:2]}"
    assert "不得作为投标文件使用" in texts[0]
    assert "未产出应答的章节" in texts, "失败章节清单没有写进文档"

    # 3) 退出码契约不变：4 = 内容不完整
    assert res.exit_code == EXIT_INCOMPLETE != 0
    report = "\n".join(format_degradation_report(res))
    assert "empty_delivery" in report or "failed" in report


@pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")
async def test_example_capability_library_banner_and_exit_code(tmp_path):
    """示例能力库：横幅文案与 mock 横幅不同，退出码 3（无生成时不被 mock 覆盖）。"""
    settings = Settings(allow_mock=False, out_dir=tmp_path)
    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=DownLLM(),
        generate=False,  # 只验证能力库这一路
        save_db=False,
        out_dir=tmp_path,
        extract_json=_extract_file(tmp_path),
    )

    assert res.example_capabilities is True
    texts = _docx_texts(res.docx_path)
    assert BANNER_EXAMPLE_CAPS in texts
    assert BANNER_MOCK not in texts
    assert BANNER_EXAMPLE_CAPS != BANNER_MOCK
    # --no-generate 的产物同样是空壳（只有抽取+矩阵），首行必须自证
    assert BANNER_STAGED in texts
    assert "未生成应答正文" in BANNER_STAGED
    assert res.exit_code == 3


def test_build_docx_renders_both_banners_independently(tmp_path):
    payload = {
        "title": "t",
        "sections": [
            {
                "title": "第一章",
                "items": [{"id": "T001", "no": "1.1", "text": "要求", "page": 1}],
                "paragraphs": [
                    {"text": "模板句", "sources": ["T001"], "kind": "技术响应（模板，LLM 不可用）"},
                    {"text": "正常句", "sources": ["T001"], "kind": "技术响应"},
                ],
            }
        ],
    }
    mock_doc = build_docx(payload, tmp_path / "m.docx", mocked=True)
    example_doc = build_docx(payload, tmp_path / "e.docx", example_capabilities=True)
    plain_doc = build_docx(payload, tmp_path / "p.docx")

    mock_texts, example_texts, plain_texts = (
        _docx_texts(mock_doc), _docx_texts(example_doc), _docx_texts(plain_doc)
    )
    assert BANNER_MOCK in mock_texts and BANNER_EXAMPLE_CAPS not in mock_texts
    assert BANNER_EXAMPLE_CAPS in example_texts and BANNER_MOCK not in example_texts
    assert BANNER_MOCK not in plain_texts and BANNER_EXAMPLE_CAPS not in plain_texts
    # 默认 kind 不刷屏，非默认 kind 才出尾注
    assert any("类型：技术响应（模板，LLM 不可用）" in t for t in mock_texts)
    assert not any(t.strip().endswith("〔类型：技术响应〕") for t in plain_texts)


def test_build_docx_failed_puts_declaration_on_first_line(tmp_path):
    """失败稿的单元级契约：首段就是废稿声明，后面列出逐节失败原因。"""
    payload = {
        "title": "t",
        "sections": [],
        "failures": [{"section_id": "gen-tech", "title": "第三章 采购需求", "reason": "LLM 不可用且未开启模板降级"}],
    }
    p = build_docx(payload, tmp_path / "f.docx", failed=True)
    texts = _docx_texts(p)
    assert texts[0] == BANNER_FAILED
    assert any("gen-tech" in t and "LLM 不可用" in t for t in texts)
    # 不传 failed 时不该出现该声明（避免把正常稿污名化）
    plain = build_docx(payload, tmp_path / "p.docx")
    assert BANNER_FAILED not in _docx_texts(plain)


def test_empty_library_is_needs_review_not_missing():
    """空库 ≠ 无命中：不能判"不满足"，否则整份标书写成"全部偏离"、技术分归零。"""
    idx = CapabilityIndex([])
    assert idx.empty_library is True
    m = idx.match(Requirement(id="T001", kind="tech", text="设备应支持 4K 全景视频播放", page=13))
    assert m.status == NEEDS_REVIEW
    assert m.status != MISSING
    assert m.confidence == 0.0
    assert m.evidence == EMPTY_LIBRARY_EVIDENCE
    assert "能力库为空" in m.evidence
    assert "BM25" not in m.evidence


def test_nonempty_library_without_hits_is_missing():
    """有库但真没命中，才是 MISSING（文案与空库分支必须不同）。"""
    idx = CapabilityIndex(
        [Capability(id="cap-quantum", type="产品", name="量子调度器", summary="量子比特纠错实验平台", tags=["量子"])]
    )
    assert idx.empty_library is False
    hit_probe = idx.search("危险化学品道路运输经营许可证及罐式集装箱检验报告")
    assert hit_probe == [], "该用例需要构造真正的零命中"
    m = idx.match(Requirement(id="T999", kind="tech", text="危险化学品道路运输经营许可证及罐式集装箱检验报告", page=9))
    assert m.status == MISSING
    assert m.evidence == NO_MATCH_EVIDENCE
    assert m.evidence != EMPTY_LIBRARY_EVIDENCE


def test_load_capabilities_marks_example_and_real(tmp_path):
    bundled = load_capabilities()
    assert bundled.is_example is True
    assert "示例数据" in bundled.meta

    real = tmp_path / "real.json"
    real.write_text(
        json.dumps({"capabilities": [{"id": "cap-a", "name": "n", "summary": "s", "tags": []}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert load_capabilities(real).is_example is False

    flagged = tmp_path / "flagged.json"
    flagged.write_text(json.dumps({"_example": True, "capabilities": []}), encoding="utf-8")
    assert load_capabilities(flagged).is_example is True


def test_allow_mock_defaults_to_off(monkeypatch):
    monkeypatch.delenv("BIDPILOT_ALLOW_MOCK", raising=False)
    assert Settings().allow_mock is False, "模板降级必须显式开启"
    monkeypatch.setenv("BIDPILOT_ALLOW_MOCK", "0")
    assert Settings().allow_mock is False
    monkeypatch.setenv("BIDPILOT_ALLOW_MOCK", "1")
    assert Settings().allow_mock is True

# ---------------------------------------------------------------------------
# 退出码优先级 / 三处留痕的一致性（Q3 审计：`_MOCK` + `_FAILED` 并存时三者互相拆台）
# ---------------------------------------------------------------------------

BANNER_TO_SEVERITY = {
    BANNER_FAILED: "empty_delivery",
    BANNER_MOCK: "mocked",
    BANNER_EXAMPLE_CAPS: "example_capabilities",
}


def _both_marks_result(tmp_path: Path) -> PipelineResult:
    """同时具备「有章节被降级为模板应答」与「没有任何通过校验的段落」的交付物。

    这个组合在 run_pipeline 的常规路径上很难同时打出来（模板段落带合法来源，通常能被
    aggregate 收进去），所以按契约直接在 PipelineResult 这一层构造：判定函数
    （exit_code_for / artifact_suffix）与 run_pipeline 用的是同一对函数，不是复制品。
    """
    return PipelineResult(
        doc_id="d1",
        tender=None,
        extraction=None,
        matches=[],
        workflow=WorkflowResult(
            doc_id="d1",
            sections=[SectionRun(section_id="gen-tech", title="第四章", state="failed", mocked=True)],
            mocked=True,
        ),
        aggregated={"sections": []},
        docx_path=tmp_path / "out.docx",
        empty_delivery=True,
        example_capabilities=True,
    )


def test_incomplete_beats_mocked_in_exit_code():
    """优先级 4 > 2 > 3 > 0：「不完整」比「有降级」更严重，压过模板应答与示例库。"""
    assert EXIT_PRIORITY == (EXIT_INCOMPLETE, EXIT_TEMPLATE_FALLBACK, EXIT_EXAMPLE_CAPABILITIES, EXIT_OK)
    assert exit_code_for([]) == EXIT_OK
    assert exit_code_for(["example_capabilities"]) == EXIT_EXAMPLE_CAPABILITIES
    assert exit_code_for(["mocked", "example_capabilities"]) == EXIT_TEMPLATE_FALLBACK
    assert exit_code_for(["mocked", "incomplete", "example_capabilities"]) == EXIT_INCOMPLETE
    assert exit_code_for(["empty_delivery", "mocked"]) == EXIT_INCOMPLETE
    assert exit_code_for(["failed", "mocked"]) == EXIT_INCOMPLETE


def test_filename_suffix_takes_the_most_severe_mark():
    """一个文件名只能表达一种污染：`_FAILED` 压过 `_MOCK`。"""
    assert artifact_suffix(empty_delivery=True, mocked=True) == FAILED_SUFFIX
    assert artifact_suffix(empty_delivery=True, mocked=False) == FAILED_SUFFIX
    assert artifact_suffix(empty_delivery=False, mocked=True) == MOCK_SUFFIX
    assert artifact_suffix(empty_delivery=False, mocked=False) == ""


def test_mocked_and_failed_marks_agree_on_exit_code_filename_and_first_banner(tmp_path):
    """三处留痕必须指向同一个结论，不许「文件名说废稿、退出码说降级」。"""
    res = _both_marks_result(tmp_path)
    severities = {p["severity"] for p in res.problems()}
    assert {"mocked", "empty_delivery", "example_capabilities"} <= severities

    # 1) 退出码
    assert res.exit_code == EXIT_INCOMPLETE
    # 2) 文件名（与 PipelineResult.suffix 同源，run_pipeline 用的就是它）
    assert res.suffix == FAILED_SUFFIX
    assert not res.suffix.endswith(MOCK_SUFFIX), "文件名不能把废稿说成「只是降级」"

    # 3) 横幅：两张都在，但第一张（最严重）必须是废稿声明
    path = build_docx(
        {"title": "t", "sections": [], "failures": [{"section_id": "gen-tech", "title": "第四章", "reason": "整节失败"}]},
        tmp_path / f"x{res.suffix}.docx",
        mocked=res.workflow.mocked,
        example_capabilities=res.example_capabilities,
        failed=res.empty_delivery,
    )
    texts = _docx_texts(path)
    assert texts[0] == BANNER_FAILED
    assert BANNER_MOCK in texts and BANNER_EXAMPLE_CAPS in texts, "其余污染不许被最严重那条吃掉"

    # 4) 三处指向同一个结论：首段横幅的严重度 → 退出码，与 res.exit_code 一致
    assert BANNER_TO_SEVERITY[texts[0]] in severities
    assert exit_code_for([BANNER_TO_SEVERITY[texts[0]]]) == res.exit_code
    assert path.name.endswith(f"{res.suffix}.docx")


def test_readme_exit_code_contract_matches_code():
    """README 的退出码表是给用户看的契约，优先级必须与 EXIT_PRIORITY 同向。"""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "优先级：4 > 2 > 3 > 0" in readme, "README 的优先级必须与 pipeline.EXIT_PRIORITY 一致"
    assert "含 `--no-generate`" not in readme, "分阶段跑不会被判成 4，README 不许再这么写"
    assert "`--no-generate`" in readme
