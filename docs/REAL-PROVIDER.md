# 直连真实供应商（DashScope）全链路实测：抽取 + 矩阵 + 生成 + 导出

`docs/REAL-RUN.md` 那次只跑了**生成**（`--extract-json` 复用旧抽取结果，走本机 magpie 网关）。
这份文档补的是**真正从 PDF 开始、直连供应商**的一条链路：抽取不做任何复用，全程 `BIDPILOT_ALLOW_MOCK=0`。

- 日期：2026-11-08
- 语料：`evals/datasets/tender_jiangsu.pdf`（41 页，5 章；sha256 前 12 位 `38e7f899d876`）
- 上游：`https://dashscope.aliyuncs.com/compatible-mode/v1`（DashScope OpenAI 兼容模式，直连，非本机网关）
- 模型：`qwen3.8-flash`（**推理模型**：默认先出思维链再出正文）
- 凭据：运行时从 `~/.config/magpie/providers.json` 注入 `BIDPILOT_LLM_BASE_URL/API_KEY`，不落盘、不进仓
- 代码默认值一个都没改（端点/模型/超时/块大小都是环境变量覆盖；`BIDPILOT_CHUNK_CHARS` 用默认 2000）

## 命令

```bash
# 抽取（不复用）+ 能力矩阵 + 生成 + 导出，全程无 mock
BIDPILOT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
BIDPILOT_LLM_API_KEY=*** \
BIDPILOT_LLM_MODEL=qwen3.8-flash \
BIDPILOT_LLM_ENABLE_THINKING=0 \
BIDPILOT_ALLOW_MOCK=0 \
BIDPILOT_CACHE_DIR=out/real_provider/_cache \
./.venv/Scripts/python.exe scripts/run_pipeline.py evals/datasets/tender_jiangsu.pdf \
    --out-dir out/real_provider --no-db \
    --extract-out out/real_provider/jiangsu_extract.json \
    --json-out out/real_provider/stats.json

# 引用可回查复核（用新抽取结果，不是仓库夹具）
./.venv/Scripts/python.exe scripts/verify_citations.py \
    --pdf evals/datasets/tender_jiangsu.pdf \
    --extract-json out/real_provider/jiangsu_extract.json
```

## 结果（一次跑完，墙钟）

| 项 | 值 |
|---|---|
| 退出码 | **3**（示例能力库；不是 0，见 README 退出码契约） |
| 评分项 / 废标 / 技术要求 | **11 / 32 / 99** 条，`needs_review` 全 **0** |
| 抽取分块 | 14 块，**重试后仍失败 0 块**，其中 **5 块在重试时升级开思考** |
| 抽取调用 / 尝试 | 19 次调用 / 20 次尝试（6 次重试，其中 1 次是 180s `ReadTimeout`） |
| 能力矩阵 | 142 条：满足 0 / 部分满足 7 / 不满足 135（能力库是虚构示例） |
| 生成 | 2 节 / **102 段**，因缺来源被拒 0 段，`mocked=False` |
| docx | `out/real_provider/tender_jiangsu_技术应答初稿.docx` **59,999 bytes** |
| 耗时 | 解析+切章 1.5 s，**抽取 375.8 s**，**生成 86.2 s**，抽取+矩阵+导出合计 462.0 s |
| token | 抽取 prompt **24,119** / completion **34,266**；生成 prompt **10,116** / completion **13,269**；合计 **34,235 / 47,535** |
| 引用可回查 | **142/142**（`verify_citations.py` 退出码 0，内容指纹匹配） |

token 口径说明：`usage.completion_tokens` 在推理模型上**包含思维链 token**（实测同一请求
`reasoning_content` 与 `content` 分开发回，但计费/计数都在 completion 里），所以「关思考」省的是这一栏。

## 真问题一：关掉思考后，空结果块会连着三轮全空

第一版代码（`enable_thinking=false` 全局关掉、重试仍关）在同一份语料上：

