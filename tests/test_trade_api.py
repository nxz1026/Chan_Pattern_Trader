"""``cpt/web/trade_api.py``：决策投喂协议的回归测试。

不触网、不连库：``_fetch_signals`` 在测试里被替换成替身。真正要守住的是三件事——


1. **状态白名单默认只放 ``confirmed``**：策略口径尚未由 owner 定案，
   默认放宽到 ``structure_ready`` 就是在「结构刚成形」时真金白银买入。
   实测当天 first_buy 是 11 条 structure_ready / 26 条 invalidated / **0 条 confirmed**，
   所以默认口径下必须一条都不发。
2. **未知 signal_type 不猜**：猜错等于把「不是买点」的东西当买点发出去。
3. **「读不到」与「今天没信号」必须分开**：读不到回 503 + 明确错误，
   不能回一个空 actions 让交易机以为「今天没信号」而安静地什么都不做。
"""

from __future__ import annotations

from datetime import date

import pytest
from cpt.web import trade_api


@pytest.fixture()
def trade_dir(tmp_path, monkeypatch):
    """把决策状态目录指到 tmp_path，并锁死状态白名单与股数。"""
    monkeypatch.setenv("CPT_TRADE_DIR", str(tmp_path))
    monkeypatch.delenv("CPT_TRADE_BUY_STATUSES", raising=False)
    monkeypatch.delenv("CPT_TRADE_VOLUME", raising=False)
    return tmp_path


def _row(code="601988", signal_type="first_buy", status="confirmed", price=10.5):
    return {
        "code": code,
        "signal_type": signal_type,
        "status": status,
        "level": 2,
        "price": price,
        "signal_id": f"sig-{code}-{signal_type}",
        "transition_time": 1791500000000,
        "confirmed_time": 1791500000000,
    }


# ── 状态白名单 ──────────────────────────────────────────────────────


def test_default_statuses_are_confirmed_only():
    """默认只认 confirmed —— 口径未由 owner 定案前，不得放宽。"""
    assert trade_api.DEFAULT_TRADE_STATUSES == ("confirmed",)
    assert trade_api._statuses() == ("confirmed",)


def test_statuses_env_overrides(monkeypatch):
    monkeypatch.setenv("CPT_TRADE_BUY_STATUSES", "confirmed, structure_ready")
    assert trade_api._statuses() == ("confirmed", "structure_ready")


def test_empty_statuses_env_falls_back_to_default(monkeypatch):
    """配错成空串时回到默认值，而不是「什么都不发」以外的第三种行为。"""
    monkeypatch.setenv("CPT_TRADE_BUY_STATUSES", "  ")
    assert trade_api._statuses() == ("confirmed",)


# ── 动作映射 ────────────────────────────────────────────────────────


def test_first_buy_maps_to_open_pos(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["for_date"] == "2026-10-08"
    assert payload["source"] == "cpt"
    assert payload["batch_id"]
    (action,) = payload["actions"]
    assert action["action"] == "BUY"
    assert action["exec"] == "OPEN_POS"
    assert action["code"] == "601988"
    assert action["volume"] == 100


def test_first_sell_maps_to_close_all(trade_dir, monkeypatch):
    monkeypatch.setattr(
        trade_api, "_fetch_signals", lambda d: (_row(code="600519", signal_type="first_sell"),)
    )
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    (action,) = payload["actions"]
    assert action["action"] == "SELL"
    assert action["exec"] == "CLOSE_ALL"


def test_unknown_signal_type_is_skipped_not_guessed(trade_dir, monkeypatch):
    """★未知类型必须跳过——猜错就是把「不是买点」当买点发出去。"""
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(signal_type="third_buy"),))
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["actions"] == []
    assert payload["batch_id"] == "", "没有可执行动作时不该生成 batch_id"


def test_same_code_two_types_both_kept(trade_dir, monkeypatch):
    """同一只票同日的「一买 + 一卖」是两条不同信号，都要投喂。"""
    monkeypatch.setattr(
        trade_api,
        "_fetch_signals",
        lambda d: (_row(signal_type="first_buy"), _row(signal_type="first_sell")),
    )
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert [a["action"] for a in payload["actions"]] == ["BUY", "SELL"]


