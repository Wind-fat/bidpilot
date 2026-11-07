"""端到端流水线：解析 → 抽取 → 能力矩阵 → 状态机生成 → 导出 Word → 落库。

入口是 scripts/run_pipeline.py。所有阶段的结果都在 PipelineResult 里，便于 CLI 打印与测试断言。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import CapabilityIndex, RequirementMatch, build_matrix, load_capabilities
from .config import Settings, get_settings
from .db import SqliteStore, init_db, make_engine, save_document, save_generation
from .export import build_docx
from .extract import ExtractResult, extract
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

    def stats(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "extraction": self.extraction.stats(),
            "capability_matrix": _matrix_stats(self.matches),
            "workflow": self.workflow.stats(),
            "docx": {"path": str(self.docx_path), "bytes": self.docx_bytes},
            "capability_library_note": self.capability_meta,
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
) -> PipelineResult:
    s = settings or get_settings()
    s.ensure_dirs()
    out = Path(out_dir) if out_dir else s.out_dir
    out.mkdir(parents=True, exist_ok=True)

    client = llm or ChatClient(s)
    doc_id = doc_id_for(pdf_path)

    tender = load_tender(pdf_path)
    extraction = await extract(
        tender, client, max_chars=max_chars or s.chunk_max_chars, max_retries=s.max_retries, concurrency=concurrency
    )

    caps, cap_meta = load_capabilities(s.capability_file)
    index = CapabilityIndex(caps)
    matches = build_matrix(extraction.tech_requirements, extraction.scoring_items, extraction.disqualifications, index)

    engine = make_engine(s.db_path) if save_db else None
    store = MemoryStore()
    if engine is not None:
        await init_db(engine)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        store = SqliteStore(async_sessionmaker(engine, expire_on_commit=False))  # type: ignore[assignment]

    wf_engine = WorkflowEngine(doc_id, extraction, index, client, store)
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

    if isinstance(client, ChatClient):
        await client.aclose()

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
    )


def run_pipeline_sync(pdf_path: str | Path, **kwargs) -> PipelineResult:
    return asyncio.run(run_pipeline(pdf_path, **kwargs))