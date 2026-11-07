"""导出 Word：按章节层级渲染，保留标题层级与 TOC 域。

TOC 域是 Word 的"目录字段"（TOC \\o "1-3" \\h \\z \\u）：python-docx 不能计算结果页码，
Word/WPS 打开后按 F9（或"更新目录"）即可生成页码。这是 Word 的标准做法，不是偷懒。
"""

from __future__ import annotations

import time
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


def _set_cn_font(document: Document, name: str = "宋体") -> None:
    style = document.styles["Normal"]
    style.font.name = name
    style.font.size = Pt(10.5)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), name)


def add_toc(document: Document, levels: str = "1-3") -> None:
    p = document.add_paragraph()
    run = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f'TOC \\o "{levels}" \\h \\z \\u'
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "（在 Word/WPS 中右键“更新域”生成目录页码）"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, sep, placeholder, end):
        run._r.append(node)


def build_docx(payload: dict, out_path: str | Path) -> Path:
    """payload: {title, doc_id, sections: [{title, items, paragraphs:[{text, sources, kind}]}]}"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    _set_cn_font(document)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(payload.get("title") or "技术应答初稿")
    run.bold = True
    run.font.size = Pt(20)

    meta = document.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run(f"BidPilot 自动生成 · {time.strftime('%Y-%m-%d %H:%M')}\n").italic = True
    meta.add_run("本文件为机器抽取+生成的初稿，所有条目均附原文页码与来源 id，须人工复核后使用。").italic = True

    document.add_heading("目录", level=1)
    add_toc(document)

    item_by_id = {}
    for section in payload.get("sections") or []:
        for it in section.get("items") or []:
            item_by_id[it["id"]] = it

    for section in payload.get("sections") or []:
        document.add_heading(section.get("title") or "章节", level=1)
        paragraphs = section.get("paragraphs") or []
        # 按首个"招标条目"来源分组：让每条要求下面挂它的应答段落
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for p in paragraphs:
            src = next((s for s in p.get("sources", []) if not str(s).startswith("cap-")), "")
            if src not in groups:
                groups[src] = []
                order.append(src)
            groups[src].append(p)

        for src in order:
            item = item_by_id.get(src)
            if item:
                heading = f"{item.get('no') or '要求'} {str(item.get('text') or '')[:60]}".strip()
                document.add_heading(heading, level=2)
            else:
                document.add_heading("补充说明", level=2)
            for p in groups[src]:
                para = document.add_paragraph(p["text"])
                para.paragraph_format.space_after = Pt(6)
                evidence = "来源：" + "；".join(_describe_source(s, item_by_id) for s in p.get("sources", []))
                ev = document.add_paragraph()
                ev_run = ev.add_run(evidence)
                ev_run.italic = True
                ev_run.font.size = Pt(8)

    document.save(str(out))
    return out


def _describe_source(source_id: str, item_by_id: dict) -> str:
    item = item_by_id.get(source_id)
    if item:
        page = item.get("page")
        return f"{source_id}（招标原文 p{page}）" if page else source_id
    return f"{source_id}（能力库）"