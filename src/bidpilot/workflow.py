"""生成 workflow：显式状态机，不允许"自由 Agent 一把梭"。

    planned ──generate──> generated ──verify──> citation_verified ──aggregate──> aggregated

- 每个阶段都是独立方法，可单独重跑（run(stage=...)）；
- 每个章节独立推进，某章节失败只让该章节停在 failed，其他章节照常继续；
- 段落必须挂来源（招标条目 id 或能力条目 id），来源为空或指向不存在的 id 的段落**不输出**，
  进 rejected 列表（审核台可见），这是硬闸门不是提示词。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .capabilities import MISSING, NEEDS_REVIEW, SATISFIED, CapabilityIndex, RequirementMatch, build_matrix
from .extract import ExtractResult
from .llm import LLM, LLMError, parse_json
from .schemas import GeneratedParagraph


class Stage(str, Enum):
    PLANNED = "planned"
    GENERATED = "generated"
    CITATION_VERIFIED = "citation_verified"
    AGGREGATED = "aggregated"
    FAILED = "failed"


class WorkflowStore(Protocol):
    async def set_state(self, doc_id: str, section_id: str, state: str, detail: str = "") -> None: ...

    async def get_state(self, doc_id: str, section_id: str) -> str | None: ...

    async def save_section(
        self, doc_id: str, section_id: str, paragraphs: list[GeneratedParagraph], rejected: list[dict]
    ) -> None: ...

    async def get_section(self, doc_id: str, section_id: str) -> dict | None: ...


class MemoryStore:
    """测试与单次运行用；SQLite 版见 db.SqliteStore。"""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], str] = {}
        self.details: dict[tuple[str, str], str] = {}
        self.sections: dict[tuple[str, str], dict] = {}

    async def set_state(self, doc_id: str, section_id: str, state: str, detail: str = "") -> None:
        self.states[(doc_id, section_id)] = state
        self.details[(doc_id, section_id)] = detail

    async def get_state(self, doc_id: str, section_id: str) -> str | None:
        return self.states.get((doc_id, section_id))

    async def save_section(self, doc_id, section_id, paragraphs, rejected) -> None:
        self.sections[(doc_id, section_id)] = {"paragraphs": paragraphs, "rejected": rejected}

    async def get_section(self, doc_id: str, section_id: str) -> dict | None:
        return self.sections.get((doc_id, section_id))


@dataclass(slots=True)
class PlannedSection:
    section_id: str
    title: str
    item_ids: list[str]
    items: list[dict]  # {id, text, page, kind}
    matches: list[RequirementMatch] = field(default_factory=list)


@dataclass(slots=True)
class SectionRun:
    section_id: str
    title: str
    state: str = Stage.PLANNED.value
    paragraphs: list[GeneratedParagraph] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    attempts: int = 0
    mocked: bool = False
    error: str = ""


@dataclass(slots=True)
class WorkflowResult:
    doc_id: str
    sections: list[SectionRun] = field(default_factory=list)
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    mocked: bool = False

    def stats(self) -> dict:
        return {
            "sections": len(self.sections),
            "paragraphs": sum(len(s.paragraphs) for s in self.sections),
            "rejected_paragraphs": sum(len(s.rejected) for s in self.sections),
            "mocked": self.mocked,
            "mocked_sections": [p["section_id"] for p in self.problems() if p["severity"] == "mocked"],
            "failed_sections": [s.section_id for s in self.sections if s.state == Stage.FAILED.value],
            "incomplete_sections": [p["section_id"] for p in self.problems() if p["severity"] == "incomplete"],
            "states": {s.section_id: s.state for s in self.sections},
            "prompt_tokens": self.usage_prompt_tokens,
            "completion_tokens": self.usage_completion_tokens,
        }

    def problems(self) -> list[dict]:
        """交付物污染清单：模板应答 / 整节点失败 / 部分批次失败，CLI 必须原样打印。"""
        out: list[dict] = []
        for s in self.sections:
            if s.mocked:
                out.append(
                    {"section_id": s.section_id, "title": s.title, "severity": "mocked",
                     "reason": "本节为模板应答（未调用模型）"}
                )
            elif s.state == Stage.FAILED.value:
                out.append(
                    {"section_id": s.section_id, "title": s.title, "severity": "failed",
                     "reason": s.error or "本节没有产出任何应答"}
                )
            elif s.error:
                out.append(
                    {"section_id": s.section_id, "title": s.title, "severity": "incomplete",
                     "reason": s.error}
                )
        return out

    def degraded_sections(self) -> list[dict]:
        """被降级为模板应答的章节（横幅与 `_MOCK` 后缀的判定依据）。"""
        return [p for p in self.problems() if p["severity"] == "mocked"]


GEN_SYSTEM = (
    "你是投标文件技术应答撰写助手。你只输出 JSON。"
    "每一段应答都必须挂上来源 id：招标条目 id（T/S/D 开头）或能力条目 id（cap- 开头）。"
    "不得编造未在给定材料中出现的资质、案例、数字或承诺。"
)

GEN_USER = """章节：{title}

