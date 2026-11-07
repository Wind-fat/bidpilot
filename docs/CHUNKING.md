# 切块参数：文档说的数 vs 实测的数

**结论一句话**：本仓的抽取切块是「按章节分块 → 章节内按页装箱」，**相邻块重叠恒为 0**，
块长由 `max_chars` 与页边界共同决定；实测默认 `max_chars=2000` 时 14 块，块长 1261–1747。

## 1. 参数与算法

| 项 | 值 | 出处 |
|---|---|---|
| 每块字符上限 | `BIDPILOT_CHUNK_CHARS`，默认 **2000** | `config.py` `chunk_max_chars`；`extract.build_chunks(max_chars=2000)` |
| 切块粒度 | 先把招标文件切成**章节**（`sections.py`），再在章节内按**整页**装箱 | `extract._section_chunks` |
| 重叠 | **无**。一页只会进一个块，相邻块不共享任何字符 | 同上；`tests/test_chunk_contract.py` |
| 页内截断 | 只发生在章节的**首页/尾页**（按章节字符边界 `char_start` / `char_end`） | `extract._section_chunks` |
| 超限的唯一例外 | **单页本身**超过 `max_chars` 时，该块只有这一页且必然超限（不会为了凑上限去切页内文本） | `tests/test_chunk_contract.py::test_single_oversized_page_is_the_only_way_to_exceed_cap` |

## 2. 本仓真实语料实测

语料：`evals/datasets/tender_jiangsu.pdf`（41 页，5 章，其中 3 章属于抽取范围）。

复现：

```bash
./.venv/Scripts/python.exe scripts/measure_chunks.py                 # 默认 2000
./.venv/Scripts/python.exe scripts/measure_chunks.py --max-chars 3200
```

| 指标 | `max_chars=2000`（当前默认） | `max_chars=3200`（旧默认） |
|---|---|---|
| 块数 | 14 | 8 |
| 块长 min / P50 / P95 / max | **1261 / 1464 / 1666 / 1747** | 1410 / 2888 / 3078 / 3079 |
| 块长 > max_chars 的块数 | 0 / 14 | 0 / 8 |
| 相邻块重叠 min / P50 / P95 / max | **0 / 0 / 0 / 0**（n=11） | **0 / 0 / 0 / 0**（n=5） |
| 相邻块共享页面 | 0 | 0 |

分章节块数（`max_chars=2000`）：第二章 7 / 第三章 1 / 第四章 6。
逐块页区间（`max_chars=2000`，14 块）：p4、p5、p6、p7、p8、p9、p10-11（章节内按页装箱，
每页 ~1.4k 字所以大多一页一块）；评分标准 p11-13 一块；技术需求 p13-14、p15-16、p17-19、
p20-22、p23-25、p26-28。

> 注意末块 p26-28 同时带 `tech_requirements` 与 `disqualifications` 两个任务：
> 块内命中废标关键词（`DISQ_TRIGGERS`）时会追加废标任务，块数不受影响。

## 3. 和 Q3 审计里 `512 字 / 50 重叠` 的关系

Q3 报告给 documind 的 ADR-0005 记了一条反例：「声称 fixed 512 字滑窗 / 50 字重叠，
实测 71/72 块 > 512 字、重叠中位 143」。**那条数字属于 documind 的语料与实现，不是本仓的。**
本仓没有"固定字数滑窗"这条实现：

- `grep -n "512" src/` 无命中；`_section_chunks` 的断点是页边界，不是字数窗口；
- 因此也不存在"重叠 50 字"——实测重叠 P50 = 0、max = 0（两档参数都是 0）。

如果将来有人把 documind 的 ADR 数字抄进本仓 README/ADR，这份实测就是证伪依据：
同一份 41 页语料上，本仓的块长分布是 1261–1747（2000 档）/ 1410–3079（3200 档），
既不是 512，也没有任何重叠。

## 4. 为什么默认改成 2000（原来写死 3200）

README「已知的漏抽/偏差来源」记的实测：3200 字/块时技术章节出现"一块一次要吐 50+ 条"，
输出 12k 字、单块耗时 629s（含一次重试），且个别 quote 对不上就要整块重来。
改成 2000 后单块 40-180s、重试代价可接受。

所以**实现向文档靠拢**（`config.chunk_max_chars` 与 `build_chunks`/`extract` 的默认值
一起改成 2000），而不是把文档改成 3200 —— 3200 的失效模式是"整块重来/条目被截断"，
对投标工具是静默漏项，比多花几次调用危险。要复现旧口径仍可显式设
`BIDPILOT_CHUNK_CHARS=3200`。

## 5. 契约与语料的分界

- **契约**（写进测试，换语料也不许变）：不重叠、不跨章节、不超 `max_chars`（单页超长除外）
  → `tests/test_chunk_contract.py`；
- **分布数字**（随语料变）：P50/P95、块数 → 本文件的表，重测用 `scripts/measure_chunks.py`。
  换一份标书后如果数字变了，改这张表，不要改断言。