# BidPilot

[![CI](https://github.com/Wind-fat/bidpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Wind-fat/bidpilot/actions/workflows/ci.yml)

招标文件（PDF）→ 结构化抽取 → 需求-能力匹配矩阵 → 带来源的应答初稿（docx）。

一条命令跑完：解析 PDF、按章节切块、逐块调用 LLM 抽取、对 quote/页码做硬校验（不合规就重试，
仍不合规的条目保留并标 `needs_review`）、算需求-能力矩阵、按章节生成应答（每段必须挂来源 id，
无来源段落直接拒掉不输出）、导出 Word、落 SQLite。

## 快速开始

```bash
# 端到端（真实 LLM：默认 http://127.0.0.1:3425/v1，模型 qwen-cn/qwen3.8-flash）
./.venv/Scripts/python.exe scripts/run_pipeline.py evals/datasets/tender_jiangsu.pdf \
    --max-chars 2000 --concurrency 3 -v

# 只抽取 + 矩阵，不生成应答
./.venv/Scripts/python.exe scripts/run_pipeline.py evals/datasets/tender_jiangsu.pdf --no-generate

# 分阶段跑：抽取一次落盘，之后反复调生成
./.venv/Scripts/python.exe scripts/run_pipeline.py <pdf> --no-generate --extract-out out/x_extract.json
./.venv/Scripts/python.exe scripts/run_pipeline.py <pdf> --extract-json out/x_extract.json

# 测试（全部离线，LLM 用 FakeLLM 注入）
./.venv/Scripts/python.exe -m pytest -q
```

产出：`out/<pdf 名>_技术应答初稿.docx`、`out/bidpilot.db`、逐块抽取缓存 `out/_cache/*.json`。

常用开关（也可用同名环境变量）：`--max-chars`(BIDPILOT_CHUNK_CHARS)、`--concurrency`、
`--extract-out` / `--extract-json`、`BIDPILOT_GEN_BATCH`（每批生成条目数，默认 12）、
`BIDPILOT_CACHE_DIR`（设为 `0` 关闭抽取缓存）、`BIDPILOT_LOG=INFO`、
`BIDPILOT_LLM_ENABLE_THINKING`（推理模型的思考开关：默认关，纯成本/延迟取舍；
抽取的**重试**一律升级成开思考，见 `docs/REAL-PROVIDER.md`）、
`BIDPILOT_LLM_TIMEOUT_S`（默认 **300s**：抽取重试会升级成开思考，单块实测 110-170s，
180s 曾卡掉一次「98.9s 就能返回」的请求；慢网/小模型可调低，如 `BIDPILOT_LLM_TIMEOUT_S=120`）。

## CI：三条红线

`.github/workflows/ci.yml` 在 GitHub 上跑三件事，任一红即失败：

| job | 命令 | 红了意味着 |
|---|---|---|
| `pytest` | `uv run python -m pytest -q`（ubuntu / windows × py3.12 / py3.13） | 任何一条断言不成立，含 `tests/test_documind_contract.py`（documind 公开面契约）与真实 41 页语料的可回查测试 |
| `ruff` | `uv run ruff check src tests scripts` | 未用导入、未定义名、导入顺序等（口径与 documind 一致） |
| `citations` | `uv run python scripts/verify_citations.py --pdf evals/datasets/tender_jiangsu.pdf --extract-json evals/fixtures/jiangsu_extract.json` | 真实语料里有引用回查不到（page + `quote_span` 在 PDF 原文里逐字对不上），或抽取结果记录的 `source` 与 `--pdf` 不是同一份文件 |

**本地等价命令**（同一份输入，不需要 GitHub）：

```bash
uv sync --frozen
uv run python -m pytest -q
uv run ruff check src tests scripts
uv run python scripts/verify_citations.py \
    --pdf evals/datasets/tender_jiangsu.pdf \
    --extract-json evals/fixtures/jiangsu_extract.json   # 期望末行：可回查 170/170，exit 0
```

两个部署细节，改 workflow 时容易踩：

- **必须 checkout 两个仓**：`pyproject.toml` 里 `documind = { path = "../documind", editable = true }`
  是 path 依赖，CI 用 `actions/checkout` 的 `path:` 把本仓放 `bidpilot/`、documind 放 `documind/`，
  两者成兄弟目录，`../documind` 才指得到；只 checkout 本仓则 `uv sync` 直接失败。
- **`evals/fixtures/jiangsu_extract.json` 是测试夹具，不是产物**（`out/` 仍在 gitignore 里）。
  它是真实 41 页语料的一次抽取快照，红线靠它可复现；说明见 `evals/fixtures/README.md`。

## 模块

| 文件 | 职责 |
|---|---|
| `sections.py` | 复用 `documind.parsing.parse_file` 拿层级+页码，再加招标语义：章节归类、废标关键词定位 |
| `extract.py` | 分段 → LLM → 强 schema + quote 逐字校验 → 带错误重试 → 兜底 needs_review；逐块落盘缓存 |
| `schemas.py` | 三条硬约束：条目必须有页码+quote；quote 必须是原文子串；不合规不丢弃而是标 needs_review |
| `capabilities.py` | 能力库 + BM25 召回 + **词面覆盖率**判定 满足/部分满足/不满足 |
| `indexing.py` | `LibraryItem`：非文档来源（能力库/规则库）的索引条目，`doc_id` 不再编造 |
| `workflow.py` | 显式状态机 planned→generated→citation_verified→aggregated，可只重跑某一节；大批量按批生成 |
| `export.py` | python-docx 导出，H1=章节 / H2=招标条目，每段附来源 id + 页码 |
| `db.py` | SQLite：条目、段落、审核动作、workflow 状态（幂等 upsert，可重跑） |
| `pipeline.py` | 串起全流程，返回带统计/耗时/token 的 `PipelineResult` |

## 抽取评测现状

**语料**：`evals/datasets/tender_jiangsu.pdf`（政府采购网公开文件，41 页）。另一份 `tender_dxh.pdf`（96 页）尚未跑。

**真实一次冷跑的结果**（`--max-chars 2000 --concurrency 3`，章节识别 → 抽取）：

```
识别章节：5 个（其中废标关键词命中 8 段）
  - [投标人须知] 第二章 供应商须知  p4-10
  - [评标办法] 第三章 评分标准序号 评分因素 评审标准 分值  p11-12
  - [技术规格/技术服务需求] 第四章 采购项目技术、规格和数量要求等  p13-27
  - [合同条款] 第五章 合同主要条款及格式  p28-32
  - [投标文件格式] 第六章 附 件附件一：投标申请及申明  p33-41
评分项      ：  11 条，needs_review 0
废标条款    ：  35 条，needs_review 0
技术要求    ： 124 条，needs_review 0
抽取分块    ：14 块，重试后仍失败 0 块
需求-能力矩阵：共 170 条  满足 0 / 部分满足 8 / 不满足 162
生成章节    ：2 节，输出段落 135 段，因缺来源被拒 0 段，mocked=False
```

> 上表是**本机网关 + 默认开思考**的一次冷跑。直连供应商的最新一次（`enable_thinking=0` + 重试升级，
> 见 `docs/REAL-PROVIDER.md`）是 **11 / 32 / 99**、矩阵 142 条、生成 102 段、docx 59,999 bytes ——
> 两边的条文粒度不同（开思考会把「6、一体化电源管理系统」拆成 6 条子项），不是同一口径的召回对比。

**召回率：未标注。** 没有人工逐条标注的 gold set，所以上表只能说"抽到多少条、多少条通过了
页码+quote 双重校验"，不能说"召回率是多少"。`needs_review = 0` 的含义是：124 条技术要求的
quote 全部在标注页原文里逐字找到、页码全部落在抽取范围内——**校验通过率 100%，不等于召回 100%**。
漏抽目前只能靠人工抽查发现。

**已知的漏抽/偏差来源（都实测过）**：

- 分块太大会掉条目：3200 字/块时技术章节有一块一次要吐 50+ 条，输出 12k 字、单块耗时 629s（含一次重试），
  且因个别 quote 对不上整块重来。改成 2000 字/块（14 块）后单块 40-180s，重试代价可接受——
  **所以默认值就是 2000**（`BIDPILOT_CHUNK_CHARS`，见 `docs/CHUNKING.md`）。本语料实测：
  2000 → 14 块、块长 1261–1747、相邻块重叠 0；3200 → 8 块、块长 1410–3079。
- 空结果要当失败：早期版本模型对某页返回 `{}`，校验层视作"合法空数组"直接放过（p23-25 就这样丢了 2 条）。
  现在空结果会触发一次重试，仍空则整块标 `chunks_failed`，不再静默丢弃。
- 同一页单独切块 vs 与相邻页同块，模型给出的条数会变（p9、p10 单独成块共抽出 4 条废标条款，
  与 p8 同块时 p8-9 一块就是 22 条）。温度 0 下仍有模型波动，没有标注集就无法量化。
- **关掉「思考」会实打实少条目**：直连 DashScope 的 A/B（3 个块，`docs/REAL-PROVIDER.md`）里
  `technical#p15-16` 开思考 34 条 / 关思考 32、28 条，其中「7 可移动组合式桌椅 桌子：7 套」
  在关思考的两次运行里都没抽到。全语料跑：全局关思考 26 废标 / 67 技术要求且有 4 块连续三轮空结果，
  关思考 + 重试升级 32 废标 / 99 技术要求、失败块 0。**所以「省 token」不等于「质量等价」。**

**能力矩阵的判定口径**：以**词面覆盖率**为主（`覆盖率 = 需求与能力文本的公共 bigram / 需求 bigram`，
≥0.45 满足、≥0.22 部分满足），BM25 只用于召回排序。两个阈值**没有人工标注集调过**。
当前 `满足 0 / 部分满足 8 / 不满足 162` 主要反映：能力库（`src/bidpilot/data/capabilities.json`，
10 条，**虚构示例数据**）里没有 VR 头显的具体硬件参数（骁龙 835、90Hz、3500mAh 这类），
所以硬件参数条目一律落"不满足"——这是"能力库无证据"，不等于"做不到"。换成含设备规格书的真实能力库后
这一栏才有商业意义。

## 成本口径

模型 `qwen-cn/qwen3.8-flash`（本机网关 `127.0.0.1:3425/v1`），`httpx trust_env=False`，
单次请求 40-180s（输出越长越慢）。下表全是实测墙钟，不是估算：

| 阶段 | 真实调用次数 | 墙钟耗时 | 说明 |
|---|---|---|---|
| 抽取（冷跑） | 18 | 3m54s + 1m16s | 14 块首轮并发 8 → 3m54s；3 个空结果块补抽 4 次调用 → 1m16s |
| 抽取（命中缓存复跑） | 0 | 0.3s | `out/_cache/*.json` 按 内容+模型+任务 哈希，14 块全命中 |
| 生成应答 | 12 | 6m48s | 124 条技术要求 + 11 条评分项，每批 ≤12 条，章节内并发 3（两次跑分别为 6m48s / 8m11s） |
| 导出 docx | 0 | <1s | python-docx 本地渲染 |

- **冷跑一次端到端 ≈ 30 次 LLM 调用 / 12-13 分钟**（抽取 18 + 生成 12）。
- token 口径：生成阶段实测 **prompt 11,292 tok / completion 60,871 tok**（另一次跑 71,006 completion，
  同一批输入，输出长度有波动）。抽取阶段的 token 数没有单独打点（复跑时缓存命中、调用数为 0，
  日志那一栏就是 0，别当成"抽取不花钱"）；输入规模为 14 块 × ~1.4k 字 ≈ 20.9k 字原文。
- 按网关 `/v1/models` 报价（输入 $0.15/M tok、输出 $0.47/M tok）折算：生成阶段约 **$0.035**，
  抽取阶段（输出量约为生成的 1/5）量级 **$0.02-0.04**，整份 41 页标书冷跑一次约 **$0.06** 量级。
- 时间主要吃在输出 token 上：抽取里约 30% 时间花在重试，生成耗时与段落数近似线性。

**直连供应商的实测口径**（`qwen3.8-flash` 是推理模型，会先出思维链；2026-11-08，详见
`docs/REAL-PROVIDER.md`）：单块抽取 **开思考 110-170s / 关思考 11-26s**，
`enable_thinking=false` 让 completion token 降到约 **1/5.7**、墙钟降到约 **1/7.2**（3 块对照：
34,886 → 6,152 completion tok，399.9s → 55.7s）。整份 41 页冷跑（关思考 + 重试升级）：
**29 次调用 / 抽取 375.8s + 生成 86.2s / prompt 34,235 + completion 47,535 tok**。
completion 这一栏**包含思维链 token**，别按「正文长度」估。

## 降级与污染标记（退出码）

降级不是错，**没人知道降级了**才是错。凡是会让交付物"看起来能交、实际不是"的情况，
三处同时留痕：docx 首页横幅、文件名后缀、退出码。

| 情况 | docx 首页横幅 | 文件名 | 退出码 |
|---|---|---|---|
| **整节失败 / 无任何通过校验的段落**（生成阶段一段都没产出） | 首段即废稿声明：「本文件不含任何通过校验的应答段落：生成阶段整节失败，交付物不完整，不得作为投标文件使用。」，正文再列「未产出应答的章节」 | `..._技术应答初稿_FAILED.docx` | 4 |
| **抽取块整块失败**（`stats.extraction.chunks_failed > 0`：某几页的评分项/废标条款/技术要求一条都没抽到，重试后仍失败） | 首段：「本文件内容不完整（抽取阶段有块整块失败）：…」——逐块点名 chunk id、页码、所属章节与**缺哪类条目**，正文再列「抽取不完整的页段」（含重试次数与最后一次错误） | `..._技术应答初稿_PARTIAL.docx` | 4 |
| 有章节被降级为模板应答（未调用模型，须显式 `BIDPILOT_ALLOW_MOCK=1`） | 「本节为模板应答，未调用模型」（加粗红字+底色），被降级段落带 `〔类型：技术响应（模板，LLM 不可用）〕` 尾注 | `..._技术应答初稿_MOCK.docx` | 2 |
| 有章节部分批次失败（但仍有通过校验的段落） | 无（只体现在退出码与清单） | 不变 | 4 |
| **低产块**（`stats.extraction.chunks_low_yield` 非空：块成功、条目全部通过校验，但「条目/千字符」低于绝对下限 `2.0` 或同章其它块均值的 `1/3`，且正文是表格/清单型） | 首段黄底（不是红字）：「以下页段可能漏抽（不是失败：条目全部通过校验，但条数明显低于同章其它页段，请人工补看原文）：…」——逐块点名 chunk id、页码、抽到几条、产出率与同章均值；正文再列「可能漏抽的页段」 | 不变（不是失败，不加后缀） | 0（不改退出码，但进 `problems()` / CLI 清单） |
| 能力库仍是示例数据（`is_example`） | 「当前能力库为示例数据（EXAMPLE DATA），需求-能力匹配结果不具备商业意义」 | 不变 | 3 |
| `--no-generate` 分阶段跑 | 首段：「本文件只有抽取与能力矩阵结果：本次运行未生成应答正文（--no-generate），不能作为投标文件使用。」 | 不变（首段横幅自证，不叫废稿） | 3（示例库）/ 0（真实库） |
| 招标文件不可解析（无文本层/空文档） | 不产出任何文件 | 无 | 5 |

优先级：4 > 2 > 3 > 0（代码里唯一的判定处是 `pipeline.EXIT_PRIORITY` / `exit_code_for`，
`tests/test_delivery_integrity.py::test_readme_exit_code_contract_matches_code` 钉住本表不许漂；
抽取块失败那一行由 `test_chunk_failure_exit.py::test_readme_chunk_failure_row_matches_code` 钉住）。

- **为什么 4 压过 2**：整节失败的文件里根本没有应答正文，模板降级至少每段都挂着原文来源、人还能逐条改。
- **为什么「块失败」也算 4**：那块内容不是「质量差」，是**根本不在文件里**——p23-25 的技术要求一条没抽到，
  应答里就不会出现这几页的要求，评审员按文件写标书会漏项。只打一行日志等于假绿（第一版实测 4/14 块失败仍报 3）。
- **`_FAILED` 与 `_PARTIAL` 都是 4，区别只在程度**：`_FAILED` = 整份没有有效应答（废稿）；
  `_PARTIAL` = 有应答但少了几块抽取内容（不完整）。一个文件名只能表达一种污染，取最严重者：
  `_FAILED` > `_PARTIAL` > `_MOCK`（`pipeline.SUFFIX_SEVERITY` 是后缀→严重度的唯一映射）。
- **两者同时成立时**（有章节降级 + 一段都没通过校验）：文件名取最严重者 `_FAILED`（一个名字只能表达一种污染），
  横幅两张都印（废稿声明在前、模板横幅在后，后者解释"为什么会这样"），退出码只出一个 **4**。
  三处指向同一个结论，不会出现"文件名说废稿、退出码说降级"。
- **`--no-generate` 本身不是 4**：它是调用者主动要求"不生成"，产物只有抽取 + 矩阵、首段横幅自证；
  若也判 4，CI 就分不清"我得重跑"和"我本来就只要抽取"。示例库仍按 3 报。
  **例外**：`--no-generate` 不能掩盖抽取块失败——块失败是 4（与生成无关），文件照样标 `_PARTIAL`。
- **「低产块」与 `_PARTIAL` 是两件事，措辞不许混用**：`_PARTIAL` 说的是「这几页的内容不在文件里」
  （块整块失败）；低产块说的是「条目都在、都能回查到原文，只是**可能少抽了**」。所以低产**不加后缀、不改退出码**
  （新增退出码会让 CI 把「请补看」当成「必须重跑」），但必须进 `problems()`、CLI 清单、docx 横幅与正文——
  四处都由 `pipeline.low_yield_rows()`（数据来自 `extract.mark_low_yield()`）一份明细生成。
  判定三条同时成立才叫低产，且都可复算（`extract.LOW_YIELD_*`）：
  1. 块**成功**（`ok=False` 的块已经在 `_PARTIAL` / 退出码 4 里，不重复报）；
  2. **结构化类块**（正文行数 ≥ 12 且中位行长 ≤ 60 字：PDF 把表格/清单打成短行）+ 正文 ≥ 400 字；
     限结构化是必要的：本语料 p4-4 那种须知散文页 1.4 条/千字符是**正常**的（2 条废标条款），
     拿它去比同章均值只会制造噪声；
  3. `条目/千字符 < 2.0`，**或** `< 同章其它块均值 × 1/3`（兄弟块 ≥ 2 个时）。
  本语料（第四章 6 个技术块）实测：p23-25（1.27 条/千字符）与 p26-28（4.76，同为清单型正文）命中；
  p13-14 / p15-16 / p17-19 / p20-22（14.3-24.7）不命中；须知散文页 p4-4（1.42，2 条废标条款）也不命中
  —— 它靠「限结构化类块」被排除，否则同章均值判据会把它一起标红。规则细节、阈值与本语料逐块产出率见
  `docs/REAL-PROVIDER.md`「表格/清单型块：定性结论与处理策略」。

  **判定依据可审计**：低产块不只报「只抽到 N 条（x 条/千字符）」，还报命中的规则与对比基准
  （同章兄弟块均值 + 样本数），CLI、`problems()`、docx 横幅与正文读的是同一句
  `extract.low_yield_basis()`（判的那一刻就写进 `ChunkReport.peer_mean / peer_count / hit_abs / hit_peer`
  —— 阈值可配之后，事后拿「现在的 env」重算必然得出另一个结论）：

  ```
  ~ 低产：technical-p13-98d1227c#p23-25（第四章…） p23-25 → 只抽到 2 条 （1.27 条/千字符）：请对照原文补看
        命中规则：低于绝对下限 2.0 条/千字符（实际 1.27）、低于同章其它块均值 7.96 的 1/3（= 2.65 条/千字符）；对比基准：同章 2 个兄弟块，均值 7.96 条/千字符
  ```

  人工判断「误报还是真漏」看这两项：命中**绝对下限**= 绝对稀疏（大概率真漏）；
  只命中**同章均值比例**= 相对同章显著稀疏（要看兄弟块有几个、均值被谁拉高）；
  两者都命中时，比例那条只是佐证。

  **低产阈值（可配）**：四个阈值默认等于 `extract.LOW_YIELD_*` 常量（上次校准的那组：
  1 份 14 块语料、1261-1747 字/块）。换标书、换 `BIDPILOT_CHUNK_CHARS`、或发现误报时按表调整，
  不用改代码；写好错的/空串会回落默认值并打一条 warning：

  | 变量 | 默认值 | 含义 | 什么时候该调 |
  |---|---|---|---|
  | `BIDPILOT_LOW_YIELD_ABS` | 2.0 | 绝对下限（条/千字符）：低于它即判低产，不看同章其它块 | 换到条目密度整体不同的语料（如纯目录页、每页只有 2 条验收项）：整份文件大面积误报 → 调低（`0` = 关掉这条，只按同章均值判）；真漏却因为「整份都稀疏」而互相掩护 → 调高 |
  | `BIDPILOT_LOW_YIELD_PEER_RATIO` | 1/3（0.3333） | 同章均值比例：低于同章兄弟块均值的这个比例也算低产 | 同章块长短/密度差异大、均值被少数超长清单块拉高（误报）→ 调小（如 `0.25`）；只掉队一点点也算漏（更敏感）→ 调大（如 `0.5`） |
  | `BIDPILOT_LOW_YIELD_MIN_CHARS` | 400 | 正文短于它的块不参评（短的块谈产出率没有统计意义） | 调大 `BIDPILOT_CHUNK_CHARS` 或换到页均字数更少的标书：有效块普遍变短 → 调小；碎块太多、产出率噪声大 → 调大 |
  | `BIDPILOT_LOW_YIELD_MIN_PEERS` | 2 | 「同章均值」至少要有几个兄弟块才算数（不够就只判绝对下限） | 章节被切得很碎、每章只剩 1-2 块 → 调小到 `1`（但要接受单块噪声）；想要更稳的基准 → 调大 |

  验收这些旋钮真的会改判（零 LLM，读已落盘的 `out/real_provider/jiangsu_extract.json` + 构造块）：

  ```
  ./.venv/Scripts/python.exe scripts/low_yield_sensitivity.py
  ```

  输出逐条给出「变量 / 方向 / 取值 / 判定结果 / 是新增还是移出」，退出码 0 = 四个变量两个方向都能改判。

`scripts/run_pipeline.py` 在非零时把清单打到 stdout，例如：

```
交付物可信度告警（本文件不得直接提交）：
  ! [mocked] gen-第四章采购项目技术、规格和数量要求等 / 第四章…：本节为模板应答（未调用模型）
  ! [example_capabilities] capability-library / 能力库：当前能力库为示例数据（EXAMPLE DATA）…
  → 退出码 2（优先级 4=内容不完整 > 2=模板应答 > 3=示例能力库 > 0=干净）
```

抽取阶段有块整块失败时（同一份 stats 同时喂给文件名/横幅/清单，四处不会各说各话）：

```
抽取分块    ：14 块，重试后仍失败 1 块（其中 1 块在重试时升级开思考）
    ! 整块失败：technical-p13-98d1227c#p23-25（第四章…） p23-25 → 缺技术要求（重试 3 次：空结果：未抽到任何条目）
    交付物标记：文件名后缀 _PARTIAL，退出码 4（抽取内容不完整）
...
交付物可信度告警（本文件不得直接提交）：
  ! [incomplete] technical-p13-98d1227c#p23-25 / 抽取块 p23-25（第四章…）：重试 3 次后仍失败（空结果：未抽到任何条目）：缺技术要求，交付物缺少这部分内容
  → 退出码 4（优先级 4=内容不完整 > 2=模板应答 > 3=示例能力库 > 0=干净）
```

抽取到「少而合法」的块时（退出码不变，但清单与横幅都在）：

```
抽取分块    ：14 块，重试后仍失败 0 块（其中 5 块在重试时升级开思考）
低产块      ：1 块（条目数明显低于同章其它页段 → 可能漏抽；不是失败，退出码与文件名不变）
    ~ 低产：technical-p13-98d1227c#p23-25（第四章…） p23-25 → 只抽到 2 条 （1.27 条/千字符）：请对照原文补看
      命中规则：低于绝对下限 2.0 条/千字符（实际 1.27）、低于同章其它块均值 7.96 的 1/3（= 2.65 条/千字符）；对比基准：同章 2 个兄弟块，均值 7.96 条/千字符
...
交付物可信度告警（本文件不得直接提交）：
  ! [low_yield] technical-p13-98d1227c#p23-25 / 低产块 p23-25（第四章…）：只抽到 2 条（1.27 条/千字符，命中规则：…；对比基准：同章 2 个兄弟块，均值 7.96 条/千字符）：条目全部通过校验，但可能漏抽了大部分内容，请人工核对这几页原文；不是失败，退出码不变
  → 退出码 0（优先级 4=内容不完整 > 2=模板应答 > 3=示例能力库 > 0=干净）
```

**模板降级默认关闭**（`allow_mock=False`）：LLM 不可用时该章节直接 `failed`，不再产出模板句。
要跑降级路径必须显式 `BIDPILOT_ALLOW_MOCK=1`——此时产物一律带横幅与 `_MOCK` 后缀。

## 已知限制

- 能力库是虚构示例数据，匹配结果只用于流程演示；示例库生效时 docx 首页有横幅、CLI 退出码 3。
- 能力库为**空**时能力矩阵判 `待人工确认` 而不是 `不满足`（空库 ≠ 无命中）；真"无命中"才是 `不满足`。
- 分块大小/判定阈值/批次大小都是经验值，没有标注集调参；分块只在**本语料**上量过
  （2000 字/块 → 14 块、块长 1261–1747、重叠 0，见 `docs/CHUNKING.md`，重测用
  `scripts/measure_chunks.py`），"不重叠 / 不跨章节 / 不超上限"这三条是契约，由
  `tests/test_chunk_contract.py` 钉住。本语料上 `needs_review` 恒为 0，
  说明校验闸门没被触发过，不说明抽取无错。
- Word 目录是 TOC 域，需要在 Word/WPS 里"更新域"才出现页码。
- 只跑过 41 页那份；96 页的 `tender_dxh.pdf` 未验证。
- **推理模型的延迟/成本是按「思考开关」实测的，不是按单价估的**（`docs/REAL-PROVIDER.md` 有完整命令与数字）：
  同一块原文，`enable_thinking=true` 单块 110-170s、`false` 11-26s；completion token 34,886 → 6,152（3 块合计），
  但关思考会少条目（p15-16 少 1 条真要求）且**连续三轮吐空数组**、整块归零。当前折中：**首轮关、重试开**。
  这条折中不是免费的：本语料抽取 65.5s（纯关）→ 375.8s（关+升级），换来失败块 4 → 0。
  **但「空数组」不能全算在思考开关头上**：2026-11-08 的补给实验把 thinking 固定为 `true`（不走上表的升级路径），
  p23-25 三轮的**第一次尝试仍然全是空数组**（2.0 / 5.2 / 5.5 s，completion 72 / 364 / 356 tok），
  出条目的是重试那一次（对话里追加了「输出不合规：空结果…请重新输出完整 JSON」）。见
  `docs/REAL-PROVIDER.md`「表格/清单型块：定性结论与处理策略」。
- **表格/清单型块默认不做特殊处理**（不按块强制开思考、不按块强制重跑）：仍走全局配置（默认关思考 + 重试升级），
  另加**低产块**标记把「少而合法」暴露出来（见上面「降级与污染标记」表）。依据是同一块**全程开思考**跑 3 轮
  仍抽到 **2 / 49 / 3** 条（墙钟 28.2 / 176.6 / 27.4 s，completion 1,623 / 13,704 / 1,754 tok）——
  开思考既不能稳定召回，也不便宜（49 条那轮 171.4 s、13,340 completion tok）。要召回优先就全局
  `BIDPILOT_LLM_ENABLE_THINKING=1`，代价同上面那条。
- **`BIDPILOT_LLM_TIMEOUT_S` 默认已从 180 提到 300**：抽取重试会升级成「开思考」的推理请求，
  单块实测 110-170s；180s 时真实语料里出现过一次 `ReadTimeout`，而同一次请求 98.9s 就返回了——
  也就是 180 卡掉的是「慢但会成功」的请求，直接丢一块内容。300s 的代价只是失败块多等 2 分钟；
  要覆盖就设环境变量（例如 `BIDPILOT_LLM_TIMEOUT_S=120`），代价是升级重试更容易超时。
- **抽取块失败已进退出码**：`chunks_failed > 0` → 退出码 4 + 文件名 `_PARTIAL` + 首页横幅点名失败块与缺失条目
  + CLI 清单；四处都由 `pipeline.chunk_failure_rows()` 一份明细生成（`tests/test_chunk_failure_exit.py` 钉住）。
  仍未做的是「自动只重跑失败块」——目前重跑一次会按缓存命中已完成的块（`out/_cache/`），失败的块重算。
- **200 页 / 26 章的规模测试用的是 Markdown 语料**（`docs/PERF.md`）：解析+切章 0.12–0.13 s、
  PDF 口径 12.06 s 是分开测的，**「26 章 + 200 页 PDF」端到端没有直接实测**。要消掉这条限定
  需引入一个 dev-only 的 PDF 生成依赖，未做。
- **CI 三条红线（pytest / ruff / 引用可回查）已写进 `.github/workflows/ci.yml`，但当前 GitHub 上是红的**：
  documind 是私有仓，actions 自带的 `GITHUB_TOKEN` 读不到它（`uv sync` 的 path 依赖需要兄弟目录）。
  工作流已给出两条修法（配 `DOCUMIND_TOKEN` fine-grained token，或把 documind 设为 public）；
  本地等价命令全绿，绿灯需要这个凭据决策。