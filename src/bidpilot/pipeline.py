"""端到端流水线：解析 → 抽取 → 能力矩阵 → 状态机生成 → 导出 Word → 落库。

入口是 scripts/run_pipeline.py。所有阶段的结果都在 PipelineResult 里，便于 CLI 打印与测试断言。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import CapabilityIndex, RequirementMatch, build_matrix, load_capabilities
from .config import Settings, get_settings
from .db import SqliteStore, init_db, make_engine, save_document, save_generation
from .export import build_docx
from .extract import ExtractResult, dump_extract, extract, load_extract
from .llm import ChatClient, LLM
from .sections import TenderDoc, load_tender
from .workflow import MemoryStore, WorkflowEngine, WorkflowResult


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
    plans: list = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    llm_usage: dict = field(default_factory=dict)

    def stats(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "extraction": self.extraction.stats(),
            "capability_matrix": _matrix_stats(self.matches),
            "workflow": self.workflow.stats(),
            "docx": {"path": str(self.docx_path), "bytes": self.docx_bytes},
            "capability_library_note": self.capability_meta,
            "timing": self.timing,
            "llm_usage": self.llm_usage,
        }


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
) -> PipelineResult:
    s = settings or get_settings()
    s.ensure_dirs()
    out = Path(out_dir) if out_dir else s.out_dir
    out.mkdir(parents=True, exist_ok=True)

    client = llm or ChatClient(s)
    doc_id = doc_id_for(pdf_path)

    tender = load_tender(pdf_path)
    t_start = time.perf_counter()
    t_extract0 = t_start
    if extract_json:
        # 分阶段跑：复用已落盘的抽取结果，不再调 LLM
        extraction = load_extract(json.loads(Path(extract_json).read_text(encoding="utf-8")))
    else:
        extraction = await extract(
            tender,
            client,
            max_chars=max_chars or s.chunk_max_chars,
            max_retries=s.max_retries,
            concurrency=concurrency,
            cache_dir=s.cache_dir,
            model=s.llm_model,
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

    caps, cap_meta = load_capabilities(s.capability_file)
    index = CapabilityIndex(caps)
    matches = build_matrix(extraction.tech_requirements, extraction.scoring_items, extraction.disqualifications, index)

    engine = make_engine(s.db_path) if save_db else None
    store = MemoryStore()
    if engine is not None:
        await init_db(engine)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        store = SqliteStore(async_sessionmaker(engine, expire_on_commit=False))  # type: ignore[assignment]

    def _chapter_of(page: int) -> str:
        sec = tender.section_of_page(page)
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
    )
    plans = await wf_engine.plan()
    workflow = await wf_engine.run() if generate else WorkflowResult(doc_id=doc_id)
    aggregated = await wf_engine.aggregate() if generate else {"doc_id": doc_id, "title": tender.title, "sections": []}

    docx_path = out / f"{Path(pdf_path).stem}_技术应答初稿.docx"
    build_docx(aggregated, docx_path)

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
        capability_meta=cap_meta,
        plans=plans,
        timing={"extract_s": extract_s, "generate_s": gen_s, "total_s": total_s},
        llm_usage=usage_summary,
    )


def run_pipeline_sync(pdf_path: str | Path, **kwargs) -> PipelineResult:
    return asyncio.run(run_pipeline(pdf_path, **kwargs))