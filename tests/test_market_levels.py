"""按市场的级别标签（R28-9）。

## 这条测试防的是什么

2026-10-02 真机实测：给 A 股结构 ``level=5``，LLM 解释正文写的是

    「该结构为深物业A在**5 分钟级别**（level=5）的一笔向上运动」

模型没胡说 —— ``RulesConfig.levels`` 的 docstring 明写「元素为**分钟**级别」，
而 A 股喂的是 ``daily_bar`` 日线。问题在上游的**标签**。

一条听起来很专业、实则完全错误的解释，比「不知道」有害得多 —— 用户会拿它做判断。

## 为什么只修标签、不重编 level

level 的**计算**含义（哪个相对层级的结构）在两个市场里是同一套；对不上的是
**展示单位**。重编 A 股的 level 数字要动全链路计算口径，而修展示标签只改提示词
与一张表 —— 便宜得多，风险小得多。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cpt.domain.levels import CN_LEVELS, CRYPTO_LEVELS, level_label, level_table_for
from cpt.llm.prompts import render_structure_payload

_BI: dict[str, Any] = {
    "id": "cn:bi:5:1756300000000",
    "level": 5,
    "kind": "bi",
    "direction": 1,
    "start_time": 1756300000000,
    "end_time": 1756400000000,
}


# --------------------------------------------------------------------------- #
# 1. 标签本身
# --------------------------------------------------------------------------- #


def test_a_share_level_5_is_daily_not_five_minutes() -> None:
    """核心断言：A 股的 level=5 **不能**标成任何分钟说法。"""
    assert level_label("a_share", 5) == "日线级别"
    assert "分钟" not in level_label("a_share", 5)


def test_crypto_level_5_stays_five_minutes() -> None:
    """加密侧 level 的单位确实是分钟 —— 不能被一起改掉。"""
    assert level_label("crypto", 5) == "5 分钟级别"
    assert level_label("crypto", 30) == "30 分钟级别"


def test_a_share_level_30_is_marked_not_produced() -> None:
    """A 股只产出 level=5。30 必须**如实说未启用**，不能编一个「30 分钟」。

    编一个会制造第二个错误：模型会拿一个不存在的级别讲内容。
    """
    spec = CN_LEVELS[30]
    assert spec.produced is False
    assert spec.minutes is None
    assert "未启用" in spec.note


def test_unknown_market_does_not_guess_minutes() -> None:
    """未知市场猜错单位比不回答更糟 —— 必须回落成「未标注」。"""
    assert "分钟" not in level_label("bond", 5)
    assert level_label("", 5).startswith("未标注级别")
    assert level_table_for("bond") == {}


def test_non_int_level_does_not_crash() -> None:
    assert level_label("a_share", None).startswith("未标注级别")
    assert level_label("a_share", "5").startswith("未标注级别")


def test_market_key_is_case_insensitive() -> None:
    assert level_label("A_Share", 5) == "日线级别"
    assert level_label("  crypto  ", 5) == "5 分钟级别"


# --------------------------------------------------------------------------- #
# 2. 提示词里必须带表 + 标签（不给表模型就只能猜）
# --------------------------------------------------------------------------- #


def _rendered(market: str) -> dict[str, Any]:
    text = render_structure_payload(code="000011", name="平安银行", market=market, structure=_BI)
    return json.loads(text)


def test_payload_carries_level_table() -> None:
    payload = _rendered("a_share")
    assert "级别说明" in payload
    assert "不要" in payload["级别说明"]["重要"]
    assert "5" in payload["级别说明"]["级别表"]
    assert payload["级别说明"]["级别表"]["5"]["label"] == "日线级别"
    # A 股那张表里**不许**出现 minutes 字段
    assert "minutes" not in payload["级别说明"]["级别表"]["5"]


def test_payload_labels_the_actual_level() -> None:
    payload = _rendered("a_share")
    assert payload["结构"]["本级标签"] == "日线级别"
    # 原始 level 仍在（审计要它），但旁边必须有标签
    assert payload["结构"]["level"] == 5


def test_payload_for_crypto_labels_minutes() -> None:
    payload = _rendered("crypto")
    assert payload["结构"]["本级标签"] == "5 分钟级别"
    assert payload["级别说明"]["级别表"]["5"]["minutes"] == 5


def test_payload_does_not_mutate_the_input_structure() -> None:
    """渲染不该改调用方的 dict —— 那是 snapshot 里的对象。"""
    original = dict(_BI)
    render_structure_payload(code="000011", name="x", market="a_share", structure=_BI)
    assert _BI == original, "render_structure_payload 污染了入参"


def test_payload_without_level_still_renders() -> None:
    """没有 level 字段的结构不能因此崩（有些叠加层不带）。"""
    payload = _rendered_without_level()
    assert "级别说明" in payload
    assert "本级标签" not in payload["结构"]


def _rendered_without_level() -> dict[str, Any]:
    text = render_structure_payload(
        code="000011", name="x", market="a_share", structure={"kind": "signal", "status": "ok"}
    )
    return json.loads(text)


# --------------------------------------------------------------------------- #
# 3. system prompt 必须写死这条口径（只给表不够，模型仍可能照数字推）
# --------------------------------------------------------------------------- #


def test_system_prompt_forbids_minute_inference() -> None:
    from cpt.llm.prompts import _SYSTEM_EXPLAIN  # noqa: PLC0415

    assert "本级标签" in _SYSTEM_EXPLAIN
    assert "a_share" in _SYSTEM_EXPLAIN
    assert "日线" in _SYSTEM_EXPLAIN
    assert "不要" in _SYSTEM_EXPLAIN


@pytest.mark.parametrize("market", ["a_share", "crypto"])
def test_every_market_in_table_has_a_label(market: str) -> None:
    """表里每个 level 都得有非空标签 —— 空标签等于把问题原样退回给模型。"""
    table = level_table_for(market)
    assert table, f"{market} 的级别表是空的"
    for key, spec in table.items():
        assert spec.key == key, f"表键 {key} 与 spec.key {spec.key} 不一致"
        assert spec.label.strip(), f"{market} level={key} 标签为空"
        rendered = spec.as_dict()
        assert rendered["label"] == spec.label


def test_crypto_table_is_not_accidentally_cn() -> None:
    """两个表必须是不同对象 —— 共用会静默把 A 股标签带进加密侧。"""
    assert CN_LEVELS is not CRYPTO_LEVELS
    assert CN_LEVELS[5].label != CRYPTO_LEVELS[5].label
