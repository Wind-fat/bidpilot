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
`BIDPILOT_CACHE_DIR`（设为 `0` 关闭抽取缓存）、`BIDPILOT_LOG=INFO`。

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

**召回率：未标注。** 没有人工逐条标注的 gold set，所以上表只能说"抽到多少条、多少条通过了
页码+quote 双重校验"，不能说"召回率是多少"。`needs_review = 0` 的含义是：124 条技术要求的
quote 全部在标注页原文里逐字找到、页码全部落在抽取范围内——**校验通过率 100%，不等于召回 100%**。
漏抽目前只能靠人工抽查发现。

**已知的漏抽/偏差来源（都实测过）**：

- 分块太大会掉条目：3200 字/块时技术章节有一块一次要吐 50+ 条，输出 12k 字、单块耗时 629s（含一次重试），
  且因个别 quote 对不上整块重来。改成 2000 字/块（14 块）后单块 40-180s，重试代价可接受。
- 空结果要当失败：早期版本模型对某页返回 `{}`，校验层视作"合法空数组"直接放过（p23-25 就这样丢了 2 条）。
  现在空结果会触发一次重试，仍空则整块标 `chunks_failed`，不再静默丢弃。
- 同一页单独切块 vs 与相邻页同块，模型给出的条数会变（p9、p10 单独成块共抽出 4 条废标条款，
  与 p8 同块时 p8-9 一块就是 22 条）。温度 0 下仍有模型波动，没有标注集就无法量化。

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

## 降级与污染标记（退出码）

降级不是错，**没人知道降级了**才是错。凡是会让交付物"看起来能交、实际不是"的情况，
三处同时留痕：docx 首页横幅、文件名后缀、退出码。

| 情况 | docx 首页横幅 | 文件名 | 退出码 |
|---|---|---|---|
| 有章节被降级为模板应答（未调用模型） | 「本节为模板应答，未调用模型」（加粗红字+底色），被降级段落带 `〔类型：技术响应（模板，LLM 不可用）〕` 尾注 | `..._技术应答初稿_MOCK.docx` | 2 |
| 能力库仍是示例数据（`is_example`） | 「当前能力库为示例数据（EXAMPLE DATA），需求-能力匹配结果不具备商业意义」 | 不变 | 3 |
| **整节失败 / 无任何通过校验的段落**（含 `--no-generate` 分阶段跑） | 首段即废稿声明：「本文件不含任何通过校验的应答段落：生成阶段整节失败，交付物不完整，不得作为投标文件使用。」（`--no-generate` 为「本文件只有抽取与能力矩阵结果…」），正文再列「未产出应答的章节」 | `..._技术应答初稿_FAILED.docx`（`--no-generate` 保持原名，首段横幅自证） | 4 |
| 有章节部分批次失败（但仍有通过校验的段落） | 无（只体现在退出码与清单） | 不变 | 4 |
| 招标文件不可解析（无文本层/空文档） | 不产出任何文件 | 无 | 5 |

优先级：2 > 4 > 3 > 0。`scripts/run_pipeline.py` 在非零时把清单打到 stdout，例如：

```
交付物可信度告警（本文件不得直接提交）：
  ! [mocked] gen-第四章采购项目技术、规格和数量要求等 / 第四章…：本节为模板应答（未调用模型）
  ! [example_capabilities] capability-library / 能力库：当前能力库为示例数据（EXAMPLE DATA）…
  → 退出码 2（2=模板应答 3=示例能力库 4=内容不完整）
```

**模板降级默认关闭**（`allow_mock=False`）：LLM 不可用时该章节直接 `failed`，不再产出模板句。
要跑降级路径必须显式 `BIDPILOT_ALLOW_MOCK=1`——此时产物一律带横幅与 `_MOCK` 后缀。

## 已知限制

- 能力库是虚构示例数据，匹配结果只用于流程演示；示例库生效时 docx 首页有横幅、CLI 退出码 3。
- 能力库为**空**时能力矩阵判 `待人工确认` 而不是 `不满足`（空库 ≠ 无命中）；真"无命中"才是 `不满足`。
- 分块大小/判定阈值/批次大小都是经验值，没有标注集调参；本语料上 `needs_review` 恒为 0，
  说明校验闸门没被触发过，不说明抽取无错。
- Word 目录是 TOC 域，需要在 Word/WPS 里"更新域"才出现页码。
- 只跑过 41 页那份；96 页的 `tender_dxh.pdf` 未验证。
- **200 页 / 26 章的规模测试用的是 Markdown 语料**（`docs/PERF.md`）：解析+切章 0.12–0.13 s、
  PDF 口径 12.06 s 是分开测的，**「26 章 + 200 页 PDF」端到端没有直接实测**。要消掉这条限定
  需引入一个 dev-only 的 PDF 生成依赖，未做。
- **CI 三条红线（pytest / ruff / 引用可回查）已写进 `.github/workflows/ci.yml`，但当前 GitHub 上是红的**：
  documind 是私有仓，actions 自带的 `GITHUB_TOKEN` 读不到它（`uv sync` 的 path 依赖需要兄弟目录）。
  工作流已给出两条修法（配 `DOCUMIND_TOKEN` fine-grained token，或把 documind 设为 public）；
  本地等价命令全绿，绿灯需要这个凭据决策。