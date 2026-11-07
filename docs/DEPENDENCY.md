# BidPilot → DocuMind 依赖契约（一页纸）

- 版本：documind `>=0.1,<0.2`（当前 `path = "../documind", editable = true`，见 pyproject.toml:20,27-28）
- 原则：**bidpilot 只吃 documind 的 5 个符号；不吃它的数据库、不做它的检索、不碰它的私有函数。**
- 本文是两侧契约测试的依据。改动任一条 = 对外接口变更，须 @hermes 报批。
- 配套：`documind/docs/ARCH-REVIEW.md`（P0-03 是本文件的立项理由）

---

## 1. 五个能力（不多不少）

| # | 能力 | 入口符号（documind 侧定义位置） | bidpilot 用它做什么 | 现在用在哪 |
|---|---|---|---|---|
| C1 | **格式解析 + 结构识别**：PDF/DOCX/MD/TXT → 章/节/条 段落序列，带页码 | `parse_file(path) -> ParsedDocument`（parsing.py:343-345；后缀表 327-333） | 拿到一份招标文件的段落流，后续所有定位都基于它 | sections.py:216 `load_tender` |
| C2 | **段落结构**：`page` / 页内 `start,end` / `level`（1=章 2=节 0=条）/ `section_path` | `ParsedDocument.paragraphs`、`Paragraph`（parsing.py:29-38, 41-46） | 章节归类（投标人须知/评标办法/技术规格…）、废标条款扫描 | sections.py:16,127-141,202-211 |
| C3 | **页文本**：`pages[i]` 是规范化后的第 i+1 页，`page_count` | `ParsedDocument.pages` / `page_count`（parsing.py:44-54、218） | 分块正文、quote 逐字校验的**基准文本** | sections.py:86-89；extract.py:181-189,207-235 |
| C4 | **span → 原文**：按 (页, 页内偏移) 取回原文切片 | `ParsedDocument.slice_text(page_start, char_start, page_end, char_end)`（parsing.py:56-73） | 保证"块文本 == 原文切片"，quote 与页码不会错位 | **当前未使用 —— 这是契约里最该用、却最容易被绕过的一条（ARCH-REVIEW P1-06）** |
| C5 | **中文词法 + BM25**：字符 bigram 分词、TF-IDF 打分、可选 `allowed` 过滤 | `tokenize(text)`、`IndexItem(id, text, …)`、`BM25Index(items).search(query, top_n, allowed=None)`（index.py:22-26, 29-41, 63-85） | 需求 ↔ 能力库的召回（不用词典、不引分词器） | capabilities.py:15,31-36,110-117 |

**附加（不稳定，允许失败）**：`ParsedDocument.outline()`（parsing.py:75-86）—— 审核台的章节树展示；上游零测试，可空。

**明确不允许使用的**（一旦需要，说明该抽第三包了，见 §3）：
`chunking.*`、`retrieve.*`、`generate.*`、`db.*`、`pipeline.*`、`app.*`、`evals.*`，以及任何下划线开头的名字（`_sentences` / `_explode` / `_assemble` / `_group_page` / `_PARSERS`）。

> 现状核查：bidpilot 的 `src/` 与 `tests/` **没有**任何私有函数依赖（grep 零命中）。问题不是越界，而是 documind 侧没有 `__all__`（`__init__.py` 全 0 行），"公开"目前只是口头约定。

---

## 2. 不支持时怎么降级（一张表，禁止自由发挥）

| 情况 | 触发点 | 必须的行为 | 禁止的行为 |
|---|---|---|---|
| 后缀不在 `pdf/docx/md/txt` | parsing.py:339 抛 `ValueError` | 捕获 → 用户可见错误"仅支持 pdf/docx/md/txt"，终止流水线 | **禁止**自己用 pypdf 直读兜底（会破坏 C3/C4 的页/偏移语义） |
| 解析成功但无文本层（扫描件） | `paragraphs == []` 或 `pages` 全空 | 视为**硬失败**：报"该文件无文本层，请先 OCR"；不建 sections、不建 chunks、不导出 | **禁止**继续跑（会产出一份只有封面+目录的 .docx 并报成功；ARCH-REVIEW P0-04 有实测） |
| 个别页为空 | extract.py:183 已有 | 跳过该页，其余照常 | 禁止把空页当"内容缺失"整篇放弃 |
| `slice_text` 不可用（未来版本删除） | import/属性缺失 | **fail-fast**，启动自检即报错 | 禁止退回 `"".join(pages)` —— 那正是当前 sections.py:86-89 的隐式做法 |
| `tokenize` / `BM25Index` 不可用 | 版本不匹配 | 降级为"关键词子串匹配"，并把该次匹配的 status 一律标 `needs_review`，不允许出现"满足" | 禁止静默把 `MISSING` 当结论（当前空库与无命中同一分支，capabilities.py:121-126） |
| `outline()` 返回空 | — | 审核台章节树显示扁平列表，不阻塞 | 禁止因此让抽取/生成失败 |
| documind 版本不匹配 | 启动时 | 明确错误：`documind X.Y 不满足 >=0.1,<0.2，请 uv sync` | 禁止让 `ImportError` 堆栈直接冒到用户 |

