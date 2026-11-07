"""解析 + 章节定位。

复用 documind.parsing.parse_file（不改它）：它给到 章/节/条 层级 + 每段的页码与页内字符偏移。
本模块只加招标文件语义：把「第 X 章」按标题关键词归类成
投标人须知 / 评标办法 / 技术规格（技术服务需求）/ 资格审查 / 合同 / 附件，
并扫描废标（无效投标）条款出现的页码。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from documind.parsing import EmptyDocumentError, Paragraph, ParsedDocument, parse_file

# slice_text 需要一个 char_end；用超大值表示"取到页尾"，这样连页长都不必读 pages（P1-06）。
PAGE_TO_END = 1 << 30


def page_text_of(parsed: ParsedDocument, page: int) -> str:
    """取某一页文本的唯一入口：走 documind 的 slice_text 强契约。

    不要退化成 `parsed.pages[page-1]`——pages 是解析器内部的分页产物，
    一旦 documind 改分页/清洗，直接索引会静默漂移，而 slice_text 是上游用测试钉住的那条契约
    （ARCH-REVIEW P1-06）。
    """
    if not (1 <= page <= parsed.page_count):
        return ""
    return parsed.slice_text(page, 0, page, PAGE_TO_END)


class TenderParseError(RuntimeError):
    """招标文件不可解析：无文本层、空文档、或解析后没有任何段落。

    这是「终止流水线」级错误——不能带着空的章节树往下走，否则会产出一份
    只有封面和目录的《技术应答初稿》并报成功（ARCH-REVIEW P0-04）。
    """

    code = "tender_unreadable"

    def __init__(self, path: str | Path, detail: str = "") -> None:
        self.path = str(path)
        self.detail = detail
        msg = (
            f"无法解析招标文件：{self.path}。"
            "该文件可能没有文本层（扫描件/图片型 PDF），请先 OCR 后再跑；"
            "本次未生成任何交付物。"
        )
        if detail:
            msg = f"{msg}（底层原因：{detail}）"
        super().__init__(msg)

# 顺序有意义：命中前面的类别就不再往后看（"采购项目技术、规格和数量要求" 应归 technical，
# 而不是被 "采购需求" 之外的其他规则抢走）。
KIND_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("bidder_instructions", ("投标人须知", "供应商须知", "投标须知", "磋商须知", "谈判须知", "响应人须知")),
    ("evaluation", ("评标办法", "评标方法", "评审办法", "评分标准", "评分办法", "评标标准", "评审标准")),
    ("technical", (
        "技术规格", "技术服务需求", "技术需求", "技术要求", "技术参数",
        "采购需求", "采购项目技术", "项目需求", "服务需求", "设备需求", "需求清单",
    )),
    ("qualification", ("资格审查", "资格预审", "符合性审查")),
    ("contract", ("合同主要条款", "合同草案", "合同条款", "合同格式", "合同书")),
    ("format", ("投标文件的格式", "投标文件格式", "响应文件格式", "投标文件格式要求", "附件")),
]

KIND_LABELS = {
    "bidder_instructions": "投标人须知",
    "evaluation": "评标办法",
    "technical": "技术规格/技术服务需求",
    "qualification": "资格审查",
    "contract": "合同条款",
    "format": "投标文件格式",
}

DISQ_KEYWORDS = ("废标", "无效投标", "无效标", "否决投标", "否决其投标", "取消投标资格", "按无效处理", "作无效标处理")

RE_CHAPTER = re.compile(r"^第[一二三四五六七八九十百零〇两]+章")


@dataclass(slots=True)
class Section:
    """一个招标章节，边界精确到**页内字符偏移**（ARCH-REVIEW P1-07）。

    旧实现只有页码，同一页起两章时后一章会吞掉前一章的开头
    （end = max(start, nxt-1) 直接塌缩成同一页）。现在起点/终点都是
    `(page, char)`：起点是本页标题段的 start，终点是本章最后一段的 end。
    """

    id: str
    kind: str
    title: str
    page_start: int
    char_start: int
    page_end: int
    char_end: int
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def pages(self) -> list[int]:
        return list(range(self.page_start, self.page_end + 1))

    @property
    def start(self) -> tuple[int, int]:
        return (self.page_start, self.char_start)

    @property
    def end(self) -> tuple[int, int]:
        return (self.page_end, self.char_end)

    def contains(self, page: int, offset: int = 0) -> bool:
        """(page, 页内偏移) 是否落在本章节区间内（闭区间，按元组比较）。"""
        return self.start <= (page, offset) <= self.end


@dataclass(slots=True)
class DisqHit:
    page: int
    text: str
    section_id: str


@dataclass(slots=True)
class TenderDoc:
    path: Path
    parsed: ParsedDocument
    sections: list[Section] = field(default_factory=list)
    disq_hits: list[DisqHit] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return self.parsed.page_count

    @property
    def title(self) -> str:
        return self.parsed.title

    def page_text(self, page: int) -> str:
        """页文本一律走 documind 的 slice_text（P1-06），不直接索引 parsed.pages。"""
        return page_text_of(self.parsed, page)

    def by_kind(self, kind: str) -> list[Section]:
        return [s for s in self.sections if s.kind == kind]

    def section_at(self, page: int, offset: int = 0) -> Section | None:
        """定位 (页, 页内偏移) 所属章节；页内偏移缺省时按页首算。"""
        return next((s for s in self.sections if s.contains(page, offset)), None)

    def section_of_page(self, page: int) -> Section | None:
        return self.section_at(page, 0)

    def outline(self) -> list[dict]:
        return self.parsed.outline()

    def stats(self) -> dict:
        return {
            "title": self.title,
            "pages": self.page_count,
            "paragraphs": len(self.parsed.paragraphs),
            "sections": [{"id": s.id, "kind": s.kind, "label": s.label, "title": s.title,
                          "pages": [s.page_start, s.page_end]} for s in self.sections],
            "disq_hits": len(self.disq_hits),
        }


def classify_title(title: str) -> str | None:
    t = re.sub(r"\s+", "", title)
    for kind, words in KIND_RULES:
        if any(w in t for w in words):
            return kind
    return None


def _section_id(kind: str, page: int, title: str) -> str:
    h = hashlib.sha1(f"{kind}|{page}|{title}".encode()).hexdigest()[:8]
    return f"{kind}-p{page}-{h}"


def _dedupe_headings(doc: ParsedDocument) -> list[Paragraph]:
    """去掉目录页重复的章标题（同一标题只保留第一次出现的那个）。"""
    seen: set[str] = set()
    out: list[Paragraph] = []
    for p in doc.paragraphs:
        if p.level != 1:
            continue
        title = re.sub(r"\s+", "", p.text)
        if len(title) < 4 or not RE_CHAPTER.match(title):
            continue
        if title in seen:
            continue
        seen.add(title)
        out.append(p)
    return out


def _paragraphs_in(
    doc: ParsedDocument, page_start: int, char_start: int, page_end: int, char_end: int
) -> list[Paragraph]:
    """按 (page, 页内起始偏移) 元组比较取本章段落（闭区间）。

    只比页会退化成"同页两章互相包含"，所以下界、上界都必须带字符偏移（P1-07）。
    """
    lo, hi = (page_start, char_start), (page_end, char_end)
    return [p for p in doc.paragraphs if lo <= (p.page, p.start) <= hi]


def _section_span(doc: ParsedDocument, para: Paragraph, bound: tuple[int, int] | None) -> tuple[int, int, list[Paragraph]]:
    """从标题段开始，取到下一个标题段（开区间）为止，返回 (page_end, char_end, 段落)。"""
    start = (para.page, para.start)
    body = [
        p
        for p in doc.paragraphs
        if start <= (p.page, p.start) and (bound is None or (p.page, p.start) < bound)
    ]
    if not body:
        return para.page, para.end, []
    return body[-1].page, body[-1].end, body


def _fallback_sections(doc: ParsedDocument) -> list[Section]:
    """没有「第 X 章」标题的招标文件：按关键词命中的页聚合出伪章节。"""
    sections: list[Section] = []
    for kind, words in KIND_RULES:
        pages = [i + 1 for i in range(doc.page_count) if any(w in page_text_of(doc, i + 1) for w in words)]
        if not pages:
            continue
        start, end = min(pages), max(pages)
        title = f"{KIND_LABELS[kind]}（关键词定位，无章节标题）"
        # 关键词定位只能到页：起点页首、终点页尾
        sections.append(
            Section(
                id=_section_id(kind, start, title),
                kind=kind,
                title=title,
                page_start=start,
                char_start=0,
                page_end=end,
                char_end=len(page_text_of(doc, end)),
                paragraphs=_paragraphs_in(doc, start, 0, end, len(page_text_of(doc, end))),
            )
        )
    return sorted(sections, key=lambda s: (s.page_start, s.char_start))


def build_sections(doc: ParsedDocument) -> list[Section]:
    headings = [p for p in _dedupe_headings(doc)]
    classified: list[tuple[Paragraph, str]] = []
    for p in headings:
        kind = classify_title(p.text)
        if kind:
            classified.append((p, kind))
    if not classified:
        return _fallback_sections(doc)

    sections: list[Section] = []
    for i, (para, kind) in enumerate(classified):
        # 本章上界 = 下一章标题的 (页, 偏移)，开区间；同一页起两章也不会互相吞
        bound = (classified[i + 1][0].page, classified[i + 1][0].start) if i + 1 < len(classified) else None
        page_end, char_end, body = _section_span(doc, para, bound)
        title = re.sub(r"\s+", " ", para.text).strip()
        sections.append(
            Section(
                id=_section_id(kind, para.page, title),
                kind=kind,
                title=title,
                page_start=para.page,
                char_start=para.start,
                page_end=page_end,
                char_end=char_end,
                paragraphs=body,
            )
        )
    return sections


def scan_disqualifications(doc: ParsedDocument, sections: list[Section]) -> list[DisqHit]:
    """废标条款没有独立章节，按段落关键词扫（页码 = 段落所在页）。"""
    hits: list[DisqHit] = []
    for p in doc.paragraphs:
        if len(p.text) < 8:
            continue
        if any(k in p.text for k in DISQ_KEYWORDS):
            sec = next((s for s in sections if s.contains(p.page, p.start)), None)
            hits.append(DisqHit(page=p.page, text=p.text, section_id=sec.id if sec else ""))
    return hits


def load_tender(path: str | Path) -> TenderDoc:
    p = Path(path)
    try:
        parsed = parse_file(p)
    except EmptyDocumentError as exc:  # documind 的护栏：无文本层即抛
        raise TenderParseError(p, str(exc)) from exc
    if not parsed.paragraphs:
        # 防自定义 Parser / 上游改动绕过护栏：这里再守一次
        raise TenderParseError(p, "解析结果没有任何段落")
    sections = build_sections(parsed)
    if not sections:
        raise TenderParseError(p, "未识别出任何章节")
    return TenderDoc(path=p, parsed=parsed, sections=sections, disq_hits=scan_disqualifications(parsed, sections))