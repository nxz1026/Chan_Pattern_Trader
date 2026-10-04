"""实时行情轮询的**降级路径**（R45 P0-1 覆盖率补测）。

## 为什么先补这里

覆盖率扫描把 ``_RealtimeProvider._run`` 标成 **15.4%**（20/25 行未覆盖）
—— 排第一，因为它是**后台轮询主循环**，天生难测。

但里面最有价值的两条分支恰恰和 R45 修了一整天的 bug **同一类**：
**上游挂了 / 数据守卫拒绝时，会不会「静默假装没事」**。

各要保证三件事：

1. 快照换成**降级**的（``runtime.degraded=True`` + 原因）——
   不是把上一份好数据留着冒充新数据；
2. ``_record_poll(ok=False)`` 被调 ⇒ ``_last_poll_ok`` 变 False
   —— 否则 ``/api/dashboard/health`` 一直显示正常，**监控会骗人**；
3. ``bars`` 清空 —— 留着旧 K 线而快照说降级，两边对不上。

⚠️ 降级原因写在 ``runtime.degraded_reason``（不是顶层）——
第一版按「顶层有 degraded 字段」写断言，全错。

全程离线：构造后把 ``_client`` 换成假对象，不联网。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.web import __main__ as web_main  # noqa: E402


class _BoomClient:
    """``fetch_validated_klines`` 抛异常，模拟上游不可达。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def fetch_validated_klines(self, *a: object, **k: object):
        raise self._exc


class _RejectingClient:
    """返回**会被数据守卫拒绝**的 bars（间隔非等 ⇒ 周期错配）。

    ⚠️ 不要用「时间戳全相同」来触发拒绝：那样 ``_infer_interval_ms``
    拿不到任何正间隔，会**回落到名义 5m**，于是 ``close_time`` 对不上，
    拒绝原因变成「契约边界」而不是「周期错配」—— 测的就不是想测的那件事了。
    """

    def fetch_validated_klines(self, *a: object, **k: object):
        from cpt.domain.models import CanonicalBar

        # ⚠️ 守卫要求 ``close_time == open_time + 间隔 - 1``（实测报的
        # 「违反契约边界: 期望 1700003599999」）。第一版我两处都写 +1，
        # 于是「该通过的」和「该被拒的」**都**因为同一个错被拒 ——
        # 测试看着有失败有通过，其实验的不是同一件事。
        # ⚠️ 周期**不能**从这些数据推断不出来，否则守卫会回落到名义 5m，
        # 于是我的 close_time 又对不上（实测报「期望 …+300000-1」）。
        # 真实数据是**等间隔**的；这里必须**先造出正间隔**才能让推断生效。
        base, step = 1_700_000_000_000, 3_600_000
        return [
            CanonicalBar(
                open_time=base + i * step, close_time=base + i * step + step - 1,
                open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0,
                quote_volume=0.0, trade_count=0, taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0, is_closed=True,
            )
            for i in (0, 1, 5, 9)     # 间隔 1×step / 4×step ⇒ 最小正间隔会被推成 step，
                                       # 而 close_time 按 step 算 ⇒ 第 3、4 根周期错配
        ]


def _provider(client: object, *, stop_thread: bool = True) -> object:
    """构造 provider 并**掐掉它自己起的后台线程**。

    ⚠️ ``_RealtimeProvider.__init__`` 最后一行是 ``self._thread.start()``
    —— 构造即开始轮询。第一版没管它，于是后台线程和我手动调的
    ``_poll_once`` **抢同一个假 client**：测试里"第一次应当成功"
    被后台线程先消费掉了，表现为 `assert False is True`。

    ⇒ **驱动它就必须先停它**（``_stop.set()`` + ``join``）。
    """
    p = web_main._RealtimeProvider(  # noqa: SLF001
        symbol="BTCUSDT", interval="1h", limit=50, poll_seconds=0.01
    )
    if stop_thread:
        p._stop.set()  # noqa: SLF001
        p._thread.join(timeout=3)  # noqa: SLF001
    p._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    return p


@pytest.mark.parametrize(
    ("client", "reason_prefix"),
    [
        (_BoomClient(TimeoutError("upstream down")), "upstream_fetch_failed"),
        (_RejectingClient(), "data_guard_rejected"),
    ],
)
def test_poll_failure_degrades_and_records(client, reason_prefix: str) -> None:
    """⚠️ 核心：两条降级路径都**必须**留痕 + 换降级快照 + 清空 bars。"""
    p = _provider(client)
    snap, bars = p._poll_once()  # noqa: SLF001

    assert bars == (), "降级时必须清空 bars —— 否则与降级快照对不上"
    runtime = snap.get("runtime") or {}
    assert runtime.get("degraded") is True, "runtime.degraded 没置位"
    assert str(runtime.get("degraded_reason", "")).startswith(reason_prefix), \
        f"降级原因不对：{runtime.get('degraded_reason')!r}"
    assert p._last_poll_ok is False, "健康状态还停在『好』⇒ 监控会骗人"  # noqa: SLF001


def test_poll_failure_clears_previous_ok_state() -> None:
    """先成功一次、再失败 —— 健康状态**必须**翻过来。"""
    class _OkOnce:
        def __init__(self) -> None:
            self.n = 0

        def fetch_validated_klines(self, *a: object, **k: object):
            from cpt.domain.models import CanonicalBar

            self.n += 1
            if self.n == 1:
                base = 1_700_000_000_000
                return [
                    CanonicalBar(
                        open_time=base + i * 3_600_000,
                        close_time=base + i * 3_600_000 + 3_600_000 - 1,
                        open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0,
                        quote_volume=0.0, trade_count=0, taker_buy_base_volume=0.0,
                        taker_buy_quote_volume=0.0, is_closed=True,
                    )
                    for i in range(30)
                ]
            raise TimeoutError("down")

    p = _provider(_OkOnce())
    p._poll_once()  # noqa: SLF001
    assert p._last_poll_ok is True, "第一次应当成功"  # noqa: SLF001
    p._poll_once()  # noqa: SLF001
    assert p._last_poll_ok is False, "失败后没翻过来 ⇒ health 会一直显示正常"  # noqa: SLF001


def test_run_loop_exits_on_stop() -> None:
    """``_run`` 必须**能停下来** —— 停不下来就是线程泄漏。"""
    p = _provider(_BoomClient(TimeoutError("x")), stop_thread=False)
    t = threading.Thread(target=p._run, daemon=True)  # noqa: SLF001
    t.start()
    time.sleep(0.2)
    p._stop.set()  # noqa: SLF001
    t.join(timeout=5)
    assert not t.is_alive(), "_run 没在 stop 之后退出"