**统一约束**：降级必须**可见**——CLI 打印、`stats()` 里带 `degraded: [...]`、交付物首页横幅。不可见的降级等于错误结果（团队信条 5）。

---

## 3. 什么时候把共用层抽成第三个包

**判断阈值（任一命中即启动，不靠感觉）**

| 触发 | 具体判据 |
|---|---|
| T1 需要第 6 个能力 | bidpilot 开始要 `chunk_document` / `retrieve` / 引用定位 —— 说明它在重建 RAG，继续抄 documind 只会抄出第二份不一致实现 |
| T2 字段级联改 | documind 需要改 `Paragraph` / `ParsedDocument` 的字段，而 bidpilot 这一版不能同步（一次改动要动 2 个仓库 + 2 次发版） |
| T3 第三个消费者 | 第三个仓库要复用 `parse_file`（哪怕只是脚本） |
| T4 安装负担成硬阻塞 | bidpilot 只用 C1–C5，却要装 documind 的全部依赖（fastembed / sqlalchemy / fastapi / uvicorn，documind `pyproject.toml:12-24`）；CI 安装时间或依赖冲突成为实际痛点 |
| T5 发布节奏分离 | documind 要对外发版/打 tag，bidpilot 要 pin 死某版 |

**抽取边界（抽什么、不抽什么）**

```
建议包名：doctext      # 只做"文档 → 结构化文本"，不做检索编排
包含：documind/parsing.py（Parser/PdfParser/DocxParser/MarkdownParser/ParsedDocument/Paragraph/parse_file）
      documind/index.py 的 tokenize / IndexItem / BM25Index / rrf_fuse
依赖：pypdf、python-docx（+ 可选 numpy；不含 fastembed/sqlalchemy/fastapi）
不含：config / db / embed / rerank / retrieve / generate / pipeline / app / evals
```

**迁移步骤（每步可回滚）**
1. 新建包，**先把测试搬过去**：`test_parsing.py` + `test_chunking.py:43` 的 span 断言 + `test_retrieval.py` 的 BM25 用例，先绿再迁代码；
2. doctext 发 `0.1.0`；
3. documind 改为 `from doctext import ...` 并**re-export 一版**（`documind.parsing` 保留 shim），保证旧 import 不断；
4. bidpilot 改 import，锁 `doctext>=0.1,<0.2`；
5. 下一 minor 删 documind 里的重复实现与 shim。

**现在不抽的理由**：2 个消费者、5 个符号、同步以天为单位——抽取成本 > 收益。但**必须先做 P0-03**（`__init__.py` 导出 + `__all__` + 两侧契约测试），否则抽包时没有"哪些是公开的"可依，等于把现状复制成第三个包。

---

## 4. 契约测试（两侧各一份，缺失则本契约无效）

`documind/tests/test_public_api.py` + `bidpilot/tests/test_documind_contract.py` 至少断言：

1. `from documind import parse_file, ParsedDocument, Paragraph, tokenize, IndexItem, BM25Index` 全部可导入（即 `__all__` 存在且被遵守）；
2. `Paragraph` 字段集 == `{text, page, start, end, level, section_path}`，且 `ParsedDocument` 可按 `title/filename/pages/paragraphs` 构造；
3. `slice_text(p, 0, p, len(pages[p-1])) == pages[p-1]`（C3 与 C4 的一致性）；
4. `BM25Index([IndexItem(id="a", text="年假 折现")]).search("年假", 3)` 返回 `[(str, float)]`，空索引返回 `[]`；
5. `get_parser("x.xyz")` 抛 `ValueError`（降级表第 1 行可执行化）。

---

## 5. 变更流程（documind 侧动 parsing/index 时）

- [ ] 两侧契约测试必须先绿（§4）
- [ ] 改 `Paragraph` / `ParsedDocument` 字段 → 升 **minor**，CHANGELOG 写"下游影响：bidpilot"
- [ ] minor 内**不允许**删 `slice_text` / `parse_file` / `tokenize` / `BM25Index`
- [ ] 新增字段必须有默认值（`Paragraph` 是 `slots=True` dataclass，位置参数已被 bidpilot 夹具依赖，见 ARCH-REVIEW P1-06）
- [ ] 改动后跑一次 bidpilot 的 26 项测试，结果贴进 PR——**这是当前唯一的跨仓库回归闸门**