"""结构化抽取：分页切块 → LLM 抽 → 强校验 → 带错误重试 → 兜底 needs_review。

为什么必须"分段 + 校验 + 重试"：
96 页一次塞进上下文，模型会漏条目、会把页码编错、会把 quote 改写成"摘要"。
这里的做法是把招标文件按页窗口切成 ~2k 字符的块（默认 2000，实测见 docs/CHUNKING.md），每块只问它包含的那几类条目，
回来后逐条做两项硬校验：page 必须落在本块的页范围内，quote 必须能在该页原文里逐字找到。
校验不过的条目不是删掉，而是标 needs_review 并记录原因——评审员在审核台里能看到它。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .llm import LLM, LLMError, parse_json
from .schemas import DisqualificationClause, ExtractionBatch, ScoringItem, TechRequirement
from .sections import Section, TenderDoc

log = logging.getLogger("bidpilot.extract")

TASK_SCORING = "scoring_items"
TASK_DISQ = "disqualifications"
TASK_TECH = "tech_requirements"

DISQ_TRIGGERS = ("废标", "无效投标", "无效标", "否决", "取消投标资格", "实质性响应", "实质性条款")
EXTRACT_KINDS = ("evaluation", "technical", "bidder_instructions", "qualification")

SYSTEM_PROMPT = (
    "你是招标文件结构化抽取器。你只输出 JSON，不输出任何解释。"
    "你抽取的每一条都必须能在给出的原文里逐字找到，页码必须是该原文所在的页。"
    "宁可少抽一条，也不要编造。"
)

TASK_SPECS: dict[str, str] = {
    TASK_SCORING: (
        "抽取**评分项**（评分表/评审标准里每一个分项）。输出 scoring_items 数组，每项字段：\n"
        '- no: 评分表编号，如 "2.1"；没有编号就留空字符串\n'
        '- item: 评分因素名称，如 "价格"、"技术设备技术参数"、"售后服务方案"\n'
        '- criteria_text: 该评分项的评审标准（可概括，但不得与原文矛盾）\n'
        '- max_score: 该评分项的分值（数字，如 20；没有分值写 null）\n'
        "- page: quote 所在页码（整数）\n"
        "- quote: 从该页原文逐字复制的片段（≤300 字，不加省略号、不改标点）\n"
    ),
    TASK_DISQ: (
        "抽取**废标 / 无效投标 / 否决投标**条款（会导致投标被否决的硬性条款）。输出 disqualifications 数组，每项字段：\n"
        "- clause_text: 条款含义的一句话概括\n"
        "- page: quote 所在页码（整数）\n"
        "- quote: 从该页原文逐字复制的片段（≤300 字）\n"
        '- category: 必填，只能是 "资格"、"形式"、"技术"、"商务"、"报价" 之一\n'
    ),
    TASK_TECH: (
        "抽取**技术要求**（设备/软件/服务的功能、性能、配置、参数、交付要求）。输出 tech_requirements 数组，每项字段：\n"
        '- no: 条目编号或序号，如 "1"、"★3"、"2.1"；没有就留空字符串\n'
        "- text: 该条要求的内容（可概括，不得与原文矛盾）\n"
        "- page: quote 起始页码（整数）\n"
        "- page_end: quote 结束页码（整数，同一页就与 page 相同）\n"
        "- quote: 从原文逐字复制的片段（≤300 字）；跨页时可只复制起始页的片段\n"
    ),
}

USER_TEMPLATE = """下面是一份招标文件的第 {start}-{end} 页原文。页与页之间用 [第 N 页] 标记，页码就是方括号里的数字。

{body}

抽取任务：
{tasks}

硬规则（违反任意一条，该条会被判废并退回重做）：
1. page 必须落在 {start}-{end} 之间，且是 quote 真正所在的那一页；
2. quote 必须是从该页原文里**逐字连续复制**的片段，不能改写、不能拼接、不能加"……"；
3. 只抽原文里真实存在的内容，不要根据常识补全；
4. 只输出一个 JSON 对象，顶层键为：{keys}；没有内容的数组输出 []。
"""

RETRY_TEMPLATE = """上一次的输出有 {n} 个问题，请逐条修正后重新输出**完整** JSON（不要只输出修正部分）：
{errors}

