"""``a_share_routes._signal_history`` 的**三条降级路径**（R45 P0-1 · 第六批）。

## 为什么补自己的新代码

覆盖率列表里排前列的 ``_signal_history`` **47.5%**（21/40 行未覆盖）——
是 R45 当天**我自己新写的**生产代码（推荐卡的「信号历史」区）。

自测覆盖率时发现「自己新写的模块覆盖不足」比「老模块忘了补」更刺眼 ——
后者是债，前者是**这一轮引入的**。

## 钉什么

三条降级路径，全是「**读不到就如实说读不到**」：

| 情形 | 期望 |
|---|---|
| 正常 | 拿到历史 + 按口径分组 |
| 纪元读不到 | **仍拿历史**，只是不分组（`epoch_ms=None`） |
| 事件读失败（``SignalEventError``） | ``available=False`` + 写明原因 |
| 整个函数炸了 | ``available=False`` + 写明异常类型 |

⚠️ 关键：**纪元读不到 ≠ 历史丢**。纪元只是用来分「切表前/后」的，
拿不到就退化成「不分组」，**不该把历史一起丢掉**。

全程离线：``current_epoch`` / ``load_signal_events`` 都换假实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.storage.signal_event_store import SignalEventError  # noqa: E402
from cpt.web import a_share_routes  # noqa: E402

_EV = [
    {
        "transition_time": 1_000,
        "status": "confirmed",
        "signal_type": "first_buy",
        "price": 10.0,
        "divergence_status": "detected",
    },
    {
        "transition_time": 5_000,
        "status": "invalidated",
        "signal_type": "first_buy",
        "price": 11.0,
        "divergence_status": "not_detected",
    },
]


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def _get_conn(self) -> str:  # noqa: ANN202
        return "CONN"

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def client(monkeypatch):
    c = _Client()
    import cpt.adapters.a_share_local as local

    monkeypatch.setattr(local, "AShareLocalClient", lambda: c)
    return c


def _patch(monkeypatch, *, epoch, events, events_exc: Exception | None = None) -> None:
    import cpt.storage.factor_epoch_store as fe
    import cpt.storage.signal_event_store as se

    if isinstance(epoch, Exception) or epoch is None:
        monkeypatch.setattr(
            fe, "current_epoch", lambda *a: (_ for _ in ()).throw(epoch) if epoch else None
        )
    else:
        monkeypatch.setattr(fe, "current_epoch", lambda *a: epoch)

    def _load(*a, **k):
        if events_exc:
            raise events_exc
        return list(events or ())

    monkeypatch.setattr(se, "load_signal_events", _load)


class _Epoch:
    def __init__(self, ms: int) -> None:
        self.switched_at = __import__("datetime").datetime.fromtimestamp(
            ms / 1000, __import__("datetime").timezone.utc
        )


def test_normal_path_groups_by_epoch(monkeypatch, client) -> None:
    """正常：拿到历史，并按切换点分「前/后」。"""
    _patch(monkeypatch, epoch=_Epoch(3_000), events=_EV)
    out = a_share_routes._signal_history("600519")  # noqa: SLF001
    assert out["available"] is True
    assert out["count"] == 2
    assert out["legacy_count"] == 1 and out["current_count"] == 1
    assert client.closed, "连接没关"


def test_epoch_unavailable_still_returns_history(monkeypatch, client) -> None:
    """⚠️ **纪元读不到 ≠ 历史丢** —— 拿不到就退化成「不分组」。"""
    _patch(monkeypatch, epoch=RuntimeError("no epoch table"), events=_EV)
    out = a_share_routes._signal_history("600519")  # noqa: SLF001
    assert out["available"] is True, "纪元缺失把历史一起丢了"
    assert out["count"] == 2
    assert out["epoch_ms"] is None
    assert all(i["legacy"] is False for i in out["items"])


def test_events_read_failure_is_reported_not_faked(monkeypatch, client) -> None:
    """事件读失败 ⇒ available=False + 写明原因，**不能返回空列表冒充「没历史」**。"""
    _patch(monkeypatch, epoch=_Epoch(3_000), events=[], events_exc=SignalEventError("db down"))
    out = a_share_routes._signal_history("600519")  # noqa: SLF001
    assert out["available"] is False
    assert out["reason"] == "signal_history_unavailable"
    assert "db down" in out["detail"]


def test_no_history_is_empty_not_error(monkeypatch, client) -> None:
    """确实没有历史 ⇒ available=False、count=0，**不是**错误。"""
    _patch(monkeypatch, epoch=_Epoch(3_000), events=[])
    out = a_share_routes._signal_history("600519")  # noqa: SLF001
    assert out["available"] is False
    assert out["count"] == 0


def test_outer_failure_still_degrades(monkeypatch, client) -> None:
    """整个函数炸了（建连接就失败）⇒ 降级，**不能带崩推荐接口**。"""
    import cpt.adapters.a_share_local as local

    monkeypatch.setattr(
        local, "AShareLocalClient", lambda: (_ for _ in ()).throw(RuntimeError("no db"))
    )
    out = a_share_routes._signal_history("600519")  # noqa: SLF001
    assert out["available"] is False
    assert out["reason"] == "signal_history_error"
    assert "RuntimeError" in out["detail"]
