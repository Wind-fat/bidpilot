"""bidpilot 侧：documind 公开 API 契约（ARCH-REVIEW P0-03 / docs/DEPENDENCY.md §4）。

与 documind/tests/test_documind_contract.py 成对：那边保证「没破坏自己声明的面」，
这边保证「我们只用这个面，且它确实还在」。

禁止把 import 改成 `from documind.parsing import ...` —— 那等于把实现当接口。
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from documind import (
    BM25Index,
    EmptyDocumentError,
    IndexItem,
    Paragraph,
    ParsedDocument,
    parse_file,
    tokenize,
)

PUBLIC = {
    "parse_file",
    "ParsedDocument",
    "Paragraph",
    "tokenize",
    "IndexItem",
    "BM25Index",
    "EmptyDocumentError",
}
PARAGRAPH_FIELDS = {"text", "page", "start", "end", "level", "section_path"}
ROOT = Path(__file__).resolve().parents[1]


def test_version_satisfies_declared_range():
    """比对 pyproject 的 `documind>=0.1,<0.2`：不满足时给一句人话，而不是 ImportError 堆栈。"""
    ver = importlib.metadata.version("documind")
    major, minor = (int(part) for part in ver.split(".")[:2])
    assert (major, minor) == (0, 1), (
        f"documind {ver} 不满足 >=0.1,<0.2，请 cd ../documind && git pull && uv sync"
    )


def test_public_symbols_exist():
    import documind

    assert set(documind.__all__) == PUBLIC
    for name in PUBLIC:
        assert getattr(documind, name, None) is not None, f"documind 缺少公开符号 {name}"


def test_product_code_never_imports_submodules():
    """把「只吃公开面」变成可执行断言：src/ 里出现 `from documind.<子模块>` 就红。"""
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if "from documind." in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"这些文件绕过了公开面：{offenders}"


def test_paragraph_field_set():
    assert set(Paragraph.__dataclass_fields__) == PARAGRAPH_FIELDS


def test_slice_text_roundtrip():
    """C3/C4 一致性：块文本必须等于原文切片，跨页用 \\n 连接、右界左闭右开。"""
    doc = ParsedDocument(
        title="t",
        filename="t.md",
        pages=["甲乙丙丁", "戊己庚辛"],
        paragraphs=[Paragraph(text="乙丙", page=1, start=1, end=3)],
    )
    assert doc.page_count == 2
    assert doc.slice_text(1, 0, 1, 4) == "甲乙丙丁"
    assert doc.slice_text(1, 1, 1, 3) == "乙丙"
    assert doc.slice_text(1, 0, 2, 2) == "甲乙丙丁\n戊己"
    assert doc.slice_text(9, 0, 9, 3) == ""


def test_bm25_search_return_shape():
    idx = BM25Index([IndexItem(id="a", text="年假可以折现吗", doc_id="d1", strategy="structure")])
    hits = idx.search("年假", 3)
    assert isinstance(hits, list)
    assert all(isinstance(i, str) and isinstance(s, float) for i, s in hits), hits
    assert hits and hits[0][0] == "a"
    # 空索引返回 [] 而不是异常：capabilities.py 靠这条区分「无命中」
    assert BM25Index([]).search("年假", 3) == []


def test_tokenize_is_character_bigram():
    toks = tokenize("年假")
    assert "年假" in toks


def test_empty_document_error_is_catchable():
    assert issubclass(EmptyDocumentError, ValueError)


def test_parse_file_is_the_entry_point(tmp_path):
    md = tmp_path / "t.md"
    md.write_text("# 第一章 总则\n\n第一条 本制度适用于全体员工。\n", encoding="utf-8")
    doc = parse_file(md)  # 只吃顶层名字，不碰 documind.parsing
    assert isinstance(doc, ParsedDocument)
    first = doc.paragraphs[0]
    assert doc.slice_text(first.page, first.start, first.page, first.end) == first.text