再次提醒：quote 必须逐字出现在对应页的原文中，否则该条会被标记为待人工复核。"""


@dataclass(slots=True)
class Chunk:
    page_start: int
    page_end: int
    section_id: str
    section_title: str
    section_kind: str
    tasks: list[str]
    body: str
    # 章节在首/尾页的页内字符边界：本块只允许这两处被截断，中间页取全页（P1-07）
    char_start: int = 0
    char_end: int = -1

    @property
    def id(self) -> str:
        return f"{self.section_id}#p{self.page_start}-{self.page_end}"


@dataclass(slots=True)
class ChunkReport:
    chunk_id: str
    pages: tuple[int, int]
    tasks: list[str]
    attempts: int = 0
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractResult:
    title: str
    source: str
    # 源文件内容指纹（sha256）。只在文件名比对不足时才有用：
    # 两份招标文件常常同名不同内容（不同项目都叫「招标文件.pdf」），
    # 只比文件名会让「拿 A 的引用去 B 里定位」在 CI 里假绿。
    source_sha256: str = ""
    scoring_items: list[ScoringItem] = field(default_factory=list)
    disqualifications: list[DisqualificationClause] = field(default_factory=list)
    tech_requirements: list[TechRequirement] = field(default_factory=list)
    reports: list[ChunkReport] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def items(self) -> list:
        return [*self.scoring_items, *self.disqualifications, *self.tech_requirements]

    def stats(self) -> dict:
        def cnt(seq):
            return {"total": len(seq), "needs_review": sum(1 for x in seq if x.needs_review)}

        failed = [r.chunk_id for r in self.reports if not r.ok]
        return {
            "title": self.title,
            "source": self.source,
            "scoring_items": cnt(self.scoring_items),
            "disqualifications": cnt(self.disqualifications),
            "tech_requirements": cnt(self.tech_requirements),
            "chunks": len(self.reports),
            "chunks_failed": failed,
            "usage": self.usage,
        }


# ---------------------------------------------------------------- chunking


def _tasks_for(section_kind: str, text: str) -> list[str]:
    tasks: list[str] = []
    if section_kind == "evaluation":
        tasks.append(TASK_SCORING)
    elif section_kind == "technical":
        tasks.append(TASK_TECH)
    if section_kind in ("evaluation", "technical", "bidder_instructions", "qualification"):
        if any(t in text for t in DISQ_TRIGGERS) or section_kind in ("bidder_instructions", "qualification"):
            tasks.append(TASK_DISQ)
    return tasks


def _section_chunks(doc: TenderDoc, section: Section, max_chars: int) -> list[Chunk]:
    """把一个章节按 max_chars 装箱成块。首/尾页按章节字符边界截断，中间页整页取。"""
    chunks: list[Chunk] = []
    pending: list[tuple[int, int, int, str]] = []  # (页, 页内起, 页内止, 文本)
    size = 0

    def flush() -> None:
        nonlocal pending, size
        if not pending:
            return
        body = "\n\n".join(f"[第 {p} 页]\n{t}" for p, _, _, t in pending)
        tasks = _tasks_for(section.kind, body)
        if tasks:
            chunks.append(
                Chunk(
                    page_start=pending[0][0],
                    page_end=pending[-1][0],
                    section_id=section.id,
                    section_title=section.title,
                    section_kind=section.kind,
                    tasks=tasks,
                    body=body,
                    char_start=pending[0][1],
                    char_end=pending[-1][2],
                )
            )
        pending, size = [], 0

    for page in range(section.page_start, section.page_end + 1):
        raw = doc.page_text(page)
        lo = section.char_start if page == section.page_start else 0
        hi = section.char_end if page == section.page_end else len(raw)
        text = raw[lo:hi].strip()
        if not text:
            continue
        if size + len(text) > max_chars and pending:
            flush()
        pending.append((page, lo, hi, text))
        size += len(text)
    flush()
    return chunks


def build_chunks(doc: TenderDoc, max_chars: int = 2000) -> list[Chunk]:
    """默认值与 Settings.chunk_max_chars 必须一致（tests/test_chunk_contract.py 钉住）。"""
    out: list[Chunk] = []
    for section in doc.sections:
        if section.kind in EXTRACT_KINDS:
            out.extend(_section_chunks(doc, section, max_chars))
    return out


# ---------------------------------------------------------------- quote 校验


def _normalized(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(ch)
        index.append(i)
    return "".join(chars), index


def locate_quote_span(page_text: str, quote: str) -> tuple[int, int] | None:
    """把模型给的 quote 对回原文，返回原文里的 **[start, end) 字符区间**；找不到返回 None。

    返回偏移量（而不只是字符串）是为了让 _validate 能拿它跟章节的字符边界比对（P1-07）：
    同一页上两章相邻时，光是"这页里有这句话"不足以证明这句话属于本章。
    """
    q = (quote or "").strip()
    if not q or not page_text:
        return None
    pos = page_text.find(q)
    if pos >= 0:
        return pos, pos + len(q)
    norm_page, index = _normalized(page_text)
    norm_q, _ = _normalized(q)
    if not norm_q:
        return None
    pos = norm_page.find(norm_q)
    if pos < 0:
        # 允许模型把长句截短到某个分句：只在首尾各裁一次，避免无限放宽
        for cut in (40, 80, 120):
            head = norm_q[:cut]
            if len(head) >= 20 and head in norm_page:
                p = norm_page.find(head)
                start = index[p]
                end = index[p + len(head) - 1] + 1
                return start, end
        return None
    start = index[pos]
    end = index[pos + len(norm_q) - 1] + 1
    return start, end


def locate_quote(page_text: str, quote: str) -> str | None:
    """把模型给的 quote 对回原文，返回**原文里真实存在的子串**；找不到返回 None。

    先做严格子串匹配；失败再做"去空白后匹配"（PDF 抽文常把中文之间的空格吃掉/加上），
    命中后把原文中的真实片段切回来。返回值保证是 page_text 的逐字子串。
    """
    span = locate_quote_span(page_text, quote)
    return page_text[span[0] : span[1]] if span else None


def _check_quote(
    doc: TenderDoc, chunk: Chunk, page: int, quote: str
) -> tuple[str, str, tuple[int, int, int] | None]:
    """校验一条 item 的 (page, quote)：返回 (错误原因, 原文中的真实片段, 证据区间)。

    错误原因为空字符串表示通过。四道闸：页在文档/块范围内 → quote 能在该页找到 →
    定位区间不越出页长 → 定位到的区间不越过本章节的字符边界。
    第三、四个返回值是给 `quote_span` 用的：区间带页号，跨页条目也不会把「哪一页」弄丢。
    """
    if not (1 <= page <= doc.page_count):
        return f"page {page} 超出文档页范围 1-{doc.page_count}", quote, None
    if not (chunk.page_start <= page <= chunk.page_end):
        return f"page {page} 不在本次抽取页范围 {chunk.page_start}-{chunk.page_end}", quote, None
    text = doc.page_text(page)
    span = locate_quote_span(text, quote)
    if span is None:
        return f"quote 未能在第 {page} 页原文中找到", quote, None
    start, end = span
    if not (0 <= start < end <= len(text)):
        return f"定位区间 [{start},{end}) 越出第 {page} 页（页长 {len(text)}）", quote, None
    if page == chunk.page_start and start < chunk.char_start:
        return (
            f"quote 起始位置 {start} 早于本章节在 p{page} 的起点 {chunk.char_start}（属于上一章节）",
            quote,
            None,
        )
    if page == chunk.page_end and 0 <= chunk.char_end < end:
        return (
            f"quote 结束位置 {end} 超出本章节在 p{page} 的终点 {chunk.char_end}（侵入下一章节）",
            quote,
            None,
        )
    return "", text[start:end], (page, start, end)


def _stamp(item, span: tuple[int, int, int], fragment: str) -> None:
    """把定位结果写回条目：quote 换成原文真实片段，quote_span 记证据区间。

    quote_span 一律由定位算出，**模型自报的同名/偏移字段直接覆盖**——它没法核验，
    而章节标签、可回查率都要拿它当依据。
    """
    item.quote = fragment
    item.quote_span = span


def _label_chapter(doc: TenderDoc, item, page: int, offset: int, fallback: str = "") -> str:
    """按**页内偏移**反查章级标签；页首那一段可能是上一章（同页跟章）。

    旧实现 (page, 0) 会让页尾条目全落到上一章的标题上：41 页真实语料里 21/170 条受影响
    （见 docs/LOCATE.md）。定位不到章节（封面/目录页）时退回本块所属章节标题，不瞎猜。
    """
    sec = doc.section_at(page, offset)
    item.section = sec.title if sec else fallback
    return item.section


def _validate(doc: TenderDoc, chunk: Chunk, batch: ExtractionBatch) -> tuple[ExtractionBatch, list[str]]:
    """就地修正/标记，返回 (batch, 错误列表)。错误列表非空 → 触发重试。

    每条通过校验的条目都拿到两样可回查的东西：
    - `quote` 换成原文里的逐字片段（引用锚点）；
    - `quote_span`（页, 页内起, 页内止）与 `section`（按**区间起点**反查的章级标签）。
    """
    errors: list[str] = []

    for i, item in enumerate(batch.scoring_items):
        _validate_one(doc, chunk, item, f"scoring_items[{i}]", errors)

    for i, item in enumerate(batch.disqualifications):
        _validate_one(doc, chunk, item, f"disqualifications[{i}]", errors)

    for i, item in enumerate(batch.tech_requirements):
        _validate_tech(doc, chunk, item, f"tech_requirements[{i}]", errors)

    return batch, errors


def _validate_one(doc: TenderDoc, chunk: Chunk, item, tag: str, errors: list[str]) -> None:
    """单页条目（评分项 / 废标条款）：定位 → 写回 → 打章节标签。"""
    err, quote, span = _check_quote(doc, chunk, item.page, item.quote)
    if err:
        item.mark_review(err)
    else:
        _stamp(item, span, quote)
        _label_chapter(doc, item, span[0], span[1], chunk.section_title)
    if item.needs_review:
        errors.extend(f"{tag} {r}" for r in item.review_reasons)


def _validate_tech(doc: TenderDoc, chunk: Chunk, item, tag: str, errors: list[str]) -> None:
    """跨页条目：先按起始页定位，失败再试结束页；quote_span 记的是**证据页**。"""
    end_page = item.page_end or item.page
    pages = sorted({item.page, end_page})
    err, quote, span = _check_quote(doc, chunk, item.page, item.quote)
    if err and end_page != item.page:
        # 跨页条目的 quote 也可能落在结束页上（起始页只写了前半句）；page/page_end 保持原样，
        # 证据落在哪一页由 quote_span 的第一位说清楚，不再把 page_end 塌成 page。
        err2, quote2, span2 = _check_quote(doc, chunk, end_page, item.quote)
        if not err2:
            err, quote, span = "", quote2, span2
    if err:
        item.mark_review(err if "页范围" in err else f"quote 未能在第 {pages} 页原文中找到：{err}")
    else:
        _stamp(item, span, quote)
        _label_chapter(doc, item, span[0], span[1], chunk.section_title)
    if item.needs_review:
        errors.extend(f"{tag} {r}" for r in item.review_reasons)


# ---------------------------------------------------------------- 抽取


def _initial_messages(chunk: Chunk) -> list[dict]:
    tasks = "\n".join(TASK_SPECS[t] for t in chunk.tasks)
    user = USER_TEMPLATE.format(
        start=chunk.page_start,
        end=chunk.page_end,
        body=chunk.body,
        tasks=tasks,
        keys=", ".join(chunk.tasks),
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


async def extract_chunk(llm: LLM, doc: TenderDoc, chunk: Chunk, max_retries: int = 2) -> tuple[ExtractionBatch, ChunkReport]:
    """单块抽取。最多重试 max_retries 次（默认 2），最后一次仍不合规的条目保留并标 needs_review。"""
    messages = _initial_messages(chunk)
    report = ChunkReport(chunk_id=chunk.id, pages=(chunk.page_start, chunk.page_end), tasks=chunk.tasks)
    best: ExtractionBatch | None = None

    for attempt in range(max_retries + 1):
        report.attempts = attempt + 1
        t0 = time.perf_counter()
        try:
            reply = await llm.complete(messages, json_mode=True, max_tokens=6000)
        except LLMError as exc:
            report.errors = [str(exc)]
            log.warning("chunk %s attempt %d LLM 失败 %.1fs: %s", chunk.id, attempt + 1, time.perf_counter() - t0, exc)
            continue
        log.info(
            "chunk %s attempt %d 返回 %d 字 / %.1fs",
            chunk.id, attempt + 1, len(reply.text), time.perf_counter() - t0,
        )
        try:
            batch = ExtractionBatch(**parse_json(reply.text))
        except Exception as exc:
            report.errors = [f"输出不符合 schema: {exc}"]
            messages = [
                *messages,
                {"role": "assistant", "content": reply.text[:4000]},
                {"role": "user", "content": RETRY_TEMPLATE.format(n=1, errors=f"- {exc}")},
            ]
            continue

        batch, errors = _validate(doc, chunk, batch)
        if not errors and not (batch.scoring_items or batch.disqualifications or batch.tech_requirements):
            # 空结果不算成功：本块被分配了抽取任务，却一条都没给出，多半是模型偷懒/输出截断。
            errors = [f"空结果：未抽到任何条目（任务 {', '.join(chunk.tasks)}）"]
        best = batch
        if not errors:
            report.ok = True
            report.errors = []
            report.counts = {
                TASK_SCORING: len(batch.scoring_items),
                TASK_DISQ: len(batch.disqualifications),
                TASK_TECH: len(batch.tech_requirements),
            }
            return batch, report

        report.errors = errors
        messages = [
            *messages,
            {"role": "assistant", "content": reply.text[:4000]},
            {"role": "user", "content": RETRY_TEMPLATE.format(n=len(errors), errors="\n".join(f"- {e}" for e in errors[:20]))},
        ]

    if best is None:
        report.ok = False
        report.counts = {TASK_SCORING: 0, TASK_DISQ: 0, TASK_TECH: 0}
        return ExtractionBatch(), report

    # 重试耗尽：条目全部保留，已标 needs_review + 原因
    report.ok = False
    report.counts = {
        TASK_SCORING: len(best.scoring_items),
        TASK_DISQ: len(best.disqualifications),
        TASK_TECH: len(best.tech_requirements),
    }
    return best, report


def _key(kind: str, quote: str, extra: str) -> str:
    norm = re.sub(r"\s+", "", quote)[:80]
    return hashlib.sha1(f"{kind}|{norm}|{extra}".encode()).hexdigest()


def merge(res: ExtractResult) -> ExtractResult:
    """全局合并 + 去重（同 quote 视为同一条；已合规的版本优先于 needs_review 版本）。"""
    def dedupe(seq: list, kind: str, extra_of):
        chosen: dict[str, object] = {}
        for item in seq:
            k = _key(kind, item.quote, extra_of(item))
            old = chosen.get(k)
            if old is None or (getattr(old, "needs_review", False) and not item.needs_review):
                chosen[k] = item
        out = list(chosen.values())
        for i, it in enumerate(out, 1):
            it.id = f"{kind[0].upper()}{i:03d}"
        return out

    res.scoring_items = dedupe(res.scoring_items, "scoring", lambda x: x.item)
    res.disqualifications = dedupe(res.disqualifications, "disqualification", lambda x: x.category.value)
    res.tech_requirements = dedupe(res.tech_requirements, "tech", lambda x: x.no)
    return res


def file_sha256(path: str | Path) -> str:
    """源文件内容指纹（流式，不整份读进内存）。"""
    h = hashlib.sha256()
    try:
        with Path(path).open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def dump_extract(res: ExtractResult) -> dict:
    """整份抽取结果序列化（含逐块报告），用于分阶段跑：抽取一次、生成多次。"""
    return {
        "title": res.title,
        "source": res.source,
        "source_sha256": res.source_sha256,
        "scoring_items": [i.model_dump(mode="json") for i in res.scoring_items],
        "disqualifications": [i.model_dump(mode="json") for i in res.disqualifications],
        "tech_requirements": [i.model_dump(mode="json") for i in res.tech_requirements],
        "reports": [asdict(r) for r in res.reports],
        "usage": res.usage,
    }


def load_extract(payload: dict) -> ExtractResult:
    reports = []
    for r in payload.get("reports") or []:
        rep = ChunkReport(**{**r, "pages": (int(r["pages"][0]), int(r["pages"][1]))})
        reports.append(rep)
    return ExtractResult(
            title=payload.get("title", ""),
            source=payload.get("source", ""),
            source_sha256=payload.get("source_sha256", "") or "",
        scoring_items=[ScoringItem(**i) for i in payload.get("scoring_items") or []],
        disqualifications=[DisqualificationClause(**i) for i in payload.get("disqualifications") or []],
        tech_requirements=[TechRequirement(**i) for i in payload.get("tech_requirements") or []],
        reports=reports,
        usage=payload.get("usage") or {},
    )


def _locate_for_item(doc: TenderDoc, item) -> tuple[int, int, int] | None:
    """按 page（跨页条目再试 page_end）重新定位 quote，返回 (页, 起, 止)。"""
    pages = [item.page]
    end_page = getattr(item, "page_end", None)
    if end_page and end_page != item.page:
        pages.append(end_page)
    for page in pages:
        if not (1 <= page <= doc.page_count):
            continue
        text = doc.page_text(page)
        span = locate_quote_span(text, item.quote)
        if span is None:
            continue
        start, end = span
        if 0 <= start < end <= len(text):
            return (page, start, end)
    return None


def relocate(doc: TenderDoc, res: ExtractResult) -> dict:
    """给**已落盘**的抽取结果补回定位：quote_span + 章级 section。不调 LLM、不改条目集合。

    分阶段跑（`--extract-json`）时，JSON 可能是旧版条目（没有 quote_span），也可能来自
    另一份文档；这里按 page + quote 重新定位，**定位不到就标 needs_review 并进明细**，
    只加严不放宽：不猜偏移、不删条目、不把"找不到"当成"通过"。
    """
    stats = {"items": len(res.items()), "located": 0, "unlocated": []}
    for item in res.items():
        span = _locate_for_item(doc, item)
        if span is None:
            page = item.page
            why = (
                f"page {page} 超出文档页范围 1-{doc.page_count}"
                if not (1 <= page <= doc.page_count)
                else f"quote 未能在第 {page} 页原文中找到"
            )
            item.mark_review(why)
            stats["unlocated"].append({"id": item.id, "page": page, "reason": why})
            continue
        page, start, end = span
        _stamp(item, span, doc.page_text(page)[start:end])
        _label_chapter(doc, item, page, start, "")
        stats["located"] += 1
    return stats


def _cache_file(cache_dir: Path, chunk: Chunk, model: str) -> Path:
    key = hashlib.sha1(f"{model}|{chunk.id}|{','.join(chunk.tasks)}|{chunk.body}".encode()).hexdigest()[:16]
    return cache_dir / f"{key}.json"


async def extract(
    doc: TenderDoc,
    llm: LLM,
    *,
    max_chars: int = 2000,
    max_retries: int = 2,
    concurrency: int = 3,
    cache_dir: str | Path | None = None,
    model: str = "",
) -> ExtractResult:
    """分块抽取。cache_dir 非空时逐块落盘：超时/中断后重跑直接命中已完成的块。"""
    res = ExtractResult(title=doc.title, source=str(doc.path), source_sha256=file_sha256(doc.path))
    chunks = build_chunks(doc, max_chars=max_chars)
    cache = Path(cache_dir) if cache_dir else None
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, concurrency))
    log.info("抽取开始：%d 块 / 并发 %d / 缓存 %s", len(chunks), concurrency, cache or "关闭")

    async def one(chunk: Chunk):
        path = _cache_file(cache, chunk, model) if cache is not None else None
        if path is not None and path.exists():
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                report = ChunkReport(**blob["report"])
                report.pages = (int(report.pages[0]), int(report.pages[1]))
                log.info("chunk %s 命中缓存 ok=%s counts=%s", chunk.id, report.ok, report.counts)
                return ExtractionBatch(**blob["batch"]), report
            except Exception as exc:  # 缓存损坏就重算，不阻断
                log.warning("chunk %s 缓存不可用（%s），重算", chunk.id, exc)
        async with sem:
            t0 = time.perf_counter()
            batch, report = await extract_chunk(llm, doc, chunk, max_retries=max_retries)
            log.info(
                "chunk %s 完成 ok=%s attempts=%d %.1fs counts=%s",
                chunk.id, report.ok, report.attempts, time.perf_counter() - t0, report.counts,
            )
        if path is not None:
            path.write_text(
                json.dumps({"report": asdict(report), "batch": batch.model_dump(mode="json")}, ensure_ascii=False),
                encoding="utf-8",
            )
        return batch, report

    pairs = await asyncio.gather(*(one(c) for c in chunks)) if chunks else []
    for batch, report in pairs:
        res.scoring_items.extend(batch.scoring_items)
        res.disqualifications.extend(batch.disqualifications)
        res.tech_requirements.extend(batch.tech_requirements)
        res.reports.append(report)
    return merge(res)
