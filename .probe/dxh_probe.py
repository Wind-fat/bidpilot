import sys, re
sys.path.insert(0, "src")
from documind.parsing import parse_file
d = parse_file("evals/datasets/tender_dxh.pdf")
print("pages", d.page_count, "avg len", sum(map(len,d.pages))//d.page_count)
kw = ["废标", "无效投标", "无效标", "否决投标", "投标人须知", "评标办法", "采购需求"]
for k in kw:
    hits = [i+1 for i,p in enumerate(d.pages) if k in p]
    print(k, hits[:25])
print("----- PAGE 46 -----")
print(d.pages[45][:1800])
print("----- PAGE 31 -----")
print(d.pages[30][:1200])
