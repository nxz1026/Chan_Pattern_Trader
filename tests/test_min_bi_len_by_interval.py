"""``min_bi_len_for`` —— 按 bar 间隔解析笔门槛。

2026-10-06：门槛从「一个标量透传给所有间隔」改成按间隔分档。这条测试盯的是
**解析契约**，不是某个具体数值 —— 数值会随校准变，契约不能变。
"""

from __future__ import annotations

import pytest
from cpt.domain.config import RulesConfig


def test_known_intervals_resolve_from_the_table() -> None:
    cfg = RulesConfig()
    assert cfg.min_bi_len_for("1d") == 6
    assert cfg.min_bi_len_for("5m") == 6


def test_unknown_interval_falls_back_to_scalar() -> None:
    """未登记的间隔回落到 ``min_bi_len``，**不是**报错也不是 None。

    回落是刻意的：新时间粒度上线时不该因为「表里没登记」就让整条链路炸掉，
    但也不能悄悄返回 0（那等于不设门槛）。
    """
    cfg = RulesConfig()
    assert cfg.min_bi_len_for("1h") == cfg.min_bi_len
    assert cfg.min_bi_len_for("15m") == cfg.min_bi_len
    assert cfg.min_bi_len_for("1w") == cfg.min_bi_len


def test_none_interval_falls_back_to_scalar() -> None:
    assert RulesConfig().min_bi_len_for(None) == RulesConfig().min_bi_len


def test_empty_string_falls_back() -> None:
    """空串是「没传」，不是「某个特殊粒度」。"""
    assert RulesConfig().min_bi_len_for("") == RulesConfig().min_bi_len


def test_every_registered_interval_has_a_positive_gate() -> None:
    """表里登记的每个值都必须 >= 1 —— 0 或负数等于「不设门槛」，那是另一种语义。"""
    for interval, value in RulesConfig().min_bi_len_by_interval:
        assert value >= 1, f"{interval} 的门槛是 {value}，必须 >= 1"


def test_table_is_immutable_and_serialisable() -> None:
    """用 tuple of pairs 而不是 dict 的原因之一：能安全序列化进 config。"""
    cfg = RulesConfig()
    assert isinstance(cfg.min_bi_len_by_interval, tuple)
    data = cfg.to_dict()
    assert "min_bi_len_by_interval" in data, "分档表必须进 to_dict —— 否则回放看不到口径"
    # 旧 fixture 缺这个键时要用默认值填充，不能炸
    rebuilt = RulesConfig.from_dict({"min_bi_len": 6})
    assert rebuilt.min_bi_len_for("5m") == 6


def test_scalar_field_still_exists_as_fallback() -> None:
    """``min_bi_len`` 本身保留 —— 它是未登记间隔的兜底，不是被取代的遗留字段。"""
    assert isinstance(RulesConfig().min_bi_len, int)
    assert RulesConfig().min_bi_len >= 1


@pytest.mark.parametrize(
    ("interval", "expected"),
    [("1d", 6), ("5m", 6), ("1h", 6), ("15m", 6), ("4h", 6), (None, 6), ("", 6)],
)
def test_resolution_table_is_total(interval: str | None, expected: int) -> None:
    """任何输入都要有确定答案 —— 解析不能有「未定义」分支。"""
    assert RulesConfig().min_bi_len_for(interval) == expected


def test_a_share_and_crypto_can_diverge_without_touching_each_other() -> None:
    """分档的**意义**就在这里：改一档不影响另一档。

    这条用 monkeypatch 模拟「将来把 5m 调大」，验证 A 股的日线档纹丝不动 ——
    如果哪天实现退化成「两个档共享一个变量」，这条会红。
    """
    import dataclasses

    cfg = RulesConfig()
    tweaked = dataclasses.replace(cfg, min_bi_len_by_interval=(("1d", 6), ("5m", 30)))
    assert tweaked.min_bi_len_for("1d") == 6, "改 5m 不该动日线"
    assert tweaked.min_bi_len_for("5m") == 30
