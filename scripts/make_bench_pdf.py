#!/usr/bin/env python
"""把 26 章语料渲染成**真 PDF**（reportlab，dev-only），供 200 页规模实测。

为什么需要它：docs/PERF.md 之前的两份 200 页语料都有口径缺口——
「41 页 PDF 循环 5 次」章标题被去重成 5 章，「26 章 Markdown 语料」没有 pypdf 抽文这一段。
本脚本把真实语料的条款正文重新组织到 26 个互不相同的章标题下，用 reportlab 逐页排版成
一份带中文文本层的 PDF：页数、章节数、抽文成本三者同时为真。

    ./.venv/Scripts/python.exe scripts/make_bench_pdf.py
    # → out/perf/tender_26ch_200p.pdf（约 200 页 / 26 章）

产物不入仓（体积大、可随时重生成）；命令写进 docs/PERF.md。
依赖：reportlab 在 pyproject.toml 的 dev 组，**不得**进运行时 dependencies。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_bench_25ch import CHAPTERS, clause_pool  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer  # noqa: E402

from bidpilot.sections import load_tender  # noqa: E402

# 中文排版用系统字体（Windows 自带）。字体文件不入仓，PDF 也不入仓：
# 只有本机重生成时才需要它，换机器时 --body-font/--head-font 指到任意中文字体即可。
FONT_CANDIDATES: dict[str, list[Path]] = {
    "body": [
        Path("C:/Windows/Fonts/simfang.ttf"),  # 仿宋：招标文件正文的常见字体
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    ],
    "head": [
        Path("C:/Windows/Fonts/simhei.ttf"),  # 黑体：章标题
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ],
}


def resolve_font(kind: str, override: str | None) -> Path:
    if override:
        p = Path(override)
        if not p.exists():
            raise SystemExit(f"--{kind}-font 指向的文件不存在：{p}")
        return p
    for cand in FONT_CANDIDATES[kind]:
        if cand.exists():
            return cand
    raise SystemExit(
        f"没找到可用的中文字体（{kind}）。用 --{kind}-font 指到一个 TTF/TTC 中文字体，"
        "或改用英文占位语料（本脚本不提供，见 docs/PERF.md 的口径说明）。"
    )


def register(name: str, path: Path) -> str:
    """注册字体；.ttc 需要 subfontIndex（reportlab 的 TTFont 支持）。"""
    font = (
        TTFont(name, str(path), subfontIndex=0)
        if path.suffix.lower() == ".ttc"
        else TTFont(name, str(path))
    )
    pdfmetrics.registerFont(font)
    return name


def styleset(body_font: str, head_font: str) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "CoverTitle", fontName=head_font, fontSize=22, leading=30,
            alignment=1, spaceAfter=16 * mm,
        ),
        "cover": ParagraphStyle(
            "CoverBody", fontName=body_font, fontSize=12, leading=22, alignment=1,
        ),
        "chapter": ParagraphStyle(
            "ChapterTitle", fontName=head_font, fontSize=16, leading=24,
            spaceBefore=0, spaceAfter=6 * mm,
        ),
        # 行距 15.5pt / 字号 10.5pt：A4 正文每页约 1500 字，接近真实招标文件的密度
        "body": ParagraphStyle(
            "Body", fontName=body_font, fontSize=10.5, leading=15.5,
            firstLineIndent=21, spaceAfter=1.5 * mm, wordWrap="CJK",
        ),
    }


def chapter_paragraphs(chapter_no: int, title: str, pool: list[str], chars: int,
                       cursor: int) -> tuple[list[str], int]:
    """造一章正文：条款逐条成段（每条独立段落，避免整章挤成一个巨块）。"""
    out = [f"{chapter_no}.1 本章适用于本项目的相关条款，投标人须逐条响应并对响应内容的真实性承担全部责任。"]
    size = len(out[0])
    n = 2
    while size < chars:
        body = pool[cursor % len(pool)]
        cursor += 1
        line = f"{chapter_no}.{n} {body}"
        out.append(line)
        size += len(line)
        n += 1
    return out, cursor


def build_pdf(
    out: Path,
    pool: list[str],
    chars_per_chapter: int,
    body_font: str,
    head_font: str,
    project: str,
) -> tuple[Path, int]:
    styles = styleset(body_font, head_font)
    story: list = [
        Spacer(1, 45 * mm),
        Paragraph(escape("公开招标文件"), styles["title"]),
        Paragraph(escape(project), styles["cover"]),
        Paragraph(escape("（性能基准语料：26 章 / 约 200 页，正文取自真实招标文件条款）"), styles["cover"]),
        Spacer(1, 30 * mm),
        Paragraph(escape("二〇二六年十一月"), styles["cover"]),
        PageBreak(),
    ]
    cursor = 0
    for i, title in enumerate(CHAPTERS, 1):
        story.append(Paragraph(escape(title), styles["chapter"]))
        paras, cursor = chapter_paragraphs(i, title, pool, chars_per_chapter, cursor)
        for text in paras:
            story.append(Paragraph(escape(text), styles["body"]))
        story.append(PageBreak())

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="26 章 200 页性能基准语料", author="BidPilot bench", subject="perf fixture",
    )
    doc.build(story)
    return out, len(pool)


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 26 章 / 200 页真 PDF 基准语料")
    ap.add_argument("--pdf", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"),
                    help="条款正文来源（真实招标文件）")
    ap.add_argument("--out", default=str(ROOT / "out" / "perf" / "tender_26ch_200p.pdf"))
    ap.add_argument("--target-pages", type=int, default=200, help="目标页数下限（默认 200）")
    ap.add_argument("--chars-per-chapter", type=int, default=9000, help="每章正文字数（迭代起点）")
    ap.add_argument("--body-font")
    ap.add_argument("--head-font")
    ap.add_argument("--project", default="项目名称：数字马克思主义学院智慧教室建设  项目编号：JSGH201908639")
    args = ap.parse_args()

    body_font = register("BenchBody", resolve_font("body", args.body_font))
    head_font = register("BenchHead", resolve_font("head", args.head_font))

    pool = clause_pool(Path(args.pdf))
    out = Path(args.out)

    # 页数由排版决定，先渲染再按比例补字；最多 5 轮，取第一个页数达标的版本。
    chars = args.chars_per_chapter
    pages = 0
    for attempt in range(1, 6):
        build_pdf(out, pool, chars, body_font, head_font, args.project)
        pages = page_count(out)
        print(f"第 {attempt} 轮：每章 {chars} 字 → {pages} 页（目标 ≥ {args.target_pages}）")
        if pages >= args.target_pages:
            break
        chars = int(chars * args.target_pages / max(pages, 1) * 1.02)

    doc = load_tender(out)
    if len(doc.sections) < len(CHAPTERS):
        raise SystemExit(
            f"章节数不对：解析出 {len(doc.sections)} 章，期望 {len(CHAPTERS)}。"
            "多半是某章标题被解析器合并/丢弃了，先看 _group_page 的标题行规则。"
        )
    print("-" * 64)
    print(f"PDF        ：{out}")
    print(f"文件大小   ：{out.stat().st_size} bytes")
    print(f"页数       ：{doc.page_count}（PDF 页对象 {pages}）")
    print(f"段落数     ：{len(doc.parsed.paragraphs)}")
    print(f"章节数     ：{len(doc.sections)}（标题 {len(CHAPTERS)} 个）")
    print(f"条款池     ：{len(pool)} 条（来自 {args.pdf}）")
    print(f"字体       ：正文 {body_font} / 标题 {head_font}（系统字体，未入仓）")
    for s in doc.sections[:3]:
        print(f"  - p{s.page_start}+{s.char_start} {s.kind:20s} {s.title}")
    print("  ……")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())