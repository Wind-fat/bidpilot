"""端到端流水线：解析 → 抽取 → 能力矩阵 → 状态机生成 → 导出 Word → 落库。

入口是 scripts/run_pipeline.py。所有阶段的结果都在 PipelineResult 里，便于 CLI 打印与测试断言。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import CapabilityIndex, RequirementMatch, build_matrix, load_capabilities
from .config import Settings, get_settings
from .db import SqliteStore, init_db, make_engine, save_document, save_generation
from .export import BANNER_EXAMPLE_CAPS, build_docx, chunk_shortfall
from .extract import (
    ExtractResult,
    build_chunks,
    dump_extract,
    extract,
    is_structured_body,
    load_extract,
    low_yield_basis,
    mark_low_yield,
    peer_yield,
    relocate,
)
from .llm import LLM, ChatClient
from .sections import TenderDoc, load_tender
from .workflow import MemoryStore, WorkflowEngine, WorkflowResult

# 退出码：非零的含义必须稳定（脚本/CI 会按值判断），且优先级见 PipelineResult.exit_code
EXIT_OK = 0
EXIT_TEMPLATE_FALLBACK = 2  # 有章节被降级为模板应答（未调用模型）
EXIT_EXAMPLE_CAPABILITIES = 3  # 能力库是示例数据，匹配结果无商业意义
EXIT_INCOMPLETE = 4  # 有章节整节失败 / 部分批次失败，交付物不完整
EXIT_UNREADABLE = 5  # 招标文件不可解析（无文本层/空文档），未产出交付物

# 严重度 → 退出码的唯一判定处（README「降级与污染标记」表必须与本表逐行一致）：
# 4 > 2 > 3 > 0。「不完整」排第一，因为「文件里根本没有有效应答」比「有章节是模板句、
# 但人还能逐条改」更危险；两张污染同时成立时文件名取最严重者（见 artifact_suffix）。
INCOMPLETE_SEVERITIES = frozenset({"failed", "incomplete", "empty_delivery"})
EXIT_PRIORITY = (EXIT_INCOMPLETE, EXIT_TEMPLATE_FALLBACK, EXIT_EXAMPLE_CAPABILITIES, EXIT_OK)

log = logging.getLogger("bidpilot.pipeline")

MOCK_SUFFIX = "_MOCK"
FAILED_SUFFIX = "_FAILED"
# 抽取块失败但仍有内容：`_PARTIAL` = 「内容不完整」的另一副面孔（整份废稿才是 `_FAILED`）。
# 两个后缀都归退出码 4，见 SUFFIX_SEVERITY。
PARTIAL_SUFFIX = "_PARTIAL"

# 后缀 → 严重度：文件名、退出码、首页横幅必须由**同一批** severity 判定，否则四处留痕会互相拆台。
SUFFIX_SEVERITY = {FAILED_SUFFIX: "empty_delivery", PARTIAL_SUFFIX: "incomplete", MOCK_SUFFIX: "mocked"}

# 抽取任务 → 中文名：横幅/CLI 要说清「因此缺了哪部分内容」，不能只报一个块号。
TASK_LABELS = {
    "scoring_items": "评分项",
    "disqualifications": "废标条款",
    "tech_requirements": "技术要求",
}


def chunk_failure_rows(extraction, tender=None) -> list[dict]:
    """逐块失败的明细（`problems()` / docx 横幅 / CLI 清单共用这一份，不各写一套）。

    条目带页码、所属章节、缺失的条目类型与最后一次错误：读者要能据此判断「缺了哪几页的什么」，
    而不是只知道「有 4 块失败」。
    """
    if extraction is None:
        return []
    sections = {s.id: s.title for s in getattr(tender, "sections", None) or []}
    rows: list[dict] = []
    for r in getattr(extraction, "reports", None) or []:
        if getattr(r, "ok", True):
            continue
        pages = getattr(r, "pages", (0, 0))
        tasks = list(getattr(r, "tasks", None) or [])
        chunk_id = r.chunk_id
        rows.append(
            {
                "chunk_id": chunk_id,
                "page_start": int(pages[0]),
                "page_end": int(pages[1]),
                "section_title": sections.get(chunk_id.split("#", 1)[0], ""),
                "tasks": tasks,
                "missing": [TASK_LABELS.get(t, t) for t in tasks],
                "attempts": int(getattr(r, "attempts", 0) or 0),
                "extracted": sum(int(v or 0) for v in (getattr(r, "counts", None) or {}).values()),
                "reason": "；".join((getattr(r, "errors", None) or [])[:2]) or "重试后仍未通过校验",
            }
        )
    return rows


def low_yield_rows(extraction, tender=None) -> list[dict]:
    """低产块明细（CLI 清单 / docx 横幅 / problems() 共用这一份）。

    「低产」= 块成功、条目全合规，但条数明显低于同章其它页段（`extract.mark_low_yield`）。
    它不是失败：交付物里的条目一条都没少校验，只是**可能漏抽**——所以退出码与文件名都不变，
    但必须让打开文件的人看到「这几页要补看原文」。
    """
    reports = list(getattr(extraction, "reports", None) or [])
    sections = {s.id: s.title for s in getattr(tender, "sections", None) or []}
    rows: list[dict] = []
    for r in reports:
        if not getattr(r, "low_yield", False):
            continue
        pages = getattr(r, "pages", (0, 0))
        items = sum(int(v or 0) for v in (getattr(r, "counts", None) or {}).values())
        # 判定基准与命中规则在 mark_low_yield 判的那一刻就写进 report 了：这里只读，不再复算
        # （阈值可配之后，事后拿「现在的 env」重算会给出与当时不同的结论）。
        if getattr(r, "thresholds", None):
            mean, n = float(r.peer_mean), int(r.peer_count)
        else:  # 老 JSON / 老缓存的兜底：没记录过判定依据，只能按当前阈值现算
            mean, n = peer_yield(reports, r)
        rows.append(
            {
                "chunk_id": r.chunk_id,
                "page_start": int(pages[0]),
                "page_end": int(pages[1]),
                "section_title": sections.get(r.chunk_id.split("#", 1)[0], ""),
                "items": items,
                "body_chars": int(r.body_chars),
                "yield_rate": float(r.yield_rate),
                "peer_mean": mean,
                "peers": n,
                "hit_abs": bool(getattr(r, "hit_abs", False)),
                "hit_peer": bool(getattr(r, "hit_peer", False)),
                "tasks": list(r.tasks),
                "labels": [TASK_LABELS.get(t, t) for t in r.tasks],
                # 判定依据要能复算：命中哪一条、跟谁比、比的是多少
                "rule": low_yield_basis(r),
            }
        )
    return rows


def refresh_chunk_yields(extraction, tender, max_chars: int) -> None:
    """按**本次实际分块**补回 body_chars / structured，然后重算低产标记。

    复用旧版 `--extract-json` 或用旧缓存时，report 里没有正文长度与正文形态，
    低产判定会静默失效（= 又一个「看起来是绿的」）。分块对不上（换了 max_chars / 换了文档）就跳过，
    只加标记、不改条目。
    """
    reports = list(getattr(extraction, "reports", None) or [])
    if not reports:
        return
    bodies = {c.id: c.body for c in build_chunks(tender, max_chars=max_chars)} if tender is not None else {}
    for r in reports:
        body = bodies.get(r.chunk_id)
        if body is None:
            continue
        r.body_chars = r.body_chars or len(body)
        r.structured = is_structured_body(body)
    mark_low_yield(reports)


def exit_code_for(severities) -> int:
    """把 problems() 的严重度集合压成一个稳定退出码；顺序即优先级（4 > 2 > 3 > 0）。

    `low_yield` 故意不参与：低产块不是失败（条目全部通过校验），只让用户看见「可能漏抽」。
    """
    sev = set(severities)
    if sev & INCOMPLETE_SEVERITIES:
        return EXIT_INCOMPLETE
    if "mocked" in sev:
        return EXIT_TEMPLATE_FALLBACK
    if "example_capabilities" in sev:
        return EXIT_EXAMPLE_CAPABILITIES
    return EXIT_OK


def artifact_suffix(*, empty_delivery: bool, mocked: bool, partial: bool = False) -> str:
    """文件名后缀：一个名字只能表达一种污染，取最严重者 —— `_FAILED` > `_PARTIAL` > `_MOCK`。

    `_PARTIAL` 专指「抽取阶段有块整块失败、交付物少了那几页的内容」（退出码同 `_FAILED` 都是 4：
    两者都是「内容不完整」，区别只在「全无」还是「少一块」）。横幅可以多张同时出现
    （失败声明在前、模板横幅在后），但退出码与文件名必须与横幅里的**最严重那条**指向同一个值，
    否则「四处留痕」会互相拆台。
    """
    if empty_delivery:
        return FAILED_SUFFIX
    if partial:
        return PARTIAL_SUFFIX
    return MOCK_SUFFIX if mocked else ""


@dataclass(slots=True)
class PipelineResult:
    doc_id: str
    tender: TenderDoc
    extraction: ExtractResult
    matches: list[RequirementMatch]
    workflow: WorkflowResult
    aggregated: dict
    docx_path: Path
    docx_bytes: int = 0
    capability_meta: str = ""
    example_capabilities: bool = False
    allow_mock: bool = False
    empty_delivery: bool = False  # 生成阶段整节失败 / 没有任何通过校验的段落
    plans: list = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    llm_usage: dict = field(default_factory=dict)

    def chunk_failures(self) -> list[dict]:
        """抽取阶段整块失败的页段（problems / 横幅 / 文件名 / CLI 共用的唯一明细）。"""
        return chunk_failure_rows(self.extraction, self.tender)

    def low_yield_chunks(self) -> list[dict]:
        """条目数明显偏低的页段（「少而合法」）：不是失败，但可能在交付物里留下空洞。"""
        return low_yield_rows(self.extraction, self.tender)

    def problems(self) -> list[dict]:
        """交付物污染清单：workflow 的三个级别 + 抽取块失败 + 空交付物 + 示例能力库。"""
        out = list(self.workflow.problems())
        for row in self.chunk_failures():
            where = f"（{row['section_title']}）" if row["section_title"] else ""
            out.append(
                {
                    "section_id": row["chunk_id"],
                    "title": f"抽取块 p{row['page_start']}-{row['page_end']}{where}",
                    "severity": "incomplete",
                    "reason": (
                        f"重试 {row['attempts']} 次后仍失败（{row['reason']}）：{chunk_shortfall(row)}"
                        + ("，交付物缺少这部分内容" if row["extracted"] == 0 else "")
                    ),
                    "pages": [row["page_start"], row["page_end"]],
                    "missing": row["missing"],
                }
            )
        # 少而合法：条目全合规、quote 全可回查，但条数明显偏低 —— 不是失败，退出码不变，
        # 但绝不能只写在 docx 里：CLI 与 problems() 也要能读到（否则用户看不到）。
        for row in self.low_yield_chunks():
            where = f"（{row['section_title']}）" if row["section_title"] else ""
            out.append(
                {
                    "section_id": row["chunk_id"],
                    "title": f"低产块 p{row['page_start']}-{row['page_end']}{where}",
                    "severity": "low_yield",
                    "reason": (
                        f"只抽到 {row['items']} 条（{row['yield_rate']} 条/千字符，"
                        f"{row['rule']}）：条目全部通过校验，但可能漏抽了大部分内容，"
                        "请人工核对这几页原文；不是失败，退出码不变"
                    ),
                    "pages": [row["page_start"], row["page_end"]],
                    "yield_rate": row["yield_rate"],
                }
            )
        if self.empty_delivery:
            out.append(
                {
                    "section_id": "delivery",
                    "title": "交付物",
                    "severity": "empty_delivery",
                    "reason": "生成阶段没有产出任何通过校验的段落：文件不含有效应答，不能作为投标文件",
                }
            )
        if self.example_capabilities:
            out.append(
                {
                    "section_id": "capability-library",
                    "title": "能力库",
                    "severity": "example_capabilities",
                    "reason": BANNER_EXAMPLE_CAPS,
                }
            )
        return out

    @property
    def exit_code(self) -> int:
        """0 = 交付物无污染；否则按严重度给出稳定码：4（不完整）> 2（模板应答）> 3（示例库）。"""
        return exit_code_for(p["severity"] for p in self.problems())

    @property
    def suffix(self) -> str:
        """落盘文件名后缀，与 exit_code 同源（`_FAILED` > `_PARTIAL` > `_MOCK` > 空）。"""
        return artifact_suffix(
            empty_delivery=self.empty_delivery,
            mocked=self.workflow.mocked,
            partial=bool(self.chunk_failures()),
        )

    def stats(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "extraction": self.extraction.stats(),
            "capability_matrix": _matrix_stats(self.matches),
            "capability_library": {
                "note": self.capability_meta,
                "is_example": self.example_capabilities,
            },
            "workflow": self.workflow.stats(),
            "delivery": {
                "problems": self.problems(),
                "exit_code": self.exit_code,
                "mock_allowed": self.allow_mock,
                "empty_delivery": self.empty_delivery,
                "chunk_failures": self.chunk_failures(),
                "low_yield_chunks": self.low_yield_chunks(),
            },
            "docx": {"path": str(self.docx_path), "bytes": self.docx_bytes},
            "capability_library_note": self.capability_meta,
            "timing": self.timing,
            "llm_usage": self.llm_usage,
        }


def format_degradation_report(result: PipelineResult) -> list[str]:
    """CLI 用的降级清单（纯字符串，便于测试断言"确实打印了被降级的章节"）。"""
    rows = result.problems()
    if not rows:
        return []
    lines = ["交付物可信度告警（本文件不得直接提交）："]
    for p in rows:
        lines.append(f"  ! [{p['severity']}] {p['section_id']} / {p['title']}：{p['reason']}")
    lines.append(
        f"  → 退出码 {result.exit_code}（优先级 4=内容不完整 > 2=模板应答 > 3=示例能力库 > 0=干净）"
    )
    return lines


def _matrix_stats(matches: list[RequirementMatch]) -> dict:
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.status] = counts.get(m.status, 0) + 1
    return {"total": len(matches), **counts}


def doc_id_for(path: str | Path) -> str:
    p = Path(path).resolve()
    h = hashlib.sha1(str(p).encode()).hexdigest()[:10]
    return f"{p.stem}-{h}"


async def run_pipeline(
    pdf_path: str | Path,
    *,
    settings: Settings | None = None,
    llm: LLM | None = None,
    concurrency: int = 3,
    max_chars: int | None = None,
    generate: bool = True,
    save_db: bool = True,
    out_dir: str | Path | None = None,
    extract_json: str | Path | None = None,
    extract_out: str | Path | None = None,
    relocate_spans: bool = True,
) -> PipelineResult:
    s = settings or get_settings()
    s.ensure_dirs()
    out = Path(out_dir) if out_dir else s.out_dir
    out.mkdir(parents=True, exist_ok=True)

    client = llm or ChatClient(s)
    doc_id = doc_id_for(pdf_path)

    t_parse0 = time.perf_counter()
    tender = load_tender(pdf_path)  # 解析 + 切章 + 废标扫描（200 页性能口径里的"解析+切章"）
    parse_s = round(time.perf_counter() - t_parse0, 1)
    t_start = time.perf_counter()
    t_extract0 = t_start
    reloc = {"items": 0, "located": 0, "unlocated": []}
    if extract_json:
        # 分阶段跑：复用已落盘的抽取结果，不再调 LLM
        extraction = load_extract(json.loads(Path(extract_json).read_text(encoding="utf-8")))
        if relocate_spans:
            # 旧版 JSON 没有 quote_span；按 page+quote 重定位后才谈得上「章节标签 / 可回查」。
            # 定位不到只加 needs_review，不猜偏移（见 docs/LOCATE.md）。
            reloc = relocate(tender, extraction)
            if reloc["unlocated"]:
                log.warning("重定位失败 %d 条（已标 needs_review）", len(reloc["unlocated"]))
    else:
        extraction = await extract(
            tender,
            client,
            max_chars=max_chars or s.chunk_max_chars,
            max_retries=s.max_retries,
            concurrency=concurrency,
            cache_dir=s.cache_dir,
            model=s.llm_model,
            thinking=s.llm_enable_thinking,
        )
    if extract_out:
        p = Path(extract_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(dump_extract(extraction), ensure_ascii=False, indent=2), encoding="utf-8")
    extract_s = round(time.perf_counter() - t_extract0, 1)
    # 低产块要按「本份文档的实际分块」算产出率：旧 extract JSON / 旧缓存缺 body_chars，这里补回并重算标记
    refresh_chunk_yields(extraction, tender, max_chars or s.chunk_max_chars)
    # 整块失败的页段：problems / 文件名 / 首页横幅 / CLI 清单都从这一份明细出发（四处同源）
    chunk_rows = chunk_failure_rows(extraction, tender)
    low_rows = low_yield_rows(extraction, tender)
    if low_rows:
        log.warning(
            "抽取有 %d 块低产（条目数明显偏低，可能漏抽；不是失败，退出码不变）：%s",
            len(low_rows), [r["chunk_id"] for r in low_rows],
        )
    if chunk_rows:
        log.warning("抽取有 %d 块整块失败（交付物不完整，退出码 4）：%s", len(chunk_rows), [r["chunk_id"] for r in chunk_rows])
    # 抽取阶段真正发生的调用数（缓存命中时为 0，重试会多算）
    calls_after_extract = len(client.usage_log) if isinstance(client, ChatClient) else 0
    tok_after_extract = (
        sum(u.prompt_tokens for u in client.usage_log),
        sum(u.completion_tokens for u in client.usage_log),
    ) if isinstance(client, ChatClient) else (0, 0)
    t_gen0 = time.perf_counter()

    caps_lib = load_capabilities(s.capability_file)
    index = CapabilityIndex(caps_lib.capabilities)
    matches = build_matrix(extraction.tech_requirements, extraction.scoring_items, extraction.disqualifications, index)

    engine = make_engine(s.db_path) if save_db else None
    store = MemoryStore()
    if engine is not None:
        await init_db(engine)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        store = SqliteStore(async_sessionmaker(engine, expire_on_commit=False))  # type: ignore[assignment]

    def _chapter_of(page: int, offset: int = 0) -> str:
        """按引用落点（页 + 页内偏移）取章节，而不是整页都算页首那一章。"""
        sec = tender.section_at(page, offset)
        return sec.title if sec else ""

    wf_engine = WorkflowEngine(
        doc_id,
        extraction,
        index,
        client,
        store,
        chapter_of=_chapter_of,
        batch_size=s.gen_batch_size,
        concurrency=concurrency,
        allow_mock=s.allow_mock,
    )
    plans = await wf_engine.plan()
    workflow = await wf_engine.run() if generate else WorkflowResult(doc_id=doc_id)
    aggregated = await wf_engine.aggregate() if generate else {"doc_id": doc_id, "title": tender.title, "sections": []}

    # 模板降级的产物必须在文件名上就暴露，不能和真实生成的文件同名；
    # 整节失败（没有任何通过校验的段落）则既不加 _MOCK，也不许落"看起来完整"的空壳（P0-05 延伸）。
    # 两者同时成立时文件名只留最严重的一个（_FAILED），退出码同源是 4，横幅两张都在。
    sections_out = aggregated.get("sections") or []
    empty_delivery = bool(generate) and not sections_out
    suffix = artifact_suffix(
        empty_delivery=empty_delivery, mocked=workflow.mocked, partial=bool(chunk_rows)
    )
    docx_path = out / f"{Path(pdf_path).stem}_技术应答初稿{suffix}.docx"
    payload = dict(aggregated)
    payload["chunk_failures"] = chunk_rows
    payload["low_yield_chunks"] = low_rows
    if empty_delivery:
        payload["failures"] = [p for p in workflow.problems() if p["severity"] in ("failed", "incomplete")]
    build_docx(
        payload,
        docx_path,
        mocked=workflow.mocked,
        example_capabilities=caps_lib.is_example,
        failed=empty_delivery,
        staged=not generate,
        chunk_failures=chunk_rows,
        low_yield_chunks=low_rows,
    )

    if engine is not None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            await save_document(session, doc_id, tender, extraction, matches, plans)
        async with sf() as session:
            await save_generation(session, doc_id, workflow.sections)
        await engine.dispose()

    gen_s = round(time.perf_counter() - t_gen0, 1)
    usage_summary = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0, "model": s.llm_model}
    if isinstance(client, ChatClient):
        usage_summary["calls"] = len(client.usage_log)
        usage_summary["prompt_tokens"] = sum(u.prompt_tokens for u in client.usage_log)
        usage_summary["completion_tokens"] = sum(u.completion_tokens for u in client.usage_log)
        usage_summary["latency_ms"] = sum(u.latency_ms for u in client.usage_log)
        await client.aclose()
    extract_calls = calls_after_extract
    total_s = round(time.perf_counter() - t_start, 1)
    usage_summary.update(
        {
            "extract_calls": extract_calls,
            "generate_calls": max(0, usage_summary["calls"] - extract_calls),
            "extract_prompt_tokens": tok_after_extract[0],
            "extract_completion_tokens": tok_after_extract[1],
            "generate_prompt_tokens": usage_summary["prompt_tokens"] - tok_after_extract[0],
            "generate_completion_tokens": usage_summary["completion_tokens"] - tok_after_extract[1],
            "relocated": reloc["located"],
            "relocate_unlocated": len(reloc["unlocated"]),
            "extract_attempts": sum(r.attempts for r in extraction.reports),
            "chunks": len(extraction.reports),
            "extract_seconds": extract_s,
            "generate_seconds": gen_s,
            "total_seconds": total_s,
        }
    )

    return PipelineResult(
        doc_id=doc_id,
        tender=tender,
        extraction=extraction,
        matches=matches,
        workflow=workflow,
        aggregated=aggregated,
        docx_path=docx_path,
        docx_bytes=docx_path.stat().st_size,
        capability_meta=caps_lib.meta,
        example_capabilities=caps_lib.is_example,
        allow_mock=s.allow_mock,
        empty_delivery=empty_delivery,
        plans=plans,
        timing={"parse_s": parse_s, "extract_s": extract_s, "generate_s": gen_s, "total_s": total_s},
        llm_usage=usage_summary,
    )


def run_pipeline_sync(pdf_path: str | Path, **kwargs) -> PipelineResult:
    return asyncio.run(run_pipeline(pdf_path, **kwargs))
