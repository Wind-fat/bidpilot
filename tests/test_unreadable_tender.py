"""跨仓库护栏：documind 的 EmptyDocumentError 必须被 BidPilot 接住。

ARCH-REVIEW P0-04 第 3 条 / P0-04 下游影响：documind 在解析出口对空文本抛错后，
bidpilot 若不捕获，空 PDF 会从「静默产出 35KB 空 docx」变成 traceback——
两种都不合格。这里钉住第三种行为：可读错误 + 退出码 5 + 不落任何交付物。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.pipeline import EXIT_UNREADABLE  # noqa: E402
from bidpilot.sections import TenderParseError, load_tender  # noqa: E402


def _blank_pdf(path: Path) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_empty_markdown_raises_readable_error(tmp_path: Path) -> None:
    p = tmp_path / "空招标文件.md"
    p.write_text("\n\n   \n", encoding="utf-8")
    with pytest.raises(TenderParseError) as ei:
        load_tender(p)
    msg = str(ei.value)
    assert "无法解析招标文件" in msg
    assert "OCR" in msg
    assert ei.value.code == "tender_unreadable"


def test_pdf_without_text_layer_raises_readable_error(tmp_path: Path) -> None:
    p = _blank_pdf(tmp_path / "扫描件.pdf")
    with pytest.raises(TenderParseError) as ei:
        load_tender(p)
    assert "OCR" in str(ei.value)


def test_pipeline_stops_before_any_deliverable(tmp_path: Path) -> None:
    """不可解析时连 docx 都不许出现。"""
    import asyncio

    from bidpilot.config import get_settings
    from bidpilot.pipeline import run_pipeline

    src = tmp_path / "空.md"
    src.write_text("", encoding="utf-8")
    out = tmp_path / "out"
    s = get_settings()
    with pytest.raises(TenderParseError):
        asyncio.run(run_pipeline(src, settings=s, out_dir=out, generate=False, save_db=False))
    produced = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert produced == [], f"不应产出任何文件，实际 {produced}"


def test_cli_exit_code_is_5(tmp_path: Path) -> None:
    src = tmp_path / "空.md"
    src.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), str(src),
         "--no-db", "--no-generate", "--out-dir", str(tmp_path / "o")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == EXIT_UNREADABLE, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "tender_unreadable" in proc.stderr
    assert "OCR" in proc.stderr
    assert not list((tmp_path / "o").rglob("*.docx"))