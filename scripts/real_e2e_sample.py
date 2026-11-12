#!/usr/bin/env python
"""对**抽样块**跑真实供应商抽取 + 对**抽样的几节**跑真实生成，逐次记录耗时与 token。

为什么单独一支脚本（而不是给 run_pipeline 加 --limit）：
1. 成本必须硬上限（`--budget`，默认 14 次真实调用）——整份 168 块跑完要 10 分钟以上、几百次调用，
   抽样要能随时停；
2. `run_pipeline` 只汇总到「阶段级 attempts + counts」，拿不到每次尝试的墙钟与 token
   （`Usage` 只在 ChatClient 内部留痕），而「块数×中位耗时」的外推要的正是**每次尝试**的数。

两条路径都复用生产代码：抽取走 `extract.extract_chunk`（同一套 prompt / 校验 / 重试升级），
生成走 `workflow.WorkflowEngine.run_section`（同一套计划 / 分批 / 来源硬闸门）。

    # 5 块抽样抽取 + 最多 3 节生成，硬上限 14 次调用
    ./.venv/Scripts/python.exe scripts/real_e2e_sample.py \
        --pdf out/perf/tender_26ch_200p.pdf --chunks p2-2,p50-50,p104-104,p128-128,p172-172 \
        --gen-sections 3 --budget 14 --out out/real_e2e/sample.json

凭据：显式给了 `BIDPILOT_LLM_API_KEY` 就用环境变量；否则从 magpie 的 providers.json
按 `--provider-name` 取 `chat` / `key`（只读，不回显、不落盘）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.capabilities import CapabilityIndex, load_capabilities  # noqa: E402
from bidpilot.citations import check_all  # noqa: E402
from bidpilot.config import Settings  # noqa: E402
from bidpilot.extract import (  # noqa: E402
    ExtractResult,
    build_chunks,
    dump_extract,
    extract_chunk,
    is_structured_body,
    load_extract,
    locate_quote_span,
    mark_low_yield,
    merge,
)
from bidpilot.llm import ChatClient, LLMError, LLMReply  # noqa: E402
from bidpilot.sections import load_tender  # noqa: E402
from bidpilot.workflow import MemoryStore, WorkflowEngine  # noqa: E402

DEFAULT_CREDS = Path.home() / ".config" / "magpie" / "providers.json"


class BudgetExceeded(RuntimeError):
    """真实调用超过 `--budget`：宁可少测一块，也不许悄悄花更多钱。"""


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded(f"已达调用上限 {self.limit} 次，停止（不是失败，是成本闸）")
        self.used += 1


class RecordingLLM:
    """透传 ChatClient，记录每次 `complete` 的耗时 / token / thinking，并占用预算。"""

    def __init__(self, inner: ChatClient, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget
        self.calls: list[dict] = []

    async def complete(self, messages, *, json_mode=True, max_tokens=None, thinking=None) -> LLMReply:  # noqa: ANN001
        self.budget.take()
        rec = {
            "thinking": thinking,
            "elapsed_s": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reply_chars": 0,
            "reply_head": "",
            "error": None,
        }
        t0 = time.perf_counter()
        try:
            reply = await self.inner.complete(
                messages, json_mode=json_mode, max_tokens=max_tokens, thinking=thinking
            )
        except LLMError as exc:
            rec["elapsed_s"] = round(time.perf_counter() - t0, 1)
            rec["error"] = f"{type(exc).__name__}: {exc}"
            self.calls.append(rec)
            raise
        rec["elapsed_s"] = round(time.perf_counter() - t0, 1)
        rec["prompt_tokens"] = reply.usage.prompt_tokens
        rec["completion_tokens"] = reply.usage.completion_tokens
        rec["reply_chars"] = len(reply.text)
        rec["reply_head"] = reply.text[:160]
        self.calls.append(rec)
        return reply

    def reset(self) -> list[dict]:
        out, self.calls = self.calls, []
        return out


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings()
    if args.creds and Path(args.creds).exists():
        providers = json.loads(Path(args.creds).read_text(encoding="utf-8")).get("providers", [])
        hit = next((p for p in providers if p.get("name") == args.provider_name), None)
        if hit is None:
            raise SystemExit(f"providers.json 里没有 name == {args.provider_name!r} 的条目")
        s.llm_base_url = hit["chat"]
        s.llm_api_key = hit["key"]
    if args.model:
        s.llm_model = args.model
    if args.timeout_s is not None:
        s.llm_timeout_s = args.timeout_s
    return s


def _quote_recheck(doc, items) -> dict:  # noqa: ANN001
    """独立复核 quote：不信条目自带的 quote_span，按 page(+page_end) 在原文里重新定位。"""
    ok, bad = 0, []
    for it in items:
        pages = [it.page] + ([it.page_end] if getattr(it, "page_end", None) else [])
        span = None
        for pg in dict.fromkeys(pages):
            if 1 <= pg <= doc.page_count:
                span = locate_quote_span(doc.page_text(pg), it.quote)
                if span:
                    break
        if span:
            ok += 1
        else:
            bad.append({"page": it.page, "quote": it.quote[:60]})
    return {"located": ok, "total": ok + len(bad), "missing": bad}


async def run(args: argparse.Namespace) -> int:
    s = _settings(args)
    budget = Budget(args.budget)
    doc = load_tender(args.pdf)
    wanted = {w.strip().lstrip("p") for w in args.chunks.split(",") if w.strip()}
    if not wanted and not args.reuse_extract:
        raise SystemExit("必须给 --chunks 或 --reuse-extract")
    chunks = [c for c in build_chunks(doc, max_chars=args.max_chars)
              if f"{c.page_start}-{c.page_end}" in wanted]
    if args.reuse_extract:
        chunks = []
    elif len(chunks) != len(wanted):
        raise SystemExit(f"只匹配到 {len(chunks)} 块 / 期望 {len(wanted)}：{[c.id for c in chunks]}")

    print(f"pdf={args.pdf} pages={doc.page_count} model={s.llm_model} base={s.llm_base_url}")
    print(f"timeout_s={s.llm_timeout_s} thinking_default={s.llm_enable_thinking} "
          f"max_chars={args.max_chars} max_retries={s.max_retries} budget={args.budget}")

    client = ChatClient(s)
    rec = RecordingLLM(client, budget)
    result = ExtractResult(title=doc.title, source=str(doc.path))
    rows: list[dict] = []
    stopped = ""
    try:
        # ---------------- 阶段 A：抽样抽取 ----------------
        print("\n== 阶段 A：抽样抽取 ==")
        for chunk in chunks:
            if budget.used >= budget.limit:
                stopped = f"预算耗尽（{budget.used}/{budget.limit}），未跑 {chunk.id}"
                print(f"  ! {stopped}")
                break
            rec.reset()
            t0 = time.perf_counter()
            try:
                batch, report = await extract_chunk(rec, doc, chunk, max_retries=s.max_retries)
            except BudgetExceeded as exc:
                stopped = str(exc)
                print(f"  ! {chunk.id}: {exc}")
                break
            wall = round(time.perf_counter() - t0, 1)
            calls = list(rec.calls)
            items = [*batch.scoring_items, *batch.disqualifications, *batch.tech_requirements]
            check = _quote_recheck(doc, items)
            row = {
                "chunk_id": chunk.id,
                "section_title": chunk.section_title,
                "pages": [chunk.page_start, chunk.page_end],
                "tasks": chunk.tasks,
                "body_chars": len(chunk.body),
                "structured": is_structured_body(chunk.body),
                "low_yield": report.low_yield,
                "attempts": report.attempts,
                "escalated": report.escalated,
                "ok": report.ok,
                "counts": report.counts,
                "items": len(items),
                "needs_review": sum(1 for i in items if i.needs_review),
                "quote_recheck": {"located": check["located"], "total": check["total"]},
                "quote_missing": check["missing"],
                "wall_s": wall,
                "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                "completion_tokens": sum(c["completion_tokens"] for c in calls),
                "attempt_detail": calls,
                "errors": report.errors,
            }
            rows.append(row)
            result.scoring_items.extend(batch.scoring_items)
            result.disqualifications.extend(batch.disqualifications)
            result.tech_requirements.extend(batch.tech_requirements)
            result.reports.append(report)
            print(f"- {chunk.id} p{chunk.page_start}-{chunk.page_end} body={len(chunk.body)} 字 "
                  f"wall={wall}s attempts={report.attempts} escalated={report.escalated} ok={report.ok} "
                  f"items={len(items)} needs_review={row['needs_review']} "
                  f"quote={check['located']}/{check['total']} "
                  f"prompt={row['prompt_tokens']} completion={row['completion_tokens']}")
            for i, call in enumerate(calls, 1):
                print(f"    attempt {i}: thinking={call['thinking']} {call['elapsed_s']}s "
                      f"prompt={call['prompt_tokens']} completion={call['completion_tokens']} "
                      f"reply={call['reply_chars']}字 err={call['error']}")

        # 与生产路径对齐：`extract.extract()` 末尾就是 mark_low_yield + merge（id 只在 merge 里分配），
        # 少了这一步，条目 id 全是空串 → 生成阶段 allowed 集合为空 → 模型给的所有来源都被判「不存在」。
        if args.reuse_extract:
            result = load_extract(json.loads(Path(args.reuse_extract).read_text(encoding="utf-8")))
            print(f"复用抽取结果：{args.reuse_extract}（条目 {len(result.items())} / 块 {len(result.reports)}）")
        else:
            mark_low_yield(result.reports)
            result = merge(result)

        # 独立复核（citations.check_all）：不重定位，只查落盘的 page/quote_span/quote 三者一致
        cite = check_all(doc, result)
        if args.dump_extract:
            dp = Path(args.dump_extract)
            dp.parent.mkdir(parents=True, exist_ok=True)
            dp.write_text(json.dumps(dump_extract(result), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"抽取结果落盘：{dp}（供 --reuse-extract）")

        # ---------------- 阶段 B：抽样生成 ----------------
        print("\n== 阶段 B：抽样生成（含来源硬闸门） ==")
        caps_lib = load_capabilities(s.capability_file)
        index = CapabilityIndex(caps_lib.capabilities)

        def _chapter_of(page: int, offset: int = 0) -> str:
            sec = doc.section_at(page, offset)
            return sec.title if sec else ""

        store = MemoryStore()
        engine = WorkflowEngine(
            "sample", result, index, rec,
            store,
            chapter_of=_chapter_of,
            batch_size=s.gen_batch_size,
            concurrency=args.concurrency,
            allow_mock=False,
        )
        plans = await engine.plan()
        # 按条目数从多到少挑，但同一「类型」不重复占位：技术章节 + 评分项各来一节
        ordered = sorted(plans, key=lambda p: len(p.items), reverse=True)
        picked = ordered[: max(1, args.gen_sections)]
        print(f"计划节数={len(plans)}  选中={[p.section_id for p in picked]}")

        gen_rows: list[dict] = []
        for plan in picked:
            batches = max(1, -(-len(plan.items) // engine.batch_size))
            if budget.used + min(batches, args.concurrency) > budget.limit:
                stopped = stopped or f"生成预算不足（已用 {budget.used}/{budget.limit}），跳过 {plan.section_id}"
                print(f"  ! {stopped}")
                break
            rec.reset()
            t0 = time.perf_counter()
            try:
                run_res = await engine.run_section(plan)
            except BudgetExceeded as exc:
                stopped = str(exc)
                print(f"  ! {plan.section_id}: {exc}")
                break
            wall = round(time.perf_counter() - t0, 1)
            calls = list(rec.calls)
            g = {
                "section_id": plan.section_id,
                "title": plan.title,
                "items": len(plan.items),
                "batches": batches,
                "state": run_res.state,
                "paragraphs": len(run_res.paragraphs),
                "rejected": len(run_res.rejected),
                "reject_reasons": sorted({r.get("reason", "") for r in run_res.rejected}),
                "attempts": run_res.attempts,
                "error": run_res.error,
                "wall_s": wall,
                "calls": len(calls),
                "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                "completion_tokens": sum(c["completion_tokens"] for c in calls),
                "call_detail": calls,
                "paragraph_sources_ok": all(p.sources for p in run_res.paragraphs),
                "paras_per_item": round(len(run_res.paragraphs) / max(1, len(plan.items)), 2),
            }
            gen_rows.append(g)
            print(f"- {plan.section_id} items={len(plan.items)} batches={batches} "
                  f"state={run_res.state} paragraphs={len(run_res.paragraphs)} rejected={len(run_res.rejected)} "
                  f"wall={wall}s calls={len(calls)} prompt={g['prompt_tokens']} "
                  f"completion={g['completion_tokens']} err={run_res.error or '-'}")
            for i, call in enumerate(calls, 1):
                print(f"    call {i}: {call['elapsed_s']}s prompt={call['prompt_tokens']} "
                      f"completion={call['completion_tokens']} reply={call['reply_chars']}字 err={call['error']}")
    finally:
        await client.aclose()

    merged = merge(result)
    extract_wall = round(sum(r["wall_s"] for r in rows), 1)
    gen_wall = round(sum(g["wall_s"] for g in gen_rows), 1)
    summary = {
        "pdf": str(args.pdf),
        "pages": doc.page_count,
        "model": s.llm_model,
        "base_url": s.llm_base_url,
        "thinking_default": s.llm_enable_thinking,
        "max_chars": args.max_chars,
        "max_retries": s.max_retries,
        "gen_batch_size": s.gen_batch_size,
        "concurrency": args.concurrency,
        "budget": args.budget,
        "calls_used": budget.used,
        "stopped": stopped,
        "chunks_total_in_doc": len(build_chunks(doc, max_chars=args.max_chars)),
        "extract": {
            "blocks": len(rows),
            "wall_s": extract_wall,
            "wall_sum_s": extract_wall,
            "attempts": sum(r["attempts"] for r in rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "items": sum(r["items"] for r in rows),
            "needs_review": sum(r["needs_review"] for r in rows),
            "ok_blocks": sum(1 for r in rows if r["ok"]),
            "rows": rows,
        },
        "citations": {
            "total": cite.total,
            "ok": cite.ok,
            "per_kind": cite.per_kind,
            "problems": [p.line() for p in cite.problems][:20],
        },
        "merged": {"items": len(merged.items())},
        "generate": {
            "sections": len(gen_rows),
            "wall_s": gen_wall,
            "paragraphs": sum(g["paragraphs"] for g in gen_rows),
            "rejected": sum(g["rejected"] for g in gen_rows),
            "prompt_tokens": sum(g["prompt_tokens"] for g in gen_rows),
            "completion_tokens": sum(g["completion_tokens"] for g in gen_rows),
            "calls": sum(g["calls"] for g in gen_rows),
            "rows": gen_rows,
        },
    }
    print("\n" + "=" * 78)
    print(f"合计：{budget.used}/{budget.limit} 次真实调用"
          + (f"（提前停止：{stopped}）" if stopped else ""))
    print(f"抽取 {len(rows)} 块 / {summary['extract']['attempts']} 次尝试 / {extract_wall}s / "
          f"prompt {summary['extract']['prompt_tokens']} + completion {summary['extract']['completion_tokens']} tok / "
          f"条目 {summary['extract']['items']}（needs_review {summary['extract']['needs_review']}）")
    print(f"引用可回查（citations.check_all）：{cite.ok}/{cite.total}  明细 {cite.per_kind}")
    print(f"生成 {len(gen_rows)} 节 / {summary['generate']['paragraphs']} 段 / "
          f"{summary['generate']['calls']} 次调用 / {gen_wall}s / "
          f"prompt {summary['generate']['prompt_tokens']} + completion {summary['generate']['completion_tokens']} tok")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**summary, "reports": [asdict(r) for r in result.reports]},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细：{p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--chunks", default="", help="页窗口，逗号分隔，如 p2-2,p50-50（--reuse-extract 时可省）")
    ap.add_argument("--gen-sections", type=int, default=3)
    ap.add_argument("--budget", type=int, default=14, help="真实调用硬上限（含重试与生成）")
    ap.add_argument("--reuse-extract", default=None,
                    help="复用已落盘的抽取结果（extract JSON），只跑生成：0 次抽取调用")
    ap.add_argument("--dump-extract", default=None, help="把本轮的抽取结果（含条目）落盘，供 --reuse-extract 复用")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--max-chars", type=int, default=2000)
    ap.add_argument("--model", default=None, help="覆盖模型名（直连供应商不带网关前缀）")
    ap.add_argument("--creds", default=str(DEFAULT_CREDS))
    ap.add_argument("--provider-name", default="Qwen (China)")
    ap.add_argument("--timeout-s", type=float, default=None)
    ap.add_argument("--out", default=None)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())