| 项 | 关闭思考（无升级） | 关闭思考 + 重试升级（当前代码） |
|---|---|---|
| 评分项 / 废标 / 技术要求 | 11 / **26** / **67** | 11 / **32** / **99** |
| 失败块 | **4**（p4-4、p9-9、p10-11、p23-25） | **0** |
| 抽取调用 | 23（14 块 + 9 次重试） | 19（14 块 + 5 次升级重试） |
| 抽取耗时 | 65.5 s | 375.8 s |
| 抽取 token | 31,370 / 15,428 | 24,119 / 34,266 |
| 退出码 | 3（**不含**块失败信息） | 3 |

失败块的日志长这样（`29 字` 就是 `{"disqualifications": []}`，三轮一模一样）：

```
chunk bidder_instructions-p4-70ae953b#p4-4 attempt 1 返回 29 字 / 1.4s
chunk bidder_instructions-p4-70ae953b#p4-4 attempt 2 返回 29 字 / 0.5s
chunk bidder_instructions-p4-70ae953b#p4-4 attempt 3 返回 29 字 / 0.5s
chunk bidder_instructions-p4-70ae953b#p4-4 完成 ok=False attempts=3 2.4s counts={...0/0/0}
```

也就是说：**「温度 0 + 关思考」会稳定地复现同一个空数组**，重试原样再来一遍没有信息增量，
整块页面的条目直接归零（p23-25 就是 README 里那条「曾经丢 2 条」的块）。

修法（`extract.extract_chunk` + `llm.ChatClient.complete(thinking=...)`）：
**首轮按默认（默认关思考，便宜快），重试一律显式升级 `enable_thinking=true`**。
理由：空结果/quote 校验不过的块是少数（本语料 5/14），只有这些块值得多花 token；
把「关思考」当常态、把「开思考」当重试手段，比全局二选一更划算。
失败块从 4 → 0，技术要求 67 → 99。`ChunkReport.escalated` 与 CLI 的「其中 N 块在重试时升级开思考」
就是这条路径的留痕（不静默）。

## 真问题二：升级后的单块会撞默认 180 s 超时

p23-25 升级重试时：

```
chunk technical-p13-98d1227c#p23-25 attempt 2 LLM 失败 180.0s: LLM 调用失败: ReadTimeout:
chunk technical-p13-98d1227c#p23-25 attempt 3 返回 3066 字 / 98.9s
chunk technical-p13-98d1227c#p23-25 完成 ok=True attempts=3 279.5s counts={'tech_requirements': 18}
```

默认 `BIDPILOT_LLM_TIMEOUT_S` 对推理模型的升级请求偏紧（实测单块最长 170 s，抖动一下就越线）。
那次实测之后**已把默认值从 180 提到 300**（`config.Settings.llm_timeout_s`）：180 卡掉的那一次，
同一请求 98.9 s 就返回了——超时比失败更糟，它丢的是一块本可成功的内容。
代价是失败块要多等 2 分钟才进下一次重试；要覆盖就设环境变量，例如 `BIDPILOT_LLM_TIMEOUT_S=120`
（更短的超时 = 更快失败、更容易丢块）。`tests/test_llm_thinking.py` 钉住默认值与覆盖行为。

## 真问题三：缓存 key 必须带 thinking 标记（已修）

`extract._cache_file` 原来只哈希 `模型|块 id|任务|正文`。加了思考开关之后，
开/关思考是两次不同的请求，却会命中同一份缓存 —— A/B 一跑，「关思考没掉条目」这种结论就是假的。
现在 key 里带 `thinking`（`True/False/None` 三种各自分区），`tests/test_llm_thinking.py` 钉住。

## 质量对照 A/B：thinking 开 vs 关（3 个块，非全量）

同一块原文、同一 prompt、同一温度 0，各跑一遍（`out/real_dashscope/ab_thinking.json`）：

