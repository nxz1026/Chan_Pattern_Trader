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

#: 真实 A 股快照**总是**带 candles（本仓下限 30 根）。R45 加数据质量位之后，
#: 早期那些「只放 signal 不放 candles」的夹具全被判成「数据不足」——
#: 那是**夹具不真实**，不是代码错：真实路径上 candles 不会缺。
#: ⇒ 默认给足 120 根，测信号逻辑的用例就不受数据质量位干扰；
#:    测数据质量位的用例显式传自己的根数。
_ENOUGH = [{"close": 1.0}] * 120


def snap(signal: dict | None, candles: list | None = None) -> dict:
    out: dict = {"schema_version": 1, "candles": list(candles if candles is not None else _ENOUGH)}
    if signal is not None:
        out["signal"] = signal
    return out


# ── 主路径：四种状态 × 两个方向 ────────────────────────────────
@pytest.mark.parametrize(
    ("status", "expect", "label"),
    [
        ("confirmed", ACTION_BUY, "买入结构"),
        ("structure_ready", ACTION_WATCH, "关注"),
        ("candidate", ACTION_WATCH, "关注"),
        ("alert", ACTION_WATCH, "关注"),
        ("invalidated", ACTION_HOLD, "观望"),
    ],
)
def test_first_buy_states(status: str, expect: str, label: str) -> None:
    out = build_recommendation(snap({"status": status, "signal_type": "first_buy", "price": 28.5}))
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


# ── invalidated 的两种含义必须分开说 ──────────────────────────
def test_invalidated_without_two_centers_says_condition_not_met() -> None:
    """结构**从未成立**（同级中枢不足两个）⇒「条件不成立」，不是「已失效」。

    实测 000002（2026-10-08）：日线只有一个中枢 → ``invalidated``，但界面上
    写「一买已失效」会让用户以为「之前确认过的一买被打掉了」——
    实际价格离中枢下沿还有 8%。
    """
    out = build_recommendation(
        snap(
            {
                "status": "invalidated",
                "signal_type": "first_buy",
                "price": 28.5,
                "center_ids": [],
            }
        )
    )
    assert out["headline"] == "结构条件不成立"
    assert out["action"] == ACTION_HOLD
    assert "两个同级中枢" in out["reason"]


def test_invalidated_with_two_centers_says_expired() -> None:
    """**曾经成立**（``center_ids`` 有两个中枢）后被打掉 ⇒ 才是「已失效」。"""
    out = build_recommendation(
        snap(
            {
                "status": "invalidated",
                "signal_type": "first_buy",
                "price": 28.5,
                "center_ids": ["bi:4", "bi:8"],
            }
        )
    )
    assert out["headline"] == "信号已失效"
    assert "不取反" in out["reason"]


def test_invalidated_without_center_ids_keeps_old_wording() -> None:
    """缺 ``center_ids`` 的快照 → 不猜，沿用旧措辞（不把「不知道」说成「没有」）。"""
    out = build_recommendation(
        snap({"status": "invalidated", "signal_type": "first_buy", "price": 1.0})
    )
    assert out["headline"] == "信号已失效"


def test_structure_ready_is_watch_not_unknown_status() -> None:
    """``structure_ready`` 是正常状态，不得降级成「暂无明确结构信号」。

    实测 000002 在 2026-10-05 的库里 ``reason`` = 「未知的信号状态
    'structure_ready'」—— 状态映射表漏了这一档。
    """
    out = build_recommendation(
        snap(
            {
                "status": "structure_ready",
                "signal_type": "first_buy",
                "price": 28.5,
                "center_ids": ["bi:4", "bi:8"],
            }
        )
    )
    assert out["available"] is True
    assert out["action"] == ACTION_WATCH
    assert out["status"] == "structure_ready"
    assert "结构已具备" in out["headline"]


