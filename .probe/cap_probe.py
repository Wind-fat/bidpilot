import sys, math
sys.path.insert(0, "src")
from documind.index import tokenize
from bidpilot.capabilities import load_capabilities, CapabilityIndex, Requirement
caps, _ = load_capabilities()
idx = CapabilityIndex(caps)
qs = [
 "VR 头盔及资源设备硬件，支持全景视频播放与语音讲解",
 "提供 7×12 小时热线服务，本地团队 24 小时内上门",
 "危险化学品道路运输经营许可证及罐式集装箱检验报告",
 "电子书借阅机系统软件著作权证书",
 "ISO9001、ISO27001 认证证书",
 "课堂考勤人脸识别终端，支持口罩识别",
]
b = idx._index
maxidf = math.log(1 + (b.n + 0.5) / 0.5)
for q in qs:
    hits = idx.search(q, 3)
    if not hits: print(q[:20], "NO HIT"); continue
    cap, score = hits[0]
    qt = set(tokenize(q)); ct = set(tokenize(cap.text))
    matched = qt & ct
    denom = sum(b.idf.get(t, maxidf) for t in qt)
    cov = sum(b.idf.get(t, maxidf) for t in matched) / denom if denom else 0
    print(f"{q[:28]:30s} -> {cap.id:26s} bm25={score:6.2f} cov={cov:.2f} norm={1-math.exp(-score/12):.2f}")
