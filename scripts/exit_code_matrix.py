#!/usr/bin/env python
"""退出码矩阵实跑：五种情形各跑一次离线流水线，打印「退出码 + docx 文件名 + 首段横幅」。

    ./.venv/Scripts/python.exe scripts/exit_code_matrix.py

为什么要有这个脚本：README 那张表是**给使用者看的契约**，但它描述的是
`pipeline.EXIT_PRIORITY` 的行为。光读代码看不出「同一份语料在五种情形下到底落什么名字、
首段是哪张横幅」，所以这里逐条实跑、逐条打印，输出可以直接和 README 表对行。

全程离线：LLM 用 FakeLLM（正常路径）或 DownLLM（网关挂掉），真实语料只用
`evals/datasets/tender_jiangsu.pdf` + `evals/fixtures/jiangsu_extract.json` 的等价夹具。
不写数据库、不联网、产物落在临时目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docx import Document  # noqa: E402

from bidpilot.config import Settings  # noqa: E402
from bidpilot.export import (  # noqa: E402
    BANNER_EXAMPLE_CAPS,
    BANNER_FAILED,
    BANNER_MOCK,
    BANNER_STAGED,
)
from bidpilot.extract import ExtractResult, dump_extract  # noqa: E402
from bidpilot.llm import FakeLLM, LLMError  # noqa: E402
from bidpilot.pipeline import (  # noqa: E402
    EXIT_UNREADABLE,
    format_degradation_report,
    run_pipeline,
)
from bidpilot.schemas import TechRequirement  # noqa: E402
from bidpilot.sections import TenderParseError  # noqa: E402

JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"


class DownLLM:
    """网关挂掉：任何调用都抛 LLMError（不联网）。"""

    async def complete(self, *a, **kw):
        raise LLMError("connection refused")


def _extract_file(tmp_path: Path) -> Path:
    res = ExtractResult(title="测试招标文件", source=str(JIANGSU))
    res.tech_requirements = [
        TechRequirement(
            id="T001", no="3.1", text="设备应支持 4K 全景视频播放，单眼分辨率不低于 1440x1600",
            page=13, page_end=13, quote="设备应支持 4K 全景视频播放", section="第三章 采购需求",
        ),
        TechRequirement(
            id="T002", no="3.2", text="提供 7×12 小时热线服务，2 小时响应",
            page=14, page_end=14, quote="提供 7×12 小时热线服务", section="第三章 采购需求",
        ),
    ]
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(dump_extract(res), ensure_ascii=False), encoding="utf-8")
    return p


def _real_caps(tmp_path: Path) -> Path:
    p = tmp_path / "caps_real.json"
    p.write_text(
        json.dumps(
            {"capabilities": [{"id": "cap-vr", "type": "产品", "name": "VR 智慧教室",
                               "summary": "VR 头盔与全景播放", "tags": ["VR"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


BANNERS = {BANNER_FAILED: "BANNER_FAILED（整节失败/废稿）",
           BANNER_STAGED: "BANNER_STAGED（--no-generate 自证）",
           BANNER_MOCK: "BANNER_MOCK（模板应答）",
           BANNER_EXAMPLE_CAPS: "BANNER_EXAMPLE_CAPS（示例能力库）"}


def _first_banner(path: Path) -> str:
    """首页第一条**横幅**（不是首段正文）：打开文件的人第一眼看到的就是它。"""
    texts = [p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()]
    for i, t in enumerate(texts):
        if t in BANNERS:
            return f"第 {i + 1} 段：{BANNERS[t]}"
    return "(无横幅)"


def _banner_texts(path: Path) -> str:
    texts = [p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()]
    return " + ".join(BANNERS[t].split("（")[0] for t in texts if t in BANNERS) or "(无)"


def _blank_pdf(path: Path) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


async def _scenario(name: str, *, tmp: Path, **kwargs) -> dict:
    try:
        res = await run_pipeline(JIANGSU, save_db=False, out_dir=tmp, extract_json=_extract_file(tmp), **kwargs)
    except TenderParseError as exc:
        return {"case": name, "exit": EXIT_UNREADABLE, "file": "(未产出)",
                "first": f"(不落文件) {str(exc).splitlines()[0][:50]}", "banners": "-"}
    return {
        "case": name,
        "exit": res.exit_code,
        "file": res.docx_path.name,
        "first": _first_banner(res.docx_path),
        "banners": _banner_texts(res.docx_path),
        "report": format_degradation_report(res),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    base = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="bidpilot-exit-"))

    tmp1 = base / "1-nogen-example"
    tmp2 = base / "2-mock-example"
    tmp3 = base / "3-failed-example"
    tmp4 = base / "4-unreadable"
    tmp5 = base / "5-clean"
    for d in (tmp1, tmp2, tmp3, tmp4, tmp5):
        d.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    # ① 仅示例库：分阶段跑，不生成应答
    rows.append(await _scenario("① 仅示例库 --no-generate", tmp=tmp1,
                                settings=Settings(allow_mock=False, out_dir=tmp1), generate=False))
    # ② 模板降级 + 示例库
    rows.append(await _scenario("② 模板降级 + 示例库", tmp=tmp2,
                                settings=Settings(allow_mock=True, out_dir=tmp2), llm=DownLLM(), generate=True))
    # ③ 整节失败 + 示例库（未开模板降级）
    rows.append(await _scenario("③ 整节失败 + 示例库", tmp=tmp3,
                                settings=Settings(allow_mock=False, out_dir=tmp3), llm=DownLLM(), generate=True))
    # ④ 不可解析：走真实解析器，用没有文本层的空白 PDF
    blank = _blank_pdf(tmp4 / "扫描件.pdf")
    try:
        await run_pipeline(blank, settings=Settings(allow_mock=False, out_dir=tmp4), save_db=False, out_dir=tmp4)
        rows.append({"case": "④ 不可解析", "exit": 0, "file": "(未产出)",
                     "first": "(未抛错——异常！)", "banners": "-"})
    except TenderParseError as exc:
        rows.append({"case": "④ 不可解析", "exit": EXIT_UNREADABLE, "file": "(未产出)",
                     "first": f"(不落文件) {str(exc).splitlines()[0][:50]}", "banners": "-"})
    # ⑤ 全正常：真实（非示例）能力库 + FakeLLM
    fake = FakeLLM(
        [json.dumps({"paragraphs": [
            {"text": "我方以 VR 教室方案响应。", "sources": ["T001"], "kind": "技术响应"},
            {"text": "我方提供 7×12 热线服务。", "sources": ["T002"], "kind": "技术响应"},
        ]}, ensure_ascii=False)],
        repeat_last=True,
    )
    rows.append(await _scenario("⑤ 全正常", tmp=tmp5,
                                settings=Settings(allow_mock=False, out_dir=tmp5, capability_file=_real_caps(tmp5)),
                                llm=fake, generate=True))

    print("| 情形 | 退出码 | docx 文件名 | 首页第一条横幅 | 全部横幅 |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['case']} | {r['exit']} | `{r['file']}` | {r['first']} | {r.get('banners', '-')} |")
    print(f"\n产物目录：{base}")
    for r in rows:
        if r.get("report"):
            print("\n" + "\n".join(r["report"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))