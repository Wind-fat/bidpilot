# evals/fixtures — 测试夹具，不是产物

这里的东西进仓的唯一理由是：**让验收在干净 clone（CI）上也能跑**。

| 文件 | 是什么 | 怎么来的 |
|---|---|---|
| `jiangsu_extract.json` | `evals/datasets/tender_jiangsu.pdf`（41 页真实语料）的一次**完整抽取结果**快照，含 `quote_span` / `section` 定位信息 | 本地跑一次全流程抽取，把 `out/jiangsu_extract.json` 原样拷入 |

## 为什么它进仓

`out/` 是 gitignore 的（每次运行都会被覆盖），所以 CI 里没有抽取结果可复核。
`scripts/verify_citations.py` 的「引用可回查」是红线，需要一份**稳定的输入**：

```
uv run scripts/verify_citations.py \
  --pdf evals/datasets/tender_jiangsu.pdf \
  --extract-json evals/fixtures/jiangsu_extract.json
```

`tests/test_locate.py::test_real_corpus_every_item_is_recheckable` 也读它——
否则干净 clone 上那条测试会静默 skip，"41 页真实语料可回查"就成了只在作者机器上成立的结论。

## 这不是什么

- **不是产品输出示例**，不要往 README/演示里贴它；
- **不是 golden 语义标准**：它只断言"每条引用都能按 page + quote_span 在 PDF 原文里逐字找回"，
  不断言"抽得全""抽得对"（抽取质量另由 PERF/LOCATE 文档的实验记录负责）；
- 改动 PDF 或抽取逻辑导致内容漂移时，**重跑抽取后覆盖它**，并在同一 commit 里说明；
  不要为了让它通过而手改条目。

体积约 100 KB（11 条评分项 / 35 条废标条款 / 124 条技术要求 + 分块报告），可接受。