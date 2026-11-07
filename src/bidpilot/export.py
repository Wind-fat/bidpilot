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
from docx.shared import Pt, RGBColor

# 首页横幅文案：两种降级必须用**不同**措辞，评审员要能一眼分清"没调模型"和"能力库是假的"
BANNER_MOCK = "本节为模板应答，未调用模型（LLM 不可用，已启用模板降级 BIDPILOT_ALLOW_MOCK=1）"
BANNER_EXAMPLE_CAPS = "当前能力库为示例数据（EXAMPLE DATA），需求-能力匹配结果不具备商业意义"
# 整节失败 / 无任何通过校验的段落：这种文件只有封面+目录，必须在第一行就自证是废稿
BANNER_FAILED = "本文件不含任何通过校验的应答段落：生成阶段整节失败，交付物不完整，不得作为投标文件使用。"
# 分阶段跑（--no-generate）：只有抽取+矩阵，没有应答正文，同样是空壳，必须自证
BANNER_STAGED = "本文件只有抽取与能力矩阵结果：本次运行未生成应答正文（--no-generate），不能作为投标文件使用。"


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


def _add_banner(document: Document, text: str, fill: str = "FFF2CC") -> None:
    """首页加粗横幅：写进文档正文（不是页眉/批注），python-docx 读得回、评审员一眼看得见。"""
    p = document.add_paragraph()
    ppr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    ppr.append(shd)  # shd 在 pPr 序列里排在 jc/spacing 之前，先 append 再设对齐
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)


def build_docx(
    payload: dict,
    out_path: str | Path,
    *,
    mocked: bool = False,
    example_capabilities: bool = False,
    failed: bool = False,
    staged: bool = False,
) -> Path:
    """payload: {title, doc_id, sections: [{title, items, paragraphs:[{text, sources, kind}]}], failures?}

    `mocked` / `example_capabilities` 为真时在首页插横幅：这两种情况下的文档内容
    不能当作投标交付物，必须让打开文件的人立刻知道。

    `failed` 为真表示**整份交付物没有有效内容**（生成阶段整节失败）：此时首段就是
    废稿声明，正文里再列出失败章节——旧实现会落一份只有封面+目录的 35KB 文件，
    打开看结构完整，只有退出码能发现问题（ARCH-REVIEW P0-05 的延伸）。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    _set_cn_font(document)

    if failed:
        _add_banner(document, BANNER_FAILED, fill="FFC7CE")
    elif staged:
        _add_banner(document, BANNER_STAGED, fill="EDEDED")

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(payload.get("title") or "技术应答初稿")
    run.bold = True
    run.font.size = Pt(20)

    if mocked:
        _add_banner(document, BANNER_MOCK)
    if example_capabilities:
        _add_banner(document, BANNER_EXAMPLE_CAPS, fill="DDEBF7")

    meta = document.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run(f"BidPilot 自动生成 · {time.strftime('%Y-%m-%d %H:%M')}\n").italic = True
    meta.add_run("本文件为机器抽取+生成的初稿，所有条目均附原文页码与来源 id，须人工复核后使用。").italic = True

    document.add_heading("目录", level=1)
    add_toc(document)

    if failed:
        document.add_heading("未产出应答的章节", level=1)
        failures = payload.get("failures") or []
        if not failures:
            document.add_paragraph("（无逐节明细：生成阶段没有规划出任何章节，本文件不包含应答正文。）")
        for f in failures:
            document.add_paragraph(
                f"{f.get('section_id') or '-'} / {f.get('title') or '-'}：{f.get('reason') or '未产出任何应答'}"
            )

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
                # kind 必须落文档：被降级段落靠这行尾注自我暴露，不能只存在于 JSON 里
                kind = str(p.get("kind") or "").strip()
                if kind and kind != "技术响应":
                    note = ev.add_run(f"　〔类型：{kind}〕")
                    note.font.size = Pt(8)
                    if "模板" in kind:
                        note.bold = True
                        note.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

    document.save(str(out))
    return out


def _describe_source(source_id: str, item_by_id: dict) -> str:
    item = item_by_id.get(source_id)
    if item:
        page = item.get("page")
        return f"{source_id}（招标原文 p{page}）" if page else source_id
    return f"{source_id}（能力库）"