def test_price_goes_to_reason_only(trade_dir, monkeypatch):
    """交易机只发市价单，price 不能成为委托价，只能进 reason 供审计。"""
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(price=12.34),))
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    (action,) = payload["actions"]
    assert "12.34" in action["reason"]
    assert "price" not in action


def test_volume_env_overrides(trade_dir, monkeypatch):
    monkeypatch.setenv("CPT_TRADE_VOLUME", "200")
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert payload["actions"][0]["volume"] == 200


# ── 缓存与幂等 ──────────────────────────────────────────────────────


def test_decisions_are_cached_per_date(trade_dir, monkeypatch):
    calls = []

    def fake(for_date):
        calls.append(for_date)
        return (_row(),)

    monkeypatch.setattr(trade_api, "_fetch_signals", fake)
    first, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    second, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert first["batch_id"] == second["batch_id"], "同一天两次请求必须拿到同一个 batch_id"
    assert calls == ["2026-10-08"], "缓存命中时不该再查库"


def test_state_file_is_not_shared_with_emotion_core(trade_dir, monkeypatch):
    """★决策目录必须独立：两个上游共用 state.json 会互相覆盖 decisions 缓存。"""
    assert trade_api.DEFAULT_TRADE_DIR != "/home/ubuntu/trade"
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    trade_api.handle_trade_decisions("date=2026-10-08")
    assert (trade_dir / "state.json").exists()
    assert "cpt" in trade_api.DEFAULT_TRADE_DIR


# ── 「读不到」vs「今天没信号」 ────────────────────────────────────────


def test_source_unavailable_returns_503_not_empty_actions(trade_dir, monkeypatch):
    """★库读不到必须回 503 + 明确错误，绝不能回空 actions。

    回空 actions 时交易机会认为「今天没信号」而安静地什么都不做——
    故障被伪装成正常，是这类接口最危险的失败形态。
    """

    def boom(for_date):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(trade_api, "_fetch_signals", boom)
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 503
    assert "error" in payload and "actions" not in payload


def test_no_actions_returns_empty_batch(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: ())
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["actions"] == []
    assert payload["batch_id"] == ""


def test_invalid_date_returns_400(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    _, status = trade_api.handle_trade_decisions("date=not-a-date")
    assert status == 400


# ── 回执 ────────────────────────────────────────────────────────────


def test_results_posted_and_idempotent(trade_dir):
    payload = {
        "batch_id": "b-1",
        "for_date": "2026-10-08",
        "trades": [{"code": "601988", "status": "FILLED"}],
    }
    first, status = trade_api.handle_trade_results_post(payload)
    assert status == 200 and first["status"] == "ok"
    # 幂等：同一 batch 再来一次，不产生第二个文件
    second, status2 = trade_api.handle_trade_results_post(payload)
    assert status2 == 200 and second.get("note") == "idempotent"
    assert len(list(trade_dir.glob("results_*.json"))) == 1


def test_results_rejects_missing_fields(trade_dir):
    _, status = trade_api.handle_trade_results_post({"batch_id": "b-1"})
    assert status == 400
    _, status2 = trade_api.handle_trade_results_post(None)
    assert status2 == 400


def test_results_get_returns_latest(trade_dir):
    payload = {"batch_id": "b-1", "for_date": "2026-10-08", "trades": []}
    trade_api.handle_trade_results_post(payload)
    got, status = trade_api.handle_trade_results_get("date=2026-10-08")
    assert status == 200 and got["batch_id"] == "b-1"


def test_health_reports_source_identity(trade_dir):
    got, status = trade_api.handle_trade_health()
    assert status == 200
    assert got["source"] == "cpt"
    assert got["service"] == "cpt_trade_api"
    assert got["statuses"] == ["confirmed"]


def test_corrupt_state_raises_rather_than_silent_reset(trade_dir):
    """状态文件损坏必须显式报错，不能悄悄当空状态重跑。"""
    (trade_dir / "state.json").write_text("{ 这不是 JSON", encoding="utf-8")
    _, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 503
    assert (trade_dir / "state.json").read_text(encoding="utf-8") == "{ 这不是 JSON"


def test_today_default_date_is_iso(trade_dir, monkeypatch):
    seen = []
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: seen.append(d) or ())
    trade_api.handle_trade_decisions("")
    assert seen == [date.today().isoformat()]
