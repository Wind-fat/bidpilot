#!/usr/bin/env python
"""低产阈值敏感性回归（**零 LLM**）：四个 `BIDPILOT_LOW_YIELD_*` 各自改小/改大，判定结果必须真的变。

两组数据：
- 真实：`out/real_provider/jiangsu_extract.json`（已落盘的真实抽取计数 / 14 块 / 不重跑模型，
  正文长度与「表格/清单型」判定按本次实际分块补齐，与 `pipeline.refresh_chunk_yields` 同一条路径）；
- 人工构造：每个变量都有**专门**的块，保证「只改这一个」时判定集合必变 —— 真实语料上
  `BIDPILOT_LOW_YIELD_ABS` 改小不会变（该语料没有低于 2.0 的结构化块），这不是 bug 而是语料事实，
  所以敏感性的证明必须带上构造块，否则「可配」只是说说。

用法：
    ./.venv/Scripts/python.exe scripts/low_yield_sensitivity.py
    ./.venv/Scripts/python.exe scripts/low_yield_sensitivity.py --json out/low_yield_sensitivity.json
退出码 0 = 八个方向（四变量 × 改小/改大）全部改变了判定集合；1 = 有方向没变化。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bidpilot.extract import (  # noqa: E402
    LOW_YIELD_ENV,
    ChunkReport,
    build_chunks,
    is_structured_body,
    load_extract,
    low_yield_basis,
    low_yield_thresholds,
    mark_low_yield,
)
from bidpilot.sections import load_tender  # noqa: E402

PDF = ROOT / "evals" / "datasets" / "tender_jiangsu.pdf"
REAL_JSON = ROOT / "out" / "real_provider" / "jiangsu_extract.json"

# (变量键, 取值, 方向说明)。两套数据各用一组取值：
# 构造块两个方向都有专门的样本，所以四变量 × 改小/改大 全部能翻；
# 真实语料只有 1 块低产（p26-28 命中同章均值比例），它的数字只落在这四个旋钮的一侧，
# 因此每个变量在真实数据上只有**一个**方向有翻转对象 —— 取最贴近该块数字的极值来证明它确实生效。
CASES = {
    "synthetic": [
        ("abs", 1.0, "lo"),
        ("abs", 2.5, "hi"),
        ("peer_ratio", 0.2, "lo"),
        ("peer_ratio", 0.5, "hi"),
        ("min_chars", 100, "lo"),
        ("min_chars", 1000, "hi"),
        ("min_peers", 1, "lo"),
        ("min_peers", 3, "hi"),
    ],
    "real": [
        ("abs", 1.0, "lo"),        # 本语料没有 < 2.0 的结构化块：改小无翻转对象
        ("abs", 12.0, "hi"),       # p23-25（11.41）被拉进来
        ("peer_ratio", 0.2, "lo"),  # p26-28（3.97）脱离 12.17 × 0.2 = 2.43
        ("peer_ratio", 1.0, "hi"),  # 无新增：同章没有「低于均值」的其它块
        ("min_chars", 100, "lo"),   # 本语料最短块 1261 字：改小无翻转对象
        ("min_chars", 1300, "hi"),  # p26-28（1261 字）不参评
        ("min_peers", 1, "lo"),     # 同章已有 5 个兄弟块：改小无影响
        ("min_peers", 6, "hi"),     # p26-28 只有 5 个兄弟块 → 均值比例判据不适用
    ],
}


def _report(chunk_id: str, items: int, body_chars: int, structured: bool = True) -> ChunkReport:
    return ChunkReport(
        chunk_id=chunk_id,
        pages=(1, 1),
        tasks=["tech_requirements"],
        attempts=1,
        ok=True,
        counts={"tech_requirements": items},
        body_chars=body_chars,
        structured=structured,
    )


def synthetic_reports() -> list[ChunkReport]:
    """每个变量一块「只受它控制」的样本；数字故意写成整比例，便于手算复核。

    chL：x=1/800（1.25）只命中绝对下限（同章均值 3.75 × 1/3 = 1.25，不满足「严格小于」）
    chM：u=2/1000（2.0）默认干净；绝对下限抬到 2.5 才命中
    chP：p=5/2000（2.5）高于绝对下限，只命中同章均值比例（10 × 1/3 = 3.33）
    chQ：s=9/2500（3.6）默认干净；比例抬到 0.5（cut 5.0）才命中
    chR：m=6/2000（3.0）只有 1 个兄弟块（默认 min_peers=2 → 不判）；min_peers=1 时命中
    chS：low=1/300（3.33）正文 300 字，默认 min_chars=400 直接不参评；min_chars=100 时命中
    """
    return [
        _report("chL#x", 1, 800),
        _report("chL#y", 3, 800),
        _report("chL#z", 3, 800),
        _report("chM#u", 2, 1000),
        _report("chM#v", 4, 1000),
        _report("chM#w", 4, 1000),
        _report("chP#p", 5, 2000),
        _report("chP#q2", 20, 2000),
        _report("chP#r2", 20, 2000),
        _report("chQ#s", 9, 2500),
        _report("chQ#t1", 10, 1000),
        _report("chQ#t2", 10, 1000),
        _report("chR#m", 6, 2000),
        _report("chR#n", 20, 2000),
        _report("chS#low", 1, 300),
        _report("chS#big", 5, 300),
        _report("chS#big2", 5, 300),
    ]


def real_reports(extract_json: Path = REAL_JSON, pdf: Path = PDF) -> list[ChunkReport] | None:
    """已落盘的真实抽取 + 本次实际分块补齐正文长度/形态（零模型调用）。"""
    if not (extract_json.exists() and pdf.exists()):
        return None
    tender = load_tender(pdf)
    chunks = {c.id: c for c in build_chunks(tender)}
    res = load_extract(json.loads(extract_json.read_text(encoding="utf-8")))
    out: list[ChunkReport] = []
    for r in res.reports:
        chunk = chunks.get(r.chunk_id)
        if chunk is None:
            continue
        r.body_chars = len(chunk.body)
        r.structured = is_structured_body(chunk.body)
        out.append(r)
    return out


def _clone(reports: list[ChunkReport]) -> list[ChunkReport]:
    return [
        ChunkReport(
            chunk_id=r.chunk_id, pages=r.pages, tasks=list(r.tasks), attempts=r.attempts,
            ok=r.ok, counts=dict(r.counts), body_chars=r.body_chars, structured=r.structured,
        )
        for r in reports
    ]


def run_case(reports: list[ChunkReport], var: str | None = None, value: float | int | None = None) -> list[str]:
    """按环境变量跑一次判定（不传 var = 清空四个变量跑基线），返回被判低产的 chunk id。"""
    for name in LOW_YIELD_ENV.values():
        os.environ.pop(name, None)
    if var is not None:
        os.environ[LOW_YIELD_ENV[var]] = str(value)
    return [r.chunk_id for r in mark_low_yield(_clone(reports))]


def baseline_detail(reports: list[ChunkReport]) -> list[dict]:
    """基线判定的逐块依据（命中规则 + 对比基准），人肉核对误报/真漏时看这一段。"""
    for name in LOW_YIELD_ENV.values():  # 别把上一个 case 的 env 当基线（否则依据里的阈值会串台）
        os.environ.pop(name, None)
    fresh = _clone(reports)
    flagged = mark_low_yield(fresh)
    by_id = {r.chunk_id: r for r in fresh}
    return [
        {
            "chunk_id": f.chunk_id,
            "items": sum(f.counts.values()),
            "body_chars": f.body_chars,
            "yield_rate": f.yield_rate,
            "peer_mean": f.peer_mean,
            "peers": f.peer_count,
            "rule": low_yield_basis(by_id[f.chunk_id]),
        }
        for f in flagged
    ]


def run_suite(reports: list[ChunkReport], cases: list[tuple[str, float, str]]) -> dict:
    base = run_case(reports)
    rows = []
    for var, value, direction in cases:
        got = run_case(reports, var, value)
        rows.append(
            {
                "env": LOW_YIELD_ENV[var],
                "value": value,
                "direction": direction,
                "flagged": got,
                "added": [c for c in got if c not in base],
                "removed": [c for c in base if c not in got],
                "changed": got != base,
            }
        )
    return {"baseline": base, "cases": rows, "detail": baseline_detail(reports)}


def _print_suite(title: str, suite: dict) -> None:
    print(f"\n## {title}")
    print(f"基线阈值：{suite['thresholds']}")
    print(f"基线低产：{suite['baseline'] or '（无）'}")
    for row in suite["detail"]:
        print(f"  - {row['chunk_id']} {row['items']} 条/{row['body_chars']} 字 = {row['yield_rate']}"
              f" 条/千字符 | {row['rule']}")
    print(f"{'变量':38} {'方向':4} {'取值':>7}  判定结果  变化")
    for c in suite["cases"]:
        mark = "有" if c["changed"] else "**无**"
        delta = f"+{c['added']} -{c['removed']}" if c["changed"] else ""
        print(f"{c['env']:38} {c['direction']:4} {str(c['value']):>7}  "
              f"{c['flagged'] or '（无）'}  {mark} {delta}")


def _defaults() -> dict:
    """不受任何 `BIDPILOT_LOW_YIELD_*` 影响的默认阈值（打印用：别把上一个 case 的 env 当基线）。"""
    for name in LOW_YIELD_ENV.values():
        os.environ.pop(name, None)
    return low_yield_thresholds().as_dict()


def main() -> int:
    # Windows 控制台默认 cp1252/cp936，中文直接 print 会 UnicodeEncodeError（CI 上真撞过）。
    # 输出编码不该决定脚本成败：能改就改，改不了就让它用 backslashreplace 兜住。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 老 Python / 被重定向
            pass

    ap = argparse.ArgumentParser(description="低产阈值敏感性回归（零 LLM）")
    ap.add_argument("--json", type=Path, default=None, help="把结果写成 JSON（供 drift/回归测试读）")
    args = ap.parse_args()

    suites: dict[str, dict] = {}
    skipped: list[str] = []
    for name, reports in (("synthetic", synthetic_reports()), ("real", real_reports())):
        if reports is None:
            print(f"（跳过真实语料：缺少 {REAL_JSON} 或 {PDF}）")
            skipped.append(name)
            continue
        suite = run_suite(reports, CASES[name])
        suite["thresholds"] = _defaults()
        suites[name] = suite

    titles = {
        "synthetic": "人工构造块（每个旋钮有专属样本，两个方向都能翻）",
        "real": f"真实落盘语料 {REAL_JSON.relative_to(ROOT).as_posix()}（零模型调用）",
    }
    for name, suite in suites.items():
        _print_suite(titles[name], suite)

    # 判定：构造块必须 8/8 全翻；真实语料要求**每个变量**至少有一个方向能翻（本语料只有 1 块低产，
    # 另一个方向没有翻转对象，这是数据事实，不是失效）。
    syn = suites["synthetic"]["cases"]
    real = suites.get("real", {"cases": []})["cases"]
    # 真实语料缺席（CI 上 out/ 是 gitignore 的）不是"旋钮失效"：只跑构造块的 8/8。
    ok = all(c["changed"] for c in syn) and (
        not real
        or all(any(c["changed"] for c in real if c["env"] == env) for env in LOW_YIELD_ENV.values())
    )
    print(f"\n构造块：{sum(c['changed'] for c in syn)}/{len(syn)} 个方向改变了判定")
    if real:
        print(f"真实语料：{sum(c['changed'] for c in real)}/{len(real)} 个方向改变；"
              f"四个变量各自至少一个方向生效 → "
              f"{ {env: any(c['changed'] for c in real if c['env'] == env) for env in LOW_YIELD_ENV.values()} }")
    print(f"结论：{'阈值可配且真的会改判' if ok else '**有旋钮无效**'}"
          + (f"（跳过：{', '.join(skipped)}）" if skipped else ""))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"ok": ok, "defaults": _defaults(), "skipped": skipped, **suites},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 已写入 {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())