| 块 | thinking | 条目 | 分类 | needs_review | quote 逐字可回查 | JSON 解析失败 | 单块墙钟 | prompt / completion |
|---|---|---|---|---|---|---|---|---|
| `evaluation#p11-13` | 开 | 12 | 11 评分 + 1 废标 | 0 | 12/12 | 0 | 119.3 s | 1,467 / 10,038 |
| `evaluation#p11-13` | 关 | 12 | 11 评分 + 1 废标 | 0 | 12/12 | 0 | **21.1 s** | 1,431 / **2,012** |
| `technical#p15-16` | 开 | 34 | 34 技术 | 0 | 34/34 | 0 | 170.4 s | 1,451 / 17,927 |
| `technical#p15-16` | 关 | 32 | 32 技术 | 0 | 32/32 | 0 | **23.7 s** | 1,415 / **3,370** |
| `technical#p26-28` | 开 | 7 | 6 技术 + 1 废标 | 0 | 7/7 | 0 | 110.2 s | 1,238 / 6,921 |
| `technical#p26-28` | 关 | 7 | 6 技术 + 1 废标 | 0 | 7/7 | 0 | **10.9 s** | 1,202 / **770** |
| **合计** | 开 | 53 | | 0 | 53/53 | 0 | **399.9 s** | 4,156 / **34,886** |
| **合计** | 关 | 51 | | 0 | 51/51 | 0 | **55.7 s** | 4,048 / **6,152** |

结论：**关思考 completion token ≈ 1/5.7、墙钟 ≈ 1/7.2，JSON 解析零失败、quote 可回查率不变**，
但**条目数少了 2 条**，少在哪必须说清楚（重新逐条对比 `technical#p15-16`，`ab_items_p15-16.json`）：

- 粒度差：开思考把「6、一体化电源管理系统」拆成 `6` + `6.A`~`6.E` 六条，关思考合并成一条（正文里 A–E 都在）。
  这 5 条差异**不是漏抽**。
- 真漏抽：**`7 可移动组合式桌椅 桌子：7 套` 这条在关思考的两次运行里都没有**（开思考两次都有）。
  该块关思考两次跑分别 32 / 28 条，开思考两次都 34 条 —— 关思考既少又抖。

因此：**抽取默认关思考是被动选择（省钱省时），不是质量等价**。要召回优先，就
`BIDPILOT_LLM_ENABLE_THINKING=1` 全开（本语料抽取 completion 约 5 倍），或者接受「只在重试时升级」的折中。
当前代码默认是折中：**首轮关、重试开**。

## 仍未覆盖

- 只跑了 41 页这一份；96 页 `tender_dxh.pdf` 未跑。
- 没有人工标注 gold set，所以上面所有「99 条」都只能读作「抽到并通过校验 99 条」，
  **不是召回率**；关思考到底漏了多少条，只有 p15-16 那 1 条是逐条比对出来的。
- 生成阶段的思考开关没有单独 A/B（本轮只做抽取对照）：生成用的仍是默认关思考，
  102 段 / 13,269 completion token 是这一配置下的数。
- 退出码不反映「抽取块失败」：第一版跑出 4 个失败块时退出码仍是 3（只有 `example_capabilities`）。
  哪些页没抽到，只能从 CLI 的「重试后仍失败 N 块」那行读到。要不要把它升级成 4（内容不完整），
  涉及 README 退出码契约与 `test_delivery_integrity.py`，本轮未改。

## 复核：300 s 默认值 + 「关思考、重试升级」是否真的稳（3 块真调，2026-11-08）

上一节把默认超时从 180 提到 300，但**没在真模型上重跑过**。这次只跑 3 块（p15-16 / p23-25 / p26-28），
用 `scripts/real_chunks_probe.py` 直接调 `build_chunks` + `extract.extract_chunk`（与生产同一套
prompt / 校验 / 重试升级），并在 LLM 外面包一层记录器，把**每次尝试**的墙钟、`thinking` 实际值、
prompt/completion token 都记下来 —— pipeline 只汇总到「块级 attempts」，拿不到这些。

```bash
PYTHONIOENCODING=utf-8 BIDPILOT_LLM_MODEL=qwen3.8-flash \
./.venv/Scripts/python.exe scripts/real_chunks_probe.py \
    --pdf evals/datasets/tender_jiangsu.pdf \
    --chunks p15-16,p23-25,p26-28 \
    --out out/real_chunks/probe_300s.json
```