def test_divergence_is_surfaced() -> None:
    out = build_recommendation(
        snap(
            {
                "status": "confirmed",
                "signal_type": "first_buy",
                "price": 1.0,
                "divergence_status": "detected",
            }
        )
    )
    assert "背驰" in out["reason"]


# ── 价格回退链 ────────────────────────────────────────────────
def test_price_falls_back_to_last_close() -> None:
    # ⚠️ 蜡烛**给足 120 根**（第一版只给 2 根，被数据质量位判成「不足」而拿不到价）
    bars = [{"close": 10.0}] * 119 + [{"close": 12.5}]
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy"}, candles=bars)
    )
    assert out["data_quality"]["sufficient"] is True
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
    [
        None,
        {},
        {"signal": None},
        {"signal": {}},
        {"signal": {"status": "???"}},
        {"signal": {"status": "confirmed"}},
    ],
)
def test_degrades_never_raises(bad) -> None:
    out = build_recommendation(bad) if bad is not None else build_recommendation(None)  # type: ignore[arg-type]
    assert out["available"] is False
    assert out["action"] == ACTION_HOLD
    assert out["reason"]


def test_snapshot_without_candles_is_insufficient() -> None:
    """一条 candles 都没有 ⇒ 连「不足 N 根」都说不清，直接判不足。"""
    out = build_recommendation(snap(None, []))
    assert out["available"] is False
    assert out["data_quality"]["bars"] == 0
    assert out["data_quality"]["sufficient"] is False


def test_non_dict_snapshot_degrades() -> None:
    assert build_recommendation([])["available"] is False  # type: ignore[arg-type]


def test_every_result_has_disclaimer() -> None:
    """看板全程标注「不构成投资建议」—— 推荐块也必须带。"""
    for s in (snap({"status": "confirmed", "signal_type": "first_buy", "price": 1.0}), snap(None)):
        assert "非投资建议" in build_recommendation(s)["disclaimer"]


# ── 数据质量位：把「不知道」和「没有」分开（R45 P0-2）────────────
def test_too_few_bars_says_data_insufficient_not_no_signal() -> None:
    """⚠️ 核心：**K 线不足**时不能说「暂无明确结构信号」。

    Oracle 上有 17 只新股落在 20 根这个区间 —— 它们原本和
    「这只票确实没信号」显示**一字不差**，用户分不出「系统不知道」
    和「系统知道没有」，而这两件事该有的动作完全不同。
    """
    out = build_recommendation(snap(None, candles=[{"close": 1.0}] * 20))
    assert out["available"] is False
    assert out["headline"] == "数据不足，暂无法判断"
    assert "20 根" in out["reason"] and "30 根" in out["reason"]
    assert out["data_quality"]["sufficient"] is False
    assert out["data_quality"]["bars"] == 20


def test_signal_present_but_bars_short_still_says_insufficient() -> None:
    """**顺序不能反**：K 线不足时本来就不可能有可信信号，
    先报「没有信号」等于把「不知道」说成「知道没有」。"""
    out = build_recommendation(
        snap(
            {"status": "confirmed", "signal_type": "first_buy", "price": 10.0},
            candles=[{"close": 1.0}] * 5,
        )
    )
    assert out["headline"] == "数据不足，暂无法判断"
    assert out["available"] is False


def test_enough_bars_no_signal_says_no_signal() -> None:
    """对照组：数据**够**但确实没信号 ⇒ 才说「暂无明确结构信号」。"""
    out = build_recommendation(snap(None, candles=[{"close": 1.0}] * 120))
    assert out["headline"] == "暂无明确结构信号"
    assert out["data_quality"]["sufficient"] is True


def test_exactly_min_bars_is_enough() -> None:
    """边界：正好 30 根算够（与 factor_recompute 的门槛一致）。"""
    out = build_recommendation(snap(None, candles=[{"close": 1.0}] * 30))
    assert out["data_quality"]["sufficient"] is True
    assert out["headline"] == "暂无明确结构信号"
