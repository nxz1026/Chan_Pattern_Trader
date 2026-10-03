"""LLM 层配置。

**凭据只从环境变量读，永远不进配置文件、永远不进版本库。**
`deploy/env/cpt-dashboard.env` 本身被 `.gitignore` 挡住，同目录的 `.example`
才是入库的那份 —— 新增键只往 `.example` 里加**占位符**。

| 环境变量 | 说明 |
|---|---|
| `CPT_LLM_ENABLED` | `1` 才启用。`0`/未设 → 整个层短路，见 `queue.submit()` |
| `CPT_LLM_PROVIDER` | 目前只有 `openai_compatible` |
| `CPT_LLM_BASE_URL` | 完整端点（OpenAI 兼容的 `/v1/chat/completions`） |
| `CPT_LLM_MODEL` | 实测 `agnes-3.0-flash` |
| `CPT_LLM_API_KEY` | **密钥**。不入库、不入日志 |
| `CPT_LLM_TIMEOUT` | 单次 HTTP 超时秒数 |
| `CPT_LLM_MAX_ATTEMPTS` | 429 退避重入的最大次数，超过即 `status='error'` |
| `CPT_LLM_BACKOFF_BASE` | 退避基数秒，实际等待 = `base × 2^attempt` + jitter |
| `CPT_LLM_BACKOFF_MAX` | 退避上限秒 |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

__all__ = ["LLMConfig", "load_config"]

#: 唯一支持的 provider。首版刻意不引 LangChain 之类重型框架
#: （`architecture.md` §4.4 明确「一个 Protocol + 一个 OpenAI 兼容实现足够」）。
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset({"openai_compatible"})

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _env_str(name: str, default: str = "", source: Mapping[str, str] | None = None) -> str:
    env = os.environ if source is None else source
    return (env.get(name) or default).strip()


def _env_int(name: str, default: int, source: Mapping[str, str] | None = None) -> int:
    raw = _env_str(name, source=source)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float, source: Mapping[str, str] | None = None) -> float:
    raw = _env_str(name, source=source)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False, source: Mapping[str, str] | None = None) -> bool:
    raw = _env_str(name, source=source).lower()
    if not raw:
        return default
    return raw in _TRUE


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """LLM 层运行配置。"""

    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = 30.0
    #: 429 退避重入的最大次数
    max_attempts: int = 5
    #: 退避基数秒：第 n 次重入等待 ≈ ``backoff_base × 2^n`` + jitter
    backoff_base: float = 2.0
    #: 退避上限秒
    backoff_max: float = 60.0

    def missing_reason(self) -> str:
        """配置不可用时给出**具体**原因，供 UI 如实显示。

        刻意不用「LLM 未配置」这种糊弄话 —— 排查时最费时间的就是
        「到底是没 enable、还是没 key、还是 key 写错了」。
        """
        if not self.enabled:
            return "llm_disabled"
        if self.provider not in SUPPORTED_PROVIDERS:
            return f"llm_unknown_provider:{self.provider}"
        if not self.base_url:
            return "llm_missing_base_url"
        if not self.model:
            return "llm_missing_model"
        if not self.api_key:
            return "llm_missing_api_key"
        return ""

    def redacted(self) -> dict[str, object]:
        """可安全落日志/返回给 API 的配置视图（**不含 key**）。"""
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key),
            "timeout": self.timeout,
            "max_attempts": self.max_attempts,
        }


def load_config(environ: Mapping[str, str] | None = None) -> LLMConfig:
    """从环境变量装载配置。

    :param environ: 覆盖用（测试注入）。传 dict 时**只读这份 dict**，
        **完全不碰** ``os.environ``。

    ## R45 修：不再清空 / 改写进程环境

    原实现为了「只读传入的 dict」，做的是::

        previous = dict(os.environ)
        os.environ.clear()          # ← 清空整个进程环境
        os.environ.update(environ)
        try: return load_config()
        finally: os.environ.clear(); os.environ.update(previous)

    注释写的是「不碰进程全局」，**而它字面上就在改进程全局**。两处真实风险：

    1. **CPT 是多线程的**（``ThreadingHTTPServer`` + worker 线程）。任何一个
       线程在 ``clear()`` 与 ``update()`` 之间读环境变量，都会拿到**残缺甚至
       空**的环境 —— 包括 ``RDSHOST`` / ``DB_PW`` / 飞书 webhook。
    2. 谁哪天在生产路径上用了 ``load_config(environ)``（现在只有
       ``tests/test_llm_layer.py`` 用，但它是**公开函数**），整台服务的环境
       会在那一瞬间消失，而异常路径下的 ``finally`` 也救不回来 ——
       其它线程早就读过了。

    改成把 ``source`` 一路传给解析函数，**从根上不碰全局**。
    """
    src = os.environ if environ is None else environ
    return LLMConfig(
        enabled=_env_bool("CPT_LLM_ENABLED", source=src),
        provider=_env_str("CPT_LLM_PROVIDER", "openai_compatible", source=src),
        base_url=_env_str("CPT_LLM_BASE_URL", source=src),
        model=_env_str("CPT_LLM_MODEL", source=src),
        api_key=_env_str("CPT_LLM_API_KEY", source=src),
        timeout=_env_float("CPT_LLM_TIMEOUT", 30.0, source=src),
        max_attempts=_env_int("CPT_LLM_MAX_ATTEMPTS", 5, source=src),
        backoff_base=_env_float("CPT_LLM_BACKOFF_BASE", 2.0, source=src),
        backoff_max=_env_float("CPT_LLM_BACKOFF_MAX", 60.0, source=src),
    )