- 上游/模型同上（DashScope 直连 + `qwen3.8-flash`），凭据运行时从 `~/.config/magpie/providers.json`
  按 `name == "Qwen (China)"` 读 `chat`/`key`，**不入仓、不回显**
- 超时用**默认 300 s**（没有传 `--timeout-s`，也没设 `BIDPILOT_LLM_TIMEOUT_S`）
- 思考用默认「关」（`thinking_default=False`），缓存未参与（脚本不走 `extract()` 的缓存层）

### 一轮 3 块（`out/real_chunks/probe_300s.json`）

| 块 id | 尝试 | 每次耗时 | 升级开思考 | prompt / completion | 条目 | needs_review | quote 逐字可回查 |
|---|---|---|---|---|---|---|---|
| `technical-p13-98d1227c#p15-16` | 1 | 29.6 s | 否 | 1,415 / 2,814 | 28（全技术） | 0 | 28/28 |
| `technical-p13-98d1227c#p23-25` | 2 | 0.5 s → **114.2 s** | 是（第 2 次） | 1,378+1,508 / 10+10,129 | 31（全技术） | 0 | 31/31 |
| `technical-p13-98d1227c#p26-28` | 1 | 6.7 s | 否 | 1,202 / 634 | 6（5 技术 + 1 废标） | 0 | 6/6 |
| **合计** | **4 次调用** | 151.0 s | 1 块升级 | **5,503 / 13,587** | 65 | 0 | 65/65 |

`p23-25` 首次尝试 0.5 s 返回 29 字（`{"tech_requirements": []}`），与上一节「关思考稳定吐空数组」
完全一致；升级后 **114.2 s** 拿回 31 条，**没有 ReadTimeout**。

### `p23-25` 复跑两轮（同一块、同一 prompt、温度 0、默认关思考）

| 轮次 | 每次尝试（thinking / 耗时 / completion） | ok | 条目 | needs_review | quote 回查 | 块墙钟 |
|---|---|---|---|---|---|---|
| A（上表） | 关 / 0.5 s / 10 → **开 / 114.2 s / 10,129** | True | **31** | 0 | 31/31 | 114.7 s |
| B | 关 / 1.5 s / 10 → 开 / 6.8 s / 447 → 开 / 9.8 s / 796 | True | **2** | 0 | 2/2 | 18.1 s |
| C | 关 / 0.9 s / 10 → **开 / 147.9 s / 14,481** | True | **3** | 0 | 3/3 | 148.8 s |

三轮合计（含 A、B、C 全部 9 次调用）：**prompt 12,877 / completion 29,331 tok**，墙钟 317.9 s。

### 结论一：300 s 够用，不建议再降

- 本次 4 次「开思考」调用的真实耗时：6.8 s / 9.8 s / 114.2 s / 147.9 s（另加上一节 170.4 s、98.9 s），
  **最长 147.9 s，最坏历史值 170.4 s**，全程 0 次 `ReadTimeout`。
- 300 s 相对最坏观测值有 **1.76× 余量**，足够吸收抖动；180 s 只有 1.06× —— 上一节那次 180 s
  掐掉的请求 98.9 s 就返回了，正是「余量不足 + 抖动」的典型。
- 再往上（600 s）只买到「更慢的失败」：一个块最多 3 次尝试，600 s 下最坏要等 30 分钟才进下一次重试，
  而本次没有任何一次调用超过 150 s。**默认 300 s 保留**；慢网/小模型用
  `BIDPILOT_LLM_TIMEOUT_S=120` 覆盖是更差的选择（会丢块，本次 114.2 s / 147.9 s 两次都会被掐）。

### 结论二：「关思考 + 重试升级」**不稳定** —— 它兜住了失败，兜不住召回

- 升级触发是**确定性的**：`p23-25` 三轮首答全部是空数组（0.5 / 1.5 / 0.9 s，completion 都是 10 tok），
  三轮全部升级开思考 —— 这条路径的**可靠性**没问题，每次都能把块从「整块归零」拉回 `ok=True`。
