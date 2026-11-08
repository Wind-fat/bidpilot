"""低产阈值可配 + 判定依据可审计：四旋钮真的会改判，README 表不许与代码漂开。

上轮遗留：四个阈值是硬编码常量，只在 1 份语料（14 块 / 1261-1747 字/块）上校准过，
换标书或换 `max_chars` 时只能改代码。这里钉住三件事：
1. `BIDPILOT_LOW_YIELD_*` 在**每次判定时**生效（不是 import 时定死），写错了回落默认值；
2. 四个变量各自改小/改大都会改变判定结果（跑 `scripts/low_yield_sensitivity.py`，零 LLM：
   已落盘的 `out/real_provider/jiangsu_extract.json` + 人工构造块）；
3. README「低产阈值（可配）」表的默认值 = 代码常量，代码改了文档没改就红。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bidpilot.extract import (
    LOW_YIELD_ABS,
    LOW_YIELD_ENV,
    LOW_YIELD_MIN_CHARS,
    LOW_YIELD_MIN_PEERS,
    LOW_YIELD_PEER_RATIO,
    ChunkReport,
    low_yield_basis,
    low_yield_thresholds,
    mark_low_yield,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "low_yield_sensitivity.py"
README = ROOT / "README.md"


def _clear(monkeypatch) -> None:
    for name in LOW_YIELD_ENV.values():
        monkeypatch.delenv(name, raising=False)


def _lone(items: int, chars: int = 1000) -> list[ChunkReport]:
    """单块章节（没有兄弟块）：只可能命中绝对下限，用来单独验证一个旋钮。"""
    return [
        ChunkReport(
            chunk_id="only#p1-1", pages=(1, 1), tasks=["tech_requirements"], attempts=1,
            ok=True, counts={"tech_requirements": items}, body_chars=chars, structured=True,
        )
    ]


# --------------------------------------------------------------------------- 1) 环境变量生效


def test_defaults_match_constants(monkeypatch):
    _clear(monkeypatch)
    t = low_yield_thresholds()
    assert t.abs_rate == LOW_YIELD_ABS == 2.0
    assert t.peer_ratio == pytest.approx(LOW_YIELD_PEER_RATIO) and LOW_YIELD_PEER_RATIO == pytest.approx(1 / 3)
    assert t.min_chars == LOW_YIELD_MIN_CHARS == 400
    assert t.min_peers == LOW_YIELD_MIN_PEERS == 2
    assert LOW_YIELD_ENV == {
        "abs": "BIDPILOT_LOW_YIELD_ABS",
        "peer_ratio": "BIDPILOT_LOW_YIELD_PEER_RATIO",
        "min_chars": "BIDPILOT_LOW_YIELD_MIN_CHARS",
        "min_peers": "BIDPILOT_LOW_YIELD_MIN_PEERS",
    }


def test_env_changes_verdict_without_reimport(monkeypatch):
    """判定时现读 env：同一个进程里改完立刻生效（不是 import 时定死）。"""
    _clear(monkeypatch)
    reports = _lone(items=2)  # 2/1000 = 2.0 条/千字符：等于下限，不算「低于」
    assert mark_low_yield(reports) == [], "2.0 与下限相等，不该判低产"
    monkeypatch.setenv("BIDPILOT_LOW_YIELD_ABS", "2.5")
    flagged = mark_low_yield(reports)
    assert [r.chunk_id for r in flagged] == ["only#p1-1"]
    assert flagged[0].hit_abs is True and flagged[0].thresholds["abs"] == 2.5
    monkeypatch.setenv("BIDPILOT_LOW_YIELD_ABS", "1.0")
    assert mark_low_yield(reports) == []


@pytest.mark.parametrize(
    "name,raw,field,want",
    [
        ("BIDPILOT_LOW_YIELD_ABS", "abc", "abs_rate", LOW_YIELD_ABS),
        ("BIDPILOT_LOW_YIELD_MIN_CHARS", "", "min_chars", LOW_YIELD_MIN_CHARS),
        ("BIDPILOT_LOW_YIELD_MIN_PEERS", "2.9", "min_peers", 2),  # 按 int 截断，与 config._env_int 一致
        ("BIDPILOT_LOW_YIELD_PEER_RATIO", " 0.5 ", "peer_ratio", 0.5),  # 前后空格要能容忍
    ],
)
def test_env_parsing_is_forgiving(monkeypatch, name, raw, field, want):
    _clear(monkeypatch)
    monkeypatch.setenv(name, raw)
    assert getattr(low_yield_thresholds(), field) == pytest.approx(want)


def test_basis_names_rule_and_peer_baseline(monkeypatch):
    """判定依据要能看出「命中哪条规则」与「跟谁比」——人工判断误报/真漏就看这句。"""
    _clear(monkeypatch)
    reports = [
        ChunkReport(chunk_id="ch#a", pages=(1, 1), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 20}, body_chars=1000, structured=True),
        ChunkReport(chunk_id="ch#b", pages=(2, 2), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 20}, body_chars=1000, structured=True),
        # 2 条 / 1600 字 = 1.25：绝对下限（2.0）与同章均值（20 × 1/3 = 6.67）都命中
        ChunkReport(chunk_id="ch#c", pages=(3, 3), tasks=["tech_requirements"], ok=True,
                    counts={"tech_requirements": 2}, body_chars=1600, structured=True),
    ]
    flagged = mark_low_yield(reports)
    assert [r.chunk_id for r in flagged] == ["ch#c"]
    target = flagged[0]
    assert (target.hit_abs, target.hit_peer) == (True, True)
    assert (target.peer_mean, target.peer_count) == (20.0, 2)
    text = low_yield_basis(target)
    assert text.startswith("命中规则：")
    assert "低于绝对下限 2.0 条/千字符（实际 1.25）" in text
    assert "低于同章其它块均值 20.0 的 1/3（= 6.67 条/千字符）" in text
    assert "对比基准：同章 2 个兄弟块，均值 20.0 条/千字符" in text


def test_basis_says_why_peer_rule_was_not_applicable(monkeypatch):
    """没有兄弟块时必须说清「只按绝对下限判」，而不是留一个空基准让人误以为是 0 条。"""
    _clear(monkeypatch)
    target = mark_low_yield(_lone(items=1, chars=1000))[0]
    assert target.hit_abs is True and target.hit_peer is False
    assert "同章可用兄弟块仅 0 个（< 2，均值比例判据不适用）" in low_yield_basis(target)


def test_restoring_defaults_is_idempotent(monkeypatch):
    """反复判定（refresh 会重复调用）不许累加/残留上一次的依据。"""
    _clear(monkeypatch)
    reports = _lone(items=1)
    monkeypatch.setenv("BIDPILOT_LOW_YIELD_ABS", "5")
    flagged = mark_low_yield(reports)
    assert flagged and flagged[0].thresholds["abs"] == 5.0
    monkeypatch.delenv("BIDPILOT_LOW_YIELD_ABS")
    assert mark_low_yield(reports) != []
    assert reports[0].thresholds["abs"] == LOW_YIELD_ABS


# --------------------------------------------------------------------------- 2) 敏感性回归（零 LLM）


def _run_sensitivity(tmp_path: Path) -> dict:
    out = tmp_path / "sensitivity.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-3000:], proc.stderr[-2000:])
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.skipif(not SCRIPT.exists(), reason="缺少 scripts/low_yield_sensitivity.py")
def test_four_knobs_flip_verdict_both_directions(tmp_path):
    """构造块：四个变量 × 改小/改大 共 8 个方向，每个方向都必须改变判定集合。"""
    data = _run_sensitivity(tmp_path)
    assert data["ok"] is True
    syn = data["synthetic"]
    assert syn["baseline"] == ["chL#x", "chP#p"], syn["baseline"]
    assert len(syn["cases"]) == 8
    for case in syn["cases"]:
        assert case["changed"] is True, f"{case['env']} {case['direction']}={case['value']} 没改判"
    # 每个变量两个方向都有 —— 否则「可配」只覆盖了一半
    per_var = {env: {c["direction"] for c in syn["cases"] if c["env"] == env} for env in LOW_YIELD_ENV.values()}
    assert all(dirs == {"lo", "hi"} for dirs in per_var.values()), per_var


def test_real_corpus_regression_uses_landed_extract(tmp_path):
    """已落盘的真实语料（零模型调用）：基线仍是 p26-28，四个变量各自至少一个方向能改判。"""
    data = _run_sensitivity(tmp_path)
    if "real" not in data:
        pytest.skip("缺少 out/real_provider/jiangsu_extract.json 或真实 PDF 样本")
    real = data["real"]
    assert real["baseline"] == ["technical-p13-98d1227c#p26-28"], real["baseline"]
    detail = real["detail"][0]
    assert detail["yield_rate"] == pytest.approx(3.97)
    assert detail["peers"] == 5 and detail["peer_mean"] == pytest.approx(12.17)
    assert "低于同章其它块均值 12.17 的 1/3" in detail["rule"], detail["rule"]
    assert detail["rule"].endswith("对比基准：同章 5 个兄弟块，均值 12.17 条/千字符")
    for env in LOW_YIELD_ENV.values():
        assert any(c["changed"] for c in real["cases"] if c["env"] == env), f"{env} 在真实语料上完全没有生效"


# --------------------------------------------------------------------------- 3) README 表 drift


def _readme_table(readme: str) -> dict[str, str]:
    """取 README「低产阈值（可配）」表：变量名 → 默认值单元格。"""
    rows: dict[str, str] = {}
    for line in readme.splitlines():
        if not line.strip().startswith("| `BIDPILOT_LOW_YIELD_"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) >= 4, f"表行缺列（变量/默认值/含义/什么时候该调）：{line}"
        rows[cells[0].strip("` ")] = cells[1]
    return rows


def _cell_number(cell: str) -> float:
    """默认值单元格 → 数字：支持 `1/3（0.3333）`、`2.0`、`400`。"""
    frac = re.search(r"(\d+)\s*/\s*(\d+)", cell)
    if frac:
        return int(frac.group(1)) / int(frac.group(2))
    num = re.search(r"-?\d+(?:\.\d+)?", cell)
    assert num, f"默认值单元格里没有数字：{cell!r}"
    return float(num.group(0))


def test_readme_threshold_table_matches_code(monkeypatch):
    """README 表 = 代码常量（代码改了文档没改就红；变量名也钉住）。"""
    _clear(monkeypatch)
    readme = README.read_text(encoding="utf-8")
    table = _readme_table(readme)
    t = low_yield_thresholds()
    want = {
        "BIDPILOT_LOW_YIELD_ABS": t.abs_rate,
        "BIDPILOT_LOW_YIELD_PEER_RATIO": t.peer_ratio,
        "BIDPILOT_LOW_YIELD_MIN_CHARS": t.min_chars,
        "BIDPILOT_LOW_YIELD_MIN_PEERS": t.min_peers,
    }
    assert set(table) == set(want), f"README 表的变量集合与 LOW_YIELD_ENV 不一致：{sorted(table)}"
    for var, value in want.items():
        assert _cell_number(table[var]) == pytest.approx(value), (
            f"README 里 {var} 写的是 {table[var]!r}，代码默认值 {value}（改了阈值就要改表）"
        )


def test_readme_table_explains_when_to_tune(monkeypatch):
    """表格的四列都要有内容：「什么时候该调」不能留空/留占位符。"""
    readme = README.read_text(encoding="utf-8")
    for line in readme.splitlines():
        if not line.strip().startswith("| `BIDPILOT_LOW_YIELD_"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert cells[2] and cells[3], f"含义/何时调整列仍为空：{line}"
        assert len(cells[3]) >= 20, f"「什么时候该调」太笼统：{cells[3]!r}"
    assert "scripts/low_yield_sensitivity.py" in readme