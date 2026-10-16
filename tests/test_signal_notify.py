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
9. 过期条目在写入时被清理、表大小有硬上限（R59 审计 L18：常驻进程内存有界）。

不调真实 webhook（``cpt.adapters.feishu.notify`` 走 urllib，会真去 POST）。
测试通过 ``_set_pusher_for_test`` 注入 mock；每个测试用 ``reset_for_test`` 清场。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cpt.application import signal_notify
from cpt.domain.models import Signal
from cpt.storage.track_store import TrackStoreError

#: 哑连接：`is_tracked` 在下面被 monkeypatch 掉，只要一个非 None 的占位。
_CONN = object()


def _notify(signal, *, prev_status, code="600519", event_time, conn=_CONN):
    """`maybe_notify` 的测试包装。

    本文件所有用例都打 600519（`is_tracked` 又被上面的 autouse fixture 顶掉），
    把这两个恒定参数收进默认值：一是少噪音，二是 `signal_notify.maybe_notify`
    全名太长，直接写会把行宽顶到 100 以上（ruff E501）。
    """
    return signal_notify.maybe_notify(
        signal, prev_status=prev_status, code=code, event_time=event_time, conn=conn
    )


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
def _tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认「该 code 在关注列表里」。

    本文件测的是节流与消息形状，不是范围收敛（那组用例自己在用例内改这个
    假实现），所以默认放行 —— 否则每个用例都得造一条真库记录。
    """
    monkeypatch.setattr(signal_notify, "is_tracked", lambda conn, code: True)


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """每个用例前清节流缓存 + 还原时间源与推送函数（兜底）。"""
    signal_notify.reset_for_test()
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000)
    yield
    signal_notify.reset_for_test()


# ── 用例 ──────────────────────────────────────────────────────────


def test_first_event_pushes_and_records_throttle() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    pushed = _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN)
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
    assert _notify(sig, prev_status=None, event_time=1, conn=_CONN) is True
    # 推第二次同 (signal_id, status)
    assert _notify(sig, prev_status="structure_ready", event_time=2, conn=_CONN) is False
    assert len(rec.calls) == 1


def test_same_signal_different_status_pushes_again() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    # 第一次：structure_ready
    assert _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN) is True
    # 第二次：跳到 confirmed（key 不同）
    sig_confirmed = _mk_signal(status="confirmed")
    assert _notify(sig_confirmed, prev_status="structure_ready", event_time=2, conn=_CONN) is True
    assert len(rec.calls) == 2
    assert rec.calls[1][0].endswith("· confirmed")


def test_different_signal_same_status_pushes_again() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    # 同一 status、不同 signal_id —— key 不同，第二次也推
    assert (
        _notify(_mk_signal(signal_id="sig-A"), prev_status=None, event_time=1, conn=_CONN) is True
    )
    assert (
        _notify(_mk_signal(signal_id="sig-B"), prev_status=None, event_time=2, conn=_CONN) is True
    )
    assert len(rec.calls) == 2


def test_status_unchanged_short_circuits_without_recording() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal()
    # prev_status 与 signal.status 相同（兜底，正常不会到这里）
    assert _notify(sig, prev_status="structure_ready", event_time=1, conn=_CONN) is False
    assert rec.calls == []


def test_confirmed_message_carries_signal_id_and_prev() -> None:
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(signal_id="sig-X", status="confirmed")
    assert _notify(sig, prev_status="structure_ready", event_time=42, conn=_CONN) is True
    _, lines = rec.calls[0]
    assert any("signal_id: sig-X" in ln for ln in lines)
    assert any("prev_status: structure_ready" in ln for ln in lines)
    assert any("event_time: 42" in ln for ln in lines)


def test_pusher_returning_false_does_not_consume_throttle() -> None:
    """pusher 返回 False（webhook 没配或送达失败）—— 不记节流，下次再试。"""
    rec = _Recorder(ok=False)
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(status="confirmed")
    assert _notify(sig, prev_status="structure_ready", event_time=1, conn=_CONN) is False
    # 同 (sig, status) —— 节流未消耗，再次调
    assert _notify(sig, prev_status="structure_ready", event_time=2, conn=_CONN) is False
    assert len(rec.calls) == 2


def test_pusher_raising_is_swallowed() -> None:
    """pusher 抛异常 —— 吞掉，``maybe_notify`` 仍返回 ``False``。"""

    def boom(title: str, lines: list[str]) -> bool:
        raise RuntimeError("feishu broken")

    signal_notify._set_pusher_for_test(boom)
    pushed = _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN)
    assert pushed is False


def test_throttle_window_can_be_advanced_by_clock() -> None:
    """时间窗口外可以重推 —— clock 注入就为了验证这一点。"""
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    sig = _mk_signal(status="confirmed")
    # t = 0
    assert _notify(sig, prev_status="structure_ready", event_time=0, conn=_CONN) is True
    # t = 23h —— 仍在窗口内
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000 + 23 * 3600 * 1000)
    assert _notify(sig, prev_status="structure_ready", event_time=1, conn=_CONN) is False
    # t = 25h —— 窗口外
    signal_notify._set_clock_for_test(lambda: 1_700_000_000_000 + 25 * 3600 * 1000)
    assert _notify(sig, prev_status="structure_ready", event_time=2, conn=_CONN) is True
    assert len(rec.calls) == 2


# ── R59（审计 L18）：节流表必须有界 ────────────────────────────────────


def test_expired_throttle_entries_are_pruned_on_write() -> None:
    """改前：过期条目永远留在 ``_LAST_PUSH_MS`` 里 —— 常驻进程内存泄漏。"""
    now = 1_700_000_000_000
    stale_key = ("stale-signal", "structure_ready")
    fresh_key = ("fresh-signal", "structure_ready")
    signal_notify._LAST_PUSH_MS[stale_key] = now - signal_notify.THROTTLE_WINDOW_MS - 1
    signal_notify._LAST_PUSH_MS[fresh_key] = now

    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    pushed = _notify(_mk_signal(signal_id="new-signal"), prev_status=None, event_time=1, conn=_CONN)

    assert pushed is True
    assert stale_key not in signal_notify._LAST_PUSH_MS
    assert fresh_key in signal_notify._LAST_PUSH_MS


def test_throttle_table_is_bounded_by_max_entries() -> None:
    """异常峰值（一轮数千个不同 signal_id）也不会让表无界增长。"""
    now = 1_700_000_000_000
    # 全部落在窗口内（否则先被过期分支清掉，测不到上限分支）。
    for i in range(signal_notify.MAX_THROTTLE_ENTRIES):
        signal_notify._LAST_PUSH_MS[(f"sig-{i}", "structure_ready")] = now - i

    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    pushed = _notify(_mk_signal(signal_id="new-signal"), prev_status=None, event_time=1, conn=_CONN)

    assert pushed is True
    assert len(signal_notify._LAST_PUSH_MS) <= signal_notify.MAX_THROTTLE_ENTRIES
    # 最旧的一条（时间最小）被淘汰，刚写入的这条保留。
    oldest = (f"sig-{signal_notify.MAX_THROTTLE_ENTRIES - 1}", "structure_ready")
    assert oldest not in signal_notify._LAST_PUSH_MS
    assert ("new-signal", "structure_ready") in signal_notify._LAST_PUSH_MS


# ── 推送范围（R4/C2） ───────────────────────────────────────────────


def test_not_tracked_is_skipped_without_writing_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """不在关注列表 → 不推，且**不写**节流表。

    "没人关注"与"被节流"是两回事：写进 ``_LAST_PUSH_MS`` 会让该信号即使后来被
    加入关注，24h 内也推不出来。
    """
    monkeypatch.setattr(signal_notify, "is_tracked", lambda conn, code: False)
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)

    pushed = _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN)
    assert pushed is False
    assert rec.calls == []
    assert signal_notify._LAST_PUSH_MS == {}


def test_missing_conn_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """``conn=None`` → 不推 + warning（调用点漏传时绝不误推）。"""
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    with caplog.at_level("WARNING"):
        pushed = _notify(_mk_signal(), prev_status=None, event_time=1, conn=None)
    assert pushed is False
    assert rec.calls == []
    assert "未提供连接" in caplog.text


def test_track_store_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """关注范围读失败（``TrackStoreError``）→ 不推 + warning。

    fail-closed：读不到就当作"没资格推"，绝不把读失败当成"不需要推"静默放行。
    """

    def _boom(conn: object, code: str) -> bool:
        raise TrackStoreError("库挂了")

    monkeypatch.setattr(signal_notify, "is_tracked", _boom)
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    with caplog.at_level("WARNING"):
        pushed = _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN)
    assert pushed is False
    assert rec.calls == []
    assert "关注范围读取失败" in caplog.text


def test_scope_check_runs_before_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """范围判定在节流**之前**：不在列表的信号不占用节流名额。"""
    seen: list[str] = []

    def _tracked(conn: object, code: str) -> bool:
        seen.append(code)
        return False

    monkeypatch.setattr(signal_notify, "is_tracked", _tracked)
    signal_notify._set_pusher_for_test(_Recorder())

    assert _notify(_mk_signal(), prev_status=None, event_time=1, conn=_CONN) is False
    assert seen == ["600519"]
    assert not signal_notify._LAST_PUSH_MS


# ── C2 ③：说人话段 ───────────────────────────────────────────────────


def _patch_human(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    """顶掉存储层的 ``latest_ok_text``。

    ``signal_notify._latest_human_text`` 是**局部 import**（避免 application →
    storage 的模块级耦合），所以必须打在 ``llm_call_store`` 上才生效。
    ``result`` 传 ``Exception`` 实例即模拟读失败。
    """
    from cpt.storage import llm_call_store

    def _fake(conn: object, *, subject_like: str) -> str | None:
        if isinstance(result, Exception):
            raise result
        return None if result is None else str(result)

    monkeypatch.setattr(llm_call_store, "latest_ok_text", _fake)


def test_human_line_is_added_when_cache_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """命中「说人话」缓存 → 推送多一行 ``说人话：…``。"""
    _patch_human(monkeypatch, "这票中枢刚走完一笔向下")
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)

    assert _notify(_mk_signal(), prev_status=None, event_time=1) is True
    assert len(rec.calls) == 1
    _, lines = rec.calls[0]
    assert "说人话：这票中枢刚走完一笔向下" in lines


def test_human_line_is_absent_when_cache_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有缓存 → 不出现该行，但**照推**（人话段是增量，不是前提）。"""
    _patch_human(monkeypatch, None)
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)

    assert _notify(_mk_signal(), prev_status=None, event_time=1) is True
    _, lines = rec.calls[0]
    assert not [line for line in lines if line.startswith("说人话：")]


