"""独立 LLM 服务层（R25）。

**独立、可插拔、关闭不影响任何核心功能。** 缠论计算、存储、回放、导出都
不 import 这一层；只有 `application/llm_cases.py` 会调它。这四条约束来自
`architecture.md` §4.1，**本轮起不再是蓝图而是代码**。

| 模块 | 职责 |
|---|---|
| `base` | Protocol 与数据类（`LLMClient` / `LLMRequest` / `LLMResult`） |
| `config` | 环境变量装载 + 可脱敏的诊断视图 |
| `registry` | provider 工厂，唯一知道具体实现的地方 |
| `prompts` | 提示词模板，集中可版本化 |
| `queue` | **异步队列 + 429 指数退避重入**（R25 新增，蓝图未涉及） |
| `providers/openai_compatible` | 首版唯一实现（OpenAI 兼容 HTTP） |

## 三条硬边界

1. **不碰 SQL**。结果落库走 `cpt/storage/llm_call_store.py`，连接由调用方传入。
   这条由 `scripts/check_sql_layering.py` 门禁强制（`llm/` 在禁入名单里）。
2. **不改结构**。只消费已产出的结构/事件 JSON，产出文字**解释**，
   绝不回写结构状态。
3. **不阻塞核心**。HTTP handler 走 `get_queue().submit()` 立刻返回，
   worker 线程才真正调 provider。
"""

from __future__ import annotations

import logging
import threading

from cpt.llm.base import (
    LLMClient,
    LLMError,
    LLMRateLimited,
    LLMRequest,
    LLMResult,
    LLMUsage,
)
from cpt.llm.config import LLMConfig, load_config
from cpt.llm.queue import Job, LLMQueue, SubmitResult
from cpt.llm.registry import build_client

_LOG = logging.getLogger(__name__)

__all__ = [
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "LLMQueue",
    "LLMRequest",
    "LLMResult",
    "LLMRateLimited",
    "LLMUsage",
    "Job",
    "SubmitResult",
    "get_queue",
    "load_config",
    "reset_queue",
]

_QUEUE_LOCK = threading.Lock()
_QUEUE: LLMQueue | None = None


def get_queue(
    *,
    config: LLMConfig | None = None,
    on_status: object = None,
) -> LLMQueue | None:
    """取进程内单例队列；**不可用时返回 ``None`` 而不是抛异常**。

    降级是这一层的常态：没配 key、服务商不支持、provider 名字写错 ——
    任何一种都不该让看板 500。调用方看到 ``None`` 就如实显示「LLM 未启用」。

    :param on_status: ``(call_id, status, detail) -> None``，落库回调。
        用 ``object`` 标注是为了不在 import 时把类型签名拉进来。
    """
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return _QUEUE

        cfg = config or load_config()
        reason = cfg.missing_reason()
        if reason:
            _LOG.info("LLM 层未启用：%s（不影响任何核心功能）", reason)
            return None

        try:
            client = build_client(cfg)
        except ValueError as exc:
            _LOG.warning("LLM provider 构建失败：%s", exc)
            return None

        callback = on_status if callable(on_status) else None
        _QUEUE = LLMQueue(client, cfg, on_status=callback)
        _LOG.info("LLM 队列就绪 provider=%s model=%s", cfg.provider, cfg.model)
        return _QUEUE


def reset_queue() -> None:
    """停掉并清空单例（测试用）。"""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            _QUEUE.stop()
            _QUEUE = None
