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

## 推送范围（R4/C2）

只推**关注列表里**的股票：推送前查 ``public.cpt_track`` 是否有该 code 的活跃行
（:func:`cpt.storage.track_store.is_tracked`）。飞书 webhook 是全局一条，没有
"谁的关注列表"这一维度 —— 但"**有人**关注"还不够：R66 起只认
``CPT_NOTIFY_USERS`` 白名单（默认 :data:`~cpt.storage.track_store.DEFAULT_NOTIFY_USERS`）
里的 ``user_id``，否则往真库打过一行 ``X-CPT-User: smoke_*`` 的测试数据就足以把
告警推给全局群（known-traps #51 第 4 条）。不在白名单或不在关注列表
→ ``return False``，且**不写**节流表（"没人关注"与"被节流"是两回事）。

⚠️ fail-closed：``conn`` 为 ``None`` 或查询抛 ``TrackStoreError`` 时**不推**并记
warning —— 与 ``record_signal_event`` 调用点上游的 R59 取舍一致（读不到就不推），
绝不把"读失败"当成"不需要推"。

这条也顺带堵住旧行为：此前范围是**全局**的（任何会被评估到的票），而评估既跑在
每根 K 线的循环里、也跑在 on-demand 快照上 —— 开一次页面、探一次活、跑一次测试，
只要撞上状态跃迁就会往群里推（2026-10-13 那条 ``000016 · invalidated`` 就是这么
来的）。

## 推送标题

``[CPT 追踪] <signal_type> · <code> · <status>`` —— 前缀
固定便于群里一眼认出是哪个项目的推送，``signal_type`` 与 ``status`` 让用户
直接看出"什么信号的什么动作"。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any, Final

from cpt.adapters.feishu import notify
from cpt.domain.models import Signal
from cpt.storage.track_store import TrackStoreError, is_tracked, parse_notify_users

_LOG = logging.getLogger(__name__)

__all__ = [
    "ENV_NOTIFY_USERS",
    "_set_clock_for_test",
    "_set_pusher_for_test",
    "build_watchlist_summary",
    "maybe_notify",
    "reset_for_test",
]

#: 推送范围白名单的部署开关（逗号分隔 ``user_id``）；未设时用
#: :data:`cpt.storage.track_store.DEFAULT_NOTIFY_USERS`。空白值同样回落默认值
#: （见 ``parse_notify_users``：空白名单会让推送永久静默，比误推更难发现）。
ENV_NOTIFY_USERS: Final[str] = "CPT_NOTIFY_USERS"


#: 24 小时（毫秒）。同 ``(signal_id, status)`` 在此窗口内不重推。
THROTTLE_WINDOW_MS: int = 24 * 60 * 60 * 1000

#: 推送标题前缀，便于群里一眼看出是哪个项目的信号推送。
_TITLE_PREFIX: str = "[CPT 追踪]"

#: 推送里的价格小数位（R4/C2 ⑤）：``signal.price`` 是后复权原值，形如
#: ``8395.3292919005`` —— 直接进消息只会把关键数字淹掉。所有价格一律两位，
#: 与前端（``dash-track.js`` 的 ``toFixed(2)``）和 LLM 提示词（``prompts.py``
#: 的 ``{price:.2f}``）保持一致。
_PRICE_DIGITS: int = 2

#: 后复权倍率的小数位：它**不是价格**，两位会把 ``6.3941`` 与 ``6.3950`` 抹成
#: 同一个数，而倍率是判断"参考价与现价能否直接比"的依据 ⇒ 留四位。
_RATIO_DIGITS: int = 4

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


def _notify_users() -> tuple[str, ...]:
    """本次推送认可的关注用户白名单（``CPT_NOTIFY_USERS``，逗号分隔）。

    每次调用都重读环境（不缓存）—— 单测改 ``monkeypatch.setenv`` 即刻生效，
    也避免"改了 unit 的 EnvironmentFile 但常驻进程还拿着旧值"这类静默。
    """
    return parse_notify_users(os.environ.get(ENV_NOTIFY_USERS))


