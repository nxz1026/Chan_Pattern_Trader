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
- **5xx / 超时 / 连接错 → `LLMError`**（可重试但不值得，退避交给上层判断）
- 响应 JSON 结构不认识 → `LLMError`（别猜，provider 改协议了就要显式炸）
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


class OpenAICompatibleClient:
    """OpenAI 兼容的 `/chat/completions`。

    **同步阻塞**是刻意的：HTTP handler 不直接调它（走 `queue.submit()`），
    只有 worker 线程会，所以「阻塞」不影响任何用户请求。
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
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"LLM 请求失败（{type(exc).__name__}）: {exc}") from exc

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

    @staticmethod
    def _classify_http_error(exc: urllib.error.HTTPError) -> LLMError:
        """把 HTTP 错误分成「可退避重试」与「重试无用」两类。"""
        status = int(exc.code)
        if status == 429:
            # 刻意**不读响应体**：实测是空的，读了也只是白费。
            return LLMRateLimited("LLM 服务商限流（HTTP 429）")
        if status in _FATAL_STATUS:
            return LLMError(f"LLM 请求被拒绝（HTTP {status}）—— 检查 base_url / model / api_key")
        return LLMError(f"LLM 服务端错误（HTTP {status}）")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM 响应缺少 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LLMError("LLM 响应缺少 choices[0].message")
        text = message.get("content")
        if not isinstance(text, str):
            raise LLMError("LLM 响应缺少 choices[0].message.content")
        return text.strip()

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> LLMUsage:
        raw = data.get("usage")
        if not isinstance(raw, dict):
            return LLMUsage()
        return LLMUsage(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
        )
