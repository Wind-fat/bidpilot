"""引用可回查：对输出条目做**独立**机械复核。

为什么单独一个模块：抽取阶段自称「都校验过了」，审核台和验收不能拿它自己的结论当证据。
这里不调用 `locate_quote_span` 重新定位（那等于用同一套逻辑自证），只检查落盘的东西：

1. `page` 在文档页范围内；
2. `quote_span` 存在，且区间落在该页长度之内；
3. `slice_text(页, 起, 页, 止)` 逐字等于 `quote`（上游强契约）；
4. `quote` 确实是该页原文的子串，位置与 `quote_span` 一致；
5. 章级标签与 `section_at(页, 起)` 反查结果一致（有章节覆盖该区间时）。

任何一条不符都会进失败明细，绝不「找不到就当通过」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import ExtractResult
from .sections import TenderDoc

KINDS = ("scoring_items", "disqualifications", "tech_requirements")


@dataclass(slots=True)
class CiteProblem:
    kind: str
    item_id: str
    page: int
    reasons: list[str]

    def line(self) -> str:
        return f"[{self.kind}] {self.item_id or '(无 id)'} p{self.page}: " + "；".join(self.reasons)


@dataclass(slots=True)
class CiteReport:
    total: int = 0
    per_kind: dict[str, int] = field(default_factory=dict)
    problems: list[CiteProblem] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return self.total - len(self.problems)

    def summary(self) -> str:
        return f"可回查 {self.ok}/{self.total}"

    def kind_summary(self) -> str:
        return " / ".join(f"{k} {v}" for k, v in self.per_kind.items())


def check_item(doc: TenderDoc, item, kind: str = "") -> list[str]:
    """单条复核；返回失败原因列表（空列表 = 可回查）。"""
    reasons: list[str] = []
    page = getattr(item, "page", None)
    if isinstance(page, bool) or not isinstance(page, int) or not (1 <= page <= doc.page_count):
        # 页码本身不成立时，后面的「第几页」检查都没有意义，直接返回
        return [f"page={page!r} 不在 1..{doc.page_count}"]
    if not (item.quote or "").strip():
        reasons.append("quote 为空")

    span = item.quote_span
    if span is None:
        return [*reasons, "缺少 quote_span（无法回查）"]
    span_page, start, end = span
    if not (1 <= span_page <= doc.page_count):
        return [*reasons, f"quote_span 的页 {span_page} 不在 1..{doc.page_count}"]

    claimed = {page, getattr(item, "page_end", None) or page}
    if span_page not in claimed:
        reasons.append(f"quote_span 落在 p{span_page}，不在 page..page_end {sorted(claimed)} 内")

    text = doc.page_text(span_page)
    if not (0 <= start < end <= len(text)):
        reasons.append(f"区间 [{start},{end}) 越出第 {span_page} 页（页长 {len(text)}）")
        return reasons

    frag = doc.parsed.slice_text(span_page, start, span_page, end)
    if frag != item.quote:
        reasons.append(f"slice_text(p{span_page},{start},{span_page},{end}) 与 quote 不一致：{frag[:40]!r}")
    if text[start : start + len(item.quote)] != item.quote:
        reasons.append(f"页文本 [{start},{start + len(item.quote)}) 与 quote 不一致")
    if item.quote and item.quote not in text:
        reasons.append("quote 不是该页原文的逐字子串")

    sec = doc.section_at(span_page, start)
    if sec and item.section != sec.title:
        reasons.append(f"section={item.section!r} 与区间起点反查的 {sec.title!r} 不一致")
    return reasons


def source_mismatch(doc: TenderDoc, res: ExtractResult, pdf_sha256: str = "") -> str | None:
    """抽取结果自称的来源，与即将回查的文档是否同一份（P1-10 后续项）。

    为什么必须有这道闸：page + quote 在**另一份**文件里也可能"偶然"命中——两份招标文件
    的第一章、格式条款、评分表措辞高度雷同。这时 verify_citations 会报 100% 可回查，
    但校验的根本不是那份被抽的文件：句柄指向 A，证据来自 B。

    两级判定：
    1. **内容指纹**（`res.source_sha256`，2026-11 起写入）。两边都有指纹时以指纹为准：
       同名不同内容会被抓住，不同名同内容则判为同一份（拷贝/重命名不该报错）。
    2. **文件名**（旧 JSON 没有指纹时）。文件名不同就一定不是同一份；同名不同路径查不出来，
       这时退化为「只能算弱校验」，调用方要把这句提示打出来。
    """
    claimed = (res.source or "").strip().replace("\\", "/")
    recorded_hash = (res.source_sha256 or "").strip().lower()
    if recorded_hash and pdf_sha256:
        if recorded_hash != pdf_sha256.strip().lower():
            name = claimed.rsplit("/", 1)[-1] or "（未记录）"
            return (
                f"抽取结果的内容指纹与本次回查的文件不一致（记录 {recorded_hash[:12]}… / 实际 "
                f"{pdf_sha256[:12]}…，文件名为 {name}）：引用来自另一份文件，回查结论不算数"
            )
        return None
    if not claimed:  # 旧 JSON 没记来源：调用方负责提示"跳过"，这里不做断言
        return None
    name = claimed.rsplit("/", 1)[-1]
    if name.casefold() != doc.path.name.casefold():
        return (
            f"抽取结果记录的是 {name}，本次回查的是 {doc.path.name}："
            "引用可能来自另一份文件，回查结论不算数"
        )
    return None


def check_all(doc: TenderDoc, res: ExtractResult) -> CiteReport:
    """逐条复核整份抽取结果，按类型计数并收集失败明细。"""
    report = CiteReport()
    for kind in KINDS:
        items = getattr(res, kind)
        report.per_kind[kind] = len(items)
        report.total += len(items)
        for item in items:
            reasons = check_item(doc, item, kind)
            if reasons:
                report.problems.append(
                    CiteProblem(kind=kind, item_id=item.id, page=int(getattr(item, "page", 0) or 0), reasons=reasons)
                )
    return report