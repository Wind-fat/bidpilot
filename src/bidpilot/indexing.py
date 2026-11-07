"""非文档来源的索引条目：`IndexItem` 的轻量派生（ARCH-REVIEW P1-10）。

documind 的 `IndexItem` 把 `doc_id` / `strategy` 做成必填，这是合理的——它装的是
「解析后的文档块」，每条块确实知道自己来自哪份招标文件、用的哪种切分策略。

但 bidpilot 侧还有另一种索引：能力库（将来的规则库/条款模板库同样如此）。它**不属于任何一份
招标文件**。以前这边只能硬填 `doc_id="capabilities"` / `strategy="capability"`：

- `doc_id` 是个假的文档 id——一旦有人按 doc_id 过滤（`BM25Index.search(allowed=...)`），
  这些值没有任何语义可解释；
- 每新增一个库就得再编一组字符串，编错也不会有任何断言拦住。

`LibraryItem` 把这件事显式建模：`doc_id` 默认空（= 不属于某份文档，调用方可显式覆盖成
真正的库标识），`strategy="whole"`（一条记录就是一个完整条目，没有再切分）。
字段集与 `IndexItem` 完全一致，`BM25Index` 照常打分排序。
"""

from __future__ import annotations

from dataclasses import dataclass

from documind import IndexItem

#: 一条索引项就是一条完整记录（能力库的一行、规则库的一条），没有二次切分。
WHOLE = "whole"


@dataclass(slots=True)
class LibraryItem(IndexItem):
    """不属于任何文档的索引条目。通用场景下 doc_id 留空，不要编造假值。"""

    doc_id: str = ""
    strategy: str = WHOLE