"""推理模型思考开关（enable_thinking）的契约。

为什么值得单测：这个字段是**成本**开关，默认值写错就是每份标书多烧 5 倍 completion token；
而缓存 key 不带上它，A/B（开/关思考）就会互相命中、把「关思考没掉条目」测成假的。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bidpilot.config import Settings
from bidpilot.extract import Chunk, _cache_file
from bidpilot.llm import ChatClient


def _settings(monkeypatch, value: str | None) -> Settings:
    if value is None:
        monkeypatch.delenv("BIDPILOT_LLM_ENABLE_THINKING", raising=False)
    else:
        monkeypatch.setenv("BIDPILOT_LLM_ENABLE_THINKING", value)
    return Settings()


def _capture(monkeypatch, settings: Settings) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    client = ChatClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    asyncio.run(client.complete([{"role": "user", "content": "x"}]))
    return seen


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (None, False),  # 未设置 → 默认关思考（省 token）
        ("0", False),
        ("1", True),
        ("true", True),
        ("auto", None),  # 不下发该字段
        ("", None),
    ],
)
def test_env_to_settings(monkeypatch, env, expected):
    assert _settings(monkeypatch, env).llm_enable_thinking is expected


@pytest.mark.parametrize("env", [None, "1"])
def test_payload_carries_enable_thinking(monkeypatch, env):
    settings = _settings(monkeypatch, env)
    body = _capture(monkeypatch, settings)
    assert body["enable_thinking"] is settings.llm_enable_thinking


def test_auto_omits_field(monkeypatch):
    body = _capture(monkeypatch, _settings(monkeypatch, "auto"))
    assert "enable_thinking" not in body


def test_cache_key_separates_thinking_modes(tmp_path):
    chunk = Chunk(
        page_start=1, page_end=1, section_id="s", section_title="t", section_kind="technical",
        tasks=["tech_requirements"], body="正文",
    )
    on = _cache_file(tmp_path, chunk, "m", True)
    off = _cache_file(tmp_path, chunk, "m", False)
    unset = _cache_file(tmp_path, chunk, "m", None)
    assert len({on, off, unset}) == 3
    # 同一输入同一开关必须稳定（否则缓存永不命中）
    assert _cache_file(tmp_path, chunk, "m", False) == off

def test_retry_escalates_to_thinking_when_first_attempt_is_empty(synthetic_doc):
    """空结果块首轮按默认（关思考），重试必须升级开思考 —— 否则整块条目归零。"""
    from bidpilot.extract import build_chunks, extract_chunk
    from bidpilot.llm import FakeLLM

    chunk = build_chunks(synthetic_doc, 2000)[0]
    fake = FakeLLM(['{"tech_requirements": []}', '{"tech_requirements": [{"text": "x", "page": 1, "page_end": 1, "quote": "占位"}]}'], repeat_last=True)
    # 用真实原文片段做 quote，才不会被校验打回
    quote = synthetic_doc.page_text(chunk.page_start)[:20].strip()
    fake.responses[1] = json.dumps(
        {
            "tech_requirements": [
                {
                    "text": "x",
                    "page": chunk.page_start,
                    "page_end": chunk.page_start,
                    "quote": quote,
                }
            ]
        },
        ensure_ascii=False,
    )

    _batch, report = asyncio.run(extract_chunk(fake, synthetic_doc, chunk, max_retries=2))

    assert fake.thinkings[0] is None  # 首轮不覆盖默认
    assert fake.thinkings[1] is True  # 重试升级
    assert report.escalated is True


def test_llm_timeout_default_is_300s_and_overridable(monkeypatch):
    """默认 300s：抽取重试升级成「开思考」后单块 110-170s，180s 会卡掉本可成功的请求（实测 98.9s 返回）。"""
    monkeypatch.delenv("BIDPILOT_LLM_TIMEOUT_S", raising=False)
    assert Settings().llm_timeout_s == 300.0
    monkeypatch.setenv("BIDPILOT_LLM_TIMEOUT_S", "120")
    assert Settings().llm_timeout_s == 120.0
