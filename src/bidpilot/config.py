"""运行配置。

所有可调项都从环境变量读。

关于降级：LLM 不可用时**默认直接失败**（该章节标 failed，不产出模板句）。
模板应答（`workflow._mock_paragraphs`）只能通过 `BIDPILOT_ALLOW_MOCK=1` 显式开启，
且一旦启用，docx 带着横幅 + `_MOCK` 文件名后缀、CLI 返回非零退出码——
因为「看起来能交、实际是模板句」的交付物比报错危险得多。
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


def _env_flag(name: str, default: bool = False) -> bool:
    """布尔开关：只认显式的真值（1/true/yes/on），其余一律 False（含未设置）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_thinking() -> bool | None:
    """BIDPILOT_LLM_ENABLE_THINKING：未设置 → False（关思考，省钱省时）；
    设为空串 / auto / unset → None（不下发该字段，留给上游默认）。"""
    raw = os.getenv("BIDPILOT_LLM_ENABLE_THINKING")
    if raw is not None and raw.strip().lower() in {"", "auto", "unset", "default"}:
        return None
    return _env_flag("BIDPILOT_LLM_ENABLE_THINKING", False)


def _env_cache_dir() -> Path | None:
    """BIDPILOT_CACHE_DIR：未设置 → out/_cache；设为 0/none/off → 关闭缓存。"""
    raw = os.getenv("BIDPILOT_CACHE_DIR")
    if raw is None:
        return ROOT / "out" / "_cache"
    if raw.strip().lower() in {"0", "none", "off"}:
        return None
    return Path(raw.strip())


@dataclass(slots=True)
class Settings:
    # ---- LLM（OpenAI 兼容）----
    llm_base_url: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_BASE_URL", "http://127.0.0.1:3425/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_API_KEY", "magpie"))
    llm_model: str = field(default_factory=lambda: os.getenv("BIDPILOT_LLM_MODEL", "qwen-cn/qwen3.8-flash"))
    # 300 而不是 180：抽取重试会**升级成开思考**（extract.extract_chunk），单块实测 110-170s；
    # 180s 时真实语料里出现过一次 ReadTimeout，而同一次请求 98.9s 就返回了——
    # 也就是说 180 卡掉的是「慢但会成功」的请求，白丢一块内容。300s 的代价只是失败块多等 2 分钟。
    # 已知更短的供应商/模型可用 BIDPILOT_LLM_TIMEOUT_S 覆盖（见 README「配置」）。
    llm_timeout_s: float = field(default_factory=lambda: _env_float("BIDPILOT_LLM_TIMEOUT_S", 300.0))
    llm_temperature: float = field(default_factory=lambda: _env_float("BIDPILOT_LLM_TEMPERATURE", 0.0))
    # 推理模型的「思考」开关（DashScope qwen3 系支持请求体顶层 enable_thinking）。
    # 默认 False：纯**成本/延迟**取舍 —— 实测同一抽取块关掉思考后 completion token 降到约 1/5.7、
    # 单块耗时降到约 1/7；quote 可回查率不变，但密集技术块会少条目（p15-16 少 1 条），
    # 所以抽取的重试会强制升级成开思考（extract.extract_chunk）。A/B 见 docs/REAL-PROVIDER.md。
    # 设为空串 / auto 表示**不下发该字段**（上游不认这个参数会 400，这类供应商用 auto）。
    llm_enable_thinking: bool | None = field(default_factory=_env_thinking)
    # 模板降级默认关闭：LLM 不可用时章节直接 failed，不产出"看起来能交"的模板句。
    allow_mock: bool = field(default_factory=lambda: _env_flag("BIDPILOT_ALLOW_MOCK", False))

    # ---- 抽取 ----
    # 2000 而不是 3200：3200 时技术章节会出现「一块 50+ 条」的超长请求（实测单块 629s、
    # 且因个别 quote 对不上整块重来），实测分布见 docs/CHUNKING.md。
    chunk_max_chars: int = field(default_factory=lambda: _env_int("BIDPILOT_CHUNK_CHARS", 2000))
    max_retries: int = field(default_factory=lambda: _env_int("BIDPILOT_MAX_RETRIES", 2))
    gen_batch_size: int = field(default_factory=lambda: _env_int("BIDPILOT_GEN_BATCH", 12))

    # ---- 路径 ----
    out_dir: Path = field(default_factory=lambda: Path(os.getenv("BIDPILOT_OUT_DIR", str(ROOT / "out"))))
    cache_dir: Path | None = field(default_factory=_env_cache_dir)
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