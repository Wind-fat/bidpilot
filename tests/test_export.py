"""docx 导出：能打开（python-docx 读回）+ 标题层级 + TOC 域存在。"""

from __future__ import annotations

from docx import Document
from docx.oxml.ns import qn

from bidpilot.export import build_docx

PAYLOAD = {
    "title": "测试招标项目 技术应答初稿",
    "doc_id": "doc-test",
    "sections": [
        {
            "title": "第三章 采购需求",
            "items": [
                {"id": "T001", "no": "3.1", "text": "设备应支持 4K 全景视频播放", "page": 2},
                {"id": "T002", "no": "3.2", "text": "提供 7×12 小时热线服务", "page": 2},
            ],
            "paragraphs": [
                {"text": "我方 VR 智慧教室方案支持 4K 全景视频播放，单眼分辨率 1440x1600。",
                 "sources": ["T001", "cap-product-vr-classroom"], "kind": "技术响应"},
                {"text": "我方提供 7×12 小时热线与本地驻场服务，2 小时响应。",
                 "sources": ["T002", "cap-service-after-sales"], "kind": "服务响应"},
            ],
        }
    ],
}


def test_docx_export_opens_and_keeps_hierarchy(tmp_path):
    out = build_docx(PAYLOAD, tmp_path / "out.docx")
    assert out.exists() and out.stat().st_size > 2000

    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert len(texts) >= 8, "导出的段落数不应为 0/极少"
    assert any("技术应答初稿" in t for t in texts)
    assert "第三章 采购需求" in texts
    assert any("3.1" in t for t in texts)

    levels = [p.style.name for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert "Heading 1" in levels and "Heading 2" in levels

    # TOC 域
    instr = doc.element.body.findall(".//" + qn("w:instrText"))
    assert any("TOC" in (node.text or "") for node in instr), "缺少目录域"

    # 证据行
    assert any(t.startswith("来源：") and "T001" in t for t in texts)


def test_docx_export_handles_empty_sections(tmp_path):
    out = build_docx({"title": "空文档", "sections": []}, tmp_path / "empty.docx")
    doc = Document(str(out))
    assert doc.paragraphs
    assert out.stat().st_size > 1000