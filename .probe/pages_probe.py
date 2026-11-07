import sys
sys.path.insert(0, "src")
from documind.parsing import parse_file
d = parse_file("evals/datasets/tender_jiangsu.pdf")
lens = [len(p) for p in d.pages]
print("page char lens:", lens)
for no in (11, 12, 13, 14):
    print(f"----- PAGE {no} -----")
    print(d.pages[no-1][:2200])
