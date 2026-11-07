"""测试夹具：一份合成的招标文件（走真实解析器，不依赖网络/LLM）、一份真实 PDF。

ARCH-REVIEW P1-06：这里不再手搓 `Paragraph(...)` / `ParsedDocument(...)`。
夹具只提供**原文**（一份 markdown），结构（页 / 段 / 页内字符偏移）全部由
documind 的 `parse_file` 现场解析出来——否则夹具字段顺序一改，测试就对不上真实解析器。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.sections import TenderDoc, load_tender  # noqa: E402

JIANGSU = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"

# documind 的 MarkdownParser 在页累计超过 4000 字时开新页。把第 2 章的评标细则写厚一点，
# 就能让「第一章/第二章 在第 1 页，第三章/第四章 在第 2 页」由解析器自己分出来，
# 而不是夹具假装的——这正是 P1-07 要的「同一页起两章」场景。
PAGE_SPLIT_THRESHOLD = 4000


def _eval_clauses(min_chars: int = PAGE_SPLIT_THRESHOLD + 400) -> list[str]:
    """把第一页撑过 4000 字阈值（内容只是分页配重，但保持了招标文件的语域）。"""
    out: list[str] = []
    i = 3
    while len("\n".join(out)) < min_chars:
        out.append(
            f"2.{i} 评标细则第 {i} 项：证明材料须加盖投标人公章，缺失或不可辨认的本项不得分，"
            "情节严重的按无效投标处理。"
        )
        i += 1
    return out


def synthetic_markdown() -> str:
    blocks = [
        "\n".join(
            [
                "# 第一章 投标人须知",
                "1.1 投标文件未按招标文件要求密封的，作无效投标处理。",
            ]
        ),
        "\n".join(
            [
                "# 第二章 评标办法",
                "2.1 价格分 40 分，采用低价优先法计算。",
                "2.2 技术参数分 20 分，打★号指标负偏离将作为无效投标。",
                *_eval_clauses(),
            ]
        ),
        "\n".join(
            [
                "# 第三章 采购需求",
                "3.1 设备应支持 4K 全景视频播放，单眼分辨率不低于 1440x1600。",
                "3.2 提供 7×12 小时热线服务，2 小时响应。",
            ]
        ),
        "\n".join(
            [
                "# 第四章 合同主要条款",
                "4.1 合同签订后 30 日内交货，逾期按日万分之五计违约金。",
            ]
        ),
    ]
    return "\n\n".join(blocks)


@pytest.fixture(scope="session")
def synthetic_path(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("synthetic") / "synthetic_tender.md"
    p.write_text(synthetic_markdown(), encoding="utf-8")
    return p


@pytest.fixture(scope="session")
def synthetic_doc(synthetic_path: Path) -> TenderDoc:
    return load_tender(synthetic_path)


@pytest.fixture(scope="session")
def real_doc() -> TenderDoc:
    if not JIANGSU.exists():
        pytest.skip("缺少真实招标文件样本")
    return load_tender(JIANGSU)