"""推荐块的**确定性契约**（R45 新增模块的首批测试）。

## 为什么要单独测

买卖与价格**刻意不放给 LLM**（理由见 ``cpt/application/recommendation.py``），
所以它是**纯函数**、零 IO、零模型 —— 也就意味着**可以完整测**。

这批测试钉住三件最容易出错的事：

1. **失效不取反** —— 「一买失效」不是「卖出」。取反会把失效信号说反，
   而这是最容易被忽略、后果又最直接的一条。
2. **价格回退链** —— ``signal.price`` 缺失时退回最新收盘价，
   两者都缺就 ``None``（前端显示「—」），**不能编一个 0**。
3. **结构缺失一律降级、不抛** —— 推荐块挂在看板主路径上，
   抛了会带崩整页。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.application.recommendation import (  # noqa: E402
    ACTION_BUY,
    ACTION_HOLD,
    ACTION_SELL,
    ACTION_WATCH,
    build_recommendation,
)


def snap(signal: dict | None, candles: list | None = None) -> dict:
    out: dict = {"schema_version": 1}
    if signal is not None:
        out["signal"] = signal
    if candles is not None:
        out["candles"] = candles
    return out


# ── 主路径：四种状态 × 两个方向 ────────────────────────────────
@pytest.mark.parametrize(
    ("status", "expect", "label"),
    [
        ("confirmed", ACTION_BUY, "买入结构"),
        ("candidate", ACTION_WATCH, "关注"),
        ("alert", ACTION_WATCH, "关注"),
        ("invalidated", ACTION_HOLD, "观望"),
    ],
)
def test_first_buy_states(status: str, expect: str, label: str) -> None:
    out = build_recommendation(
        snap({"status": status, "signal_type": "first_buy", "price": 28.5})
    )
    assert out["available"] is True
    assert out["action"] == expect
    assert out["action_label"] == label
    assert out["price"] == 28.5


def test_first_sell_confirmed_is_sell() -> None:
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_sell", "price": 10.0})
    )
    assert out["action"] == ACTION_SELL
    assert out["action_label"] == "卖出结构"


def test_invalidated_buy_is_NOT_sell() -> None:
    """⚠️ 最容易搞反的一条：一买失效 ⇒ **观望**，不是卖出。

    取反会把「之前那个判断不成立了」说成「现在该卖了」——
    方向完全相反，而界面上只显示一个动作词。
    """
    out = build_recommendation(
        snap({"status": "invalidated", "signal_type": "first_buy", "price": 28.5})
    )
    assert out["action"] == ACTION_HOLD
    assert out["action"] != ACTION_SELL
    assert "不取反" in out["reason"]


def test_divergence_is_surfaced() -> None:
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy",
              "price": 1.0, "divergence_status": "detected"})
    )
    assert "背驰" in out["reason"]


# ── 价格回退链 ────────────────────────────────────────────────
def test_price_falls_back_to_last_close() -> None:
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy"},
             candles=[{"close": 10.0}, {"close": 12.5}])
    )
    assert out["price"] == 12.5


def test_price_accepts_string() -> None:
    """快照来自 jsonb，价格可能是字符串。"""
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy", "price": "28.5"})
    )
    assert out["price"] == 28.5


def test_no_price_anywhere_is_none_not_zero() -> None:
    """⚠️ 缺价格必须给 ``None``，**不能给 0** —— 0 会被前端画成「跌到 0」。"""
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy"}, candles=[])
    )
    assert out["price"] is None


# ── 降级：永不抛 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [None, {}, {"signal": None}, {"signal": {}},
     {"signal": {"status": "???"}},
     {"signal": {"status": "confirmed"}}],
)
def test_degrades_never_raises(bad) -> None:
    out = build_recommendation(bad) if bad is not None else build_recommendation(None)  # type: ignore[arg-type]
    assert out["available"] is False
    assert out["action"] == ACTION_HOLD
    assert out["reason"]


def test_non_dict_snapshot_degrades() -> None:
    assert build_recommendation([])["available"] is False  # type: ignore[arg-type]


def test_every_result_has_disclaimer() -> None:
    """看板全程标注「不构成投资建议」—— 推荐块也必须带。"""
    for s in (snap({"status": "confirmed", "signal_type": "first_buy", "price": 1.0}),
              snap(None)):
        assert "非投资建议" in build_recommendation(s)["disclaimer"]
