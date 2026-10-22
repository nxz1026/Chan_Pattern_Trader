"""观察期巡检测试（R83）。

五条判据各有一个「能抓到」的用例 —— 这是**巡检脚本的自检**：
一个永远说「全绿」的巡检比没有巡检更糟（R38 那条「记下来 ≠ 有人看」的延伸）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import scripts.watch_period_check as w

# ───────────────────────────── 假对象 ─────────────────────────────


class _Cursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _SeqConn:
    """按 SQL 内容分发：``max(id)`` 查询给 ``self.id_rows``，其它给 ``self.other``。"""

    def __init__(self, id_rows: list[tuple[int, int]], other: Any = None) -> None:
        self.id_rows = list(id_rows)
        self.other = other

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        if "max(id)" in str(sql):
            return _Cursor(self.id_rows.pop(0) if self.id_rows else None)
        return _Cursor(self.other)

    def close(self) -> None:
        pass


# ───────────────────────────── 序列 ─────────────────────────────


def test_sequence_ok_when_ahead() -> None:
    """``last_value > max(id)`` 是**正常**的：nextval 跳号（回滚/并发/测试）都会这样。"""
    conn = _SeqConn([(100, 150), (200, 260)])
    assert w.check_sequence(conn)["ok"] is True


def test_sequence_reports_lag_per_table() -> None:
    """落后才是病，且要指名是哪张表、差多少。"""
    result = w.check_sequence(_SeqConn([(910570, 900009), (892, 898)]))
    assert result["ok"] is False
    assert len(result["problems"]) == 1
    assert "cpt_structure_event" in result["problems"][0]
    assert "10561" in result["problems"][0]


def test_sequence_handles_empty_table() -> None:
    """空表 ``max(id)`` 是 NULL，不能当成落后。"""
    assert w.check_sequence(_SeqConn([(None, 5), (None, None)]))["ok"] is True


# ───────────────────────────── 批次 ─────────────────────────────


def test_batch_ok_on_a_normal_day() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # 周三
    result = w.check_batch(_SeqConn([], other=(now - timedelta(hours=4), 50)), now=now)
    assert result["ok"] is True


def test_batch_flags_a_stalled_batch_on_a_weekday() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # 周三
    result = w.check_batch(_SeqConn([], other=(now - timedelta(hours=40), 50)), now=now)
    assert result["ok"] is False
    assert "40.0h" in result["problems"][0]


def test_batch_uses_a_wider_window_on_weekends() -> None:
    """周五跑完到周一才下一轮 ⇒ 周末 40h 是**正常**的，不能误报。

    这条钉住的是「日历是判据的一部分」：只用一个阈值会在每个周末误报两次。
    """
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # 周六
    result = w.check_batch(_SeqConn([], other=(now - timedelta(hours=40), 50)), now=now)
    assert result["ok"] is True
    assert result["detail"]["threshold_hours"] == w.BATCH_LAG_ALERT_HOURS_WEEKEND


def test_batch_weekend_window_still_catches_a_two_day_stall() -> None:
    """周末的宽窗口不能宽到「停摆三天也不响」。"""
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # 周六
    result = w.check_batch(_SeqConn([], other=(now - timedelta(hours=100), 50)), now=now)
    assert result["ok"] is False


def test_batch_with_no_rows_is_a_problem_not_a_green() -> None:
    """一条水位都没有 ≠ 一切正常。空表恰恰是最该报的状态。"""
    result = w.check_batch(_SeqConn([], other=None))
    assert result["ok"] is False
    assert "没有任何 A 股水位行" in result["problems"][0]


def test_batch_market_constant_matches_the_live_table() -> None:
    """``market`` 取值是 ``'cn'`` —— R83 第一版写成 ``'ashare'`` 造出过一条假告警。

    这条读真库（不可用时跳过），因为常量写错的代价正好是「巡检永远说批次没跑」。
    """
    try:
        import psycopg
        from cpt.adapters.a_share_local import connection_kwargs

        conn = psycopg.connect(**connection_kwargs())
    except Exception:  # noqa: BLE001
        pytest.skip("真库不可用")
    try:
        distinct = {
            row[0]
            for row in conn.execute("SELECT DISTINCT market FROM public.cpt_run_metric").fetchall()
        }
    finally:
        conn.close()
    if not distinct:
        pytest.skip("cpt_run_metric 为空")
    assert w.ASHARE_MARKET in distinct


# ───────────────────────────── 服务 ─────────────────────────────


def test_service_flags_inactive_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "SERVICE_NAME", "not-a-real-unit.service")
    result = w.check_service()
    assert result["ok"] is False
    assert "inactive" in result["problems"][0]


def test_service_flags_unreachable_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """unit 活着但 health 探不通 —— 这正是「进程在、服务死」的形态。"""
    monkeypatch.setattr(w, "HEALTH_URL", "http://127.0.0.1:9/nope")
    monkeypatch.setattr(w.os, "popen", lambda cmd: _FakePopen("active\n"))
    result = w.check_service()
    assert result["ok"] is False
    assert "探不通" in result["problems"][0]


class _FakePopen:
    def __init__(self, out: str) -> None:
        self._out = out

    def read(self) -> str:
        return self._out

    def close(self) -> None:
        pass


# ───────────────────────────── 盘口 ─────────────────────────────


def test_quote_flags_a_fully_empty_book(monkeypatch: pytest.MonkeyPatch) -> None:
    from cpt.adapters.a_share_public import SinaQuoteClient

    class _C:
        def fetch_quotes(self, codes: Any) -> dict[str, dict[str, Any]]:
            return {"600519": {"last": 0.0, "bids": [None] * 5, "asks": [None] * 5}}

    monkeypatch.setattr(SinaQuoteClient, "fetch_quotes", _C().fetch_quotes)
    result = w.check_quote("600519")
    assert result["ok"] is False
    assert "十档全空" in result["problems"][0]


def test_quote_flags_missing_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from cpt.adapters.a_share_public import SinaQuoteClient

    class _C:
        def fetch_quotes(self, codes: Any) -> dict[str, dict[str, Any]]:
            return {}

    monkeypatch.setattr(SinaQuoteClient, "fetch_quotes", _C().fetch_quotes)
    result = w.check_quote("600519")
    assert result["ok"] is False
    assert "没有返回" in result["problems"][0]


def test_quote_ok_with_a_partial_book(monkeypatch: pytest.MonkeyPatch) -> None:
    """涨停股只有买盘（卖五档全空）也是通的 —— 探针不该把正常形态当故障。"""
    from cpt.adapters.a_share_public import SinaQuoteClient

    class _C:
        def fetch_quotes(self, codes: Any) -> dict[str, dict[str, Any]]:
            return {
                "600519": {
                    "last": 3.99,
                    "date": "2026-10-21",
                    "time": "14:30:00",
                    "bids": [{"price": 3.99, "volume": 100.0}] + [None] * 4,
                    "asks": [None] * 5,
                }
            }

    monkeypatch.setattr(SinaQuoteClient, "fetch_quotes", _C().fetch_quotes)
    result = w.check_quote("600519")
    assert result["ok"] is True
    assert result["detail"]["levels"] == 1


# ───────────────────────────── 告警策略 ─────────────────────────────


def _report(problems: list[str]) -> dict[str, Any]:
    return {
        "checked_at": "2026-10-21T12:00:00+00:00",
        "ok": not problems,
        "problems": problems,
        "checks": [],
    }


def test_state_key_ignores_numbers_that_move_every_run() -> None:
    """指纹只看问题集合 —— 否则延迟数值每小时变一次，等于每次都「状态变了」。"""
    assert w._state_key(_report(["A"])) == w._state_key(_report(["A"]))


def test_first_run_with_problems_still_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """首次没有基线 ≠ 没有故障。首次就发现问题必须发。"""
    monkeypatch.setattr(w, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(w, "collect", lambda **_: _report(["坏了"]))
    monkeypatch.setattr(w, "webhook_configured", lambda: True)
    sent: list[str] = []
    monkeypatch.setattr(w, "notify_problem", lambda s, d=(): sent.append(s) or True)
    rc = w.main([])
    assert rc == 1
    assert sent and "1 个问题" in sent[0]


def test_second_run_with_same_state_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """状态没变就不重复发 —— 天天响等于不响。"""
    monkeypatch.setattr(w, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(w, "collect", lambda **_: _report(["坏了"]))
    monkeypatch.setattr(w, "webhook_configured", lambda: True)
    sent: list[str] = []
    monkeypatch.setattr(w, "notify_problem", lambda s, d=(): sent.append(s) or True)
    w.main([])  # 第一次：发
    w.main([])  # 第二次：静默
    assert len(sent) == 1


def test_recovery_sends_a_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """从坏到好也该知道一声。"""
    monkeypatch.setattr(w, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(w, "webhook_configured", lambda: True)
    sent: list[str] = []
    monkeypatch.setattr(w, "notify_problem", lambda s, d=(): sent.append(s) or True)
    bad = {"v": True}
    monkeypatch.setattr(w, "collect", lambda **_: _report(["坏了"]) if bad["v"] else _report([]))
    w.main([])
    bad["v"] = False
    rc = w.main([])
    assert rc == 0
    assert len(sent) == 2
    assert "恢复" in sent[1]


def test_missing_webhook_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """告警通道自己坏了却报成功，是最坏的失效（run_inspection.py 同款教训）。"""
    monkeypatch.setattr(w, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(w, "collect", lambda **_: _report(["坏了"]))
    monkeypatch.setattr(w, "webhook_configured", lambda: False)
    rc = w.main([])
    assert rc == 78
    assert "未配置" in capsys.readouterr().err


def test_state_file_is_valid_json_after_a_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s.json"
    monkeypatch.setattr(w, "STATE_PATH", state)
    monkeypatch.setattr(w, "collect", lambda **_: _report([]))
    monkeypatch.setattr(w, "webhook_configured", lambda: True)
    monkeypatch.setattr(w, "notify_problem", lambda s, d=(): True)
    w.main([])
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert "state_key" in payload and "at" in payload


# ───────────────────────────── 汇总 ─────────────────────────────


def test_collect_keeps_going_when_one_check_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个检查挂了不能把其它检查的结论一起变没。"""
    monkeypatch.setattr(
        w, "check_service", lambda: {"name": "service", "ok": True, "problems": [], "detail": {}}
    )

    def boom(**_: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(w, "check_quote", boom)
    report = w.collect()
    # 异常被降级成一条问题，而不是把整个巡检带崩
    quote = next(c for c in report["checks"] if c["name"] == "quote")
    assert quote["ok"] is False
    assert any("db down" in p for p in report["problems"])


# ───────────────── R89 B4：盘中采集新鲜度 ─────────────────


def test_intraday_is_skipped_outside_the_session(tmp_path, monkeypatch):
    """收盘后不该判 —— 全天候判就是每天 15:00 之后必红一次。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
    log = tmp_path / "collect.log"
    stale_at = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    log.write_text(
        f'{{"at": "{stale_at.isoformat()}", "outcome": "ok"}}\n',
        encoding="utf-8",
    )
    mod = _load_module()
    monkeypatch.setattr(mod, "INTRADAY_LOG", log)
    # 收盘后：同一条老记录不该报
    got = mod.check_intraday(now=datetime(2026, 10, 22, 16, 0, tzinfo=TZ))
    assert got["ok"] is True, got["problems"]
    assert got["detail"]["in_session"] is False
    # 盘中：超过 10 分钟必须报
    got2 = mod.check_intraday(now=stale_at + timedelta(minutes=30))
    assert got2["ok"] is False
    assert "没有新记录" in got2["problems"][0]


def test_intraday_missing_log_is_healthy_outside_session(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
    mod = _load_module()
    monkeypatch.setattr(mod, "INTRADAY_LOG", tmp_path / "nope.log")
    outside = datetime(2026, 10, 22, 16, 0, tzinfo=TZ)
    assert mod.check_intraday(now=outside)["ok"] is True
    bad = mod.check_intraday(now=datetime(2026, 10, 22, 10, 0, tzinfo=TZ))
    assert bad["ok"] is False
    assert "日志不存在" in bad["problems"][0]


def test_intraday_single_error_is_not_a_problem(tmp_path, monkeypatch):
    """单轮失败是常态 —— 连续失败由 intraday_collectd 自己判。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 22, 10, 30, tzinfo=TZ)
    log = tmp_path / "collect.log"
    line = f'{{"at": "{now.isoformat()}", "outcome": "error", "error": "boom"}}\n'
    log.write_text(line, encoding="utf-8")
    mod = _load_module()
    monkeypatch.setattr(mod, "INTRADAY_LOG", log)
    assert mod.check_intraday(now=now)["ok"] is True


def _load_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "watch_period_check.py"
    spec = importlib.util.spec_from_file_location("watch_period_check_b4", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
