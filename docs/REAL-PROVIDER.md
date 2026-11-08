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

默认 `BIDPILOT_LLM_TIMEOUT_S=180` 对推理模型的升级请求偏紧（实测单块最长 170 s，抖动一下就越线）。
本轮**没有**改默认值（避免把「跑通」建立在调参上），只把超时当已知风险记下来：
生产跑建议 `BIDPILOT_LLM_TIMEOUT_S=300`，代价是单块失败要多等 2 分钟才进下一次重试。

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