"""P1-10：能力库这类「非文档来源」的索引条目不该编造 doc_id / strategy。

守住三件事：
1. `LibraryItem` 仍是 `IndexItem`（documind 的 BM25Index 照常吃），字段集没漂移；
2. 默认值表达「不属于某份文档」，调用方不用编字符串；
3. BM25 在 `LibraryItem` 上照常打分与排序（不是只「能构造」）。
"""

from __future__ import annotations

import inspect

from documind import BM25Index, IndexItem

from bidpilot.capabilities import CapabilityIndex, load_capabilities
from bidpilot.indexing import WHOLE, LibraryItem


def test_library_item_is_an_index_item_with_same_fields():
    assert issubclass(LibraryItem, IndexItem)
    assert set(LibraryItem.__dataclass_fields__) == set(IndexItem.__dataclass_fields__)
    # documind 那两个必填字段在派生类型里有默认值——这才是「不编也能构造」的来源
    assert inspect.signature(LibraryItem).parameters["doc_id"].default == ""
    assert inspect.signature(LibraryItem).parameters["strategy"].default == WHOLE


def test_library_item_constructs_without_inventing_a_doc_id():
    item = LibraryItem(id="cap-1", text="提供 7×12 小时热线服务，本地团队 24 小时内上门")
    assert item.doc_id == ""
    assert item.strategy == WHOLE
    # 其余可选字段保持 documind 的默认值，没有被派生类型改掉
    assert (item.page_start, item.char_start, item.page_end, item.char_end) == (0, 0, 0, 0)
    assert item.section_path == "" and item.parent_id is None and item.is_parent is False


def test_bm25_scores_and_ranks_library_items():
    """打分与排序都要成立：命中分数 > 0，且按分数降序，无关条目不进结果。"""
    items = [
        LibraryItem(id="cap-hotline", text="提供 7×12 小时热线服务，本地团队 24 小时内上门"),
        LibraryItem(id="cap-vr", text="VR 头盔及资源设备硬件，支持全景视频播放与语音讲解"),
        LibraryItem(id="cap-training", text="提供驻场培训与师资支持，含中文教材"),
        LibraryItem(id="cap-hazmat", text="危险化学品道路运输经营许可证"),
    ]
    idx = BM25Index(items)

    hits = idx.search("7×12 小时热线服务与本地上门", top_n=4)
    ids = [i for i, _ in hits]
    scores = [s for _, s in hits]
    assert ids[0] == "cap-hotline", hits
    assert scores == sorted(scores, reverse=True), scores
    assert all(s > 0 for s in scores), scores
    assert "cap-hazmat" not in ids, f"无关条目不该出现在命中里：{hits}"

    # 打分与条目顺序无关：倒序输入同一批条目，冠军不变
    flipped = BM25Index(list(reversed(items))).search("7×12 小时热线服务与本地上门", top_n=4)
    assert flipped[0][0] == "cap-hotline", flipped


def test_capability_index_does_not_fabricate_a_doc_id():
    caps = load_capabilities().capabilities
    idx = CapabilityIndex(caps)
    assert idx.items and all(isinstance(it, LibraryItem) for it in idx.items)
    assert {it.doc_id for it in idx.items} == {""}, "能力库条目不该再带 doc_id='capabilities' 这类假值"
    # 索引没被这次改造弄哑：真实查询仍然命中
    assert idx.search("VR 头盔硬件与全景视频播放", top_n=1)[0][0].id.startswith("cap-")
