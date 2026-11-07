"""审核台 API（FastAPI）。前端是 static/index.html，不引任何前端框架。

接口：
  GET  /                                   审核台
  GET  /api/documents                      文档列表
  GET  /api/documents/{doc_id}             条目 + 生成段落 + 能力匹配
  POST /api/review                         接受/改写/拒绝（落 SQLite）
  POST /api/documents/{doc_id}/sections/{sid}/rerun   只重跑某一节
  GET  /api/documents/{doc_id}/export      导出当前库里的内容为 docx
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .capabilities import CapabilityIndex, build_matrix, load_capabilities
from .config import Settings, get_settings
from .db import (
    DocumentRow,
    SqliteStore,
    apply_review,
    init_db,
    load_document,
    load_scoring_view,
    make_engine,
    rebuild_extraction,
)
from .export import build_docx
from .llm import ChatClient
from .workflow import WorkflowEngine

STATIC = Path(__file__).resolve().parent / "static"


class ReviewBody(BaseModel):
    target_type: str  # item | paragraph
    target_id: str
    action: str  # accept | edit | reject
    edited_text: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    s.ensure_dirs()
    engine = make_engine(s.db_path)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI(title="BidPilot 审核台")

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db(engine)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await engine.dispose()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/documents")
    async def documents() -> list[dict]:
        async with sf() as session:
            rows = (await session.execute(select(DocumentRow))).scalars().all()
            return [{"id": r.id, "title": r.title, "pages": r.page_count} for r in rows]

    @app.get("/api/documents/{doc_id}")
    async def document(doc_id: str) -> dict:
        async with sf() as session:
            try:
                return await load_document(session, doc_id)
            except KeyError:
                raise HTTPException(404, f"文档不存在: {doc_id}") from None

    @app.post("/api/review")
    async def review(body: ReviewBody) -> dict:
        if body.action not in ("accept", "edit", "reject"):
            raise HTTPException(400, "action 必须是 accept/edit/reject")
        async with sf() as session:
            try:
                return await apply_review(session, body.target_type, body.target_id, body.action, body.edited_text)
            except KeyError:
                raise HTTPException(404, f"目标不存在: {body.target_id}") from None

    @app.post("/api/documents/{doc_id}/sections/{section_id}/rerun")
    async def rerun(doc_id: str, section_id: str) -> dict:
        """只重跑这一节（其余节的落库结果不动）。"""
        async with sf() as session:
            result = await rebuild_extraction(session, doc_id)
            if not result.items():
                raise HTTPException(404, f"文档没有可重跑的条目: {doc_id}")
        caps, _ = load_capabilities(s.capability_file)
        client = ChatClient(s)
        try:
            wf = WorkflowEngine(doc_id, result, CapabilityIndex(caps), client, SqliteStore(sf))
            run = await wf.run(only_section=section_id, restart=True)
        finally:
            await client.aclose()
        return {"sections": [{"id": r.section_id, "state": r.state, "paragraphs": len(r.paragraphs)} for r in run.sections]}

    @app.get("/api/documents/{doc_id}/export")
    async def export(doc_id: str) -> FileResponse:
        async with sf() as session:
            view = await load_scoring_view(session, doc_id)
        if not view["sections"]:
            raise HTTPException(409, "没有通过引用校验的章节，无法导出")
        out = s.out_dir / f"{doc_id}_审核版.docx"
        build_docx(view, out)
        return FileResponse(str(out), filename=out.name)

    return app


app = create_app()