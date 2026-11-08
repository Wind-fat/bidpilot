"""「少而合法」必须留痕：`ChunkReport.low_yield` / CLI 清单 / docx 横幅+正文 / `problems()` 四处同源。

背景（docs/REAL-PROVIDER.md「p23-25 定性实验」）：同一块、同一 prompt、温度 0、**全程开思考**，
三轮仍然抽出 2 / 49 / 3 条，而 `ok=True`、needs_review=0、quote 100% 可回查 ——
「少而合法」的退化对现有四道闸完全不可见。这里钉住两件事：
1. 它必须被标出来（四处留痕一致）；
2. 它**不是失败**：退出码与文件名都不许因为低产而变化（否则用户会把它当「缺内容」）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

from bidpilot.config import Settings
from bidpilot.export import BANNER_LOW_YIELD_HEAD, BANNER_PARTIAL_HEAD, low_yield_banner
from bidpilot.extract import (
    LOW_YIELD_ABS,
    ChunkReport,
    ExtractResult,
    build_chunks,
    dump_extract,
    is_structured_body,
    mark_low_yield,
    peer_yield,
)
from bidpilot.llm import FakeLLM
from bidpilot.pipeline import EXIT_OK, PARTIAL_SUFFIX, format_degradation_report, run_pipeline
from bidpilot.schemas import TechRequirement
from bidpilot.sections import load_tender

ROOT = Path(__file__).resolve().parents[1]
JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"

pytestmark = pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")


def _docx_texts(path: Path) -> list[str]:
    return [p.text for p in Document(str(path)).paragraphs if p.text.strip()]


def _quote_on_page(tender, page: int) -> str:
    for seg in tender.page_text(page).split("。"):
        seg = seg.strip()
        if len(seg) >= 12:
            return seg[:40]
    raise AssertionError(f"第 {page} 页没有可用于定位的原文片段")


def _report(chunk, n: int) -> ChunkReport:
    """按真实块的正文长度/形态造一个块报告（产出率与结构化判定都由真实正文决定）。"""
    key = chunk.tasks[0]
    return ChunkReport(
        chunk_id=chunk.id,
        pages=(chunk.page_start, chunk.page_end),
        tasks=chunk.tasks,
        attempts=1,
        ok=True,
        counts={key: n},
        body_chars=len(chunk.body),
        structured=is_structured_body(chunk.body),
    )


def _write_low_yield_extract(tmp_path: Path) -> tuple[Path, str, list[str]]:
    """真实分块 + 手工计数：1 个表格/清单型块只抽 2 条（低产），其余同章块正常。

    返回 (extract json, 低产块 id, 必须**不**被判低产的块 id 列表)。
    """
    tender = load_tender(JIANGSU)
    chunks = build_chunks(tender)
    tech = [c for c in chunks if c.section_kind == "technical"]
    target = next(c for c in tech if is_structured_body(c.body))  # 本语料里是 p23-25（图书/期刊清单）
    peers = [c for c in tech if c is not target][:2]
    prose = next(c for c in chunks if c.section_kind == "bidder_instructions")

    res = ExtractResult(title="低产块测试", source=str(JIANGSU))
    res.reports = [_report(target, 2), *(_report(c, 12) for c in peers), _report(prose, 2)]
    seq = 1
    for chunk, n in ((target, 2), *((c, 12) for c in peers)):
        page = chunk.page_start
        quote = _quote_on_page(tender, page)
        for i in range(n):
            res.tech_requirements.append(
                TechRequirement(id=f"T{seq:03d}", no=f"{page}.{i + 1}", text=f"{quote}（第 {i + 1} 条）",
                                page=page, page_end=page, quote=quote)
            )
            seq += 1
    p = tmp_path / "extract_low_yield.json"
    p.write_text(json.dumps(dump_extract(res), ensure_ascii=False), encoding="utf-8")
    return p, target.id, [c.id for c in peers] + [prose.id]


def _caps_file(tmp_path: Path) -> Path:
    """非示例能力库：让「低产」成为唯一告警，才能断言退出码没被它改动。"""
    p = tmp_path / "caps.json"
    p.write_text(
        json.dumps(
            {"capabilities": [
                {"id": "cap-vr", "type": "产品", "name": "VR 智慧教室", "summary": "VR 头盔与全景播放", "tags": ["VR"]}
            ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _fake_generation() -> FakeLLM:
    return FakeLLM(
        [json.dumps({"paragraphs": [{"text": "我方以 VR 教室方案响应。", "sources": ["T001"], "kind": "技术响应"}]},
                    ensure_ascii=False)],
        repeat_last=True,
    )


# --------------------------------------------------------------------------- 判定规则


def test_structured_body_detection_matches_real_paragraph_shapes(real_doc):
    """「表格/清单型」= 行数够多且中位行长很短；须知类散文页（p4-4）不许被当结构化块。"""
    chunks = build_chunks(real_doc)
    structured = [c.id for c in chunks if is_structured_body(c.body)]
    assert any(cid.endswith("#p23-25") for cid in structured), structured
    assert not any(cid.endswith("#p4-4") for cid in structured), structured
    # 散文：行数够多但每行都是长段 → 不算结构化
    prose = "\n".join("本项要求供应商按招标文件规定的格式提交材料，缺失或不可辨认的不得分。" * 2 for _ in range(20))
    assert is_structured_body(prose) is False
    # 清单：20 行短行 → 结构化
    assert is_structured_body("\n".join(f"第 {i} 项 书名{i} 作者{i} 2012" for i in range(20))) is True


def test_mark_low_yield_flags_only_structured_underproducer():
    """低产三条判据同时成立才标记：块成功 + 结构化 + 低于绝对下限/同章均值 1/3。"""
    reports = [
        ChunkReport(chunk_id="ch#p1-1", pages=(1, 1), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 24}, body_chars=1500, structured=True),
        ChunkReport(chunk_id="ch#p2-2", pages=(2, 2), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 24}, body_chars=1500, structured=True),
        # 低产：2 条 / 1600 字 = 1.25 条/千字符（同章均值 16 的 1/3 是 5.33，绝对下限是 2.0）
        ChunkReport(chunk_id="ch#p3-3", pages=(3, 3), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 2}, body_chars=1600, structured=True),
        # 同样稀疏但是散文正文（须知类）：不判低产
        ChunkReport(chunk_id="ch#p4-4", pages=(4, 4), tasks=["disqualifications"], ok=True,
                    counts={"disqualifications": 2}, body_chars=1400, structured=False),
        # 失败块走「整块失败」，不重复报低产
        ChunkReport(chunk_id="ch#p5-5", pages=(5, 5), tasks=["tech_requirements"], ok=False,
                    counts={"tech_requirements": 1}, body_chars=1500, structured=True,
                    errors=["空结果：未抽到任何条目"]),
    ]
    flagged = mark_low_yield(reports)
    assert [r.chunk_id for r in flagged] == ["ch#p3-3"]
    target = flagged[0]
    assert target.yield_rate == pytest.approx(1.25)
    # 同章均值按**所有成功块**算（含散文块 1.43）：散文只是自己不判低产，仍要进均值，
    # 否则「同章均值」会被悄悄换成「同章结构化块均值」，与文档写的规则不一致。
    mean, n = peer_yield(reports, target)
    assert n == 3 and mean == pytest.approx((16.0 + 16.0 + 1.43) / 3, abs=0.01)
    assert reports[3].yield_rate == pytest.approx(1.43) and reports[3].low_yield is False, "散文页不许被误判"


def test_absolute_floor_flags_structured_chunk_without_peers():
    """同章只有一个块（没有均值可比）时，绝对下限仍然要能兜住「少而合法」。"""
    lone = ChunkReport(chunk_id="only#p1-1", pages=(1, 1), tasks=["tech_requirements"], ok=True,
                       counts={"tech_requirements": 1}, body_chars=1500, structured=True)
    assert mark_low_yield([lone]) == [lone] and lone.yield_rate < 2.0
    ok = ChunkReport(chunk_id="only#p1-1", pages=(1, 1), tasks=["tech_requirements"], ok=True,
                     counts={"tech_requirements": 5}, body_chars=600, structured=True)
    assert mark_low_yield([ok]) == []


# --------------------------------------------------------------------------- 四处留痕


async def test_low_yield_leaves_four_consistent_traces(tmp_path):
    """1 个低产块 → ChunkReport / problems() / docx 横幅+正文 / CLI 清单 四处必须一致，
    并且退出码仍是 0、文件名不带 `_PARTIAL`（低产不是失败）。"""
    extract_json, low_id, normal_ids = _write_low_yield_extract(tmp_path)
    settings = Settings(allow_mock=False, out_dir=tmp_path, capability_file=_caps_file(tmp_path))
    res = await run_pipeline(
        JIANGSU, settings=settings, llm=_fake_generation(), generate=True, save_db=False,
        out_dir=tmp_path, extract_json=extract_json,
    )

    # 0) 前提：没有块整块失败、没有模板降级 —— 唯一的告警就是低产
    assert res.chunk_failures() == []
    assert res.workflow.mocked is False and res.empty_delivery is False
    assert res.example_capabilities is False

    # 1) ChunkReport：低产块被标记，同章正常块与散文块都不许被标
    by_id = {r.chunk_id: r for r in res.extraction.reports}
    target = by_id[low_id]
    assert target.low_yield is True and target.ok is True and target.structured is True
    assert target.yield_rate == pytest.approx(2 * 1000 / target.body_chars, abs=0.01)
    assert not any(by_id[cid].low_yield for cid in normal_ids), "正常块被误判成低产"
    assert res.stats()["extraction"]["chunks_low_yield"] == [low_id]

    # 2) problems()：severity=low_yield，说明「不是失败、退出码不变」
    low_problems = [p for p in res.problems() if p["severity"] == "low_yield"]
    assert len(low_problems) == 1 and low_problems[0]["section_id"] == low_id
    assert "可能漏抽" in low_problems[0]["reason"] and "不是失败" in low_problems[0]["reason"]
    assert res.exit_code == EXIT_OK == 0, "低产不许改动退出码"
    assert res.suffix == "" and not res.docx_path.name.endswith(PARTIAL_SUFFIX + ".docx")

    # 3) docx：首页黄底横幅 + 正文明细，措辞与 `_PARTIAL`（缺内容）分开
    texts = _docx_texts(res.docx_path)
    assert texts[0].startswith(BANNER_LOW_YIELD_HEAD), texts[0]
    assert low_id in texts[0] and "只抽到 2 条" in texts[0]
    assert "不是失败" in texts[0]
    assert not any(t.startswith(BANNER_PARTIAL_HEAD) for t in texts), "低产不是「内容缺失」"
    assert any("可能漏抽的页段" in t for t in texts)
    assert any(low_id in t and "条/千字符" in t for t in texts)
    # 判定依据可审计：横幅/正文要点明命中规则与对比基准（兄弟块均值 + 样本数）
    assert "命中规则" in texts[0] and "对比基准" in texts[0], texts[0]
    assert "个兄弟块" in texts[0], texts[0]
    assert any("命中规则" in t and "对比基准" in t for t in texts[1:])

    # 4) CLI 清单（format_degradation_report 与 run_pipeline 打印的是同一份 problems()）
    report = "\n".join(format_degradation_report(res))
    assert "[low_yield]" in report and low_id in report and "可能漏抽" in report
    assert report.splitlines()[-1].strip().startswith("→ 退出码 0")
    assert res.stats()["delivery"]["low_yield_chunks"][0]["chunk_id"] == low_id


def test_low_yield_banner_names_chunk_pages_and_rate():
    """横幅是可读文本：块 id + 页码 + 条目数 + 产出率 + 同章均值都要在。"""
    rows = [
        {
            "chunk_id": "technical-x#p23-25",
            "page_start": 23,
            "page_end": 25,
            "section_title": "第四章 采购项目技术、规格和数量要求等",
            "items": 2,
            "yield_rate": 1.27,
            "peer_mean": 18.65,
        }
    ]
    text = low_yield_banner(rows)
    assert text.startswith(BANNER_LOW_YIELD_HEAD)
    assert "technical-x#p23-25" in text and "p23-25" in text
    assert "只抽到 2 条" in text and "1.27 条/千字符" in text and "18.65" in text
    assert "不是失败" in text and "漏抽" in text


def test_cli_lists_low_yield_chunk_and_keeps_exit_code_zero(tmp_path):
    """真跑 CLI（--extract-json + --no-generate，不联网）：单列低产清单，进程码仍为 0。"""
    extract_json, low_id, _ = _write_low_yield_extract(tmp_path)
    out_dir = tmp_path / "out"
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "BIDPILOT_CAPABILITIES": str(_caps_file(tmp_path)),
    }
    env.pop("BIDPILOT_ALLOW_MOCK", None)

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_pipeline.py"),
            str(JIANGSU),
            "--extract-json",
            str(extract_json),
            "--no-generate",
            "--no-db",
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "低产块" in proc.stdout and low_id in proc.stdout, proc.stdout
    assert "可能漏抽" in proc.stdout and "不是失败" in proc.stdout
    assert "_PARTIAL" not in proc.stdout
    assert (out_dir / "tender_jiangsu_技术应答初稿.docx").exists()


def test_cli_env_thresholds_can_silence_low_yield(tmp_path):
    """同一份 extract JSON、同一个低产块：把两把阈值调到它下面，CLI 就必须不再报低产（env 真进到了判定里）。

    这是「阈值可配」的端到端证据：不重新抽取、不调模型，只改环境变量。
    """
    extract_json, _low_id, _ = _write_low_yield_extract(tmp_path)
    out_dir = tmp_path / "out"
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "BIDPILOT_CAPABILITIES": str(_caps_file(tmp_path)),
        # 该块 1.27 条/千字符；绝对下限压到 1.0、比例压到 0.01（7.96 × 0.01 = 0.08）→ 两条判据都不成立
        "BIDPILOT_LOW_YIELD_ABS": "1.0",
        "BIDPILOT_LOW_YIELD_PEER_RATIO": "0.01",
    }
    env.pop("BIDPILOT_ALLOW_MOCK", None)

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_pipeline.py"),
            str(JIANGSU),
            "--extract-json",
            str(extract_json),
            "--no-generate",
            "--no-db",
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "低产块      ：0 块" in proc.stdout, proc.stdout
    assert "[low_yield]" not in proc.stdout, proc.stdout
    texts = _docx_texts(out_dir / "tender_jiangsu_技术应答初稿.docx")
    assert not any(t.startswith(BANNER_LOW_YIELD_HEAD) for t in texts), "阈值调低后横幅不该出现"


def test_docs_pin_low_yield_rule_banner_and_strategy():
    """README / docs 不许与代码常量漂开：横幅句首、阈值、字段名、定性实验命令都要对得上。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "REAL-PROVIDER.md").read_text(encoding="utf-8")
    assert BANNER_LOW_YIELD_HEAD in readme, "README 的横幅句首必须与 export.BANNER_LOW_YIELD_HEAD 一致"
    assert BANNER_LOW_YIELD_HEAD in doc
    assert "chunks_low_yield" in readme and "low_yield_rows" in readme
    assert str(LOW_YIELD_ABS) in readme and "1/3" in readme
    # 处理策略必须写清「默认不开思考 / 不强制重跑」，并给出可复现的实验命令
    assert "表格/清单型块：定性结论与处理策略" in doc
    assert "--thinking always" in doc and "--repeat 3" in doc
    assert "不默认开" in doc and "不强制" in doc