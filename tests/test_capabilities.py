"""能力库匹配：示例数据 + BM25，状态三分类。"""

from __future__ import annotations

from bidpilot.capabilities import MISSING, PARTIAL, SATISFIED, CapabilityIndex, Requirement, load_capabilities


def test_library_is_example_data():
    caps, meta = load_capabilities()
    assert len(caps) >= 5
    assert "示例数据" in meta, "能力库必须明确标注为示例数据"
    assert all(c.id.startswith("cap-") for c in caps)


def test_match_vr_requirement():
    caps, _ = load_capabilities()
    idx = CapabilityIndex(caps)
    m = idx.match(Requirement(id="T001", kind="tech", text="VR 头盔及资源设备硬件，支持全景视频播放与语音讲解", page=13))
    assert m.matched_capability_id.startswith("cap-")
    assert m.status in (SATISFIED, PARTIAL)
    assert 0 < m.confidence <= 1
    assert m.evidence


def test_match_after_sales():
    caps, _ = load_capabilities()
    idx = CapabilityIndex(caps)
    m = idx.match(Requirement(id="T002", kind="tech", text="提供 7×12 小时热线服务，本地团队 24 小时内上门", page=27))
    assert m.status in (SATISFIED, PARTIAL)
    assert "售后" in m.matched_capability or "培训" in m.matched_capability


def test_unrelated_requirement_is_missing():
    caps, _ = load_capabilities()
    idx = CapabilityIndex(caps)
    m = idx.match(Requirement(id="T999", kind="tech", text="危险化学品道路运输经营许可证及罐式集装箱检验报告", page=9))
    assert m.status == MISSING
    assert m.confidence < 0.35


def test_matrix_marks_missing_with_status_field():
    """不满足在数据层就是 status 字段的值（前端据此标红），不是靠 UI 猜。"""
    caps, _ = load_capabilities()
    idx = CapabilityIndex(caps)
    rows = [idx.match(Requirement(id=f"X{i}", kind="tech", text=t, page=1)) for i, t in enumerate(
        ["VR 智慧教室整体解决方案与驻场培训", "危险化学品运输资质和危废处置许可"]
    )]
    assert rows[0].status != MISSING
    assert rows[1].status == MISSING
    assert rows[1].to_row()["status"] == "不满足"