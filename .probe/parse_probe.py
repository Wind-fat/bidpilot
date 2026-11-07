import sys
sys.path.insert(0, "src")
from documind.parsing import parse_file
for f in ["evals/datasets/tender_jiangsu.pdf", "evals/datasets/tender_dxh.pdf"]:
    d = parse_file(f)
    print("="*20, f, "pages", d.page_count, "paras", len(d.paragraphs))
    print("title:", d.title[:80])
    heads = [p for p in d.paragraphs if p.level >= 1]
    print("headings:", len(heads))
    for p in heads[:45]:
        print(f"  L{p.level} p{p.page} [{len(p.text)}] {p.text[:60]}")
