"""测试夹具：一份合成的招标文件（不依赖网络/LLM），一份真实 PDF（用于章节识别）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from documind.parsing import Paragraph, ParsedDocument  # noqa: E402

from bidpilot.sections import TenderDoc, build_sections, scan_disqualifications  # noqa: E402

JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"

PAGE1 = "\n".join(
    [
        "第一章 投标人须知",
        "1.1 投标文件未按招标文件要求密封的，作无效投标处理。",
        "第二章 评标办法",
        "2.1 价格分 40 分，采用低价优先法计算。",
        "2.2 技术参数分 20 分，打★号指标负偏离将作为无效投标。",
    ]
)
PAGE2 = "\n".join(
    [
        "第三章 采购需求",
        "3.1 设备应支持 4K 全景视频播放，单眼分辨率不低于 1440x1600。",
        "3.2 提供 7×12 小时热线服务，2 小时响应。",
        "第四章 合同主要条款",
    ]
)


def _para(text: str, page: int, start: int, level: int = 0, section_path: str = "") -> Paragraph:
    return Paragraph(text=text, page=page, start=start, end=start + len(text), level=level, section_path=section_path)


@pytest.fixture(scope="session")
def synthetic_doc() -> TenderDoc:
    paragraphs: list[Paragraph] = []
    for page_no, page_text, path_map in (
        (1, PAGE1, {
            "第一章 投标人须知": "第一章 投标人须知",
            "第二章 评标办法": "第二章 评标办法",
        }),
        (2, PAGE2, {
            "第三章 采购需求": "第三章 采购需求",
            "第四章 合同主要条款": "第四章 合同主要条款",
        }),
    ):
        cursor = 0
        current = ""
        for line in page_text.split("\n"):
            level = 1 if line.startswith("第") and "章" in line[:6] else 0
            if line in path_map:
                current = path_map[line]
            paragraphs.append(_para(line, page_no, cursor, level, current))
            cursor += len(line) + 1
    parsed = ParsedDocument(title="测试招标文件", filename="synthetic.pdf", pages=[PAGE1, PAGE2], paragraphs=paragraphs)
    sections = build_sections(parsed)
    return TenderDoc(
        path=Path("synthetic.pdf"),
        parsed=parsed,
        sections=sections,
        disq_hits=scan_disqualifications(parsed, sections),
    )


@pytest.fixture(scope="session")
def real_doc() -> TenderDoc:
    if not JIANGSU.exists():
        pytest.skip("缺少真实招标文件样本")
    from bidpilot.sections import load_tender

    return load_tender(JIANGSU)