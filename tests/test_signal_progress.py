"""``cpt.application.signal_progress`` 的翻译层测试。

本模块的职责是**把结构事实翻成人话**，不发明结论。所以测试钉三件事：

1. 三步顺序与状态转移（首个失败之后的步骤必须是 ``pending``，不是 ``failed``）；
2. 每句 ``explain`` 都能对上 `FirstBuyFacts` 的一个字段组合（即「无据可查的话
   不许出现」）；
3. ``action`` 不得出现交易指令式措辞 —— R70 已经证明它没有预测力。
"""

from __future__ import annotations

from dataclasses import dataclass

from cpt.application.signal_progress import (
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    first_buy_progress,
)


@dataclass(frozen=True)
class _F:
    """``FirstBuyFacts`` 的最小替身（只要本模块用到的四个字段）。"""

    has_two_centers: bool
    has_divergence_leg: bool
    has_reversal_bi: bool
    divergence_status: str = "not_detected"


def _states(result: dict) -> list[str]:
    return [step["state"] for step in result["steps"]]


def test_steps_have_fixed_ids_in_judgment_order() -> None:
    result = first_buy_progress(_F(True, True, True))
    assert [step["id"] for step in result["steps"]] == [
        "two_centers",
        "divergence_leg",
        "reversal_bi",
    ]


def test_first_failure_blocks_the_rest_as_pending_not_failed() -> None:
    """第一步就没过时，后两步是「没走到」，不是「失败了」两次。"""
    result = first_buy_progress(_F(False, False, False))
    assert _states(result) == [STEP_FAILED, STEP_PENDING, STEP_PENDING]


def test_second_step_failure_keeps_third_pending() -> None:
    result = first_buy_progress(_F(True, False, False))
    assert _states(result) == [STEP_DONE, STEP_FAILED, STEP_PENDING]


def test_all_steps_done_when_structure_is_complete() -> None:
    result = first_buy_progress(_F(True, True, True, "detected"))
    assert _states(result) == [STEP_DONE, STEP_DONE, STEP_DONE]


def test_explain_names_the_step_that_blocked() -> None:
    """每个 explain 必须能在 facts 里找到对应：中枢不够 / 无背驰段 / 无反弹。"""
    no_center = first_buy_progress(_F(False, False, False))["explain"]
    no_leg = first_buy_progress(_F(True, False, False))["explain"]
    no_reversal = first_buy_progress(_F(True, True, False))["explain"]
    assert "中枢" in no_center and "两个" in no_center
    assert "背驰段" in no_leg
    assert "还没反弹" in no_reversal
    # 三句必须互不相同，否则翻译层等于没区分
    assert len({no_center, no_leg, no_reversal}) == 3


def test_divergence_three_states_produce_three_distinct_sentences() -> None:
    """背驰三态只是旁证，但三态必须说成三句不同的话。"""
    detected = first_buy_progress(_F(True, True, True, "detected"))["explain"]
    not_detected = first_buy_progress(_F(True, True, True, "not_detected"))["explain"]
    not_checked = first_buy_progress(_F(True, True, True, "not_checked"))["explain"]
    assert len({detected, not_detected, not_checked}) == 3
    assert "成立" in detected
    assert "不成立" in not_detected
    assert "没算出来" in not_checked


def test_up_trend_returns_none_facts_branch_without_claiming_a_signal() -> None:
    result = first_buy_progress(None, segment_status="up")
    assert _states(result) == [STEP_PENDING, STEP_PENDING, STEP_PENDING]
    assert "向上走势" in result["explain"]
    assert result["evidence"]["divergence_status"] == "not_checked"


def test_unknown_segment_status_says_data_is_too_short_instead_of_guessing() -> None:
    """笔序列太短时必须承认「判断不了」，不许硬按结构事实编一句出来。"""
    result = first_buy_progress(_F(True, True, False), segment_status="unknown")
    assert "数据不足" in result["explain"]
    # 三步状态照实输出，但解释层不再假装走到哪
    assert _states(result) == [STEP_DONE, STEP_DONE, STEP_FAILED]


def test_action_never_gives_a_buy_instruction() -> None:
    """行动建议只允许「别追空/别割肉/别加仓」，不许出现买入指令。"""
    for facts in (
        _F(False, False, False),
        _F(True, False, False),
        _F(True, True, False),
        _F(True, True, True, "detected"),
        _F(True, True, True, "not_detected"),
    ):
        action = first_buy_progress(facts, segment_status="down")["action"]
        assert "买入" not in action.replace("不该做的", "")
        assert "加仓" not in action or "不该" in action


def test_ready_position_action_mentions_both_do_and_dont() -> None:
    """位置到了时，必须同时说「该做什么」和「不该做什么」。"""
    action = first_buy_progress(_F(True, True, True), segment_status="down")["action"]
    assert "该做的" in action
    assert "不该做的" in action
    # R89：措辞必须是「跌穿前 20 日最低价的 96% 以下」，不是含糊的「跌超 4%」——
    # 两者样本差 23 倍（19,592 vs 476,252）、收益差 3 个百分点。
    assert "跌到前 20 日最低价的 96% 以下" in action
    assert "跌超 4% 之后" not in action


def test_progress_is_read_only_and_does_not_mutate_facts() -> None:
    facts = _F(True, True, True, "detected")
    before = (facts.has_two_centers, facts.has_divergence_leg, facts.has_reversal_bi)
    first_buy_progress(facts)
    assert (facts.has_two_centers, facts.has_divergence_leg, facts.has_reversal_bi) == before
