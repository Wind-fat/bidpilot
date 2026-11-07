"""结构化抽取：分页切块 → LLM 抽 → 强校验 → 带错误重试 → 兜底 needs_review。

为什么必须"分段 + 校验 + 重试"：
96 页一次塞进上下文，模型会漏条目、会把页码编错、会把 quote 改写成"摘要"。
这里的做法是把招标文件按页窗口切成 ~3k 字符的块，每块只问它包含的那几类条目，
回来后逐条做两项硬校验：page 必须落在本块的页范围内，quote 必须能在该页原文里逐字找到。
校验不过的条目不是删掉，而是标 needs_review 并记录原因——评审员在审核台里能看到它。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field

from .llm import LLM, LLMError, parse_json
from .schemas import DisqualificationClause, ExtractionBatch, ScoringItem, TechRequirement
from .sections import TenderDoc

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
        "- section: 该条所属小节名（尽力而为，没有就留空字符串）\n"
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


def build_chunks(doc: TenderDoc, max_chars: int = 3200) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in doc.sections:
        if section.kind not in EXTRACT_KINDS:
            continue
        pending: list[tuple[int, str]] = []
        size = 0

        def flush() -> None:
            nonlocal pending, size
            if not pending:
                return
            body = "\n\n".join(f"[第 {p} 页]\n{t}" for p, t in pending)
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
                    )
                )
            pending, size = [], 0

        for page in range(section.page_start, section.page_end + 1):
            text = doc.page_text(page).strip()
            if not text:
                continue
            if size + len(text) > max_chars and pending:
                flush()
            pending.append((page, text))
            size += len(text)
        flush()
    return chunks


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


def locate_quote(page_text: str, quote: str) -> str | None:
    """把模型给的 quote 对回原文，返回**原文里真实存在的子串**；找不到返回 None。

    先做严格子串匹配；失败再做"去空白后匹配"（PDF 抽文常把中文之间的空格吃掉/加上），
    命中后把原文中的真实片段切回来。返回值保证是 page_text 的逐字子串。
    """
    q = (quote or "").strip()
    if not q or not page_text:
        return None
    if q in page_text:
        return q
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
                return page_text[start:end]
        return None
    start = index[pos]
    end = index[pos + len(norm_q) - 1] + 1
    return page_text[start:end]


def _validate(doc: TenderDoc, chunk: Chunk, batch: ExtractionBatch) -> tuple[ExtractionBatch, list[str]]:
    """就地修正/标记，返回 (batch, 错误列表)。错误列表非空 → 触发重试。"""
    errors: list[str] = []

    for i, item in enumerate(batch.scoring_items):
        tag = f"scoring_items[{i}]"
        if not (chunk.page_start <= item.page <= chunk.page_end):
            item.mark_review(f"page {item.page} 不在本次抽取页范围 {chunk.page_start}-{chunk.page_end}")
        else:
            found = locate_quote(doc.page_text(item.page), item.quote)
            if found is None:
                item.mark_review(f"quote 未能在第 {item.page} 页原文中找到")
            else:
                item.quote = found
        if item.needs_review:
            errors.extend(f"{tag} {r}" for r in item.review_reasons)

    for i, item in enumerate(batch.disqualifications):
        tag = f"disqualifications[{i}]"
        if not (chunk.page_start <= item.page <= chunk.page_end):
            item.mark_review(f"page {item.page} 不在本次抽取页范围 {chunk.page_start}-{chunk.page_end}")
        else:
            found = locate_quote(doc.page_text(item.page), item.quote)
            if found is None:
                item.mark_review(f"quote 未能在第 {item.page} 页原文中找到")
            else:
                item.quote = found
        if item.needs_review:
            errors.extend(f"{tag} {r}" for r in item.review_reasons)

    for i, item in enumerate(batch.tech_requirements):
        tag = f"tech_requirements[{i}]"
        pages = {item.page, item.page_end}
        if item.page < chunk.page_start or item.page > chunk.page_end:
            item.mark_review(f"page {item.page} 不在本次抽取页范围 {chunk.page_start}-{chunk.page_end}")
        else:
            found = None
            for page in (item.page, item.page_end):
                if chunk.page_start <= page <= chunk.page_end:
                    found = locate_quote(doc.page_text(page), item.quote)
                    if found:
                        item.page, item.page_end = page, page
                        item.quote = found
                        break
            if found is None:
                item.mark_review(f"quote 未能在第 {sorted(pages)} 页原文中找到")
            else:
                item.quote = found
        if not item.section:
            sec = doc.section_of_page(item.page)
            item.section = sec.title if sec else chunk.section_title
        if item.needs_review:
            errors.extend(f"{tag} {r}" for r in item.review_reasons)

    return batch, errors


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
        try:
            reply = await llm.complete(messages, json_mode=True, max_tokens=6000)
        except LLMError as exc:
            report.errors = [str(exc)]
            continue
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


async def extract(
    doc: TenderDoc, llm: LLM, *, max_chars: int = 3200, max_retries: int = 2, concurrency: int = 3
) -> ExtractResult:
    res = ExtractResult(title=doc.title, source=str(doc.path))
    chunks = build_chunks(doc, max_chars=max_chars)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(chunk: Chunk):
        async with sem:
            return await extract_chunk(llm, doc, chunk, max_retries=max_retries)

    pairs = await asyncio.gather(*(one(c) for c in chunks)) if chunks else []
    for batch, report in pairs:
        res.scoring_items.extend(batch.scoring_items)
        res.disqualifications.extend(batch.disqualifications)
        res.tech_requirements.extend(batch.tech_requirements)
        res.reports.append(report)
    return merge(res)