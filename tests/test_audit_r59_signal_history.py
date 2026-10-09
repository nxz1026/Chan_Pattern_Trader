"""R59（审计 M22）：读信号历史失败必须 fail-closed，不能当成「首次评估」。

## 缺陷

``_derive_first_buy_signal`` / ``_derive_first_sell_signal`` 原先用
``previous=None`` **同时** 表示两件事：

- **没有历史行** —— 首次评估（确定的事实）；
- **查询失败** —— 状态未知（没有值）。

状态机 ``assess_first_buy`` 把 ``previous=None`` 当首次评估，于是在
``has_reversal_bi`` 时直接给出 ``confirmed``。于是一次瞬时读失败就能让已经
``invalidated``（粘滞终态）的信号**复活**：写 created 事件、重开 revision、
误推送（审计原话：「绕过 invalidated 终态粘滞」）。

## 判据（断言可观测行为，不断言实现细节）

- 读失败：不写事件、不推送，且回传的 ``prev_status`` 让变化检测恒为「无变化」
  （返回 ``None`` 会被 ``_attach_signal_change`` 读成「新信号」而误报横幅）；
- 真的没有历史行（``load_previous_signal`` 返回 ``None``）：仍然正常写事件 ——
  证明两条路被**分开**了，而不是把所有情况都一律 fail-closed（那会让状态机
  永久停摆）。
"""

from __future__ import annotations

import types
from typing import Any, Literal

import pytest
from cpt.application import a_share_snapshot as snap
from cpt.application import first_buy_bridge, signal_notify
from cpt.domain import signal as signal_domain
from cpt.domain.config import RulesConfig
from cpt.domain.models import Signal

_CODE = "000011"
_LEVEL = RulesConfig().levels[0]


class _FakeConn:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _FakeClient:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def _get_conn(self) -> _FakeConn:
        return self.conn


def _mk_signal(
    status: str = "structure_ready",
    signal_type: Literal["first_buy", "first_sell"] = "first_buy",
) -> Signal:
    return Signal(
        signal_id=f"{signal_type}:{_LEVEL}:bi:1:1000",
        level=_LEVEL,
        signal_type=signal_type,
        status=status,  # type: ignore[arg-type]
        structure_id="bi:1:1000",
        center_ids=("c1",),
        divergence_status="not_checked",
        alert_time=None,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=10.5,
        source_revision=0,
    )


def _facts() -> types.SimpleNamespace:
    # has_reversal_bi=False ⇒ 不走 transition_*；无 bar ⇒ event_time=0。
    return types.SimpleNamespace(
        structure_id="bi:1:1000",
        center_ids=("c1",),
        has_two_centers=True,
        has_divergence_leg=False,
        has_reversal_bi=False,
        divergence_status="none",
    )


class _Spy:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.pushes: list[tuple[Any, ...]] = []

    def record(self, *args: Any, **kwargs: Any) -> bool:
        self.events.append(args)
        return True

    def notify(self, *args: Any, **kwargs: Any) -> bool:
        self.pushes.append(args)
        return True


def _boom(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("db down")


def _level_bis() -> list[Any]:
    return [types.SimpleNamespace(level=_LEVEL, direction=-1)]


def test_first_buy_read_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(snap, "derive_first_buy_facts", lambda **kw: _facts())
    monkeypatch.setattr(snap, "assess_first_buy", lambda **kw: _mk_signal())
    monkeypatch.setattr(snap, "record_signal_event", spy.record)
    monkeypatch.setattr(signal_notify, "maybe_notify", spy.notify)
    monkeypatch.setattr(snap, "load_previous_signal", _boom)

    signal, prev_status = snap._derive_first_buy_signal(
        _level_bis(), [], (), client=_FakeClient(), code=_CODE
    )

    assert signal is not None
    # 改前：prev_status 会塌成 None，于是写一条 created 事件并推送（信号复活）。
    assert spy.events == []
    assert spy.pushes == []
    # 回传本轮 status ⇒ _attach_signal_change 判「无变化」，不会误报「新信号」横幅。
    assert prev_status == signal.status


def test_first_buy_missing_history_row_still_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照：**真的**没有历史行时仍必须推进状态机（区分「未知」与「没有」）。"""
    spy = _Spy()
    monkeypatch.setattr(snap, "derive_first_buy_facts", lambda **kw: _facts())
    monkeypatch.setattr(snap, "assess_first_buy", lambda **kw: _mk_signal())
    monkeypatch.setattr(snap, "record_signal_event", spy.record)
    monkeypatch.setattr(signal_notify, "maybe_notify", spy.notify)
    monkeypatch.setattr(snap, "load_previous_signal", lambda *a, **kw: None)

    _, prev_status = snap._derive_first_buy_signal(
        _level_bis(), [], (), client=_FakeClient(), code=_CODE
    )

    assert prev_status is None  # 首次评估
    assert len(spy.events) == 1
    assert len(spy.pushes) == 1


def test_first_sell_read_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """一卖是同一条纪律（局部 import，故补丁打在两个源模块上）。"""
    spy = _Spy()
    monkeypatch.setattr(first_buy_bridge, "derive_first_sell_facts", lambda **kw: _facts())
    monkeypatch.setattr(
        signal_domain, "assess_first_sell", lambda **kw: _mk_signal(signal_type="first_sell")
    )
    monkeypatch.setattr(snap, "record_signal_event", spy.record)
    monkeypatch.setattr(signal_notify, "maybe_notify", spy.notify)
    monkeypatch.setattr(snap, "load_previous_signal", _boom)

    signal, prev_status = snap._derive_first_sell_signal(
        _level_bis(), [], (), client=_FakeClient(), code=_CODE
    )

    assert signal is not None
    assert spy.events == []
    assert spy.pushes == []
    assert prev_status == signal.status
