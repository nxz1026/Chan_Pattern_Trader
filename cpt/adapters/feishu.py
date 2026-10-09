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
- **200 不等于送达**：飞书失败时**也回 HTTP 200**，把 ``errcode``/``code``
  写在响应体里（限流、webhook 失效、关键词不匹配都走这条路）。所以必须
  解析响应体，只看 HTTP status 会把「没送到」记成「已送达」（2026-10-08 审计 H8）。
- **凭据不入日志**：webhook URL 里带着 hook token（等于写权限）。异常原文常把
  URL 整个带出来（``ValueError: unknown url type: '…'``），所以落日志前必须
  按 :func:`_redact` 抹掉（审计 M9）。

## 消息形态

用最朴素的 ``text`` 类型（不是 interactive 卡片）—— 告警要在任何客户端里
**一眼可读**，花里胡哨的卡片在手机通知里反而看不清。要分节就用 ``**加粗**``
（飞书 text 支持 markdown 子集）。
"""

from __future__ import annotations

import json
import logging
import os
import re
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

#: webhook URL 的尾部就是凭据本身（``/hook/<token>``），落日志前一律抹掉
_HOOK_RE: Final[re.Pattern[str]] = re.compile(r"/hook/[^/\s'\"?]+", re.IGNORECASE)

#: 飞书响应体里表示"到底送没送到"的字段（不同版本/网关叫法不同）
_RESULT_KEYS: Final[tuple[str, ...]] = ("code", "errcode", "StatusCode")


def _redact(text: str, url: str = "") -> str:
    """把 hook token 从准备落日志的文本里抹掉（审计 M9）。

    ``urllib`` 的 ``ValueError``/``URLError`` 常把完整 URL 打进异常原文
    （``unknown url type: 'https://open.feishu.cn/open-apis/bot/v2/hook/xxx'``），
    而那个 URL 等于**往群里发消息的写权限**，不能长期躺在 journal 里。
    """
    out = text.replace(url, "<webhook>") if url else text
    return _HOOK_RE.sub("/hook/<redacted>", out)


def _body_error(text: str) -> str | None:
    """解析飞书响应体：返回错误描述；``None`` = 确认送达。

    ⚠️ 这个函数存在的唯一理由：**飞书失败时也返回 HTTP 200**。可见的失败有
    限流（``code=9499``/``19024``）、webhook 失效、关键词不匹配、签名过期。
    空 body 视作"无从判断"（兼容老替身与裸 200），**不是**送达的证据，但也不
    能否证 —— 保持旧口径；非空且不是 JSON 则**按没送到处理**：告警通道上
    「误报没送到」远比「静默漏报」便宜。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return f"响应体不是 JSON（{stripped[:120]}）"
    if not isinstance(data, dict):
        return f"响应体不是 JSON 对象（{stripped[:120]}）"
    for key in _RESULT_KEYS:
        if key not in data:
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError):
            return f"{key} 不是整数（{data[key]!r}）"
        if value != 0:
            detail = data.get("msg") or data.get("StatusMessage") or ""
            return f"{key}={value} {detail}".strip()
    return None


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
    """推一条文本告警。返回 ``True`` = **确认送达**，``False`` = 没配或没送出去。

    刻意**不抛异常**：观测通道不该有能力让业务路径崩掉。

    ``opener`` 是测试注入点，返回 ``(status, body)``；为了不破坏已有替身，
    也接受裸 ``int``（那种情况只看 status，**校验不到飞书的 errcode**）。
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

    def _default_open(req: urllib.request.Request, t: float) -> tuple[int, str]:
        with urllib.request.urlopen(req, timeout=t) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 200) or 200)
            # ⚠️ body 必须读：飞书失败时也回 200，判定在 body 里（审计 H8）。
            body = resp.read().decode("utf-8", "replace")
        return status, body

    send = opener if opener is not None else _default_open
    try:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        result = send(req, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 这一条 warning 是**该记的**：告警通道本身坏了，必须让人知道。
        # 但异常原文常带完整 webhook（含 token）⇒ 落日志前先抹掉（M9）。
        _LOG.warning(
            "飞书告警发送失败 %s：%s: %s",
            title,
            type(exc).__name__,
            _redact(str(exc), url),
        )
        return False

    if isinstance(result, tuple):
        status, resp_body = int(result[0]), str(result[1] if len(result) > 1 else "")
    else:  # 老替身只回一个状态码 —— 兼容，但校验不到 body 语义
        status, resp_body = int(result), ""

    if status >= 300:
        _LOG.warning("飞书告警被拒 %s：HTTP %s", title, status)
        return False

    problem = _body_error(resp_body)
    if problem is not None:
        # HTTP 200 + errcode ≠ 送达。写成「未送达」而不是「已送达」，
        # 否则 webhook 失效时会变成静默丢失（审计 H8）。
        _LOG.warning("飞书告警未送达 %s：%s", title, _redact(problem, url))
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
