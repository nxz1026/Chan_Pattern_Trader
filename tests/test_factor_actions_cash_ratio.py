"""`factor_from_actions`：**坏现金只否掉现金那一半**（R67 / 审计 P2-6）。

一条 `CorporateAction` 可以同时带现金与 `share_ratio`（送转 + 派息同日），
而 Wind 的列集合按标的而变。原实现里，`drop = cash / prev` 落在合法区间之外时
直接 `continue` —— 把**与该现金无关的送转台阶**一起丢了。

为什么这不是「少一个台阶」那么轻：`factor_from_actions` 是从最新一次除权**往前
连乘**、输出分段常数，于是丢一个台阶不只是那一天偏小，而是**从那天起向后全错**。

判据与 `cash is None` 分支完全一致：没有可信现金 ⇒ 只按送转走。
"""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

import pytest
from cpt.adapters import a_share_factor as af


def _act(ex_date: date, *, cash: float | None, ratio: float) -> SimpleNamespace:
    return SimpleNamespace(
        ex_date=ex_date, cash_pre_tax=cash, cash_after_tax=None, share_ratio=ratio
    )


def test_bad_cash_still_applies_the_share_ratio_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★回归：派息 50 元 / 前收 10 元（drop=5，数据错）时，1 送 0.5 仍要生效。"""
    ex = date(2026, 6, 1)
    with caplog.at_level(logging.WARNING):
        out = af.factor_from_actions([_act(ex, cash=50.0, ratio=0.5)], prev_closes={ex: 10.0})

    assert out == {ex: 1.5}, f"送转台阶被坏现金一起丢了：{out}"
    assert "跳过异常除权" in caplog.text, "数据错仍然要留痕"


def test_negative_cash_is_also_only_a_cash_problem() -> None:
    """负数派息同样是现金自己的问题（ratio 照走）。"""
    ex = date(2026, 6, 1)
    out = af.factor_from_actions([_act(ex, cash=-1.0, ratio=2.0)], prev_closes={ex: 10.0})
    assert out == {ex: 3.0}, out


def test_good_cash_and_ratio_still_compound_together() -> None:
    """正常路径不许被这次改动碰到：`f = (1+ratio)/(1-drop)`。"""
    ex = date(2026, 6, 1)
    out = af.factor_from_actions([_act(ex, cash=2.0, ratio=0.5)], prev_closes={ex: 10.0})
    assert out[ex] == pytest.approx(1.5 / 0.8)


def test_bad_cash_without_ratio_still_skips_the_step() -> None:
    """没有送转可救时保持原语义：宁可少一个台阶，也不用猜的收盘价。"""
    ex = date(2026, 6, 1)
    out = af.factor_from_actions([_act(ex, cash=50.0, ratio=0.0)], prev_closes={ex: 10.0})
    assert out == {}


def test_missing_prev_close_with_ratio_keeps_working() -> None:
    """取不到除权前收盘时的既有处置（只按送转）与上面两条要一致。"""
    ex = date(2026, 6, 1)
    out = af.factor_from_actions([_act(ex, cash=2.0, ratio=0.5)], prev_closes={})
    assert out == {ex: 1.5}


def test_steps_compound_backwards_in_time() -> None:
    """分段常数是**往前连乘**：坏现金那天之后的所有台阶都不许被它带偏。"""
    older, newer = date(2026, 5, 1), date(2026, 6, 1)
    acts = [
        _act(older, cash=1.0, ratio=0.0),
        _act(newer, cash=50.0, ratio=0.5),
    ]
    out = af.factor_from_actions(acts, prev_closes={older: 10.0, newer: 10.0})
    assert out[newer] == 1.5
    assert out[older] == pytest.approx(1.5 * (1.0 / 0.9))
