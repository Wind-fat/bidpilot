#!/usr/bin/env python
"""只对指定页窗口的块跑**真实供应商**抽取，逐次尝试记录耗时 / token / 是否升级思考。

为什么不给 `run_pipeline.py` 加 `--only-chunks`：pipeline 只汇总到「块级 attempts + counts」，
拿不到每次尝试的墙钟与 token（`Usage` 只在 ChatClient 内部留痕）。这份脚本直接调
`extract.extract_chunk`，把 LLM 包一层记录器，成本可控（3 块 × 最多 3 次尝试），
且与生产路径共用同一套 prompt / 校验 / 重试升级逻辑。

    ./.venv/Scripts/python.exe scripts/real_chunks_probe.py \
        --pdf evals/datasets/tender_jiangsu.pdf --chunks p15-16,p23-25,p26-28 \
        --out out/real_chunks/probe.json

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

from bidpilot.config import Settings  # noqa: E402
from bidpilot.extract import (  # noqa: E402
    ExtractResult,
    build_chunks,
    extract_chunk,
    locate_quote_span,
    merge,
)
from bidpilot.llm import ChatClient, LLMError, LLMReply  # noqa: E402
from bidpilot.sections import load_tender  # noqa: E402

DEFAULT_CREDS = Path.home() / ".config" / "magpie" / "providers.json"


class RecordingLLM:
    """把每次 `complete` 的耗时 / token / thinking 记下来，其余行为原样透传。"""

    def __init__(self, inner: ChatClient) -> None:
        self.inner = inner
        self.calls: list[dict] = []

    async def complete(self, messages, *, json_mode=True, max_tokens=None, thinking=None) -> LLMReply:  # noqa: ANN001
        rec = {"thinking": thinking, "elapsed_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
               "reply_chars": 0, "reply_head": "", "error": None}
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
        # 存回复开头：「空数组」必须能自证（29 字就是 {"tech_requirements": []}），不能只看计数
        rec["reply_head"] = reply.text[:160]
        self.calls.append(rec)
        return reply


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings()
    if args.creds and Path(args.creds).exists():
        providers = json.loads(Path(args.creds).read_text(encoding="utf-8")).get("providers", [])
        hit = next((p for p in providers if p.get("name") == args.provider_name), None)
        if hit is None:
            raise SystemExit(f"providers.json 里没有 name == {args.provider_name!r} 的条目")
        s.llm_base_url = hit["chat"]
        s.llm_api_key = hit["key"]
    if args.timeout_s is not None:
        s.llm_timeout_s = args.timeout_s
    return s


def _quote_check(doc, items) -> dict:  # noqa: ANN001
    """独立复核：不信任条目自带的 quote_span，重新按 page(+page_end) 在原文里定位。"""
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


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--chunks", required=True, help="页窗口，逗号分隔，如 p15-16,p23-25")
    ap.add_argument("--max-chars", type=int, default=2000)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--out", default=None, help="把逐块/逐次尝试明细写成 JSON")
    ap.add_argument("--creds", default=str(DEFAULT_CREDS))
    ap.add_argument("--provider-name", default="Qwen (China)")
    ap.add_argument("--timeout-s", type=float, default=None, help="覆盖 Settings.llm_timeout_s")
    args = ap.parse_args()

    s = _settings(args)
    doc = load_tender(args.pdf)
    wanted = {w.strip().lstrip("p") for w in args.chunks.split(",") if w.strip()}
    chunks = [c for c in build_chunks(doc, max_chars=args.max_chars) if f"{c.page_start}-{c.page_end}" in wanted]
    if len(chunks) != len(wanted):
        raise SystemExit(f"只匹配到 {len(chunks)} 块 / 期望 {len(wanted)}：{[c.id for c in chunks]}")

    print(f"pdf={args.pdf} pages={doc.page_count} model={s.llm_model} base={s.llm_base_url}")
    print(f"timeout_s={s.llm_timeout_s} thinking_default={s.llm_enable_thinking} "
          f"chunks={len(chunks)} max_chars={args.max_chars}")

    client = ChatClient(s)
    result = ExtractResult(title=doc.title, source=str(doc.path))
    rows: list[dict] = []
    try:
        for chunk in chunks:
            rec = RecordingLLM(client)
            t0 = time.perf_counter()
            batch, report = await extract_chunk(rec, doc, chunk, max_retries=args.max_retries)
            wall = round(time.perf_counter() - t0, 1)
            items = [*batch.scoring_items, *batch.disqualifications, *batch.tech_requirements]
            check = _quote_check(doc, items)
            row = {
                "chunk_id": chunk.id,
                "pages": [chunk.page_start, chunk.page_end],
                "tasks": chunk.tasks,
                "body_chars": len(chunk.body),
                "attempts": report.attempts,
                "escalated": report.escalated,
                "ok": report.ok,
                "counts": report.counts,
                "items": len(items),
                "needs_review": sum(1 for i in items if i.needs_review),
                "quote_check": check,
                "items_detail": [
                    {"page": it.page, "page_end": getattr(it, "page_end", None), "quote": it.quote,
                     "label": getattr(it, "no", "") or getattr(it, "item", "") or getattr(it, "category", ""),
                     "needs_review": it.needs_review}
                    for it in items
                ],
                "wall_s": wall,
                "attempt_detail": rec.calls,
                "prompt_tokens": sum(c["prompt_tokens"] for c in rec.calls),
                "completion_tokens": sum(c["completion_tokens"] for c in rec.calls),
                "errors": report.errors,
            }
            rows.append(row)
            print("-" * 78)
            print(f"{chunk.id}  pages p{chunk.page_start}-{chunk.page_end}  body={len(chunk.body)} 字  wall={wall}s")
            for i, call in enumerate(rec.calls, 1):
                print(f"  attempt {i}: thinking={call['thinking']} {call['elapsed_s']}s "
                      f"prompt={call['prompt_tokens']} completion={call['completion_tokens']} "
                      f"reply={call['reply_chars']}字 err={call['error']}")
                print(f"    reply_head={call['reply_head'][:100]!r}")
            print(f"  attempts={report.attempts} escalated={report.escalated} ok={report.ok} counts={report.counts}")
            print(f"  items={len(items)} needs_review={row['needs_review']} "
                  f"quote回查={check['located']}/{check['total']}")
            result.scoring_items.extend(batch.scoring_items)
            result.disqualifications.extend(batch.disqualifications)
            result.tech_requirements.extend(batch.tech_requirements)
            result.reports.append(report)
    finally:
        await client.aclose()

    merged = merge(result)
    total_prompt = sum(r["prompt_tokens"] for r in rows)
    total_completion = sum(r["completion_tokens"] for r in rows)
    total_calls = sum(len(r["attempt_detail"]) for r in rows)
    total_wall = round(sum(r["wall_s"] for r in rows), 1)
    summary = {
        "pdf": str(args.pdf),
        "model": s.llm_model,
        "base_url": s.llm_base_url,
        "timeout_s": s.llm_timeout_s,
        "thinking_default": s.llm_enable_thinking,
        "max_chars": args.max_chars,
        "calls": total_calls,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "wall_s": total_wall,
        "merged_items": len(merged.items()),
        "merged_needs_review": sum(1 for i in merged.items() if i.needs_review),
        "rows": rows,
    }
    print("=" * 78)
    print(f"合计：{len(rows)} 块 / {total_calls} 次调用 / {total_wall}s / "
          f"prompt {total_prompt} tok / completion {total_completion} tok")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({**summary, "merged_reports": [asdict(r) for r in merged.reports]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"明细：{args.out}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))