"""飞书（Feishu/Lark）群机器人 webhook —— CPT 的**告警出口**。

## 为什么单独一个模块

日志落到 journal 之后**没有人看** —— R38 实测：journal 里躺了 22 小时一条真
bug（``LLM 状态落库失败 ... 'str' object is not callable``，2026-10-01 11:55），
没人发现。所以"记下来"不够，得**推出去**。

## 边界（刻意为之）

- **best-effort**：告警失败**绝不能拖垮被观测的路径**。所有异常就地吞掉、
  只记一条 ``warning``，调用方拿到的是 ``False`` 而不是异常。
- **凭据不入库**：webhook 从环境变量 ``CPT_FEISHU_WEBHOOK`` 读，真实值放在
  ``deploy/env/cpt-dashboard.env``（被 .gitignore 挡住），仓里只有
  ``deploy/env/cpt-dashboard.env.example`` 的占位符。
- **不重试**：告警本身失败再重试只会放大故障；交给下一次巡检。

## 消息形态

用最朴素的 ``text`` 类型（不是 interactive 卡片）—— 告警要在任何客户端里
**一眼可读**，花里胡哨的卡片在手机通知里反而看不清。要分节就用 ``**加粗**``
（飞书 text 支持 markdown 子集）。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Final

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_WEBHOOK",
    "notify",
    "notify_problem",
    "webhook_configured",
]

#: webhook 从这个环境变量读
ENV_WEBHOOK: Final[str] = "CPT_FEISHU_WEBHOOK"

#: 告警是**旁路**，不能拖慢主路径 —— 5 秒足够，投不出就放弃
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

#: 飞书 text 消息的单条上限（超长会被截断，表现为"消息莫名少了一截"）
_MAX_CHARS: Final[int] = 3500


def webhook_configured() -> bool:
    """是否配了 webhook（没配时巡检要能在报告里说清，而不是静默失败）。"""
    return bool(os.environ.get(ENV_WEBHOOK, "").strip())


def _clip(text: str) -> str:
    if len(text) <= _MAX_CHARS:
        return text
    # 截断时必须**留痕**，否则"消息少了一截"会变成第二个谜题
    dropped = len(text) - _MAX_CHARS
    return f"{text[:_MAX_CHARS]}\n…（已截断 {dropped} 字符）"


def notify(
    title: str,
    lines: list[str] | tuple[str, ...] = (),
    *,
    webhook: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Any = None,
) -> bool:
    """推一条文本告警。返回 ``True`` = 送达，``False`` = 没配或没送出去。

    刻意**不抛异常**：观测通道不该有能力让业务路径崩掉。
    """
    url = (webhook or os.environ.get(ENV_WEBHOOK, "")).strip()
    if not url:
        _LOG.info("未配置 %s，跳过告警（title=%s）", ENV_WEBHOOK, title)
        return False

    body = [f"**{title}**", *(f"· {line}" for line in lines if line)]
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": _clip("\n".join(body))}},
        ensure_ascii=False,
    ).encode("utf-8")

    def _default_open(req: urllib.request.Request, t: float) -> int:
        with urllib.request.urlopen(req, timeout=t) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200)

    send = opener if opener is not None else _default_open
    try:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        status = send(req, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 这一条 warning 是**该记的**：告警通道本身坏了，必须让人知道
        _LOG.warning("飞书告警发送失败 %s：%s: %s", title, type(exc).__name__, exc)
        return False

    if status >= 300:  # 飞书失败时也返回 200 + errcode，所以还要看 body 语义
        _LOG.warning("飞书告警被拒 %s：HTTP %s", title, status)
        return False
    _LOG.info("飞书告警已送达：%s", title)
    return True


def notify_problem(
    summary: str,
    details: list[str] | tuple[str, ...] = (),
    **kwargs: Any,
) -> bool:
    """巡检专用：标题带 ``[CPT]`` 前缀，方便在群里一眼认出是哪个项目。"""
    return notify(f"[CPT] {summary}", details, **kwargs)
