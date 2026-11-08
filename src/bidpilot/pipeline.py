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
from .export import BANNER_EXAMPLE_CAPS, build_docx
from .extract import ExtractResult, dump_extract, extract, load_extract, relocate
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


def exit_code_for(severities) -> int:
    """把 problems() 的严重度集合压成一个稳定退出码；顺序即优先级（4 > 2 > 3 > 0）。"""
    sev = set(severities)
    if sev & INCOMPLETE_SEVERITIES:
        return EXIT_INCOMPLETE
    if "mocked" in sev:
        return EXIT_TEMPLATE_FALLBACK
    if "example_capabilities" in sev:
        return EXIT_EXAMPLE_CAPABILITIES
    return EXIT_OK


def artifact_suffix(*, empty_delivery: bool, mocked: bool) -> str:
    """文件名后缀：一个名字只能表达一种污染，取最严重者 —— `_FAILED` > `_MOCK`。

    横幅可以两张同时出现（失败声明在前、模板横幅在后），但退出码与文件名必须与
    横幅里的**最严重那条**指向同一个值，否则「三处留痕」会互相拆台。
    """
    if empty_delivery:
        return FAILED_SUFFIX
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

    def problems(self) -> list[dict]:
        """交付物污染清单的汇总视图：workflow 的三个级别 + 空交付物 + 示例能力库。"""
        out = list(self.workflow.problems())
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
        """落盘文件名后缀，与 exit_code 同源（`_FAILED` > `_MOCK` > 空）。"""
        return artifact_suffix(empty_delivery=self.empty_delivery, mocked=self.workflow.mocked)

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
    suffix = artifact_suffix(empty_delivery=empty_delivery, mocked=workflow.mocked)
    docx_path = out / f"{Path(pdf_path).stem}_技术应答初稿{suffix}.docx"
    payload = dict(aggregated)
    if empty_delivery:
        payload["failures"] = [p for p in workflow.problems() if p["severity"] in ("failed", "incomplete")]
    build_docx(
        payload,
        docx_path,
        mocked=workflow.mocked,
        example_capabilities=caps_lib.is_example,
        failed=empty_delivery,
        staged=not generate,
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
