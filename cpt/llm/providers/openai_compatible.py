"""OpenAI 兼容 provider —— 首版唯一实现。

对应 `architecture.md` §4.2 的 `providers/openai_compatible.py`。用标准库
`urllib` 而不是 SDK：整个 provider 就是一个 POST + JSON 解析，多一层依赖
就多一层装不上的风险（CI 只装 `requirements-dev.txt`）。

## 错误分类是这一层最重要的职责

实测 `agnes-ai.cn`（2026-10-01）：

- 正常：HTTP 200，320–800 ms（首次冷启动 7.4 s）
- 限流：HTTP **429**，**响应体为空**、**无 `Retry-After`**
- 免费档：`cost` 恒为 0

所以：

- **429 → `LLMRateLimited`**（可退避重试）
- **401/403 → `LLMError`**（key 无效，重试无意义）
- **5xx / 连接错 → 带 `retryable` 标记的 `LLMError`**：服务端瞬时故障 / 请求根本
  没送达，**安全可重试**，由队列按上限退避重入（审计 L6）。
- **超时 → 不带标记的 `LLMError`，刻意不重试**：服务商可能已经受理并计费，
  重试会把一次调用算成两次 —— at-most-once 优先于可用性（审计 L6）。
  `timeout` **只是单次 socket 操作的超时**，不是总时长；总 deadline 由
  `_read_body` 用 `time.monotonic()` 自己兜（审计 M6）。
- 响应 JSON 结构不认识 → `LLMError`（别猜，provider 改协议了就要显式炸）
- **回答不完整（`finish_reason != "stop"`）或内容是空白 → `LLMError`**：
  `length` 意味着被 `max_tokens` 截断，半句话当完整结论落库比报错更糟 ——
  看板上一条读不通的"结论"和一条真结论长得一模一样（2026-10-08 审计 M10）。
- `usage` 里的数字**不可信**（实测有服务商回 `"N/A"`）：解析失败按 0 计，
  绝不能让它以 `ValueError` 逃出本模块（`complete()` 的契约是只抛 `LLMError`）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from cpt.llm.base import LLMError, LLMRateLimited, LLMRequest, LLMResult, LLMUsage

_LOG = logging.getLogger(__name__)

__all__ = ["OpenAICompatibleClient"]

#: 「重试也无用」的 4xx —— 明确区别于 429，给上层不同处置
_FATAL_STATUS = frozenset({400, 401, 403, 404, 413, 422})

#: 这些 ``finish_reason`` 说明回答**没写完**，绝不能当完整结论用
_INCOMPLETE_FINISH = frozenset({"length", "content_filter"})

#: 响应体硬上限（审计 M6）：防止恶意/异常服务端无限吐字节把 worker 撑爆内存。
#: 超过即抛 ``LLMError``，**绝不静默截断** —— 截断的文本会被当完整结论写进
#: ``result_text`` 审计列，一条读得通但内容缺半截的"结论"比报错更危险。
_MAX_RESPONSE_BYTES = 512 * 1024

#: 每次 ``read()`` 的块大小（审计 M6）：小块读才能在块与块之间用 monotonic
#: deadline 收紧剩余时间；一次 ``read()`` 读全部则退化成"永不返回"。
_READ_CHUNK = 64 * 1024

#: 「安全可重试」标记的属性名（审计 L6）。
#:
#: 刻意用 ``setattr`` 打属性而不是新建 ``LLMError`` 子类：``cpt/llm/base.py``
#: 不在本次最小修复的改动范围内，而队列侧只需 ``getattr(exc, "retryable", False)``，
#: 用属性标记的改动面最小、且不打穿 "``complete()`` 只抛 ``LLMError``" 的既有契约。
_RETRYABLE_ATTR = "retryable"


def _retryable_error(message: str) -> LLMError:
    """构造一个标记为「安全可重试」的 ``LLMError``（审计 L6）。"""
    exc = LLMError(message)
    setattr(exc, _RETRYABLE_ATTR, True)
    return exc


def _is_timeout_error(exc: BaseException) -> bool:
    """``urllib`` 的超时既可能是裸 ``TimeoutError``，也可能是包着它的 ``URLError``。"""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TimeoutError)
    return False


def _set_read_timeout(resp: Any, seconds: float) -> None:
    """尽力把底层 socket 的读超时收紧到 ``seconds``（审计 M6，best-effort）。

    ``http.client.HTTPResponse`` 没有公开 API 可以改超时，只能沿
    ``resp.fp.raw._sock`` 摸到底层 socket。摸不到也没关系 —— 外层
    monotonic deadline 才是**正确性依赖**，这里只是让阻塞的 ``read()``
    更快醒来看一眼 deadline，所以失败静默。
    """
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(max(0.05, seconds))
    except (AttributeError, OSError, ValueError):
        pass


class OpenAICompatibleClient:
    """OpenAI 兼容的 `/chat/completions`。

    **同步阻塞**是刻意的：HTTP handler 不直接调它（走 `queue.submit()`），
    只有 worker 线程会，所以「阻塞」不影响任何用户请求。

    :param timeout: **单次 socket 操作**的超时，同时被当作整份响应的
        **总 deadline**（审计 M6）—— 两者共用这一个配置值，刻意不新增配置项：
        「一次调用最多占 worker 多久」本来就是用户配 ``CPT_LLM_TIMEOUT`` 时的
        语义，只是旧实现漏了总时长这一半。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started = time.monotonic()
        # 审计 M6：总 deadline 从**发起请求前**开始算，覆盖连接 + 读体全过程。
        deadline = started + float(self._timeout)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = self._read_body(resp, deadline)
        except urllib.error.HTTPError as exc:
            raise self._classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if _is_timeout_error(exc):
                # 审计 L6：超时**刻意不重试**（at-most-once）。服务商可能已经
                # 受理并计费，重试会把一次调用算成两次。
                raise LLMError(
                    f"LLM 请求超时（总时长 {self._timeout:.0f}s，{type(exc).__name__}）: {exc}"
                ) from exc
            # 审计 L6：连接错（非超时）说明请求根本没送达，安全重试。
            raise _retryable_error(f"LLM 连接失败（{type(exc).__name__}）: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM 响应不是合法 JSON: {exc}") from exc

        return LLMResult(
            text=self._extract_text(data),
            model=str(data.get("model") or self._model),
            usage=self._extract_usage(data),
            latency_ms=latency_ms,
            raw=data,
        )

    # ------------------------------------------------------------------ 内部

    def _read_body(self, resp: Any, deadline: float) -> bytes:
        """读完整份响应体，带**总时长** deadline 与**字节上限**（审计 M6）。

        ``urlopen(timeout=...)`` 只约束单次 socket 操作，不是总时长：服务端每
        20s 吐一字节就能让一次 ``resp.read()`` 永不返回，把唯一的 worker 永久
        卡死、后端队列无界堆积、状态永停 ``running``。所以按块读，每块之前用
        monotonic deadline 算剩余时间，超时或超字节上限都抛 ``LLMError``。
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(
                    f"LLM 响应超过总时长上限（{self._timeout:.0f}s），已取消"
                    "—— 慢速吐字节的服务端不能占住 worker"
                )
            _set_read_timeout(resp, remaining)
            chunk = resp.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                # 不静默截断：截断文本会被当完整结论写进 result_text 审计列
                raise LLMError(f"LLM 响应超过 {_MAX_RESPONSE_BYTES // 1024} KiB 上限，已拒绝")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _classify_http_error(exc: urllib.error.HTTPError) -> LLMError:
        """把 HTTP 错误分成「可退避重试」与「重试无用」两类。"""
        status = int(exc.code)
        if status == 429:
            # 刻意**不读响应体**：实测是空的，读了也只是白费。
            return LLMRateLimited("LLM 服务商限流（HTTP 429）")
        if status in _FATAL_STATUS:
            return LLMError(f"LLM 请求被拒绝（HTTP {status}）—— 检查 base_url / model / api_key")
        if status >= 500:
            # 审计 L6：5xx 是服务端瞬时故障，标记可重试，交给队列退避重入。
            return _retryable_error(f"LLM 服务端错误（HTTP {status}）—— 瞬时故障，将按上限重试")
        return LLMError(f"LLM 服务端错误（HTTP {status}）")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM 响应缺少 choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise LLMError("LLM 响应缺少 choices[0].message")

        reason = choice.get("finish_reason")
        reason = reason.strip() if isinstance(reason, str) else ""
        # 只在 provider **明确**说了原因时才判：缺字段/为 null 时不猜（老网关不给）
        if reason and reason != "stop":
            if reason in _INCOMPLETE_FINISH:
                raise LLMError(
                    f"LLM 回答不完整（finish_reason={reason}）—— 截断/被过滤的回答"
                    "不能当结论落库，请调大 max_tokens 或重试"
                )
            raise LLMError(f"LLM 回答未正常结束（finish_reason={reason}）")

        text = message.get("content")
        if not isinstance(text, str):
            raise LLMError("LLM 响应缺少 choices[0].message.content")
        stripped = text.strip()
        if not stripped:
            # 空回答以 STATUS_OK 落库 = 看板上一条空白"结论"，与实际失败不可区分
            raise LLMError("LLM 响应内容为空（choices[0].message.content 全是空白）")
        return stripped

    @staticmethod
    def _tokens(raw: dict[str, Any], key: str) -> int:
        """``usage`` 里的数字**不可信**（实测有服务商回 ``"N/A"``）。

        直接 ``int()`` 会抛 ``ValueError`` —— 它既不是 ``LLMError``，又打穿了
        「``complete()`` 只抛 ``LLMError``」的契约（审计 M10）。负数也按 0 计。
        """
        value = raw.get(key)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            _LOG.warning("usage.%s 不是整数（%r），按 0 计", key, value)
            return 0

    @classmethod
    def _extract_usage(cls, data: dict[str, Any]) -> LLMUsage:
        raw = data.get("usage")
        if not isinstance(raw, dict):
            return LLMUsage()
        return LLMUsage(
            prompt_tokens=cls._tokens(raw, "prompt_tokens"),
            completion_tokens=cls._tokens(raw, "completion_tokens"),
        )
