"""LLM 层的契约（Protocol 与数据类）。

`application/` 只依赖这里的 Protocol，不依赖任何 SDK、不依赖任何 provider 实现。
这是 `architecture.md` §4.2 的原话：「一个 Protocol + 一个 OpenAI 兼容实现足够」。

## 这一层不做什么

- **不碰 SQL**。结果落库走 `cpt/storage/llm_call_store.py`，由调用方（application）
  传连接进来。这条由 `scripts/check_sql_layering.py` 门禁强制（`llm/` 在禁入名单里）。
- **不认识「结构」「信号」这些领域概念**。它只收字符串、吐字符串。
  「把结构 JSON 变成人话」那是 `application/llm_cases.py` 的编排职责。
- **不阻塞调用方**。同步的 `LLMClient.complete()` 存在，但上层用的是
  `queue.submit()`——fire-and-forget，见 `queue.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResult",
    "LLMUsage",
]


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、响应格式等）。

    **限流不归这里**：`LLMRateLimited` 单独一个子类，因为它的处置完全不同
    （退避重入队列，而不是记失败）。
    """


class LLMRateLimited(LLMError):
    """服务商限流（HTTP 429）。

    实测 `agnes-ai.cn` 的 429 特征（2026-10-01 实测）：

    - **响应体是空的**（一个字节都没有），所以不要试图解析错误消息；
    - **没有 `Retry-After` 头**，所以客户端拿不到服务端给的等待时长，
      只能自己算退避；
    - 限流是**令牌桶**而非硬冷却：限流恢复后仍会零星 429（实测恢复约 20s，
      之后单请求仍有 ~1/8 概率吃到 429）。

    所以调用方**必须**把它当成常态，而不是异常路径。
    """


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """token 用量。

    **刻意没有 ``cost`` 字段**：实测免费档 ``x-litellm-response-cost-*`` 恒为
    0.0，一列永远为 0 的字段就是冗余（迁移 SQL 也没建 ``cost_est``）。换付费
    provider 时再加。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """一次补全请求。

    :param purpose: 用例标识（`explain_structure` / `summarize_diff` / `annotate`），
        会落进审计表的 ``purpose`` 列。
    :param system: 系统提示词。
    :param user: 用户提示词。
    :param max_tokens: 输出上限。缠论结构解释通常 200~400 够了。
    :param temperature: 解释类任务用 0.2 附近 —— 要稳定，不要发挥。
    :param subject_id: 被解释对象的业务 id（结构 id 等），只作审计用。
    """

    purpose: str
    system: str
    user: str
    max_tokens: int = 512
    temperature: float = 0.2
    subject_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResult:
    """一次补全的结果。"""

    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """provider 无关的补全接口。

    实现方**允许**同步阻塞（真实 HTTP 调用本来就是同步的）——
    「不阻塞核心」是靠**上层不直接调它**实现的：HTTP handler 走
    `queue.submit()` 立刻返回，worker 线程才调 `complete()`。
    """

    def complete(self, request: LLMRequest) -> LLMResult:
        """执行一次补全。

        :raises LLMRateLimited: 服务商限流（429）—— 调用方应退避后重试。
        :raises LLMError: 其它失败（鉴权、网络、响应不可解析）。
        """
        ...
