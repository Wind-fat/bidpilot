"""SQLite 持久化（sqlalchemy[asyncio] + aiosqlite）：条目、生成段落、审核动作、workflow 状态。

为什么把 workflow 状态也落库：审核台要能看到"这一节卡在哪个状态"，并且能只重跑这一节；
状态只在内存里的话，服务重启就回到起点，也没法解释为什么某节没有内容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .capabilities import RequirementMatch
from .extract import ExtractResult
from .schemas import GeneratedParagraph
from .sections import TenderDoc


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(default=lambda: time.time())


class ItemRow(Base):
    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    page: Mapped[int] = mapped_column(Integer, default=0)
    section_id: Mapped[str] = mapped_column(String(80), default="")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|edited|rejected
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class SectionRow(Base):
    __tablename__ = "sections"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    section_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(24), default="planned")
    detail: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[float] = mapped_column(default=lambda: time.time())


class ParagraphRow(Base):
    __tablename__ = "paragraphs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    section_id: Mapped[str] = mapped_column(String(80), index=True)
    section_title: Mapped[str] = mapped_column(Text, default="")
    ord: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(32), default="技术响应")
    state: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|edited|rejected


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(16))  # item|paragraph
    target_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))  # accept|edit|reject
    edited_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(default=lambda: time.time())


def make_engine(db_path: str | Path):
    url = f"sqlite+aiosqlite:///{Path(db_path).as_posix()}"
    return create_async_engine(url, echo=False)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class SqliteStore:
    """WorkflowStore 的落库实现。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.sf = session_factory

    async def set_state(self, doc_id: str, section_id: str, state: str, detail: str = "") -> None:
        async with self.sf() as s:
            row = await s.get(SectionRow, (doc_id, section_id))
            if row is None:
                row = SectionRow(doc_id=doc_id, section_id=section_id)
                s.add(row)
            row.state = state
            row.detail = detail
            row.updated_at = time.time()
            await s.commit()

    async def get_state(self, doc_id: str, section_id: str) -> str | None:
        async with self.sf() as s:
            row = await s.get(SectionRow, (doc_id, section_id))
            return row.state if row else None

    async def save_section(
        self, doc_id: str, section_id: str, paragraphs: list[GeneratedParagraph], rejected: list[dict]
    ) -> None:
        async with self.sf() as s:
            existing = (
                await s.execute(select(ParagraphRow).where(ParagraphRow.doc_id == doc_id, ParagraphRow.section_id == section_id))
            ).scalars().all()
            for row in existing:
                await s.delete(row)
            for i, p in enumerate(paragraphs):
                s.add(
                    ParagraphRow(
                        doc_id=doc_id, section_id=section_id, ord=i, text=p.text, sources=p.sources, kind=p.kind
                    )
                )
            await s.commit()
        self._last_rejected = rejected  # 审核台按需读取（拒绝段落不输出，只做审计）

    async def get_section(self, doc_id: str, section_id: str) -> dict | None:
        async with self.sf() as s:
            rows = (
                await s.execute(
                    select(ParagraphRow)
                    .where(ParagraphRow.doc_id == doc_id, ParagraphRow.section_id == section_id)
                    .order_by(ParagraphRow.ord)
                )
            ).scalars().all()
            if not rows:
                return None
            return {
                "paragraphs": [
                    GeneratedParagraph(text=r.text, sources=list(r.sources or []), kind=r.kind) for r in rows
                ],
                "rejected": [],
            }


def _kind_of_item(item) -> str:
    from .schemas import DisqualificationClause, ScoringItem, TechRequirement

    if isinstance(item, ScoringItem):
        return "scoring"
    if isinstance(item, DisqualificationClause):
        return "disqualification"
    if isinstance(item, TechRequirement):
        return "tech"
    return "unknown"


async def save_document(
    session: AsyncSession,
    doc_id: str,
    tender: TenderDoc,
    result: ExtractResult,
    matches: list[RequirementMatch],
    plans: list,
) -> None:
    session.add(DocumentRow(id=doc_id, title=tender.title, source=str(tender.path), page_count=tender.page_count))
    m_by_req = {m.requirement_id: m for m in matches}
    section_of_item: dict[str, str] = {}
    for plan in plans:
        session.add(SectionRow(doc_id=doc_id, section_id=plan.section_id, title=plan.title, state="planned"))
        for it in plan.items:
            section_of_item[it["id"]] = plan.section_id

    for item in result.items():
        kind = _kind_of_item(item)
        payload = item.model_dump(mode="json")
        payload["match"] = m_by_req[item.id].to_row() if item.id in m_by_req else None
        session.add(
            ItemRow(
                id=item.id,
                doc_id=doc_id,
                kind=kind,
                page=int(getattr(item, "page", 0) or 0),
                section_id=section_of_item.get(item.id, ""),
                needs_review=bool(item.needs_review),
                payload=payload,
            )
        )
    await session.commit()


