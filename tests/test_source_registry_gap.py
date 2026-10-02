"""A 股探活的「缺整天」判定必须用**交易日历**，不是「周一到周五」（R31）。

## 现场

2026-10-02 真机探测报：

    a_share_local   缺 1 个工作日整天：2026-09-25

而 2026-09-25 是**周五**。第一反应是「数据缺了一天」—— 去查 `public.daily_bar`
确实是 0 行。但再查 `public.trade_calendar`：

    2026-09-24  Thu  is_open=true
    2026-09-25  Fri  is_open=false   ← 法定休市日
    2026-09-28  Mon  is_open=true

**那天根本不开市，没有数据是完全正确的。** 探活在报假警，而且把整个源标成
``status: degraded``。假警比不报警更贵 —— 它会把人引去查一个不存在的数据问题。

## 根因：一行过期的注释

原实现：

    # 工作日缺整天 = 可疑；节假日不在此列（本地没有交易日历，所以只报
    # "工作日无数据"，由人判断是否为节假日）。
    if cursor_day.weekday() < 5 and cursor_day not in have:

「本地没有交易日历」这个前提**早已过期**：`public.trade_calendar` 有 13k 行、
覆盖 1990→2026 底，而 `a_share_local.is_trade_day` 早就在用（收盘倒计时 R21/R24）。

这与 R30 在 `domain/config.py` 里发现的是同一类问题：**环境变了，描述环境的
那句话没跟着变**。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from cpt.adapters import source_registry as sr


class _FakeConn:
    """够探活用的假连接：只认这两条查询。"""

    def __init__(self, per_day: list[tuple[dt.date, int]], open_days: set[dt.date]) -> None:
        self._per_day = per_day
        self._open = open_days

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._c = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, *args: Any) -> None:
        flat = " ".join(sql.split()).lower()
        # ⚠️ 派发顺序有讲究：按天计数的 SQL 里**也**含 `count(*) from
        # public.daily_bar`，先判它就会把按天结果错当成一个总数（1 列），
        # 报出一句看不懂的 IndexError。**先判更具体的。**
        if "group by date" in flat:
            self._rows = [(d, n) for d, n in self._c._per_day]
        elif flat.startswith("select count(*) from public.daily_bar"):
            self._rows = [(sum(n for _, n in self._c._per_day),)]
        elif "count(*) from asel.ref_adjust_factor" in flat:
            self._rows = [(5000,)]
        else:  # pragma: no cover — 探活不该发别的查询
            raise AssertionError(f"探活发了预期外的查询: {sql[:70]}")

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


def _run(monkeypatch: pytest.MonkeyPatch, per_day, open_days) -> dict[str, Any]:
    """跑一次 _probe_a_share_local，注入假连接。"""
    import cpt.adapters.a_share_local as asl

    class _FakeClient:
        def _get_conn(self) -> _FakeConn:
            return _FakeConn(per_day, open_days)

        def close(self) -> None:
            return None

    monkeypatch.setattr(asl, "AShareLocalClient", _FakeClient, raising=False)
    # 探活里写的是 `from cpt.adapters.a_share_local import ... open_days_between`
    # —— 函数内 import 在**调用时**读模块属性，所以只需要打 asl 这一处。
    real = asl.open_days_between
    monkeypatch.setattr(
        asl,
        "open_days_between",
        lambda conn, a, b: (
            real(conn, a, b)
            if not isinstance(conn, _FakeConn)
            else {d for d in conn._open if a <= d <= b}
        ),
        raising=False,
    )
    return dict(sr._probe_a_share_local())  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 真机现场：2026-09-25（周五）是休市日
# --------------------------------------------------------------------------- #


def test_holiday_weekday_is_not_reported_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """周五但休市 → **不能**报缺整天。"""
    thu, mon = dt.date(2026, 9, 24), dt.date(2026, 9, 28)  # 25 号（周五）故意无数据
    per_day = [(thu, 5206), (mon, 5221)]
    open_days = {thu, mon}  # 交易日历说：只有这两天开市

    out = _run(monkeypatch, per_day, open_days)

    assert out["missing_trade_days"] == [], f"休市日被误报成缺整天：{out}"
    assert out["status"] == "ok", out["detail"]
    assert "2026-09-25" not in out["detail"]


def test_real_missing_trading_day_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """真缺的开市日**必须**报出来 —— 不能为了消假警把真警也一起消掉。"""
    thu, fri, mon = dt.date(2026, 9, 24), dt.date(2026, 9, 25), dt.date(2026, 9, 28)
    per_day = [(thu, 5206), (mon, 5221)]  # 25 号没数据
    open_days = {thu, fri, mon}  # 但日历说 25 号**开市**

    out = _run(monkeypatch, per_day, open_days)

    assert out["missing_trade_days"] == ["2026-09-25"], out
    assert out["status"] == "degraded"
    assert "2026-09-25" in out["detail"]


def test_weekend_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """周六周日按定义不在 ``open_days``，不会被当成缺口。"""
    sat, mon = dt.date(2026, 9, 26), dt.date(2026, 9, 28)
    out = _run(monkeypatch, [(sat, 5000), (mon, 5221)], {sat, mon})
    assert out["missing_trade_days"] == []


# --------------------------------------------------------------------------- #
# 日历不可用时：不许安静地回「ok」
# --------------------------------------------------------------------------- #


def test_calendar_unavailable_is_reported_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """日历查不到时退回 weekday 口径，但**必须**说明自己退回了。

    「安静地当成没有缺口」等于把探测器的失效也一起藏起来 —— 那正是本轮
    一路在收拾的那类假象。
    """
    thu = dt.date(2026, 9, 24)
    out = _run(monkeypatch, [(thu, 5206)], set())  # 日历返回空

    assert out["trade_calendar_available"] is False
    assert "交易日历" in out["detail"], out["detail"]
    assert "退回" in out["detail"], out["detail"]


def test_calendar_available_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    thu = dt.date(2026, 9, 24)
    out = _run(monkeypatch, [(thu, 5206)], {thu})
    assert out["trade_calendar_available"] is True


# --------------------------------------------------------------------------- #
# 键名兼容
# --------------------------------------------------------------------------- #


def test_legacy_key_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    """``missing_weekdays`` 保留 —— 可能已有脚本/前端在读它。"""
    thu = dt.date(2026, 9, 24)
    out = _run(monkeypatch, [(thu, 5206)], {thu})
    assert "missing_weekdays" in out
    assert out["missing_weekdays"] == out["missing_trade_days"]
