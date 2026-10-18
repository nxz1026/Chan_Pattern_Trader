"""A 股市场时间的唯一口径（时区锚点）。

R59（审计 H10 / L11）：仓内多处用 ``datetime.now()`` / ``date.today()`` 判断
「今天是不是交易日」「距 15:00 收盘还有多久」，读的是**宿主机本地时区** ——
而生产机是 ``Etc/UTC``（见 ``deploy/cron/crontab`` 自述），于是：

* 北京 15:00–23:00 之间 ``close_countdown`` 仍报「开市」、``seconds_to_close``
  最长多出约 8 小时（H10，纯展示，不污染结构计算）；
* 交易日历查询在北京时间 08:00 之前会用**前一天**的日期，fail-closed 误判
  「今日不能买」（L11）。

本模块把这条口径收敛到一处：市场时区 ``Asia/Shanghai``、收盘 ``15:00``。
需要「现在几点 / 今天几号」的调用方一律走 :func:`market_now` /
:func:`market_today` / :func:`seconds_to_close`，不要自己 ``now()``。

⚠️ **本模块只回答「现在几点 / 今天几号 / 某一天的起止时刻」**。把某个 ``date``
转成 Unix 毫秒（K 线 ``open_ms`` 的既有约定是「该日 UTC 零点」）是另一件事，
不要在 :mod:`cpt.domain.market_time` 里做 —— 那会把两套口径搅在一起，见
``cpt/adapters/a_share_factor.py`` 里 ``open_ms`` 的说明。
"""

from __future__ import annotations

import datetime as dt
from typing import Final
from zoneinfo import ZoneInfo

MARKET_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
"""A 股交易日的时区（北京时间）。"""

MARKET_CLOSE: Final[dt.time] = dt.time(15, 0)
"""A 股连续竞价收盘时刻（北京时间 15:00）。"""


def market_now() -> dt.datetime:
    """当前市场时间（带 ``MARKET_TZ`` 时区，可直接参与时间差运算）。"""
    return dt.datetime.now(dt.UTC).astimezone(MARKET_TZ)


def market_today() -> dt.date:
    """市场时区下的今天 —— 查交易日历、算 BJT 日期分界都用它。"""
    return market_now().date()


def market_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """市场时区下某一天的**半开区间** ``[起, 止)``，两个端点都带 ``MARKET_TZ``。

    给 ``timestamptz`` 列做「某个自然日内」的过滤用。**不要**写成
    ``transition_time >= %s::date``：那是按**服务器时区**切的，而生产机是
    ``Etc/UTC``，传进来的日期却是**北京日** —— 两者在北京 00:00–08:00 之间差
    一整天（R67 / P1-1：``cpt_signal_event`` 生产 312/312 行全部落错窗口，
    交易机在凌晨拉不到当天最新决策）。用显式的 tz-aware 端点还能继续吃到
    ``transition_time`` 上的索引。

    ⚠️ 与 K 线 ``open_ms`` 的约定无关：后者是「该日 UTC 零点」的整数毫秒，
    见 ``cpt/adapters/a_share_factor.py``；两者不可互相换算。
    """
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=MARKET_TZ)
    return start, start + dt.timedelta(days=1)


def seconds_to_close(now: dt.datetime | None = None) -> int:
    """距当日 :data:`MARKET_CLOSE` 的秒数；已收盘返回 0。

    :param now: 带时区的当前时刻；省略即 :func:`market_now`。**不要传宿主本地
        时间**（naive ``datetime`` 会被 ``astimezone`` 当成本地时区解释，正是本
        模块要消灭的那件事），要固定时刻请用
        ``dt.datetime(2026, 10, 8, 14, 30, tzinfo=MARKET_TZ)``。
    """
    moment = market_now() if now is None else now.astimezone(MARKET_TZ)
    close = moment.replace(
        hour=MARKET_CLOSE.hour,
        minute=MARKET_CLOSE.minute,
        second=0,
        microsecond=0,
    )
    return max(0, int((close - moment).total_seconds()))