async def save_generation(session: AsyncSession, doc_id: str, runs: list) -> None:
    for run in runs:
        for i, p in enumerate(run.paragraphs):
            session.add(
                ParagraphRow(
                    doc_id=doc_id, section_id=run.section_id, section_title=run.title, ord=i,
                    text=p.text, sources=p.sources, kind=p.kind,
                )
            )
    await session.commit()


async def apply_review(session: AsyncSession, target_type: str, target_id: str, action: str, edited_text: str = "") -> dict:
    status = {"accept": "accepted", "edit": "edited", "reject": "rejected"}[action]
    if target_type == "item":
        row = await session.get(ItemRow, target_id)
        if row is None:
            raise KeyError(target_id)
        row.status = status
        if action == "edit" and edited_text:
            payload = dict(row.payload or {})
            payload["edited_text"] = edited_text
            row.payload = payload
    else:
        row = await session.get(ParagraphRow, int(target_id))
        if row is None:
            raise KeyError(target_id)
        row.state = status
        if action == "edit" and edited_text:
            row.text = edited_text
    session.add(ReviewRow(target_type=target_type, target_id=str(target_id), action=action, edited_text=edited_text))
    await session.commit()
    return {"target_type": target_type, "target_id": str(target_id), "status": status}


async def load_document(session: AsyncSession, doc_id: str) -> dict:
    doc = await session.get(DocumentRow, doc_id)
    if doc is None:
        raise KeyError(doc_id)
    items = (await session.execute(select(ItemRow).where(ItemRow.doc_id == doc_id).order_by(ItemRow.kind, ItemRow.id))).scalars().all()
    sections = (
        await session.execute(select(SectionRow).where(SectionRow.doc_id == doc_id))
    ).scalars().all()
    paras = (
        await session.execute(
            select(ParagraphRow).where(ParagraphRow.doc_id == doc_id).order_by(ParagraphRow.section_id, ParagraphRow.ord)
        )
    ).scalars().all()
    para_by_section: dict[str, list[dict]] = {}
    for p in paras:
        para_by_section.setdefault(p.section_id, []).append(
            {"id": p.id, "text": p.text, "sources": list(p.sources or []), "state": p.state, "kind": p.kind}
        )
    return {
        "doc": {"id": doc.id, "title": doc.title, "source": doc.source, "pages": doc.page_count},
        "items": [
            {
                "id": i.id, "kind": i.kind, "page": i.page, "needs_review": i.needs_review,
                "status": i.status, "section_id": i.section_id, "payload": i.payload,
            }
            for i in items
        ],
        "sections": [
            {
                "section_id": s.section_id, "title": s.title, "state": s.state, "detail": s.detail,
                "paragraphs": para_by_section.get(s.section_id, []),
            }
            for s in sections
        ],
    }


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


async def rebuild_extraction(session: AsyncSession, doc_id: str) -> ExtractResult:
    """从库里还原抽取结果（供"只重跑某一节"用，不必重新解析 PDF 和重抽）。"""
    from .schemas import DisqualificationClause, ScoringItem, TechRequirement

    rows = (await session.execute(select(ItemRow).where(ItemRow.doc_id == doc_id))).scalars().all()
    doc = await session.get(DocumentRow, doc_id)
    res = ExtractResult(title=doc.title if doc else doc_id, source=doc.source if doc else "")
    for row in rows:
        payload = {k: v for k, v in (row.payload or {}).items() if k != "match"}
        try:
            if row.kind == "scoring":
                res.scoring_items.append(ScoringItem(**payload))
            elif row.kind == "disqualification":
                res.disqualifications.append(DisqualificationClause(**payload))
            elif row.kind == "tech":
                res.tech_requirements.append(TechRequirement(**payload))
        except Exception:  # 单条脏数据不该拖垮整个重跑
            continue
    return res


async def load_scoring_view(session: AsyncSession, doc_id: str) -> dict:
    """审核台 + 导出的统一视图。"""
    payload = await load_document(session, doc_id)
    items_by_id = {i["id"]: i for i in payload["items"]}
    sections = []
    for sec in payload["sections"]:
        if sec["state"] not in ("citation_verified", "aggregated"):
            continue
        sections.append(
            {
                "title": sec["title"],
                "section_id": sec["section_id"],
                "state": sec["state"],
                "paragraphs": sec["paragraphs"],
                "items": [items_by_id[i] for i in items_by_id if items_by_id[i].get("section_id") == sec["section_id"]],
            }
        )
    return {"title": payload["doc"]["title"], "doc_id": doc_id, "sections": sections}