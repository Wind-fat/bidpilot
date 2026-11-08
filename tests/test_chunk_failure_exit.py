"""抽取块失败必须变成「不可忽视的交付物状态」：退出码 4 / `_PARTIAL` / 首页横幅 / CLI 清单四者同源。

背景：第一版真调跑出 4/14 块整块失败（每块重试 3 次仍空），CLI 仍报 3、文件名没有后缀，
用户看不出交付物少了 p23-25 的技术要求。这里用手工构造的「1 块失败 + 其余正常 + 示例能力库」
把这条契约钉住，不重跑 41 页（生成用 FakeLLM，抽取结果直接落盘 JSON）。
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
from bidpilot.export import BANNER_FAILED, BANNER_MOCK, BANNER_PARTIAL_HEAD, build_docx, partial_banner
from bidpilot.extract import ChunkReport, ExtractResult, dump_extract
from bidpilot.llm import FakeLLM
from bidpilot.pipeline import (
    EXIT_EXAMPLE_CAPABILITIES,
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_TEMPLATE_FALLBACK,
    FAILED_SUFFIX,
    MOCK_SUFFIX,
    PARTIAL_SUFFIX,
    SUFFIX_SEVERITY,
    artifact_suffix,
    exit_code_for,
    format_degradation_report,
    run_pipeline,
)
from bidpilot.schemas import TechRequirement
from bidpilot.sections import load_tender

ROOT = Path(__file__).resolve().parents[1]
JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"

pytestmark = pytest.mark.skipif(not JIANGSU.exists(), reason="缺少真实招标文件样本")

FAILED_ERROR = "空结果：未抽到任何条目（任务 tech_requirements）"


def _docx_texts(path: Path) -> list[str]:
    return [p.text for p in Document(str(path)).paragraphs if p.text.strip()]


def _quote_on_page(tender, page: int) -> str:
    """从真实页文本里切一段**保证能定位**的原文（避免 relocate 把条目打成 needs_review）。"""
    for seg in tender.page_text(page).split("。"):
        seg = seg.strip()
        if len(seg) >= 12:
            return seg[:40]
    raise AssertionError(f"第 {page} 页没有可用于定位的原文片段")


def _write_extract(tmp_path: Path) -> tuple[Path, str]:
    """1 个块失败 + 2 个块正常；块 id 用真实章节 id，章节标题才查得到。"""
    tender = load_tender(JIANGSU)
    tech = next(s for s in tender.sections if s.kind == "technical")
    ok_page = tech.page_start
    fail_page = min(tech.page_start + 1, tech.page_end)

    res = ExtractResult(title="测试招标文件", source=str(JIANGSU))
    res.tech_requirements = [
        TechRequirement(
            id="T001",
            no="3.1",
            text=_quote_on_page(tender, ok_page),
            page=ok_page,
            page_end=ok_page,
            quote=_quote_on_page(tender, ok_page),
        ),
        TechRequirement(
            id="T002",
            no="3.2",
            text=_quote_on_page(tender, fail_page),
            page=fail_page,
            page_end=fail_page,
            quote=_quote_on_page(tender, fail_page),
        ),
    ]
    failed_id = f"{tech.id}#p{fail_page}-{tech.page_end}"
    res.reports = [
        ChunkReport(
            chunk_id=f"{tech.id}#p{ok_page}-{ok_page}",
            pages=(ok_page, ok_page),
            tasks=["tech_requirements"],
            attempts=1,
            ok=True,
            counts={"tech_requirements": 1},
        ),
        ChunkReport(
            chunk_id=failed_id,
            pages=(fail_page, tech.page_end),
            tasks=["tech_requirements"],
            attempts=3,
            escalated=True,
            ok=False,
            errors=[FAILED_ERROR],
            counts={"tech_requirements": 0},
        ),
        ChunkReport(
            chunk_id=f"{tech.id}#p{tech.page_end}-{tech.page_end}",
            pages=(tech.page_end, tech.page_end),
            tasks=["tech_requirements"],
            attempts=1,
            ok=True,
            counts={"tech_requirements": 1},
        ),
    ]
    p = tmp_path / "extract_failed_chunk.json"
    p.write_text(json.dumps(dump_extract(res), ensure_ascii=False), encoding="utf-8")
    return p, failed_id


def _fake_generation() -> FakeLLM:
    return FakeLLM(
        [
            json.dumps(
                {
                    "paragraphs": [
                        {"text": "我方以 VR 教室方案响应。", "sources": ["T001"], "kind": "技术响应"}
                    ]
                },
                ensure_ascii=False,
            )
        ],
        repeat_last=True,
    )


async def test_failed_chunk_sets_exit_code_4_and_partial_suffix_and_banner(tmp_path):
    """「1 块失败 + 其余正常 + 示例能力库」：退出码 / 文件名 / 横幅 / 清单四处必须一致。"""
    extract_json, failed_id = _write_extract(tmp_path)
    settings = Settings(allow_mock=False, out_dir=tmp_path)  # 能力库用默认示例库

    res = await run_pipeline(
        JIANGSU,
        settings=settings,
        llm=_fake_generation(),
        generate=True,
        save_db=False,
        out_dir=tmp_path,
        extract_json=extract_json,
    )

    # 0) 前提：不是「整份废稿」，只是少了一块；示例能力库同时成立
    assert res.empty_delivery is False
    assert res.workflow.mocked is False
    assert res.example_capabilities is True
    assert res.chunk_failures() and res.chunk_failures()[0]["chunk_id"] == failed_id

    # 1) 退出码：4（内容不完整）压过 3（示例库）
    assert res.exit_code == EXIT_INCOMPLETE == 4
    assert exit_code_for([SUFFIX_SEVERITY[res.suffix]]) == res.exit_code, "后缀与退出码必须同源"

    # 2) 文件名：_PARTIAL，不许冒充完整稿、也不许冒充废稿
    assert res.suffix == PARTIAL_SUFFIX
    assert res.docx_path.name.endswith("_技术应答初稿_PARTIAL.docx"), res.docx_path.name
    assert not res.docx_path.name.endswith(FAILED_SUFFIX + ".docx")
    assert not res.docx_path.name.endswith(MOCK_SUFFIX + ".docx")
    assert res.docx_path.exists()

    # 3) 首页横幅：第一段就点名失败块 + 缺了哪类内容
    texts = _docx_texts(res.docx_path)
    assert texts[0].startswith(BANNER_PARTIAL_HEAD), texts[0]
    assert failed_id in texts[0], "横幅必须带失败块标识"
    assert "技术要求" in texts[0], "横幅必须说明缺了哪部分内容"
    assert "不得直接作为投标文件使用" in texts[0]
    assert BANNER_FAILED not in texts and BANNER_MOCK not in texts

    # 4) 正文也列明细（打开文件的人不靠退出码也能看到）
    assert any("抽取不完整的页段" in t for t in texts)
    assert any(failed_id in t and FAILED_ERROR in t for t in texts)

    # 5) CLI 清单：逐块列出失败块，末行给出同一个退出码
    report = format_degradation_report(res)
    body = "\n".join(report)
    assert failed_id in body
    assert "缺" in body and "技术要求" in body
    assert report[-1].strip().startswith("→ 退出码 4")

    # 6) stats 里同一份明细（JSON 消费方看到的也是它）
    assert res.stats()["delivery"]["chunk_failures"][0]["chunk_id"] == failed_id
    assert res.stats()["extraction"]["chunks_failed"] == [failed_id]


def test_partial_banner_names_chunk_pages_and_missing_items(tmp_path):
    """横幅是可读文本，不是只贴一个块号：chunk id + 页码 + 缺失条目类型都要在。"""
    rows = [
        {
            "chunk_id": "technical-x#p23-25",
            "page_start": 23,
            "page_end": 25,
            "section_title": "第四章 采购项目技术、规格和数量要求等",
            "missing": ["技术要求", "废标条款"],
            "attempts": 3,
            "reason": FAILED_ERROR,
        }
    ]
    text = partial_banner(rows)
    assert text.startswith(BANNER_PARTIAL_HEAD)
    assert "technical-x#p23-25" in text and "p23-25" in text
    assert "技术要求、废标条款" in text
    assert "重试 3 次" in text

    doc = build_docx({"title": "t", "sections": []}, tmp_path / "p.docx", chunk_failures=rows)
    texts = _docx_texts(doc)
    assert texts[0] == text
    plain = build_docx({"title": "t", "sections": []}, tmp_path / "plain.docx")
    assert not _docx_texts(plain)[0].startswith(BANNER_PARTIAL_HEAD), "正常稿不许带不完整横幅"


def test_review_only_chunk_is_not_claimed_as_missing():
    """抽到了但 quote/page 校验不过 ≠ 「内容缺失」：横幅只许说 needs_review，不许说「缺」。"""
    rows = [
        {
            "chunk_id": "technical-x#p9-10",
            "page_start": 9,
            "page_end": 10,
            "section_title": "",
            "missing": ["技术要求"],
            "attempts": 3,
            "reason": "quote 未能在第 9 页原文中找到",
            "extracted": 4,
        }
    ]
    text = partial_banner(rows)
    assert "needs_review" in text and "人工复核" in text
    assert "缺技术要求" not in text, "已抽到的条目不能被说成「缺失」"


def test_partial_suffix_ranks_between_failed_and_mock(tmp_path):
    """一个文件名只表达一种污染：`_FAILED` > `_PARTIAL` > `_MOCK`，且都指向同一个退出码。"""
    assert artifact_suffix(empty_delivery=False, mocked=False, partial=True) == PARTIAL_SUFFIX
    assert artifact_suffix(empty_delivery=False, mocked=True, partial=True) == PARTIAL_SUFFIX
    assert artifact_suffix(empty_delivery=True, mocked=False, partial=True) == FAILED_SUFFIX
    assert artifact_suffix(empty_delivery=True, mocked=True, partial=True) == FAILED_SUFFIX
    assert SUFFIX_SEVERITY == {FAILED_SUFFIX: "empty_delivery", PARTIAL_SUFFIX: "incomplete", MOCK_SUFFIX: "mocked"}
    assert exit_code_for([SUFFIX_SEVERITY[FAILED_SUFFIX]]) == EXIT_INCOMPLETE
    assert exit_code_for([SUFFIX_SEVERITY[PARTIAL_SUFFIX]]) == EXIT_INCOMPLETE
    assert exit_code_for([SUFFIX_SEVERITY[MOCK_SUFFIX]]) == EXIT_TEMPLATE_FALLBACK
    assert exit_code_for([SUFFIX_SEVERITY[MOCK_SUFFIX], "example_capabilities"]) == EXIT_TEMPLATE_FALLBACK
    assert exit_code_for(["example_capabilities"]) == EXIT_EXAMPLE_CAPABILITIES != EXIT_OK


def test_cli_stdout_lists_failed_chunk_and_exits_4(tmp_path):
    """真跑 CLI（不联网：--extract-json + --no-generate）：stdout 点名失败块，进程码 4。"""
    extract_json, failed_id = _write_extract(tmp_path)
    out_dir = tmp_path / "out"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
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
    assert proc.returncode == EXIT_INCOMPLETE == 4, (proc.returncode, proc.stdout, proc.stderr)
    assert failed_id in proc.stdout, proc.stdout
    assert "缺技术要求" in proc.stdout
    assert "_PARTIAL" in proc.stdout
    assert "退出码 4" in proc.stdout
    assert (out_dir / "tender_jiangsu_技术应答初稿_PARTIAL.docx").exists()


# ---------------------------------------------------------------------------
# 文档漂移：README 退出码表必须与代码里的后缀/退出码逐行一致
# ---------------------------------------------------------------------------


def _readme_exit_code_rows() -> list[list[str]]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## 降级与污染标记（退出码）")
    block = readme[start:]
    end = block.find("\n## ", 1)
    if end > 0:
        block = block[:end]
    rows: list[list[str]] = []
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 4 and cells[3].isdigit():  # 只收「情况/横幅/文件名/退出码」的数据行
            rows.append(cells)
    return rows


def test_readme_chunk_failure_row_matches_code():
    """`chunks_failed` 这一行的横幅文案、文件名后缀、退出码必须与代码里的常量一致。"""
    rows = _readme_exit_code_rows()
    assert rows, "README 退出码表没解析出来，文档结构变了就先修测试"
    matched = [r for r in rows if PARTIAL_SUFFIX in r[2]]
    assert len(matched) == 1, f"退出码表里应有且只有一行用 {PARTIAL_SUFFIX}：{[r[2] for r in rows]}"
    row = matched[0]
    assert "chunks_failed" in row[0], "这一行必须说清触发条件是 chunks_failed > 0"
    assert row[3] == str(EXIT_INCOMPLETE) == "4"
    assert BANNER_PARTIAL_HEAD in row[1], "README 里的横幅句首必须与 export.BANNER_PARTIAL_HEAD 一致"
    assert "缺哪类条目" in row[1], "横幅必须承诺说明「缺了哪部分内容」"


def test_readme_suffix_matrix_agrees_with_suffix_severity():
    """表里每个后缀 → 退出码，必须与 SUFFIX_SEVERITY + exit_code_for 算出的一致。"""
    seen: dict[str, int] = {}
    for row in _readme_exit_code_rows():
        for suffix in (FAILED_SUFFIX, PARTIAL_SUFFIX, MOCK_SUFFIX):
            if suffix in row[2]:
                seen[suffix] = int(row[3])
    assert seen == {FAILED_SUFFIX: 4, PARTIAL_SUFFIX: 4, MOCK_SUFFIX: 2}, seen
    for suffix, severity in SUFFIX_SEVERITY.items():
        assert exit_code_for([severity]) == seen[suffix], f"{suffix} 的退出码与 README 不一致"