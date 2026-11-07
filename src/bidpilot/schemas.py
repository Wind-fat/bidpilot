"""抽取结果的强 schema。

三条硬约束（都由 Pydantic 或 extract.validate 强制）：
1. 每条都必须有页码 + 原文 quote；
2. quote 必须是该页原文的逐字子串（见 extract.locate_quote）；
3. 校验不过的条目**不丢弃**，标 needs_review=True 并带上原因。
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DisqCategory(str, Enum):
    QUALIFICATION = "资格"
    FORM = "形式"
    TECHNICAL = "技术"
    COMMERCIAL = "商务"
    PRICE = "报价"


_CATEGORY_HINTS: list[tuple[DisqCategory, tuple[str, ...]]] = [
    (DisqCategory.PRICE, ("报价", "价格", "最高限价", "低价", "单价", "总价")),
    (DisqCategory.QUALIFICATION, ("资格", "资质", "证书", "营业执照", "信用", "社保", "纳税", "联合体")),
    (DisqCategory.TECHNICAL, ("技术", "参数", "规格", "★", "实质性", "负偏离", "检测", "性能")),
    (DisqCategory.FORM, ("密封", "签字", "盖章", "格式", "装订", "份数", "逾期", "递交", "澄清", "串通")),
    (DisqCategory.COMMERCIAL, ("工期", "交货", "质保", "售后", "服务期", "合同", "付款", "履约")),
]


def guess_category(text: str) -> DisqCategory:
    for cat, words in _CATEGORY_HINTS:
        if any(w in text for w in words):
            return cat
    return DisqCategory.FORM


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = ""
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    def mark_review(self, reason: str) -> None:
        self.needs_review = True
        if reason not in self.review_reasons:
            self.review_reasons.append(reason)


class ScoringItem(_Base):
    """评分项。no 是评分表里的编号（如 "2.1"），item 是评分因素，criteria_text 是评审标准原文。"""

    no: str = ""
    item: str = ""
    criteria_text: str
    max_score: float | None = None
    page: int
    quote: str

    @field_validator("no", "item", mode="before")
    @classmethod
    def _str(cls, v):
        return "" if v is None else str(v)

    @field_validator("max_score", mode="before")
    @classmethod
    def _score(cls, v):
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v):
        return _coerce_page(v)

    @field_validator("quote", "criteria_text", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _nonempty(self):
        for name in ("item", "criteria_text", "quote"):
            if not getattr(self, name).strip():
                self.mark_review(f"{name} 为空")
        if self.max_score is None:
            self.mark_review("max_score 未解析出分值")
        return self


class DisqualificationClause(_Base):
    """废标/无效投标条款。category 四分类之外还有 报价，共 5 类。"""

    clause_text: str
    page: int
    quote: str
    category: DisqCategory = DisqCategory.FORM

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v):
        return _coerce_page(v)

    @field_validator("clause_text", "quote", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v)

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v):
        if isinstance(v, DisqCategory):
            return v
        raw = "" if v is None else str(v).strip()
        for cat in DisqCategory:
            if raw == cat.value or raw == cat.name:
                return cat
        for cat, words in _CATEGORY_HINTS:
            if raw and any(w in raw for w in words):
                return cat
        return guess_category(raw)

    @model_validator(mode="after")
    def _nonempty(self):
        if not self.clause_text.strip():
            self.mark_review("clause_text 为空")
        if not self.quote.strip():
            self.mark_review("quote 为空")
        return self


class TechRequirement(_Base):
    """技术要求条目。page..page_end 允许跨页，quote 必须能在 page（必要时 page_end）里找到。"""

    no: str = ""
    text: str
    page: int
    page_end: int | None = None
    quote: str
    section: str = ""

    @field_validator("no", mode="before")
    @classmethod
    def _no(cls, v):
        return "" if v is None else str(v)

    @field_validator("page", "page_end", mode="before")
    @classmethod
    def _page(cls, v):
        return None if v in (None, "") else _coerce_page(v)

    @field_validator("text", "quote", "section", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _fill(self):
        if self.page_end is None:
            self.page_end = self.page
        if self.page_end < self.page:
            self.mark_review(f"page_end({self.page_end}) < page({self.page})")
            self.page_end = self.page
        if not self.text.strip():
            self.mark_review("text 为空")
        if not self.quote.strip():
            self.mark_review("quote 为空")
        return self


class GeneratedParagraph(BaseModel):
    """生成内容的段落。sources 是招标条目 id 或能力条目 id，空 sources 的段落一律不输出。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    text: str
    sources: list[str] = Field(default_factory=list)
    kind: str = "技术响应"


class ExtractionBatch(BaseModel):
    """一次 LLM 调用的返回体（分任务给，缺的任务就是空列表）。"""

    model_config = ConfigDict(extra="ignore")

    scoring_items: list[ScoringItem] = Field(default_factory=list)
    disqualifications: list[DisqualificationClause] = Field(default_factory=list)
    tech_requirements: list[TechRequirement] = Field(default_factory=list)


def _coerce_page(v) -> int:
    if isinstance(v, bool):
        raise ValueError("page 不能是布尔值")
    if isinstance(v, int):
        if v < 1:
            raise ValueError(f"page 必须是 >=1 的整数，收到 {v}")
        return v
    m = re.search(r"\d+", str(v))
    if not m:
        raise ValueError(f"page 无法解析为页码: {v!r}")
    n = int(m.group())
    if n < 1:
        raise ValueError(f"page 必须是 >=1 的整数，收到 {v!r}")
    return n