"""来源一致性：内容指纹优先于文件名（`quote_span` 之后的下一条闸）。

背景：只比文件名时，「两份都叫 tender.pdf、内容不同」这种最常见的情形查不出来，
于是“拿 A 的引用去 B 里定位”照样能报 100% 可回查。这里钉住两级判定。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from documind import parse_file  # noqa: E402

from bidpilot.citations import source_mismatch  # noqa: E402
from bidpilot.extract import ExtractResult, dump_extract, file_sha256, load_extract  # noqa: E402
from bidpilot.sections import TenderDoc, build_sections, scan_disqualifications  # noqa: E402

MD = "# 第一章 总则\n\n第一条 本制度适用于全体员工。\n"


def _doc(tmp_path: Path, name: str, text: str = MD) -> tuple[TenderDoc, Path]:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    parsed = parse_file(p)
    sections = build_sections(parsed)
    return (
        TenderDoc(path=p, parsed=parsed, sections=sections,
                  disq_hits=scan_disqualifications(parsed, sections)),
        p,
    )


def test_dump_load_roundtrip_keeps_sha(tmp_path: Path) -> None:
    _, src = _doc(tmp_path, "a.md")
    res = ExtractResult(title="t", source=str(src), source_sha256=file_sha256(src))
    back = load_extract(json.loads(json.dumps(dump_extract(res), ensure_ascii=False)))
    assert back.source_sha256 == res.source_sha256
    assert len(back.source_sha256) == 64


def test_same_name_different_content_is_caught(tmp_path: Path) -> None:
    """同名不同内容：指纹不一致 → 必须报，不能因为文件名相同就放行。"""
    doc, src = _doc(tmp_path, "tender.md")
    res = ExtractResult(title="t", source=str(src), source_sha256="0" * 64)
    why = source_mismatch(doc, res, pdf_sha256=file_sha256(src))
    assert why and "内容指纹" in why


def test_renamed_copy_with_same_content_is_not_a_mismatch(tmp_path: Path) -> None:
    """不同名同内容（拷贝/重命名）不算不一致：指纹一致就以指纹为准。"""
    doc, src = _doc(tmp_path, "renamed.md")
    other = tmp_path / "original.md"
    other.write_text(MD, encoding="utf-8")
    res = ExtractResult(title="t", source=str(other), source_sha256=file_sha256(other))
    assert source_mismatch(doc, res, pdf_sha256=file_sha256(src)) is None


def test_legacy_json_without_hash_falls_back_to_filename(tmp_path: Path) -> None:
    doc, src = _doc(tmp_path, "a.md")
    same = ExtractResult(title="t", source=str(src))  # 旧格式：无指纹
    assert source_mismatch(doc, same, pdf_sha256=file_sha256(src)) is None
    other = ExtractResult(title="t", source=str(tmp_path / "b.md"))
    why = source_mismatch(doc, other, pdf_sha256=file_sha256(src))
    assert why and "本次回查的是" in why


def test_empty_source_is_skipped(tmp_path: Path) -> None:
    doc, src = _doc(tmp_path, "a.md")
    assert source_mismatch(doc, ExtractResult(title="t", source=""), pdf_sha256=file_sha256(src)) is None