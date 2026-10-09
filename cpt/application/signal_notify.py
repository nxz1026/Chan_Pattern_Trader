"""A 股信号跃迁 → 飞书推送（段 3：把"信号动了"送到群里）。

## 为什么单独一个模块

``record_signal_event`` 是 storage 层 —— SQL、connection 边界、事务都在那里。
如果在那里直接调 ``cpt.adapters.feishu.notify``，storage 就开始依赖 adapter，
违反 R24「storage 越纯越好」的精神。所以推送放在 application 层，并在两个
``record_signal_event`` 调用点之后立刻触达。

## 节流

24h 内同 ``(signal_id, status)`` 不重推 —— 内存 dict，进程重启清空。
理由：record_signal_event 本身「同 status 不重写」，但**同一 status 可以因价格
不同写多行**（状态机评估的循环里反复命中同一终态）。24h 节流挡掉这部分噪音，
同时把"重启后历史信号再推一次"作为可接受代价（不是常态路径）。

⚠️ R59（审计 L18）：这个 dict 的 key 含 ``structure_id``（经 ``signal_id``），
结构会不断新增 ⇒ 不做清理就是常驻进程的内存泄漏。写入前一律走
:func:`_prune_throttle`（按窗口过期 + ``MAX_THROTTLE_ENTRIES`` 硬上限），
保证它**有界**。

## 边界（刻意为之）

- **best-effort**：调用方拿到 ``False`` 而不是异常；观测通道不该有能力让业务路径崩。
- **凭据不入库**：webhook 从 ``CPT_FEISHU_WEBHOOK`` 读；仓里只有
  ``deploy/env/cpt-dashboard.env.example`` 的占位符。
- **不重试**：告警本身失败再重试只会放大故障；交给下一次评估轮询。

## 推送标题

``[CPT 追踪] <signal_type> · <code> · <status>`` —— 前缀
固定便于群里一眼认出是哪个项目的推送，``signal_type`` 与 ``status`` 让用户
直接看出"什么信号的什么动作"。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from cpt.adapters.feishu import notify
from cpt.domain.models import Signal

_LOG = logging.getLogger(__name__)

__all__ = [
    "_set_clock_for_test",
    "_set_pusher_for_test",
    "maybe_notify",
    "reset_for_test",
]

#: 24 小时（毫秒）。同 ``(signal_id, status)`` 在此窗口内不重推。
THROTTLE_WINDOW_MS: int = 24 * 60 * 60 * 1000

#: 推送标题前缀，便于群里一眼看出是哪个项目的信号推送。
_TITLE_PREFIX: str = "[CPT 追踪]"

#: 节流缓存：``(signal_id, status) -> 上次推送时间（Unix 毫秒）``。
#: 进程重启清空 —— 设计上接受"重启后历史信号再推一次"作为可接受代价。
_LAST_PUSH_MS: dict[tuple[str, str], int] = {}

#: 节流表硬上限（R59 审计 L18）。
#: key 含 ``structure_id``（经 ``signal_id``），而结构会随时间不断新增 ⇒ 若只增
#: 不减，常驻进程的内存会随每个新结构单调涨。上限针对的是**异常峰值**
#: （一轮里几千个不同 signal_id 同时跃迁），正常路径靠下面按窗口过期淘汰兜住。
MAX_THROTTLE_ENTRIES: int = 4096


def _prune_throttle(now_ms: int) -> None:
    """写入后清理节流表，保证它**有界**（R59 审计 L18）。

    两步：

    1. **按窗口过期**：``now_ms - last >= THROTTLE_WINDOW_MS`` 的条目再也不可能
       挡住任何推送（命中条件是 ``< THROTTLE_WINDOW_MS``），留着纯属内存 —— 这
       就是常驻进程泄漏的来源。
    2. **按上限淘汰**：过期清完仍超过 ``MAX_THROTTLE_ENTRIES`` 时，丢掉最旧的
       若干条（按上次推送时间）。代价是「被淘汰的 key 可能提前允许重推一次」，
       但那只在 24h 内出现数千个不同信号时才会发生，远优于无界增长。
    """
    if not _LAST_PUSH_MS:
        return
    expired = [key for key, last in _LAST_PUSH_MS.items() if now_ms - last >= THROTTLE_WINDOW_MS]
    for key in expired:
        del _LAST_PUSH_MS[key]
    overflow = len(_LAST_PUSH_MS) - MAX_THROTTLE_ENTRIES
    if overflow > 0:
        oldest = sorted(_LAST_PUSH_MS.items(), key=lambda item: item[1])[:overflow]
        for key, _ in oldest:
            del _LAST_PUSH_MS[key]


def _default_clock() -> int:
    """默认时间源：当前 Unix 毫秒。"""
    return int(time.time() * 1000)


def _default_pusher(title: str, lines: list[str]) -> bool:
    """默认推送函数：调 ``cpt.adapters.feishu.notify``。"""
    return notify(title, lines)


#: 测试可注入。
_clock: Callable[[], int] = _default_clock
#: 测试可注入。
_pusher: Callable[[str, list[str]], bool] = _default_pusher


def _set_clock_for_test(clock: Callable[[], int]) -> None:
    """测试用：注入时间源。"""
    global _clock
    _clock = clock


def _set_pusher_for_test(pusher: Callable[[str, list[str]], bool]) -> None:
    """测试用：注入推送函数。"""
    global _pusher
    _pusher = pusher


def maybe_notify(
    signal: Signal,
    prev_status: str | None,
    code: str,
    event_time: int,
) -> bool:
    """信号跃迁后调一次。返回是否真的推过一次。

    :param signal: 当前评估后的 ``Signal``（含 ``signal_id / signal_type / status``）。
    :param prev_status: 上一状态；``None`` = 首次评估。
    :param code: A 股 6 位代码。
    :param event_time: 本次评估的事件时间（Unix 毫秒）—— 仅用于消息展示。
    :returns: ``True`` 表示本轮真的发出去了；``False`` 表示节流跳过 / 推送失败 / 无状态变化。
    """
    # record_signal_event 已保证「status 变化才进」，所以「无变化」这一档
    # 在本调用点不会触发；但留一道兜底，避免把"无变化"也记进节流 dict。
    if signal.status == prev_status:
        return False

    key = (signal.signal_id, signal.status)
    now_ms = _clock()
    last = _LAST_PUSH_MS.get(key)
    if last is not None and now_ms - last < THROTTLE_WINDOW_MS:
        _LOG.info(
            "跳过飞书推送（24h 节流）signal=%s status=%s last=%s now=%s",
            signal.signal_id,
            signal.status,
            last,
            now_ms,
        )
        return False

    title = f"{_TITLE_PREFIX} {signal.signal_type} · {code} · {signal.status}"
    lines = [
        f"signal_id: {signal.signal_id}",
        f"level: {signal.level}",
        f"prev_status: {prev_status or 'none'}",
        f"price: {signal.price}",
        f"event_time: {event_time}",
    ]
    try:
        pushed = _pusher(title, lines)
    except Exception as exc:
        # best-effort：观测通道不该有能力让业务路径崩掉
        _LOG.warning(
            "飞书推送抛异常 signal=%s status=%s %s: %s",
            signal.signal_id,
            signal.status,
            type(exc).__name__,
            exc,
        )
        return False

    if not pushed:
        # 没配 webhook 或推送失败 —— 不记节流，下次状态再变时还试
        _LOG.info("飞书推送未送达 signal=%s status=%s", signal.signal_id, signal.status)
        return False

    # 先写入再清理：``MAX_THROTTLE_ENTRIES`` 是**写入之后**的不变量。若先清理
    # 再插入，表恰好在上限时插入会让它变成 ``MAX + 1``（清理看不到那条还没写
    # 进去的记录），上限就成了摆设。新条目的时间是 ``now_ms``（最新），
    # 淘汰按时间升序，永远不会误删刚记下的这条。
    _LAST_PUSH_MS[key] = now_ms
    _prune_throttle(now_ms)
    return True


def reset_for_test() -> None:
    """测试用：清空节流缓存、还原时间源与推送函数。"""
    _LAST_PUSH_MS.clear()
    _set_clock_for_test(_default_clock)
    _set_pusher_for_test(_default_pusher)
