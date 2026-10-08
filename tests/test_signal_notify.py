"""``cpt.application.signal_notify`` 纯函数测试。

## 覆盖

1. 首次出现（``prev_status=None``）—— 推一次，记节流。
2. 同 ``(signal_id, status)`` 在 24h 内 —— 跳过（节流）。
3. 同一 ``signal_id`` 不同 ``status`` —— 推（key 包含 status）。
4. 不同 ``signal_id`` 同 ``status`` —— 推（key 包含 signal_id）。
5. ``signal.status == prev_status`` —— 跳过（兜底；正常不会到这里）。
6. ``signal.status == "confirmed"`` 时告警消息字段正确填充。
7. pusher 抛异常 —— 吞掉，``maybe_notify`` 仍返回 ``False``；下一次再试。
8. 24h 窗口外可以重推 —— clock 注入可验证。

不调真实 webhook（``cpt.adapters.feishu.notify`` 走 urllib，会真去 POST）。
测试通过 ``_set_pusher_for_test`` 注入 mock；每个测试用 ``reset_for_test`` 清场。
"""

from __future__ import annotations

import pytest
from cpt.application import signal_notify
from cpt.domain.models import Signal

# ── 工具 ──────────────────────────────────────────────────────────


def _mk_signal(signal_id: str = "sig-1", status: str = "structure_ready") -> Signal:
    """最小可用 ``Signal`` —— 测试不依赖完整字段。"""
    return Signal(
        signal_id=signal_id,
        level=1,
        signal_type="first_buy",
        status=status,  # type: ignore[arg-type]
        structure_id="s-1",
        center_ids=(),
        divergence_status="not_checked",
        alert_time=None,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=10.0,
        source_revision=1,
    )


class _Recorder:
    """测试用：记下所有调过的 ``(title, lines)``，并返回指定的成功位。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, title: str, lines: list[str]) -> bool:
        self.calls.append((title, lines))
        return self.ok


@pytest.fixture(autouse=True)
def _isolate() -> None:
    """每个用例前清节流缓存 + 还原时间源与推送函数（兜底）。"""
    signal_notify.reset_for_test()
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000)
    yield
    signal_notify.reset_for_test()


# ── 用例 ──────────────────────────────────────────────────────────


def test_first_event_pushes_and_records_throttle() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    pushed = signal_notify.maybe_notify(
        _mk_signal(), prev_status=None, code="600519", event_time=1
    )
    assert pushed is True
    assert len(rec.calls) == 1
    title, lines = rec.calls[0]
    assert title.startswith("[CPT 追踪] ")
    assert "600519" in title and "structure_ready" in title
    assert any("prev_status: none" in ln for ln in lines)


def test_same_signal_same_status_within_24h_skipped() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal()
    assert signal_notify.maybe_notify(
        sig, prev_status=None, code="600519", event_time=1
    ) is True
    # 推第二次同 (signal_id, status)
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=2
    ) is False
    assert len(rec.calls) == 1


def test_same_signal_different_status_pushes_again() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    # 第一次：structure_ready
    assert signal_notify.maybe_notify(
        _mk_signal(), prev_status=None, code="600519", event_time=1
    ) is True
    # 第二次：跳到 confirmed（key 不同）
    sig_confirmed = _mk_signal(status="confirmed")
    assert signal_notify.maybe_notify(
        sig_confirmed, prev_status="structure_ready", code="600519", event_time=2
    ) is True
    assert len(rec.calls) == 2
    assert rec.calls[1][0].endswith("· confirmed")


def test_different_signal_same_status_pushes_again() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    # 同一 status、不同 signal_id —— key 不同，第二次也推
    assert signal_notify.maybe_notify(
        _mk_signal(signal_id="sig-A"), prev_status=None, code="600519", event_time=1
    ) is True
    assert signal_notify.maybe_notify(
        _mk_signal(signal_id="sig-B"), prev_status=None, code="600519", event_time=2
    ) is True
    assert len(rec.calls) == 2


def test_status_unchanged_short_circuits_without_recording() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal()
    # prev_status 与 signal.status 相同（兜底，正常不会到这里）
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=1
    ) is False
    assert rec.calls == []


def test_confirmed_message_carries_signal_id_and_prev() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(signal_id="sig-X", status="confirmed")
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=42
    ) is True
    _, lines = rec.calls[0]
    assert any("signal_id: sig-X" in ln for ln in lines)
    assert any("prev_status: structure_ready" in ln for ln in lines)
    assert any("event_time: 42" in ln for ln in lines)


def test_pusher_returning_false_does_not_consume_throttle() -> None:
    """pusher 返回 False（webhook 没配或送达失败）—— 不记节流，下次再试。"""
    rec = _Recorder(ok=False)
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(status="confirmed")
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=1
    ) is False
    # 同 (sig, status) —— 节流未消耗，再次调
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=2
    ) is False
    assert len(rec.calls) == 2


def test_pusher_raising_is_swallowed() -> None:
    """pusher 抛异常 —— 吞掉，``maybe_notify`` 仍返回 ``False``。"""

    def boom(title: str, lines: list[str]) -> bool:
        raise RuntimeError("feishu broken")

    signal_notify._set_pusher_for_test(boom)
    pushed = signal_notify.maybe_notify(
        _mk_signal(), prev_status=None, code="600519", event_time=1
    )
    assert pushed is False


def test_throttle_window_can_be_advanced_by_clock() -> None:
    """时间窗口外可以重推 —— clock 注入就为了验证这一点。"""
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(status="confirmed")
    # t = 0
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=0
    ) is True
    # t = 23h —— 仍在窗口内
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000 + 23 * 3600 * 1000)
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=1
    ) is False
    # t = 25h —— 窗口外
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000 + 25 * 3600 * 1000)
    assert signal_notify.maybe_notify(
        sig, prev_status="structure_ready", code="600519", event_time=2
    ) is True
    assert len(rec.calls) == 2
