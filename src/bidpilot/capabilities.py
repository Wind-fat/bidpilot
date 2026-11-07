"""能力库 + 需求-能力矩阵。

能力库是可编辑的本地 JSON（src/bidpilot/data/capabilities.json，**示例数据**）。
匹配用 documind.index.BM25Index（字符 bigram 分词，中文不需要词典）。
状态判定是**启发式阈值**，没有人工标注集调过参——README 里如实写了这一点。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from documind.index import BM25Index, IndexItem, tokenize

from .config import get_settings
from .schemas import DisqualificationClause, ScoringItem, TechRequirement

SATISFIED = "满足"
PARTIAL = "部分满足"
MISSING = "不满足"

# 判定以**词面覆盖率**为主、BM25 为辅。
# 只用 BM25 会把"危险化学品运输许可证"这种长尾需求判成"满足"——它命中的是能力库里
# 某个含"许可证/报告"字样的条目，分数不低但语义毫不相干。覆盖率能拦住这类假阳性。
COVER_SATISFIED = 0.45
COVER_PARTIAL = 0.22


def _coverage(req_text: str, cap_text: str) -> float:
    q = set(tokenize(req_text))
    if not q:
        return 0.0
    g = set(tokenize(cap_text))
    return len(q & g) / len(q)


@dataclass(slots=True)
class Capability:
    id: str
    type: str
    name: str
    summary: str
    tags: list[str]
    evidence: str = ""

    @property
    def text(self) -> str:
        return f"{self.name} {' '.join(self.tags)} {self.summary}"


@dataclass(slots=True)
class Requirement:
    id: str
    kind: str  # tech | scoring | disqualification
    text: str
    page: int


@dataclass(slots=True)
class RequirementMatch:
    requirement_id: str
    requirement: str
    kind: str
    page: int
    matched_capability: str
    matched_capability_id: str
    status: str
    confidence: float
    evidence: str
    alternatives: list[str]

    def to_row(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "requirement": self.requirement,
            "kind": self.kind,
            "page": self.page,
            "matched_capability": self.matched_capability,
            "matched_capability_id": self.matched_capability_id,
            "status": self.status,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "alternatives": self.alternatives,
        }


def load_capabilities(path: str | Path | None = None) -> tuple[list[Capability], str]:
    p = Path(path) if path else get_settings().capability_file
    payload = json.loads(p.read_text(encoding="utf-8"))
    caps = [
        Capability(
            id=c["id"],
            type=c.get("type", ""),
            name=c.get("name", ""),
            summary=c.get("summary", ""),
            tags=list(c.get("tags") or []),
            evidence=c.get("evidence", ""),
        )
        for c in payload.get("capabilities", [])
    ]
    return caps, payload.get("_meta", "")


class CapabilityIndex:
    def __init__(self, caps: list[Capability]) -> None:
        self.caps = caps
        self.by_id = {c.id: c for c in caps}
        items = [
            IndexItem(id=c.id, text=c.text, doc_id="capabilities", strategy="capability") for c in caps
        ]
        self._index = BM25Index(items)

    def search(self, text: str, top_n: int = 3) -> list[tuple[Capability, float]]:
        hits = self._index.search(text, top_n=top_n)
        return [(self.by_id[cid], score) for cid, score in hits if cid in self.by_id]

    def match(self, req: Requirement, top_n: int = 3) -> RequirementMatch:
        hits = self.search(req.text, top_n=top_n)
        if not hits:
            return RequirementMatch(
                requirement_id=req.id, requirement=req.text, kind=req.kind, page=req.page,
                matched_capability="", matched_capability_id="", status=MISSING, confidence=0.0,
                evidence="能力库无任何命中（BM25 分数为 0）", alternatives=[],
            )
        cap, score = hits[0]
        cover = _coverage(req.text, cap.text)
        conf = round(0.7 * cover + 0.3 * (1 - math.exp(-score / 12.0)), 3)
        if cover >= COVER_SATISFIED:
            status = SATISFIED
        elif cover >= COVER_PARTIAL:
            status = PARTIAL
        else:
            status = MISSING
        evidence = (
            f"{cap.id}（{cap.type}）词面覆盖={cover:.2f} BM25={score:.2f}；{cap.evidence}"
            if cap.evidence
            else f"{cap.id} 词面覆盖={cover:.2f} BM25={score:.2f}"
        )
        return RequirementMatch(
            requirement_id=req.id, requirement=req.text, kind=req.kind, page=req.page,
            matched_capability=cap.name, matched_capability_id=cap.id, status=status,
            confidence=conf, evidence=evidence, alternatives=[c.id for c, _ in hits[1:]],
        )


def requirements_from(
    tech: list[TechRequirement], scoring: list[ScoringItem], disq: list[DisqualificationClause]
) -> list[Requirement]:
    reqs: list[Requirement] = []
    for t in tech:
        reqs.append(Requirement(id=t.id, kind="tech", text=f"{t.no} {t.text}", page=t.page))
    for s in scoring:
        reqs.append(Requirement(id=s.id, kind="scoring", text=f"{s.item} {s.criteria_text}", page=s.page))
    for d in disq:
        reqs.append(Requirement(id=d.id, kind="disqualification", text=d.clause_text, page=d.page))
    return reqs


def build_matrix(
    tech: list[TechRequirement], scoring: list[ScoringItem], disq: list[DisqualificationClause], index: CapabilityIndex
) -> list[RequirementMatch]:
    return [index.match(r) for r in requirements_from(tech, scoring, disq)]