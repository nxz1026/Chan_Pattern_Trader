"""provider 工厂 —— **唯一知道具体实现的地方**（`architecture.md` §4.2）。

上层（``application/llm_cases.py``）只拿 :class:`~cpt.llm.base.LLMClient` Protocol，
不 import 任何 provider 模块。首版只有一个实现，所以这个工厂很薄 —— 厚起来那天
再考虑注册表。
"""

from __future__ import annotations

from cpt.llm.base import LLMClient
from cpt.llm.config import LLMConfig

__all__ = ["build_client"]


def build_client(config: LLMConfig) -> LLMClient:
    """按 ``config.provider`` 构建 client。

    :raises ValueError: provider 未知，或配置不完整（``missing_reason`` 非空）。
        调用方应先看 ``config.missing_reason()`` 给出可操作的原因。
    """
    reason = config.missing_reason()
    if reason:
        raise ValueError(reason)

    if config.provider == "openai_compatible":
        from cpt.llm.providers.openai_compatible import (  # noqa: PLC0415
            OpenAICompatibleClient,
        )

        return OpenAICompatibleClient(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            timeout=config.timeout,
        )

    raise ValueError(f"llm_unknown_provider:{config.provider}")
