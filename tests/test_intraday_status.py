"""R89 B3：盘中采集状态（后端判定 + 前端呈现）单测。**不连库**。

钉四件事：
1. **四种状态不许合并** —— ``never``/``running``/``stale``/``failed`` 各有各的话。
2. **``empty`` / ``skipped`` 不算失败** —— 午休、竞价前都会空，当失败会天天红。
3. **「今天休市」与「采集挂了」必须分开** —— 周末库里最新交易日落后是正常的。
4. **日志坏行不许让整块消失** —— 断电写一半是常态。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cpt.application import intraday_status as ist

TZ = ZoneInfo("Asia/Shanghai")


def _rec(at: datetime, outcome: str, **extra: object) -> dict[str, object]:
    return {"at": at.isoformat(), "outcome": outcome, **extra}


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")


# ───────────────────────── 日志解析 ─────────────────────────


def test_missing_log_returns_empty(tmp_path: Path) -> None:
    assert ist.parse_log_tail(tmp_path / "nope.log") == []


def test_broken_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    p = tmp_path / "x.log"
    now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    p.write_text(
        json.dumps(_rec(now, "ok"))
        + "\n这不是 JSON\n"
        + '{"at": "2026-10-22T10:01:00+08:00", "outcome": "ok"\n'  # 写了一半，缺后括号
        + json.dumps(_rec(now + timedelta(minutes=2), "empty"))
        + "\n"
    )
    got = ist.parse_log_tail(p)
    assert [r["outcome"] for r in got] == ["ok", "empty"]


def test_tail_truncation_drops_the_half_line(tmp_path: Path) -> None:
    p = tmp_path / "x.log"
    base = datetime(2026, 10, 22, 9, 30, tzinfo=TZ)
    _write(p, [_rec(base + timedelta(minutes=i), "ok") for i in range(200)])
    got = ist.parse_log_tail(p, max_bytes=600)
    # 只要解析出来的都是完整 JSON，且数量少于全部
    assert 0 < len(got) < 200
    assert all("outcome" in r for r in got)


# ───────────────────────── 四种状态 ─────────────────────────


def test_no_records_is_never_not_failed() -> None:
    got = ist.collect_status([], now=datetime(2026, 10, 22, 10, 0, tzinfo=TZ))
    assert got["state"] == "never"
    assert "还没跑过" in got["headline"]


def test_recent_ok_is_running() -> None:
    now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    got = ist.collect_status(
        [_rec(now - timedelta(minutes=1), "ok", candidates=25, sealed=25)],
        now=now,
        market_day=date(2026, 10, 22),
    )
    assert got["state"] == "running"
    assert "正在采" in got["headline"]
    assert "候选 25 只" in got["action"]


def test_three_consecutive_errors_is_failed() -> None:
    now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    recs = [_rec(now - timedelta(minutes=3 - i), "error", error="boom") for i in range(3)]
    got = ist.collect_status(recs, now=now)
    assert got["state"] == "failed"
    assert got["consecutive_errors"] == 3
    assert "连续 3 轮报错" in got["headline"]


def test_two_errors_then_ok_resets_the_streak() -> None:
    now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    recs = [
        _rec(now - timedelta(minutes=3), "error"),
        _rec(now - timedelta(minutes=2), "error"),
        _rec(now - timedelta(minutes=1), "ok"),
    ]
    got = ist.collect_status(recs, now=now)
    assert got["state"] == "running"
    assert got["consecutive_errors"] == 0


def test_two_errors_is_not_failed_yet() -> None:
    """阈值是 3：连续两轮报错还不算挂（新浪抖一下很正常）。"""
    now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    recs = [_rec(now - timedelta(minutes=2 - i), "error") for i in range(2)]
    got = ist.collect_status(recs, now=now)
    assert got["state"] == "running"
    assert got["consecutive_errors"] == 2


def test_empty_and_skipped_are_not_failures() -> None:
    """午休与竞价前都会空 —— 把它们当失败会让状态条天天红。

    **必须凑够 ``max_failures`` 条**：只放 2 条的话，就算把 empty 误算进
    连续失败也到不了阈值 3，状态仍是 running —— 测试会假绿（R89 实测：
    第一版只放 2 条，反向注入把 empty 算失败，21 条全绿没一条抓住）。
    """
    now = datetime(2026, 10, 22, 12, 0, tzinfo=TZ)
    recs = [
        _rec(now - timedelta(minutes=6), "empty"),
        _rec(now - timedelta(minutes=4), "skipped", reason="非交易时段"),
        _rec(now - timedelta(minutes=2), "empty"),
    ]
    got = ist.collect_status(recs, now=now)
    assert got["state"] == "running", "空结果堆满 3 条也不许判成挂了"
    assert got["consecutive_errors"] == 0
    assert got["today_errors"] == 0
    assert got["today_skipped"] == 3


def test_old_record_is_stale() -> None:
    now = datetime(2026, 10, 22, 16, 0, tzinfo=TZ)
    got = ist.collect_status([_rec(now - timedelta(hours=5), "ok")], now=now)
    assert got["state"] == "stale"
    assert "小时前" in got["headline"]


# ───────────────────────── 休市 vs 挂了 ─────────────────────────


def test_stale_on_a_non_trading_day_says_market_closed() -> None:
    """周末库里最新交易日落后于今天 ⇒ 说「休市」，不是「采集挂了」。"""
    now = datetime(2026, 10, 24, 11, 0, tzinfo=TZ)  # 周六
    recs = [_rec(datetime(2026, 10, 23, 15, 0, tzinfo=TZ), "ok")]
    got = ist.collect_status(recs, now=now, market_day=date(2026, 10, 23))
    assert got["state"] == "stale"
    assert "休市" in got["headline"]
    assert got["is_market_day_today"] is False


def test_stale_on_a_trading_day_tells_you_to_check_the_timer() -> None:
    now = datetime(2026, 10, 22, 14, 0, tzinfo=TZ)
    recs = [_rec(now - timedelta(hours=2), "ok")]
    got = ist.collect_status(recs, now=now, market_day=date(2026, 10, 22))
    assert got["state"] == "stale"
    assert "cpt-intraday-collect.timer" in got["action"]


def test_failed_wins_over_market_closed() -> None:
    """连续报错比「今天休市」更严重 —— 先报挂了。"""
    now = datetime(2026, 10, 24, 11, 0, tzinfo=TZ)
    recs = [_rec(now - timedelta(minutes=3 - i), "error") for i in range(3)]
    got = ist.collect_status(recs, now=now, market_day=date(2026, 10, 23))
    assert got["state"] == "failed"


def test_unknown_market_day_degrades_to_log_only() -> None:
    now = datetime(2026, 10, 22, 16, 0, tzinfo=TZ)
    got = ist.collect_status([_rec(now - timedelta(hours=5), "ok")], now=now, market_day=None)
    assert got["market_day"] is None
    assert got["is_market_day_today"] is None
    assert got["state"] == "stale"


# ───────────────────────── 路由与前端接线 ─────────────────────────


def test_route_is_registered_and_degrades() -> None:
    """路由必须存在，且坏日志时降级成 available=false 而不是抛。"""
    from cpt.web import app as web

    assert hasattr(web, "_intraday_status_payload")
    src = Path(web.__file__).read_text(encoding="utf-8")
    assert "/api/dashboard/intraday-status" in src
    assert "_intraday_status_payload()" in src


def test_payload_degrades_when_log_path_is_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cpt.web import app as web

    monkeypatch.setenv("CPT_INTRADAY_LOG", str(tmp_path))  # 目录而非文件
    monkeypatch.setattr(web, "_latest_market_day", lambda: None)
    payload = web._intraday_status_payload()
    assert payload["available"] is False
    assert payload["reason"] == "intraday_log_unavailable"


def test_payload_reads_the_configured_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cpt.web import app as web

    now = datetime.now(TZ)
    log = tmp_path / "collect.log"
    _write(log, [_rec(now - timedelta(minutes=1), "ok", candidates=3, sealed=2)])
    monkeypatch.setenv("CPT_INTRADAY_LOG", str(log))
    monkeypatch.setattr(web, "_latest_market_day", lambda: now.date())
    payload = web._intraday_status_payload()
    assert payload["available"] is True
    assert payload["status"]["state"] == "running"
    assert "候选 3 只" in payload["status"]["action"]


def test_frontend_renders_all_four_states_and_hides_on_degrade() -> None:
    """前端必须四个状态各有自己的标记，且降级时隐藏。"""
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "dash-signal.js").read_text(
        encoding="utf-8"
    )
    for state in ("running", "stale", "failed", "never"):
        assert f"{state}:" in src, f"INTRADAY_STATE_LABELS 缺 {state}"
    assert "payload.available !== true" in src
    assert "node.hidden = true" in src


def test_status_line_lives_in_the_first_screen_banner() -> None:
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(
        encoding="utf-8"
    )
    banner_at = html.index('data-testid="market-step-banner"')
    line_at = html.index('data-testid="intraday-status-line"')
    body_at = html.index('data-testid="dashboard-body"')
    assert banner_at < line_at < body_at, "采集状态必须在第一屏横幅里、在 dashboard-body 之前"
    assert "hidden" in html[line_at : line_at + 200]


def test_frontend_hits_the_intraday_route_once() -> None:
    root = Path(__file__).resolve().parents[1] / "dashboard"
    hits = [
        (p.name, p.read_text(encoding="utf-8").count("/intraday-status"))
        for p in root.glob("dash-*.js")
    ]
    total = sum(n for _, n in hits)
    assert total == 1, f"/intraday-status 只许请求一次，实际：{hits}"


def test_status_endpoint_is_not_in_the_hot_polling_loop() -> None:
    """一轮采集 2 分钟一次，30s 轮询是浪费 —— 只跟着 refreshOps 刷。"""
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "dash-ops.js").read_text(
        encoding="utf-8"
    )
    idx = src.index("loadIntradayStatus();")
    # 调用点必须在 refreshOps 里（loadWatchlist 那一组），不在 30s 轮询函数里
    assert "loadWatchlist();" in src[:idx]
