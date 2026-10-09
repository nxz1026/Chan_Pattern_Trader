"""进程内限流：按「来源 + 端点」计数的滑动窗口（审计 H1 / H3）。

## 为什么需要

R59（审计 H1）：``/api/dashboard/a-share/llm/summarize`` 与 ``.../llm/explain``
每调一次就真花一次 LLM 配额，而路由**没有任何速率上限** —— 一个循环脚本可以在
一分钟内把当天的额度烧光。R59（审计 H3）：``GET /api/dashboard/sources?include_quota=1``
同理，一次请求就是一次 Wind 付费探测。这里提供两者共用的进程内闸门。

## 为什么不上 Redis / 中间件

这是**单进程、单用户**（nginx 后面一个人）的 dashboard：跨进程共享的计数器要引入
新依赖与新的故障面，而它防的是「同一入口被脚本刷」，进程内滑动窗口足够。
多进程部署时每个 worker 各自计数（上限仍是 N×配置），这是已知取舍；真要跨进程
再换实现。

## 为什么 key 里不用 ``X-CPT-User``

S2（``X-CPT-User`` 是自报、零校验）不在本批修复范围。既然来源身份能被请求方随手
改写，把用户名的哈希当限流键等于**给攻击者一个绕过开关**（每换一个名字就换一个
桶）。所以 bucket key 只用「客户端 IP + 端点标签」：IP 来自 socket 对端，
``X-Forwarded-For`` **故意不采信**（未配 nginx 时它可伪造；采信反而变成新的绕过面）。
代价：nginx 转发场景下所有请求同 IP，限流退化为「全局按端点」，对单用户 dashboard
是安全的那一侧。

## 可注入时钟

所有判定只读 ``clock()``（默认 ``time.monotonic``）。测试要证明「窗口过后恢复」
就换一个假时钟推进，**不要 sleep**（审计硬约束 #7）。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["RateLimitDecision", "SlidingWindowLimiter", "client_ip"]

#: bucket key 的数量上限。
#:
#: key 含远端 IP，理论上可被大量不同来源撑爆内存；超过上限时先清掉窗口内已无事件
#: 的 key，再按「最早记录时间」淘汰，保证内存有界。
_MAX_KEYS = 2048


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """一次限流判定的结果。

    Attributes:
        allowed: 是否放行。
        retry_after: 建议等待秒数（向上取整，至少 1）；``allowed=True`` 时为 0。
    """

    allowed: bool
    retry_after: int


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数器（线程安全）。

    窗口语义：只统计 ``(now - window_seconds, now]`` 内的事件。第 ``max_events`` 次
    放行，第 ``max_events + 1`` 次拒绝。

    Args:
        max_events: 窗口内允许的次数上限（必须 ≥ 1）。
        window_seconds: 窗口长度（秒，必须 > 0）。
        clock: 单调时钟，默认 ``time.monotonic``；测试注入假时钟。
    """

    def __init__(
        self,
        *,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_events = max_events
        self._window = float(window_seconds)
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        # 审计硬约束：handler 会被 ThreadingHTTPServer 多线程并发调用，计数必须加锁。
        self._lock = threading.Lock()

    @property
    def max_events(self) -> int:
        """窗口内允许的次数上限（供路由填 ``X-RateLimit-Limit``）。"""
        return self._max_events

    @property
    def window_seconds(self) -> float:
        """窗口长度（秒）。"""
        return self._window

    def check(self, key: str) -> RateLimitDecision:
        """记账并返回是否放行。

        Args:
            key: bucket 标识（例如 ``"llm_explain:127.0.0.1"``）。

        Returns:
            :class:`RateLimitDecision`；被拒时 ``retry_after`` 是窗口内最早那条记录
            过期所需的秒数（向上取整），可直接写进 ``Retry-After``。
        """
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                bucket = deque()
                self._events[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_events:
                # 不记账：被拒的请求不该延长窗口（否则持续刷＝永久封禁）。
                retry = max(1, math.ceil(bucket[0] + self._window - now))
                return RateLimitDecision(allowed=False, retry_after=retry)
            bucket.append(now)
            self._prune_unlocked(cutoff)
            return RateLimitDecision(allowed=True, retry_after=0)

    def _prune_unlocked(self, cutoff: float) -> None:
        """在持锁状态下回收空桶，并在 key 数超限时淘汰最旧的 key。"""
        if len(self._events) <= _MAX_KEYS:
            return
        for existing in [k for k, v in self._events.items() if not v or v[-1] <= cutoff]:
            del self._events[existing]
        while len(self._events) > _MAX_KEYS:
            oldest = min(self._events, key=lambda k: self._events[k][-1])
            del self._events[oldest]


def client_ip(handler: object) -> str:
    """从 handler 取出对端 IP（取不到时退化为 ``"-"``，绝不抛异常）。

    Args:
        handler: ``BaseHTTPRequestHandler`` 实例（鸭子类型，只要 ``client_address``）。

    Returns:
        对端 IP 字符串。
    """
    addr = getattr(handler, "client_address", None)
    if isinstance(addr, tuple) and addr:
        return str(addr[0])
    return "-"