def test_human_cache_failure_does_not_block_push(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """缓存读失败（``LLMCallError``）→ 只少一段，**绝不吞掉通知**。"""
    from cpt.storage.llm_call_store import LLMCallError

    _patch_human(monkeypatch, LLMCallError("库挂了"))
    rec = _Recorder()
    signal_notify._set_pusher_for_test(rec)
    with caplog.at_level("INFO"):
        assert _notify(_mk_signal(), prev_status=None, event_time=1) is True
    _, lines = rec.calls[0]
    assert not [line for line in lines if line.startswith("说人话：")]
    assert "跳过推送的人话段" in caplog.text


# ── C2 ④：关注列表收盘汇总 ─────────────────────────────────────────


def _summary_row(
    code: str,
    *,
    label: str = "观望",
    buy: float | None = 3.56,
    sell: float | None = 3.92,
    stop: float | None = None,
    raw_close: float | None = 3.73,
) -> dict[str, object]:
    """一行 ``cpt_track_snapshot``（只保留汇总渲染用到的字段）。"""
    return {
        "code": code,
        "note": None,
        "as_of": "2026-10-14T09:05:58+00:00",
        "payload": {
            "current": {"action": "hold", "action_label": label, "raw_close": raw_close},
            "suggested_points": {
                "buy": {"reference": {"price_level": buy}},
                "sell": {"reference": {"price_level": sell}},
                "stop_loss_reference": stop,
            },
        },
    }


def test_watchlist_summary_is_none_when_there_are_no_rows() -> None:
    """一条都渲染不出来 ⇒ None；宁可不推，也不发一条空消息。"""
    assert signal_notify.build_watchlist_summary([]) is None


def test_watchlist_summary_names_every_tracked_code() -> None:
    title, lines = signal_notify.build_watchlist_summary(
        [
            _summary_row("000002"),
            _summary_row("600519", label="长期持有", buy=1374.11, sell=1429.25),
        ]
    )
    assert title.startswith("[CPT 追踪] ")
    assert "2 只" in title
    assert len(lines) == 2
    assert lines[0] == "000002 观望 · 买点 3.56 · 卖点 3.92 · 现价 3.73"
    assert "买点 1374.11" in lines[1]


def test_watchlist_summary_skips_rows_without_a_payload() -> None:
    title, lines = signal_notify.build_watchlist_summary(
        [{"code": "000002", "payload": "not-a-dict"}, _summary_row("600519")]
    )
    assert "1 只" in title
    assert lines == ["600519 观望 · 买点 3.56 · 卖点 3.92 · 现价 3.73"]


def test_watchlist_summary_keeps_a_row_without_points() -> None:
    """只有 ``current``、没有 ``suggested_points`` ⇒ 仍要出现这一只。"""
    row = _summary_row("000002")
    row["payload"] = {"current": {"action": "hold", "action_label": "观望", "raw_close": 3.73}}
    title, lines = signal_notify.build_watchlist_summary([row])
    assert "1 只" in title
    assert lines == ["000002 观望 · 现价 3.73"]


def test_watchlist_summary_falls_back_to_action_and_skips_bad_numbers() -> None:
    """``action_label`` 缺失用 ``action``；``price_level`` 不是数就不写那一段。"""
    row = _summary_row("000002", buy=None, sell=None, raw_close=None)
    payload = row["payload"]
    assert isinstance(payload, dict)
    payload["current"] = {"action": "watch"}
    payload["suggested_points"] = {
        "buy": {"reference": {"price_level": "3.56"}},  # 字符串不是数 ⇒ 不写这一段
        "stop_loss_reference": 3.30,
    }
    title, lines = signal_notify.build_watchlist_summary([row])
    assert "1 只" in title
    assert lines == ["000002 watch · 止损 3.3"]
