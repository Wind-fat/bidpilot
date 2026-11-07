import sys, asyncio
sys.path.insert(0, "src")
from bidpilot.sections import load_tender
from bidpilot.extract import build_chunks
for f in ["evals/datasets/tender_jiangsu.pdf"]:
    doc = load_tender(f)
    print(doc.stats()["sections"])
    print("disq hits", len(doc.disq_hits), [(h.page, h.text[:30]) for h in doc.disq_hits[:5]])
    cs = build_chunks(doc)
    print("chunks", len(cs))
    for c in cs:
        print(f"  {c.section_kind:20s} p{c.page_start}-{c.page_end} chars={len(c.body):5d} tasks={c.tasks}")