def _fmt_num(value: Any, digits: int) -> str | None:
    """把数值格式化成固定小数位；**不是数就返回 ``None``**（调用方据此少写一行）。

    与 :func:`_level` 同一套判据：``bool`` 是 ``int`` 的子类但绝不是价格，
    字符串 ``"3.56"`` 也不当数用（那说明上游字段错了，宁可少一行）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.{digits}f}"


def _points_lines(points: dict[str, Any]) -> list[str]:
    """把 ``track_points.compute_points`` 的结果折成几行推送文本（R4/C2 ②）。

    取值一律走 ``.get`` —— 这个 dict 来自另一个模块的纯函数，字段缺了宁可少一
    行，也不能让整条推送因为 ``KeyError`` 丢掉。

    价格一律两位小数（R4/C2 ⑤，见 :data:`_PRICE_DIGITS`）。
    """
    out: list[str] = []
    action = points.get("action")
    status = points.get("status")
    if action:
        suffix = f"（{status}）" if status else ""
        out.append(f"动作: {action}{suffix}")
    buy = (points.get("buy") or {}).get("reference") or {}
    buy_text = _fmt_num(buy.get("price_level"), _PRICE_DIGITS)
    if buy_text is not None:
        out.append(f"参考买点: {buy_text}")
    sell = (points.get("sell") or {}).get("reference") or {}
    sell_text = _fmt_num(sell.get("price_level"), _PRICE_DIGITS)
    if sell_text is not None:
        out.append(f"参考卖点: {sell_text}")
    stop_text = _fmt_num(points.get("stop_loss_reference"), _PRICE_DIGITS)
    if stop_text is not None:
        out.append(f"止损参考: {stop_text}")
    close_text = _fmt_num(points.get("raw_close"), _PRICE_DIGITS)
    if close_text is not None:
        out.append(f"现价: {close_text}")
    ratio_text = _fmt_num(points.get("price_ratio"), _RATIO_DIGITS)
    if ratio_text is not None:
        out.append(f"后复权倍率: {ratio_text}")
    reason = points.get("reason")
    if reason:
        out.append(f"说明: {reason}")
    return out


def _level(points: dict[str, Any], side: str) -> float | None:
    """取 ``buy``/``sell`` 的参考价；字段缺了或不是数就返回 None。"""
    ref = (points.get(side) or {}).get("reference") or {}
    value = ref.get("price_level")
    return float(value) if isinstance(value, (int, float)) else None


def build_watchlist_summary(
    rows: list[dict[str, Any]],
) -> tuple[str, list[str]] | None:
    """把「关注列表」每只票的最新建议折成一条收盘汇总（R4/C2 ④）。

    :param rows: ``cpt.storage.track_store.latest_active_snapshots`` 的返回
        （已按 code 去重、只留最新一条快照）。
    :returns: ``(title, lines)``；一条都渲染不出来时返回 ``None`` —— 宁可不推，
        也不发一条空消息出去。

    为什么是**另一条**推送：跃迁推送是「有事就说」，而汇总的价值恰恰在于
    「没事也让人看一眼」。所以它不走 :func:`maybe_notify` 的 24h 节流，
    也不看有没有问题 —— 它每天收盘后固定说一次。

    ``current`` 里没有股票名（快照载荷只有代码），所以这里只显示代码。
    """
    entries: list[str] = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        current = payload.get("current")
        if not isinstance(current, dict):
            continue
        code = str(row.get("code") or payload.get("code") or "")
        if not code:
            continue
        label = current.get("action_label") or current.get("action") or "—"
        parts = [f"{code} {label}"]
        points = payload.get("suggested_points")
        if isinstance(points, dict):
            buy = _level(points, "buy")
            sell = _level(points, "sell")
            if buy is not None:
                parts.append(f"买点 {buy:.{_PRICE_DIGITS}f}")
            if sell is not None:
                parts.append(f"卖点 {sell:.{_PRICE_DIGITS}f}")
            stop = points.get("stop_loss_reference")
            if isinstance(stop, (int, float)) and not isinstance(stop, bool):
                parts.append(f"止损 {float(stop):.{_PRICE_DIGITS}f}")
        raw_close = current.get("raw_close")
        if isinstance(raw_close, (int, float)) and not isinstance(raw_close, bool):
            parts.append(f"现价 {float(raw_close):.{_PRICE_DIGITS}f}")
        entries.append(" · ".join(parts))
    if not entries:
        return None
    return f"{_TITLE_PREFIX} 关注列表收盘汇总（{len(entries)} 只）", entries


def _latest_human_text(conn: Any, code: str) -> str | None:
    """取该 code 最近一次成功的「说人话」（跨用户）。

    推送是全局一条（不知道 ``user_id``），所以按 ``track:%:<code>`` 跨用户取 ——
    追踪页的键是 ``track:<user>:<code>``（``cpt/web/track_api.py::_subject_id``）。

    **命中才加，不命中绝不现造 LLM** —— 在推送路径上跑一次 LLM 会引入延迟与
    费用，还可能因为失败反而把整条通知丢了。读失败也只降级成「这次不带人话」。
    """
    from cpt.storage.llm_call_store import latest_ok_text  # noqa: PLC0415

    try:
        return latest_ok_text(conn, subject_like=f"track:%:{code}")
    except Exception as exc:  # noqa: BLE001 — 旁路：读不到就不带这一段
        _LOG.info("跳过推送的人话段（缓存读取失败）code=%s: %s", code, exc)
        return None


def maybe_notify(
    signal: Signal,
    prev_status: str | None,
    code: str,
    event_time: int,
    *,
    conn: Any | None = None,
    points: dict[str, Any] | None = None,
) -> bool:
    """信号跃迁后调一次。返回是否真的推过一次。

    :param signal: 当前评估后的 ``Signal``（含 ``signal_id / signal_type / status``）。
    :param prev_status: 上一状态；``None`` = 首次评估。
    :param code: A 股 6 位代码。
    :param event_time: 本次评估的事件时间（Unix 毫秒）—— 仅用于消息展示。
    :param conn: 调用方的连接，**只用于读关注列表**。为 ``None`` 时 fail-closed
        不推 —— 调用点必须把自己的 ``conn`` 传进来，本模块不自建连接
        （理由见模块 docstring 的「推送范围」）。
    :param points: ``track_points.compute_points`` 的结果（R4/C2 ②：参考买卖点、
        止损、现价、后复权倍率）。为 ``None`` 时只推信号本身 —— 算不出来不拦
        推送，只是少一段。
    :returns: ``True`` 表示本轮真的发出去了；``False`` 表示不在关注列表 /
        关注范围读失败 / 节流跳过 / 推送失败 / 无状态变化。
    """
    # record_signal_event 已保证「status 变化才进」，所以「无变化」这一档
    # 在本调用点不会触发；但留一道兜底，避免把"无变化"也记进节流 dict。
    if signal.status == prev_status:
        return False

    # 推送范围（R4/C2）：只推关注列表里的票。放在节流**之前** ——
    # 不在列表就不算「本可以推但被节流」，不写 _LAST_PUSH_MS。
    # R66：再加一层 user 白名单 —— "有人关注"不等于"真实用户关注"。
    if conn is None:
        _LOG.warning("跳过飞书推送（未提供连接，无法判定关注范围）code=%s", code)
        return False
    try:
        tracked = is_tracked(conn, code, _notify_users())
    except TrackStoreError as exc:
        # fail-closed：读不到关注范围就不推，绝不把「读失败」当成「不需要推」
        _LOG.warning("跳过飞书推送（关注范围读取失败）code=%s: %s", code, exc)
        return False
    if not tracked:
        _LOG.info("跳过飞书推送（不在关注列表）code=%s status=%s", code, signal.status)
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
    price_text = _fmt_num(signal.price, _PRICE_DIGITS)
    lines = [
        f"signal_id: {signal.signal_id}",
        f"level: {signal.level}",
        f"prev_status: {prev_status or 'none'}",
        f"price: {price_text if price_text is not None else signal.price}",
        f"event_time: {event_time}",
    ]
    if points:
        lines.extend(_points_lines(points))
    if conn is not None:
        human = _latest_human_text(conn, code)
        if human:
            lines.append(f"说人话：{human}")

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
        # 没配 webhook 或推送失败 —— 不记节流，下次状态再变时还试。
        # WARNING：能走到这里说明**已经过了关注列表闸门**，即「本该送到却没送到」，
        # 是一次真实的漏报（2026-10-15 的 000002 就丢在这一行，当时是 INFO）。
        _LOG.warning("飞书推送未送达 signal=%s status=%s", signal.signal_id, signal.status)
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
