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

from documind.parsing import Paragraph, ParsedDocument, parse_file

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
    id: str
    kind: str
    title: str
    page_start: int
    page_end: int
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def pages(self) -> list[int]:
        return list(range(self.page_start, self.page_end + 1))


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
        if 1 <= page <= len(self.parsed.pages):
            return self.parsed.pages[page - 1]
        return ""

    def by_kind(self, kind: str) -> list[Section]:
        return [s for s in self.sections if s.kind == kind]

    def section_of_page(self, page: int) -> Section | None:
        for s in self.sections:
            if s.page_start <= page <= s.page_end:
                return s
        return None

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


def _paragraphs_in(doc: ParsedDocument, start: int, end: int) -> list[Paragraph]:
    return [p for p in doc.paragraphs if start <= p.page <= end]


def _fallback_sections(doc: ParsedDocument) -> list[Section]:
    """没有「第 X 章」标题的招标文件：按关键词命中的页聚合出伪章节。"""
    sections: list[Section] = []
    for kind, words in KIND_RULES:
        pages = [i + 1 for i, page in enumerate(doc.pages) if any(w in page for w in words)]
        if not pages:
            continue
        start, end = min(pages), max(pages)
        title = f"{KIND_LABELS[kind]}（关键词定位，无章节标题）"
        sections.append(
            Section(
                id=_section_id(kind, start, title),
                kind=kind,
                title=title,
                page_start=start,
                page_end=end,
                paragraphs=_paragraphs_in(doc, start, end),
            )
        )
    return sorted(sections, key=lambda s: s.page_start)


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
        start = para.page
        if i + 1 < len(classified):
            nxt = classified[i + 1][0].page
            end = max(start, nxt - 1) if nxt > start else start
        else:
            end = doc.page_count
        title = re.sub(r"\s+", " ", para.text).strip()
        sections.append(
            Section(
                id=_section_id(kind, start, title),
                kind=kind,
                title=title,
                page_start=start,
                page_end=end,
                paragraphs=_paragraphs_in(doc, start, end),
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
            sec = next((s for s in sections if s.page_start <= p.page <= s.page_end), None)
            hits.append(DisqHit(page=p.page, text=p.text, section_id=sec.id if sec else ""))
    return hits


def load_tender(path: str | Path) -> TenderDoc:
    p = Path(path)
    parsed = parse_file(p)
    sections = build_sections(parsed)
    return TenderDoc(path=p, parsed=parsed, sections=sections, disq_hits=scan_disqualifications(parsed, sections))