"""推荐块的**确定性契约**（R45 新增模块的首批测试）。

## 为什么要单独测

买卖与价格**刻意不放给 LLM**（理由见 ``cpt/application/recommendation.py``），
所以它是**纯函数**、零 IO、零模型 —— 也就意味着**可以完整测**。

这批测试钉住三件最容易出错的事：

1. **失效不取反** —— 「下跌位置作废」不是「卖出信号」。取反会把失效信号说反，
   而这是最容易被忽略、后果又最直接的一条。
2. **R76 起全链路不许出现买点语气** —— 见文件末尾 ``test_no_buy_language_*``。
   R70 实测这个信号没有预测力（688,496 次回放，confirmed 组 20 日 +0.02%），
   任何祈使动作都会被用户照着做，所以文案本身是**功能**，不是装饰。
3. **价格回退链** —— ``signal.price`` 缺失时退回最新收盘价，
   两者都缺就 ``None``（前端显示「—」），**不能编一个 0**。
4. **结构缺失一律降级、不抛** —— 推荐块挂在看板主路径上，
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
        ("confirmed", ACTION_BUY, "下跌位置已到"),
        ("structure_ready", ACTION_WATCH, "位置已到，还没确认"),
        ("candidate", ACTION_WATCH, "位置已到，还没确认"),
        ("alert", ACTION_WATCH, "位置已到，还没确认"),
        ("invalidated", ACTION_HOLD, "没有位置提示"),
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
    assert out["action_label"] == "上涨位置已到"


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
    assert "不是反向信号" in out["reason"]


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
    assert "两段重合的价格区间" in out["reason"]


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
    assert out["headline"] == "结构已作废"
    assert "不是反向信号" in out["reason"]


def test_invalidated_without_center_ids_keeps_old_wording() -> None:
    """缺 ``center_ids`` 的快照 → 不猜，沿用旧措辞（不把「不知道」说成「没有」）。"""
    out = build_recommendation(
        snap({"status": "invalidated", "signal_type": "first_buy", "price": 1.0})
    )
    assert out["headline"] == "结构已作废"


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
    assert "位置到了" in out["headline"]


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
    assert "力度确实在衰减" in out["reason"]


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


# ── R76：全链路不许出现买点语气 ────────────────────────────────
#
# 为什么把文案当**契约**来测：R70 实测这个信号没有预测力（5,131 只票 /
# 688,496 次回放，confirmed 组 5/10/20 日 +0.11%/+0.18%/+0.02%，胜率 46%，
# 低于随便挑一天买入的基线）。在这种前提下，「买入」「卖出」「加仓」这类
# 祈使词不是措辞问题，是**在输出一个没有依据的指令**。
#
# 这几条测试钉的是**所有出口**：推荐块（看板主卡 + 追踪页 + 飞书推送都读它）、
# 飞书推送正文、追踪页 explain_algorithm、LLM 提示词。

#: 一出现就算犯规的词。``买点`` / ``卖点`` 也算 —— 用户对这两个词的反应
#: 和「买入」没有区别（R71 之前页面上就是「参考买点 xx.xx」）。
_BUY_WORDS = (
    "买入",
    "卖出",
    "买点",
    "卖点",
    "加仓",
    "减仓",
    "抄底",
    "清仓",
    "建仓",
    "一买",
    "一卖",
)

#: 否定式豁免：``不是买点`` / ``非买卖指令`` 这类是**明确的否认**，恰恰是要保留的
#: 那种写法。检查前先把它们摘掉，剩下的才视为犯规。
#: ⚠️ 「不推荐买入」这种**不**在豁免里 —— 它仍然把「买入」当成了一个选项。
_NEGATIONS = (
    "不是买点",
    "不是卖点",
    "不是买入价",
    "不是卖出价",
    "不是买卖指令",
    "非买卖指令",
    "不是买入信号",
)


def _all_text(out: dict) -> str:
    """推荐块里**用户能看到**的全部文本（不含码值字段）。"""
    return " ".join(
        str(out.get(k) or "") for k in ("action_label", "headline", "reason", "disclaimer")
    )


def _forbidden_hits(text: str) -> list[str]:
    """摘掉合法否定式之后，还剩下的所有犯规词。"""
    probe = text
    for ok in _NEGATIONS:
        probe = probe.replace(ok, "")
    return [w for w in _BUY_WORDS if w in probe]


@pytest.mark.parametrize(
    ("status", "sig_type", "center_ids", "divergence"),
    [
        ("confirmed", "first_buy", ["bi:4", "bi:8"], "detected"),
        ("confirmed", "first_sell", ["bi:4", "bi:8"], "not_detected"),
        ("confirmed", "first_buy", None, "not_checked"),
        ("structure_ready", "first_buy", ["bi:4", "bi:8"], "detected"),
        ("structure_ready", "first_sell", ["bi:4", "bi:8"], "not_detected"),
        ("candidate", "first_buy", ["bi:4", "bi:8"], ""),
        ("alert", "first_buy", ["bi:4", "bi:8"], "detected"),
        ("invalidated", "first_buy", [], "detected"),
        ("invalidated", "first_sell", ["bi:4", "bi:8"], "detected"),
        ("invalidated", "first_buy", None, ""),
    ],
)
def test_no_buy_language_in_recommendation(
    status: str, sig_type: str, center_ids: list | None, divergence: str
) -> None:
    """全状态 × 全方向里，用户可见文本**一个买点词都不许有**。"""
    signal: dict = {"status": status, "signal_type": sig_type, "price": 28.5}
    if center_ids is not None:
        signal["center_ids"] = center_ids
    if divergence:
        signal["divergence_status"] = divergence
    out = build_recommendation(snap(signal))
    text = _all_text(out)
    hits = _forbidden_hits(text)
    assert not hits, f"{status}/{sig_type} 的输出里出现了买点语气：{hits} —— {text!r}"


def test_no_buy_language_in_degraded_paths() -> None:
    """降级路径（没数据 / 没信号 / 未知状态）同样不许出现买点词。"""
    cases = [
        build_recommendation({"candles": []}),
        build_recommendation(snap(None)),
        build_recommendation(snap({"status": "weird_status", "signal_type": "first_buy"})),
        build_recommendation(snap({"status": "confirmed", "signal_type": "weird_type"})),
        build_recommendation("not-a-dict"),  # type: ignore[arg-type]
    ]
    for out in cases:
        text = _all_text(out)
        hits = _forbidden_hits(text)
        assert not hits, f"降级路径出现买点语气：{hits} —— {text!r}"


def test_confirmed_headline_states_it_is_not_a_buy_point() -> None:
    """``confirmed`` 是最容易被当成「可以买了」的状态 —— 必须**自己否掉**。"""
    out = build_recommendation(
        snap({"status": "confirmed", "signal_type": "first_buy", "price": 10.0})
    )
    assert "不是买点" in out["headline"]
    # R70 的实测数字要跟着出去：只说「不是买点」而不说为什么，用户会当成免责话术。
    assert "+0.57%" in out["headline"] and "45.4%" in out["headline"]


def test_action_label_is_descriptive_not_imperative() -> None:
    """动作词只描述**价格处在什么位置**，不带动词。"""
    for status, sig_type in (
        ("confirmed", "first_buy"),
        ("confirmed", "first_sell"),
        ("structure_ready", "first_buy"),
        ("invalidated", "first_buy"),
    ):
        label = build_recommendation(
            snap({"status": status, "signal_type": sig_type, "price": 1.0})
        )["action_label"]
        assert "位置" in label or "没有" in label, label


def test_status_and_divergence_are_translated_to_chinese() -> None:
    """``reason`` 里原来直出英文枚举（``信号状态：structure_ready``）。"""
    out = build_recommendation(
        snap(
            {
                "status": "structure_ready",
                "signal_type": "first_buy",
                "price": 10.0,
                "divergence_status": "detected",
            }
        )
    )
    assert "structure_ready" not in out["reason"]
    assert "位置已到" in out["reason"]
    # 「背驰」保留（术语表里有解释），但必须带一句人话。
    assert "力度确实在衰减" in out["reason"]


def test_not_detected_divergence_is_explained_not_omitted() -> None:
    """``not_detected`` 原来**完全不提** —— 用户不知道背驰查了没有。"""
    out = build_recommendation(
        snap(
            {
                "status": "structure_ready",
                "signal_type": "first_buy",
                "price": 10.0,
                "divergence_status": "not_detected",
            }
        )
    )
    assert "没有明显衰减" in out["reason"]