- 但拉回来的**内容量差 10 倍以上**：31 / 2 / 3 条。B 轮甚至升级后的第 2 次仍是空数组（thinking 花了
  447 tok 只吐出 29 字），第 3 次才给 2 条；C 轮升级这次烧了 **14,481 completion tok 只产出 3 条**
  （3 条分别落在 p23/p24/p25 的清单表上，不是截断，是模型自己收敛到了「一页一条」的粗粒度）。
- **`ok=True` 不代表召回正常**：B、C 两轮 `needs_review=0`、quote 3/3 与 2/2 全可回查、
  JSON 解析零失败 —— 所有现有信号都是绿的，等于这个块「静默少抽 90%」。
  现有护栏（空结果 / page 越界 / quote 对不上）对「少而合法」的退化**完全没有感知**。
- 因此 `p15-16` 同理：这次 1 次尝试 28 条（上一节关思考两次是 32 / 28，开思考 34），
  28 条全合规、不触发任何重试 —— 关思考的召回抖动是被 `ok` 吞掉的，不是被重试修掉的。

### 这轮没解释清的

- **为什么同一块会 31 / 2 / 3 条**：已由下面的「表格/清单型块：定性结论」用 3 轮全开思考的对照回答
  —— 不是升级路径不稳，是这块清单型正文本身不稳（结论一）。
- **token 账不能线性外推**：C 轮升级 14,481 completion tok 只换回 3 条，比 A 轮 10,129 tok 换 31 条贵 15 倍/条。
  单块最贵一次 14,481 tok 是 `max_tokens=6000` 请求值的 2.4 倍（上游对推理 token 不按该字段截断）。
- 没有跑整份 41 页，所以「3 块里 1 块会静默少抽」不能直接换算成全量失败率；`p15-16` 的 28 条也没有逐条
  与上一节开思考的 34 条做 diff（这条差异上一节做过，结论是 5 条粒度差 + 1 条真漏抽）。

## 表格/清单型块：定性结论与处理策略（p23-25，3 轮全开思考，2026-11-08）

要回答的问题：`p23-25` 三轮 31 / 2 / 3 条，究竟是**「首轮关思考 → 重试升级」这条路径**不稳，
还是**表格/清单型正文本身**就不稳？

### 实验设计（6 次调用，预算 ≤ 9）

```bash
PYTHONIOENCODING=utf-8 BIDPILOT_LLM_MODEL=qwen3.8-flash \
./.venv/Scripts/python.exe scripts/real_chunks_probe.py \
    --pdf evals/datasets/tender_jiangsu.pdf --chunks p23-25 \
    --thinking always --repeat 3 \
    --out out/real_chunks/probe_thinking_p23.json
```

- `--thinking always`：`RecordingLLM` 把**每一次**请求的 `thinking` 一律改写成 `True`
  （日志首行 `thinking_forced=True`，每次尝试都打印 `thinking=True`）——**第一次尝试就开思考**，
  `extract_chunk` 的「首轮关 / 重试升级」在这条路径上不生效，变量被彻底摘掉。
- 其余与生产完全一致：同一块（正文 1578 字）、同一 prompt、`temperature=0`、`max_tokens=6000`、
  默认 300 s 超时、`max_chars=2000`、不走缓存、`--repeat 3` 每轮独立。
- 上游/模型同上（DashScope 直连 + `qwen3.8-flash`），凭据运行时从 `~/.config/magpie/providers.json` 读，不入仓。

### 结果（`out/real_chunks/probe_thinking_p23.json` / `run_thinking_p23.log`）

