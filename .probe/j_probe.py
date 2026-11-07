import sys
sys.path.insert(0, "src")
from documind.parsing import parse_file
d = parse_file("evals/datasets/tender_jiangsu.pdf")
for k in ["废标","无效投标","无效标","否决","资格审查","实质性"]:
    print(k, [i+1 for i,p in enumerate(d.pages) if k in p])
print("--- p24 ---"); print(d.pages[23][:500])
print("--- p27 ---"); print(d.pages[26][:400])
print("--- p5 ---"); print(d.pages[4][:700])
