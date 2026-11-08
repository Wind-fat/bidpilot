#!/usr/bin/env python
"""200 页性能实测：把真实 41 页招标文件循环拼成 200 页，只跑「解析 + 章节 + 能力矩阵」。

    ./.venv/Scripts/python.exe scripts/bench_200p.py

口径（写进 docs/PERF.md，别混着比）：
- **解析+切章** = `load_tender()` 一次：pypdf 抽文 → 分页/分段 → 章节切分 → 废标扫描；
- **抽取/矩阵/导出** = `run_pipeline(generate=False)` 里 parse 之后的部分（复用已落盘的抽取 JSON，
  不调 LLM）；它内部会**再解析一次**文档，所以两段之和才是"一次完整跑"的耗时；
- 全程离线、0 次 LLM 调用；生成阶段（--no-generate）不在本口径内。

产物：out/perf/tender_200p.pdf + out/perf/bench_200p.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pypdf import PdfReader  # noqa: E402

from bidpilot.extract import build_chunks  # noqa: E402
from bidpilot.pipeline import run_pipeline  # noqa: E402
from bidpilot.sections import build_sections, load_tender, scan_disqualifications  # noqa: E402


def build_pdf(src: Path, dst: Path, pages: int) -> Path:
    """把 src 的页循环复制到 pages 页（本地、离线，不引入新依赖：pypdf 已在依赖里）。"""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(src))
    source_pages = len(reader.pages)
    if source_pages == 0:
        raise SystemExit(f"源文件没有页：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for i in range(pages):
        writer.add_page(reader.pages[i % source_pages])
    with dst.open("wb") as fh:
        writer.write(fh)
    return dst


def machine() -> dict:
    import shutil

    total_ram = 0.0
    try:  # 只在 Windows 下可用，其它平台留 null 不影响结论
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            total_ram = round(st.ullTotalPhys / (1024**3), 1)
    except Exception:  # pragma: no cover - 非 Windows
        pass
    disk = shutil.disk_usage(str(ROOT))
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "ram_gb": total_ram or None,
        "disk_total_gb": round(disk.total / (1024**3), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="BidPilot 200 页性能实测")
    ap.add_argument("--pdf", default=str(ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"))
    ap.add_argument("--extract-json", default=str(ROOT / "out" / "jiangsu_extract.json"))
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--out-dir", default=str(ROOT / "out" / "perf"))
    ap.add_argument("--repeat", type=int, default=1, help="重复跑几次取最好成绩")
    ap.add_argument(
        "--no-relocate",
        action="store_true",
        help="不复用抽取结果里的 quote 重新定位（抽取 JSON 来自另一份语料时必须关，否则会被判 needs_review）",
    )
    args = ap.parse_args()

    src = Path(args.pdf)
    extract = Path(args.extract_json)
    if not src.exists():
        raise SystemExit(f"缺少源 PDF：{src}")
    if not extract.exists():
        raise SystemExit(f"缺少抽取结果 JSON（避免调用 LLM）：{extract}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    big = out_dir / f"tender_{args.pages}p.pdf" if src.suffix.lower() == ".pdf" else src

    # 源可以是 PDF（不足 pages 页则循环拼页；已够页则原样测，
    # 免得把 204 页的 26 章语料截成 200 页），也可以是 documind 直接吃的 md/txt 长语料（原样测）。
    is_pdf = src.suffix.lower() == ".pdf"
    src_pages = len(PdfReader(str(src)).pages) if is_pdf else 0
    t0 = time.perf_counter()
    if is_pdf and src_pages < args.pages:
        build_pdf(src, big, args.pages)
    else:
        big = src
    build_s = round(time.perf_counter() - t0, 2)
    relocate = is_pdf and not args.no_relocate

    runs = []
    for _ in range(max(1, args.repeat)):
        t1 = time.perf_counter()
        doc = load_tender(big)
        parse_s = round(time.perf_counter() - t1, 2)

        # 拆开看：解析（pypdf 抽文 + 分页分段）与切章各自花多少
        t_split = time.perf_counter()
        sections = build_sections(doc.parsed)
        build_sections_s = round(time.perf_counter() - t_split, 3)
        t_scan = time.perf_counter()
        hits = scan_disqualifications(doc.parsed, sections)
        scan_s = round(time.perf_counter() - t_scan, 3)

        # 切章之后立刻要按章分块喂模型：章节数放大后真正会涨的是这一段（只算切块，不调 LLM）
        t_chunk = time.perf_counter()
        chunks = build_chunks(doc)
        chunk_s = round(time.perf_counter() - t_chunk, 3)

        t2 = time.perf_counter()
        res = asyncio.run(
            run_pipeline(
                big,
                generate=False,  # 不调 LLM
                save_db=False,
                out_dir=out_dir,
                extract_json=extract,
                relocate_spans=relocate,
            )
        )
        pipeline_s = round(time.perf_counter() - t2, 2)
        runs.append(
            {
                "parse_s": parse_s,
                "build_sections_s": build_sections_s,
                "scan_disq_s": scan_s,
                "build_chunks_s": chunk_s,
                "chunks": len(chunks),
                "pipeline_wall_s": pipeline_s,
                "pipeline_internal": dict(res.timing),
                "pages": doc.page_count,
                "paragraphs": len(doc.parsed.paragraphs),
                "sections": len(doc.sections),
                "disq_hits": len(doc.disq_hits),
                "disq_hits_second_pass": len(hits),
                "docx": res.docx_path.name,
                "exit_code": res.exit_code,
                "llm_calls": res.llm_usage.get("calls", 0),
            }
        )

    best = min(runs, key=lambda r: r["parse_s"] + r["pipeline_internal"]["total_s"])
    single_pass_total = round(best["parse_s"] + best["pipeline_internal"]["total_s"], 2)
    report = {
        "source_pdf": str(src),
        "source_pages": src_pages or None,
        "relocate": relocate,
        "built_pdf": str(big),
        "built_pages": best["pages"],
        "built_bytes": big.stat().st_size,
        "build_pdf_s": build_s,
        "machine": machine(),
        "runs": runs,
        "best": {
            "parse_s": best["parse_s"],
            "pipeline_s": best["pipeline_internal"]["total_s"],
            "single_pass_total_s": single_pass_total,
            "budget_s": 20 * 60,
            "budget_ratio": round(single_pass_total / (20 * 60), 4),
        },
    }
    (out_dir / "bench_200p.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 68)
    if is_pdf and src_pages < args.pages:
        print(f"拼接 PDF   ：{big}  {best['pages']} 页 / {report['built_bytes']} bytes（源 {report['source_pages']} 页 × "
              f"{best['pages'] / report['source_pages']:.2f} 循环）")
    elif is_pdf:
        print(f"真 PDF     ：{big}  {best['pages']} 页 / {report['built_bytes']} bytes（源 {report['source_pages']} 页，"
              "已够页，原样测不拼页）")
    else:
        print(f"长语料     ：{big}  {best['pages']} 页 / {report['built_bytes']} bytes（原样测，不拼页）")
    print(f"机器       ：{report['machine']['processor'] or report['machine']['machine']} / "
          f"{report['machine']['cpu_count']} 核 / {report['machine']['ram_gb']} GB RAM / "
          f"Python {report['machine']['python']}")
    print("-" * 68)
    for i, r in enumerate(runs, 1):
        print(f"第 {i} 次     ：解析+切章 {r['parse_s']}s（其中切章 {r['build_sections_s']}s / 废标扫描 {r['scan_disq_s']}s"
              f" / 分块 {r['build_chunks_s']}s）"
              f" | 抽取+矩阵+导出 {r['pipeline_internal']['total_s']}s "
              f"| 单次全流程 {round(r['parse_s'] + r['pipeline_internal']['total_s'], 2)}s "
              f"| 段落 {r['paragraphs']} / 章节 {r['sections']} / 分块 {r['chunks']} / 废标命中 {r['disq_hits']}"
              f" | LLM 调用 {r['llm_calls']} | 退出码 {r['exit_code']}")
    print("-" * 68)
    print(f"最好成绩   ：解析+切章 {best['parse_s']}s + 其余 {best['pipeline_internal']['total_s']}s "
          f"= {single_pass_total}s（预算 1200s，占 {report['best']['budget_ratio'] * 100:.2f}%）")
    print(f"明细       ：{out_dir / 'bench_200p.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())