| 轮次 | 尝试 1（thinking=True） | 尝试 2（thinking=True） | 条目 | 块墙钟 | prompt / completion | needs_review | quote 回查 | 产出率 |
|---|---|---|---|---|---|---|---|---|
| 1 | 2.0 s / 24 字 / `{"tech_requirements":[]}`（空） | 26.2 s / 328 字 | **2** | 28.2 s | 2,917 / 1,623 | 0 | 2/2 | 1.27 条/千字符 |
| 2 | 5.2 s / 25 字 / `{"tech_requirements": []}`（空） | 171.4 s / 8,058 字 | **49** | 176.6 s | 2,918 / 13,704 | 0 | 49/49 | 31.05 条/千字符 |
| 3 | 5.5 s / 24 字 / `{"tech_requirements":[]}`（空） | 21.9 s / 532 字 | **3** | 27.4 s | 2,917 / 1,754 | 0 | 3/3 | 1.90 条/千字符 |
| 合计 | 3/3 空（completion 72 / 364 / 356） | 3/3 出条目 | 2 / 49 / 3 | 232.2 s | 8,752 / 17,081 | 0 | 54/54 | — |

`ok=True`、`needs_review=0`、quote 逐条可回查 —— 也就是说，**三轮里两轮是「少而合法」的静默少抽**，
现有四道闸（空结果 / page 越界 / quote 对不上 / 章节越界）一条都不会响。

### 结论一：不是升级路径不稳，是清单型正文不稳

1. **思考恒定开着，条目数仍然差 24 倍**（2 / 49 / 3）。升级路径（首轮关思考）被排除为原因。
2. **首次尝试在开思考下也 3/3 空数组**（2.0 / 5.2 / 5.5 s，24-25 字，completion 72 / 364 / 356）。
   上一节的「空数组 = 关思考」是不成立的推断：空数组是这块**首答的稳定行为**。
   真正把空数组换成条目数的是**重试那一轮追加的消息**（把 `{"tech_requirements":[]}` 作为 assistant 回贴，
   再加「输出不合规：空结果…请重新输出完整 JSON」）——即"再问一次并点名问题"，而不是"开思考"。
3. 为什么这块会这样：p23-25 的正文是**附表**（图书清单 / 期刊清单 / 教学案例清单），
   「这些书要不要逐本算一条技术要求」没有唯一答案。模型在三轮里分别选了
   「1 条汇总 + 1 条清单要求」/「49 条逐本图书」/「3 条分页汇总」，三种都合规。
4. 样本量与口径限制：**3 个样本、无人工标注 gold set**。所以只能说**条目数不可复现**，
   不能说 49 条是"正确"、2 条是"漏抽 96%"——本仓没有标准答案，49 条那轮还多了 171 s / 13,340 completion tok 的代价。

### 结论二：处理策略（代码里就是这么做的）

| 问题 | 决定 | 依据 |
|---|---|---|
| 表格/清单块默认开思考吗？ | **不默认开**。全局仍是 `BIDPILOT_LLM_ENABLE_THINKING` 默认 `False` + 重试升级；要召回优先就全局设 `1` | 开思考在 p23-25 上 3 次里 2 次仍只出 2-3 条，completion 却多 ~10 倍（1,623 → 13,704） |
| 低产就强制重跑吗？ | **不强制**。不按块重跑、不加"重跑到条数达标"的循环 | 条目数不可复现 → 重跑是赌运气；最贵一次 171 s / 13,340 completion 只换回 49 条 |
| 那怎么不静默？ | **低产块护栏**：`ChunkReport.low_yield` + `yield_rate`（条/千字符），`extract.mark_low_yield()` 判定；`pipeline.low_yield_rows()` 单一明细喂给 `problems()`、CLI 清单、docx 黄色横幅与正文明章 | 本语料把 p23-25 标出来：1.27 条/千字符（同章 5 个兄弟块均值 15.87 → 阈值 5.29）；p4-4 这类散文页（1.42，2 条废标条款）**不**被误判（限结构化类块） |
| 退出码/文件名要变吗？ | **不变**（0，不加后缀），只进 `problems()` 与清单 | 它不是失败：条目都在、都通过了校验；新增退出码/后缀会让用户把"请补看"当成"必须重跑"，并与 `_PARTIAL`（内容缺失）混淆 |

交付物首页的黄色横幅（句首常量 `export.BANNER_LOW_YIELD_HEAD`，与红色 `_PARTIAL` 横幅刻意分开）：