【招标要求条目】（id | 页码 | 内容）
{items}

【能力库匹配结果】（需求 id -> 能力 / 状态 / 置信度 / 证据）
{matches}

请针对上述每条招标要求写出应答段落，输出 JSON：
{{"paragraphs": [{{"text": "应答正文（100-250 字，中文，逐条对应要求，不用 markdown）", "sources": ["T001", "cap-product-vr-classroom"], "kind": "技术响应"}}]}}

硬规则：
1. sources 只能使用上面出现过的 id，每段至少 1 个；
2. 状态为「不满足」的要求，必须写成"偏离说明 + 补齐措施"，不得声称已满足；
3. 不要复述整个招标条目，直接给应答内容；
4. 只输出 JSON。"""


def build_plan(
    doc_id: str,
    result: ExtractResult,
    matches: list[RequirementMatch],
    chapter_of=None,
) -> list[PlannedSection]:
    """按章节组织生成计划。

    chapter_of(page, offset) 给出该条引用**落点**所属的招标文件章节（offset 是 quote 的页内起点，
    同页跟章时页首那一段可能属于上一章）；有它时按**章节**聚合（一份标书通常 4-6 章，而不是
    模型的 30+ 个表格小节），没有时退回条目自带的 section。
    """
    by_section: dict[str, list] = {}
    for t in result.tech_requirements:
        offset = t.char_offset or 0
        key = (chapter_of(t.page, offset) if chapter_of else "") or t.section or "技术要求"
        by_section.setdefault(key, []).append(t)
    match_by_req = {m.requirement_id: m for m in matches}
    plans: list[PlannedSection] = []
    for title, items in by_section.items():
        ordered = sorted(items, key=lambda x: (x.page, x.no))
        plans.append(
            PlannedSection(
                section_id=f"gen-{_slug(title)}",
                title=title,
                item_ids=[i.id for i in ordered],
                items=[
                    {"id": i.id, "text": i.text, "page": i.page, "kind": "tech", "no": i.no, "review": i.needs_review}
                    for i in ordered
                ],
                matches=[match_by_req[i.id] for i in ordered if i.id in match_by_req],
            )
        )
    # 评分项单独一节：逐条对照评分因素写应答要点（引用 S 条目）
    if result.scoring_items:
        plans.append(
            PlannedSection(
                section_id="gen-scoring",
                title="评分项应答要点",
                item_ids=[s.id for s in result.scoring_items],
                items=[
                    {"id": s.id, "text": f"{s.item}：{s.criteria_text}", "page": s.page, "kind": "scoring",
                     "no": s.no, "review": s.needs_review}
                    for s in result.scoring_items
                ],
                matches=[match_by_req[s.id] for s in result.scoring_items if s.id in match_by_req],
            )
        )
    return plans


def _slug(title: str) -> str:
    keep = [ch for ch in title if ch.isalnum() or ch in "（）()、"]
    return "".join(keep)[:24] or "section"


def _format_items(plan: PlannedSection) -> str:
    return "\n".join(f"{i['id']} | p{i['page']} | {i['text'][:220]}" for i in plan.items)


def _format_matches(plan: PlannedSection) -> str:
    if not plan.matches:
        return "（无匹配）"
    return "\n".join(
        f"{m.requirement_id} -> {m.matched_capability_id or '无'} / {m.status} / conf={m.confidence}" for m in plan.matches
    )


def verify_citations(paragraphs: list[dict], allowed: set[str]) -> tuple[list[GeneratedParagraph], list[dict]]:
    """硬闸门：无来源 / 来源不存在 的段落一律不输出。"""
    accepted: list[GeneratedParagraph] = []
    rejected: list[dict] = []
    for p in paragraphs:
        text = (p.get("text") or "").strip()
        sources = [str(s) for s in (p.get("sources") or []) if str(s).strip()]
        if not text:
            rejected.append({"reason": "空段落", **p})
            continue
        if not sources:
            rejected.append({"reason": "缺少来源（sources 为空）", **p})
            continue
        unknown = [s for s in sources if s not in allowed]
        if unknown:
            rejected.append({"reason": f"来源 id 不存在: {', '.join(unknown)}", **p})
            continue
        accepted.append(GeneratedParagraph(text=text, sources=sources, kind=p.get("kind") or "技术响应"))
    return accepted, rejected


def _mock_paragraphs(plan: PlannedSection) -> list[dict]:
    """LLM 不可用时的模板应答：只用招标条目 + 能力库原文，不添加任何事实。"""
    out: list[dict] = []
    match_by_req = {m.requirement_id: m for m in plan.matches}
    for item in plan.items:
        m = match_by_req.get(item["id"])
        if m and m.status not in (MISSING, NEEDS_REVIEW):
            out.append(
                {
                    "text": f"针对贵方要求「{item['text'][:80]}」，我方以{m.matched_capability}响应（{m.status}，置信度 {m.confidence}）。",
                    "sources": [item["id"], m.matched_capability_id],
                    "kind": "技术响应（模板，LLM 不可用）",
                }
            )
        else:
            out.append(
                {
                    "text": f"针对贵方要求「{item['text'][:80]}」，当前能力库无匹配项，按偏离处理并提交补齐方案。",
                    "sources": [item["id"]],
                    "kind": "技术响应（模板，LLM 不可用）",
                }
            )
    return out


class WorkflowEngine:
    def __init__(
        self,
        doc_id: str,
        result: ExtractResult,
        caps: CapabilityIndex,
        llm: LLM,
        store: WorkflowStore,
        *,
        chapter_of=None,
        batch_size: int = 12,
        concurrency: int = 3,
        allow_mock: bool = False,
    ) -> None:
        self.doc_id = doc_id
        self.result = result
        self.caps = caps
        self.llm = llm
        self.store = store
        self.allow_mock = allow_mock
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.matches = build_matrix(result.tech_requirements, result.scoring_items, result.disqualifications, caps)
        self.plans = build_plan(doc_id, result, self.matches, chapter_of=chapter_of)
        self.allowed: set[str] = {
            *(t.id for t in result.tech_requirements),
            *(s.id for s in result.scoring_items),
            *(d.id for d in result.disqualifications),
            *(c.id for c in caps.caps),
        }

    async def plan(self) -> list[PlannedSection]:
        for plan in self.plans:
            await self.store.set_state(self.doc_id, plan.section_id, Stage.PLANNED.value, f"{len(plan.items)} 条要求")
        return self.plans

    async def generate_section(self, plan: PlannedSection, max_retries: int = 1) -> tuple[list[dict], int, bool, str]:
        """按批调用模型（每批 <= batch_size 条要求），避免一次要 40 条应答被 max_tokens 截断。

        返回 (原始段落, 尝试次数, 是否降级为模板, 失败原因)。
        失败原因为空表示模型正常返回。
        """
        if not plan.items:
            return [], 1, False, ""
        batches = [plan.items[i : i + self.batch_size] for i in range(0, len(plan.items), self.batch_size)]
        sem = asyncio.Semaphore(self.concurrency)

        async def one(batch: list[dict]):
            async with sem:
                return await self._generate_batch(plan, batch, max_retries)

        results = await asyncio.gather(*(one(b) for b in batches))
        paragraphs = [p for ps, _, _, _ in results for p in ps]
        attempts = max(a for _, a, _, _ in results)
        mocked = any(m for _, _, m, _ in results)
        errors = [e for *_, e in results if e]
        return paragraphs, attempts, mocked, "; ".join(dict.fromkeys(errors))

    async def _generate_batch(
        self, plan: PlannedSection, batch_items: list[dict], max_retries: int = 1
    ) -> tuple[list[dict], int, bool, str]:
        ids = {i["id"] for i in batch_items}
        sub = PlannedSection(
            section_id=plan.section_id,
            title=plan.title,
            item_ids=[i["id"] for i in batch_items],
            items=batch_items,
            matches=[m for m in plan.matches if m.requirement_id in ids],
        )
        messages = [
            {"role": "system", "content": GEN_SYSTEM},
            {
                "role": "user",
                "content": GEN_USER.format(
                    title=sub.title, items=_format_items(sub), matches=_format_matches(sub)
                ),
            },
        ]
        last_err = ""
        for attempt in range(max_retries + 1):
            try:
                reply = await self.llm.complete(messages, json_mode=True, max_tokens=6000)
            except LLMError as exc:
                last_err = str(exc)
                break  # 网络类错误重试意义不大，直接降级
            try:
                data = parse_json(reply.text)
                paragraphs = data.get("paragraphs") or []
                if not isinstance(paragraphs, list) or not paragraphs:
                    raise LLMError("paragraphs 为空")
                return paragraphs, attempt + 1, False, ""
            except LLMError as exc:
                last_err = str(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.text[:3000]},
                    {"role": "user", "content": f"输出不合规：{exc}。请重新输出 JSON，键为 paragraphs。"},
                ]
        # 退化：模板应答（仍带来源），在结果里标 mocked —— 且只有在显式开启时才允许
        if not self.allow_mock:
            return [], max_retries + 1, False, last_err or "LLM 不可用"
        return _mock_paragraphs(sub), max_retries + 1, True, ""

    async def run_section(self, plan: PlannedSection, restart: bool = False) -> SectionRun:
        run = SectionRun(section_id=plan.section_id, title=plan.title)
        prior = await self.store.get_state(self.doc_id, plan.section_id)
        if prior in (Stage.CITATION_VERIFIED.value, Stage.AGGREGATED.value) and not restart:
            saved = await self.store.get_section(self.doc_id, plan.section_id)
            if saved:
                run.state = prior
                run.paragraphs = list(saved.get("paragraphs") or [])
                run.rejected = list(saved.get("rejected") or [])
                return run

        raw, attempts, mocked, err = await self.generate_section(plan)
        run.attempts, run.mocked = attempts, mocked
        await self.store.set_state(self.doc_id, plan.section_id, Stage.GENERATED.value, f"attempts={attempts} mocked={mocked}")

        accepted, rejected = verify_citations(raw, self.allowed)
        run.paragraphs, run.rejected = accepted, rejected
        if not accepted:
            run.state = Stage.FAILED.value
            # 降级未开启时的失败要能自解释：不是"模型答得差"，而是"模型没跑起来"
            run.error = (
                f"LLM 不可用且未开启模板降级（{err}）；本节点未产出任何应答"
                if err and not mocked
                else "全部段落因缺少来源被拒绝"
            )
            await self.store.set_state(self.doc_id, plan.section_id, Stage.FAILED.value, run.error)
            await self.store.save_section(self.doc_id, plan.section_id, [], rejected)
            return run

        run.state = Stage.CITATION_VERIFIED.value
        if err and not mocked:
            # 有批次失败但仍有段落通过校验：内容不完整，必须留下痕迹（否则等于静默少答）
            run.error = f"部分批次未取到模型输出（{err}）；本节内容不完整"
        await self.store.set_state(
            self.doc_id, plan.section_id, Stage.CITATION_VERIFIED.value, f"accepted={len(accepted)} {run.error}".strip()
        )
        await self.store.save_section(self.doc_id, plan.section_id, accepted, rejected)
        return run

    async def run(self, only_section: str | None = None, restart: bool = False) -> WorkflowResult:
        res = WorkflowResult(doc_id=self.doc_id)
        plans = [p for p in self.plans if only_section is None or p.section_id == only_section]
        for plan in plans:
            res.sections.append(await self.run_section(plan, restart=restart))
        res.mocked = any(s.mocked for s in res.sections)
        return res

    async def aggregate(self) -> dict:
        """汇总：章节标题层级 + 已通过校验的段落（无来源段落已在 verify 阶段剔除）。"""
        outline = []
        for plan in self.plans:
            run = await self.run_section(plan, restart=False)
            if run.state != Stage.CITATION_VERIFIED.value:
                continue
            outline.append(
                {
                    "section_id": plan.section_id,
                    "title": plan.title,
                    "items": plan.items,
                    "paragraphs": [p.model_dump() for p in run.paragraphs],
                }
            )
            await self.store.set_state(self.doc_id, plan.section_id, Stage.AGGREGATED.value, "aggregated")
        return {"doc_id": self.doc_id, "title": self.result.title, "sections": outline}

    def matrix_rows(self) -> list[dict]:
        return [m.to_row() for m in self.matches]

    def matrix_stats(self) -> dict:
        counts = {SATISFIED: 0, "部分满足": 0, MISSING: 0}
        for m in self.matches:
            counts[m.status] = counts.get(m.status, 0) + 1
        return {"total": len(self.matches), **counts}