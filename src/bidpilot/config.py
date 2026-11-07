"""运行配置。

所有可调项都从环境变量读，默认值保证「本机 magpie 网关闭着也能跑」的降级路径可见：
LLM 不可用时 workflow 走 `BIDPILOT_ALLOW_MOCK=1` 的模板应答（仍带来源，会在报告里标注 mocked）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(slots=True)
class Settings:
    # ---- LLM（OpenAI 兼容）----
    llm_base_url: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_BASE_URL", "http://127.0.0.1:3425/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_API_KEY", "magpie"))
    llm_model: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_MODEL", "qwen-cn/qwen3.8-flash"))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("BIDPILOT_LLM_TIMEOUT_S", 180.0))
    llm_temperature: float = field(default_factory=lambda: _env_float("BIDPILOT_LLM_TEMPERATURE", 0.0))
    allow_mock: bool = field(default_factory=lambda: os.getenv("BIDPILOT_ALLOW_MOCK", "1") != "0")

    # ---- 抽取 ----
    chunk_max_chars: int = field(default_factory=lambda: _env_int("BIDPILOT_CHUNK_CHARS", 3200))
    max_retries: int = field(default_factory=lambda: _env_int("BIDPILOT_MAX_RETRIES", 2))

    # ---- 路径 ----
    out_dir: Path = field(default_factory=lambda: Path(os.getenv("BIDPILOT_OUT_DIR", str(ROOT / "out"))))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("BIDPILOT_DB", str(ROOT / "out" / "bidpilot.db"))))
    capability_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("BIDPILOT_CAPABILITIES", str(ROOT / "src" / "bidpilot" / "data" / "capabilities.json"))
        )
    )

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s