```
以下页段可能漏抽（不是失败：条目全部通过校验，但条数明显低于同章其它页段，请人工补看原文）：technical-p13-98d1227c#p23-25（第四章…） p23-25 只抽到 2 条（1.27 条/千字符，同章其它块均值 15.87；命中规则：低于绝对下限 2.0 条/千字符（实际 1.27）、低于同章其它块均值 15.87 的 1/3（= 5.29 条/千字符）；对比基准：同章 5 个兄弟块，均值 15.87 条/千字符）。
```

横幅里「命中规则 / 对比基准」不是装饰：绝对下限说明**绝对稀疏**（大概率真漏），
只命中同章均值比例则要看兄弟块有几个、均值被谁拉高（可能是误报）。这两项与 `problems()`、
CLI 清单同源（`extract.low_yield_basis()`，判定时已写进 `ChunkReport`）。

### 本语料逐块产出率（低产阈值的校准依据）

正文长度取 `extract.build_chunks`（`max_chars=2000`）当前分块；条目数取 `out/jiangsu_extract.json`
那次「关思考 + 重试升级」的整份抽取结果（p26-28 的条目数取 `out/real_chunks/probe_300s.json`，6 条）。

| 块 | 正文（字） | 条目 | 条/千字符 | 结构化 | 低产 |
|---|---|---|---|---|---|
| `bidder_instructions#p4-4` | 1412 | 2 | 1.42 | 否（散文须知） | 否（限结构化类块的直接受益者） |
| `technical#p13-14` | 1327 | 19 | 14.32 | 否 | 否 |
| `technical#p15-16` | 1747 | 33 | 18.89 | 否 | 否 |
| `technical#p17-19` | 1435 | 24 | 16.72 | 否 | 否 |
| `technical#p20-22` | 1622 | 40 | 24.66 | 否 | 否 |
| `technical#p23-25` | 1578 | 2 | **1.27** | **是**（23 行，中位 27 字） | **是**（两条判据都命中） |
| `technical#p26-28` | 1261 | 6 | 4.76 | 是（27 行，中位 28 字） | **是**（擦线：兄弟块均值 15.17 → 阈值 5.06） |

阈值（`extract.LOW_YIELD_*`，可被 `BIDPILOT_LOW_YIELD_ABS / _PEER_RATIO / _MIN_CHARS / _MIN_PEERS`
覆盖 —— 换语料/换 `max_chars` 后按 README「低产阈值（可配）」表调，并用
`scripts/low_yield_sensitivity.py` 复核旋钮是否真的改判）：
`< 2.0 条/千字符` 或 `< 同章兄弟块均值 × 1/3`（兄弟块 ≥ 2 个），
且块成功、正文 ≥ 400 字、正文为表格/清单型（行数 ≥ 12 且中位行长 ≤ 60 字）。

第四章 6 个技术块里命中 2 块（都是附表/清单型正文：p23-25 图书期刊清单、p26-28 教学案例清单），
其余 4 块 14.32-24.66 条/千字符全部不命中，散文页 p4-4（1.42）因「限结构化类块」被排除。
**p26-28 是擦线命中**（4.76 vs 阈值 5.06）——这是护栏选择的保守方向：宁可把「可能漏抽」多报一块，
也不要再出现「31 / 2 / 3 条而全绿」。代价是用户可能在一个其实抽得尚可的清单块上多看一眼原文。

### 这次仍没做到的

- 只有 **3 个样本**（6 次调用，受预算限制），没有 gold set：**召回率仍然未知**，
  「49 条」也不等于正确。要判定谁对，得人工标注 p23-25 的图书/期刊条目应拆成几条。
- 低产阈值只在这一份语料上校准过（14 块，长 1261-1747 字）；换标书/换 `max_chars` 后
  2.0 与 1/3 是否合适，没有第二个语料验证。
- 96 页 `tender_dxh.pdf` 未跑；`p15-16`（28/32/34 条抖动）也没做「全程开思考」的对照。