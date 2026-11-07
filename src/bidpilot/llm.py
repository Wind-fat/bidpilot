"""LLM 客户端（OpenAI 兼容）。

要点：
- 走 OpenAI 兼容的 /chat/completions，base_url / api_key / model 从环境变量读；
- httpx 必须 trust_env=False：本机网关 127.0.0.1 会被系统代理（Clash 之类）接走，表现为永远超时；
- 测试注入 FakeLLM，不依赖网络（见 tests/conftest.py）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import Settings, get_settings


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class Usage:
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    mocked: bool = False


@dataclass(slots=True)
class LLMReply:
    text: str
    usage: Usage = field(default_factory=Usage)


class LLM(Protocol):
    async def complete(self, messages: list[dict], *, json_mode: bool = True, max_tokens: int | None = None) -> LLMReply: ...


class ChatClient:
    """OpenAI 兼容客户端。一次请求一次连接复用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self.usage_log: list[Usage] = []

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,  # 本机网关不能走系统代理
                timeout=self.s.llm_timeout_s,
            )
        return self._client

    async def complete(
        self, messages: list[dict], *, json_mode: bool = True, max_tokens: int | None = None, temperature: float | None = None
    ) -> LLMReply:
        payload: dict = {
            "model": self.s.llm_model,
            "messages": messages,
            "temperature": self.s.llm_temperature if temperature is None else temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        t0 = time.perf_counter()
        try:
            r = await self._http().post(
                f"{self.s.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # 网络 / 认证 / 超时
            raise LLMError(f"LLM 调用失败: {type(exc).__name__}: {exc}") from exc
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not content.strip():
            raise LLMError("LLM 返回空内容")
        u = data.get("usage") or {}
        usage = Usage(
            model=data.get("model", self.s.llm_model),
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        self.usage_log.append(usage)
        return LLMReply(text=content, usage=usage)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class FakeLLM:
    """测试用：按顺序吐出预置响应；可指定重复最后一次。"""

    def __init__(self, responses: list[str], repeat_last: bool = False) -> None:
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.calls: list[list[dict]] = []

    async def complete(self, messages: list[dict], *, json_mode: bool = True, max_tokens: int | None = None) -> LLMReply:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        text = self.responses[idx] if self.responses else "{}"
        return LLMReply(text=text, usage=Usage(model="fake", mocked=True))


def parse_json(text: str) -> dict:
    """模型偶尔把 JSON 包在 fenced block 里，或前后带一句解释。"""
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1]
        if t.lower().lstrip().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as exc:
        raise LLMError(f"输出不是合法 JSON: {text[:200]!r}") from exc