import sys, asyncio, time
sys.path.insert(0, "src")
from bidpilot.sections import load_tender
from bidpilot.extract import build_chunks, extract_chunk
from bidpilot.llm import ChatClient

async def main():
    doc = load_tender("evals/datasets/tender_jiangsu.pdf")
    cs = build_chunks(doc)
    c = [x for x in cs if x.section_kind == "evaluation"][0]
    llm = ChatClient()
    t0 = time.time()
    batch, rep = await extract_chunk(llm, doc, c, max_retries=2)
    print("elapsed", round(time.time()-t0,1), "ok", rep.ok, "attempts", rep.attempts)
    print("errors", rep.errors[:6])
    print("counts", rep.counts)
    for it in batch.scoring_items:
        print(f"  S {it.no!r} {it.item!r} score={it.max_score} p{it.page} rv={it.needs_review} {it.quote[:40]!r}")
    for it in batch.disqualifications:
        print(f"  D [{it.category.value}] p{it.page} rv={it.needs_review} {it.clause_text[:40]!r}")
    await llm.aclose()

asyncio.run(main())
