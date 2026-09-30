"""一买 / 一卖信号桥（R20 接线一买，R21 接线一卖）测试。

**入口纪律**：R19 起本仓的接线类验收规矩是「入口必须是生产构造函数」。R20
补的用例里 1-3 条一律从 :func:`build_ashare_snapshot` 进，证明生产路径真的
会走 :mod:`cpt.application.first_buy_bridge`；4-6 条是纯函数级的口径锁定，
只负责把「保守口径四条」钉死，不负责证明生产可达。R21 追加一卖镜像用例。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.application.a_share_snapshot import build_ashare_snapshot
from cpt.application.first_buy_bridge import derive_first_buy_facts, derive_first_sell_facts
from cpt.domain.models import Bi, ZhongShu

from tests.test_web_a_share import _FakeClient, _make_canonical, _zigzag_bars

_VALID_STATUS = {"structure_ready", "alert", "candidate", "confirmed", "invalidated"}


def _tail_down_bars(*, n: int = 200) -> list[Any]:
    """末尾 12 根单边下跌的正弦序列（制造「末笔向下」的评估前提）。

    为什么不用现成的 ``_zigzag_bars``：它的末笔方向取决于正弦相位，n=200 时末笔
    恰好向上 → 一买无意义 → 桥按保守口径返回 ``None``。实测踩过：用它写
    ``test_production_entry_emits_first_buy_signal`` 时红了，但根因是**测试数据**，
    不是接线。

    **为什么不靠合成数据造出两个中枢**：实测搜索了 60 组参数
    （阶梯 + 正弦，amp 0.8~3.5，seg 20~50，n 140~260），``zs`` 全为 0 或 1
    —— ``_extend``（``cpt/domain/zhongshu.py:113``）会把重叠笔一路并进同一中枢，
    要造出 2 个中枢得让价格**明确跌破**中枢下限再在新低位重叠，代价远大于
    收益。真实形态用真库验：600519 → 6 个中枢、末笔向下、信号
    ``structure_ready`` + 背驰 ``detected``。
    """
    end_ms = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    bars: list[Any] = []
    for index in range(n):
        close = 10.0 + 3.0 * math.sin(index / 3.0)
        if index >= n - 12:
            close -= (index - (n - 12)) * 0.5
        bars.append(_make_canonical(end_ms - (n - 1 - index) * 24 * 3600 * 1000, close))
    return bars


# --------------------------------------------------------------------------- #
# 生产入口（build_ashare_snapshot）
# --------------------------------------------------------------------------- #


def test_production_entry_calls_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """守门主线：生产入口**真的会调** :func:`derive_first_buy_facts`。

    用 spy 而不是断言 ``signal is not None``：后者的成败取决于**测试数据能否
    造出两个中枢**（实测 60 组参数都造不出，见 :func:`_tail_down_bars` 的说明），
    一旦数据形态变了，用例会因为「数据没结构」而红，与接线是否正确无关——
    这正是 R19 记下的「测试数据也会让守门用例假红」的第二次复发。

    spy 直接盯「生产路径有没有走这个函数」，未接线时必红，且不挑数据。
    """
    import cpt.application.a_share_snapshot as target

    seen: list[dict[str, object]] = []
    original = target.derive_first_buy_facts

    def _spy(**kwargs: object):  # noqa: ANN202
        seen.append(dict(kwargs))
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(target, "derive_first_buy_facts", _spy)
    snap = build_ashare_snapshot("600519", client=_FakeClient(_tail_down_bars()))
    assert seen, "生产入口没调用一买桥 —— 桥没接上"
    bis = snap["overlays"]["bis"]
    call = seen[-1]
    assert call["level"] == bis[-1]["level"], "桥评估的级别必须与末笔同级"
    assert call["trend_direction"] == bis[-1]["direction"], "趋势方向必须取自数据，不能硬编码 -1"
    assert "zhongshus" in call and "bis" in call


def test_signal_does_not_change_structure() -> None:
    """信号引用的中枢 id 必须真的对应某笔的 ``source_ids``。

    信号是**旁挂**在快照上的元数据，不是结构的输入；一旦它引用了不存在的结构，
    面板上的「中枢」链接就是死的。判据与 M4 fixture 迁移同源。
    """
    snap = build_ashare_snapshot("600519", client=_FakeClient(_tail_down_bars()))
    overlays = snap["overlays"]
    assert overlays["bis"], "结构为空则本用例无意义"
    signal = snap["signal"]
    if signal is None or not signal["center_ids"]:
        pytest.skip("该序列无两个中枢，信号无 center_ids")
    all_ids = {sid for bi in overlays["bis"] for sid in bi["source_ids"]}
    for center_id in signal["center_ids"]:
        assert center_id in all_ids, f"center_id {center_id} 不对应任何笔"


def test_uptrend_tail_produces_no_signal() -> None:
    """保守口径的反面：最后一笔向上时**不产信号**。

    趋势方向取自数据（最后一笔方向），不是硬编码 ``-1``。若有人把
    ``trend_direction`` 写死成 ``-1``，本用例会红。
    """
    snap = build_ashare_snapshot("600519", client=_FakeClient(_zigzag_bars(n=200)))
    bis = snap["overlays"]["bis"]
    if not bis:
        pytest.skip("该序列没出笔")
    if bis[-1]["direction"] == 1:
        assert snap["signal"] is None, "最后一笔向上却产出了一买信号"


# --------------------------------------------------------------------------- #
# 纯函数口径锁定
# --------------------------------------------------------------------------- #


def _bi(direction: int, start: int, end: int, level: int = 5) -> Bi:
    return Bi(
        level=level,
        direction=direction,
        start_time=start,
        end_time=end,
        high=110.0 if direction == 1 else 90.0,
        low=90.0 if direction == 1 else 110.0,
        source_ids=(f"fx:{end}",),
        power_price=5.0,
        power_volume=100.0,
        length=3,
    )


def _zs(level: int, start: int, end: int, first_bi: str) -> ZhongShu:
    return ZhongShu(
        level=level,
        start_time=start,
        end_time=end,
        high=105.0,
        low=95.0,
        bi_ids=(first_bi,),
    )


def test_bridge_returns_none_for_upward_trend() -> None:
    facts = derive_first_buy_facts(level=5, trend_direction=1, bis=[], zhongshus=[])
    assert facts is None, "向上趋势必须返回 None（一买无意义）"


def test_bridge_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="trend_direction"):
        derive_first_buy_facts(level=5, trend_direction=0, bis=[], zhongshus=[])


def test_bridge_uses_two_most_recent_centers() -> None:
    """保守口径第 1 条：取**最后两个**中枢，不是任意两个。"""
    centers = [
        _zs(5, 0, 10, "fx:a"),
        _zs(5, 20, 30, "fx:b"),
        _zs(5, 40, 50, "fx:c"),
    ]
    bis = [
        _bi(-1, 50, 60),  # 背驰段（中枢三之后、向下）
        _bi(1, 60, 70),  # 反向笔
    ]
    facts = derive_first_buy_facts(level=5, trend_direction=-1, bis=bis, zhongshus=centers)
    assert facts is not None
    assert facts.has_two_centers
    assert facts.has_divergence_leg
    assert facts.has_reversal_bi
    # 最后两个 = fx:b / fx:c，不是 fx:a / fx:b
    assert facts.center_ids == ("fx:b", "fx:c")
    assert facts.structure_id == "level5:fx:c"


def test_bridge_marks_not_checked_when_power_metrics_missing() -> None:
    """力度度量未填充时是 ``not_checked``，**不是** ``not_detected``。

    两者语义完全不同：前者是「数据缺」，后者是「算过了，不背驰」。混为一谈
    会让面板显示出错误的结论。
    """
    centers = [_zs(5, 0, 10, "fx:a"), _zs(5, 20, 30, "fx:b")]
    weak = Bi(
        level=5,
        direction=-1,
        start_time=0,
        end_time=40,
        high=105.0,
        low=95.0,
        source_ids=("fx:z",),
        power_price=0.0,
        power_volume=0.0,
        length=0,  # 未填充
    )
    facts = derive_first_buy_facts(level=5, trend_direction=-1, bis=[weak], zhongshus=centers)
    assert facts is not None
    assert facts.divergence_status == "not_checked"


# --------------------------------------------------------------------------- #
# 一卖纯函数口径锁定（R21 镜像）
# --------------------------------------------------------------------------- #


def test_sell_bridge_returns_none_for_downward_trend() -> None:
    """向下趋势 → 一卖无意义，返回 ``None``。"""
    facts = derive_first_sell_facts(level=5, trend_direction=-1, bis=[], zhongshus=[])
    assert facts is None, "向下趋势必须返回 None（一卖无意义）"


def test_sell_bridge_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="trend_direction"):
        derive_first_sell_facts(level=5, trend_direction=0, bis=[], zhongshus=[])


def test_sell_bridge_uses_last_two_centers_and_upward_leg() -> None:
    """一卖背驰段方向向上（与一买镜像）：中枢二结束之后、沿 ``_UP`` 方向运行。"""
    centers = [
        _zs(5, 0, 10, "fx:a"),
        _zs(5, 20, 30, "fx:b"),
        _zs(5, 40, 50, "fx:c"),
    ]
    bis = [
        _bi(1, 50, 60),  # 背驰段（中枢三之后、向上）
        _bi(-1, 60, 70),  # 反向笔（向下）
    ]
    facts = derive_first_sell_facts(level=5, trend_direction=1, bis=bis, zhongshus=centers)
    assert facts is not None
    assert facts.has_two_centers
    assert facts.has_divergence_leg
    assert facts.has_reversal_bi
    # 最后两个 = fx:b / fx:c
    assert facts.center_ids == ("fx:b", "fx:c")
    assert facts.structure_id == "level5:fx:c"


def test_sell_bridge_no_reversal_without_counter_bi() -> None:
    """背驰段之后没有反向笔 → ``has_reversal_bi=False``。"""
    centers = [_zs(5, 0, 10, "fx:a"), _zs(5, 20, 30, "fx:b")]
    bis = [_bi(1, 30, 40)]  # 只有背驰段，没有反向笔
    facts = derive_first_sell_facts(level=5, trend_direction=1, bis=bis, zhongshus=centers)
    assert facts is not None
    assert facts.has_divergence_leg
    assert not facts.has_reversal_bi


def test_sell_signal_assessed_with_upward_trend() -> None:
    """``assess_first_sell`` 在向上趋势 + 两个中枢 + 背驰 → ``structure_ready``。"""
    from cpt.domain.signal import assess_first_sell

    signal = assess_first_sell(
        level=5,
        structure_id="level5:fx:z2",
        center_ids=("fx:z1", "fx:z2"),
        trend_direction=1,
        has_two_centers=True,
        has_divergence_leg=True,
        has_reversal_bi=False,
        divergence_status="detected",
        price=120.0,
        source_revision=0,
        event_time=1_700_000_000_000,
    )
    assert signal.signal_type == "first_sell"
    assert signal.status == "structure_ready"
    assert signal.signal_id == "first_sell:5:level5:fx:z2"


def test_sell_signal_invalidated_without_two_centers() -> None:
    """只有一个中枢 → ``invalidated``（结构不满足）。"""
    from cpt.domain.signal import assess_first_sell

    signal = assess_first_sell(
        level=5,
        structure_id="level5:empty",
        center_ids=(),
        trend_direction=1,
        has_two_centers=False,
        has_divergence_leg=False,
        has_reversal_bi=False,
        price=100.0,
        source_revision=0,
        event_time=1_700_000_000_000,
    )
    assert signal.signal_type == "first_sell"
    assert signal.status == "invalidated"


def test_sell_signal_confirmed_with_reversal_bi() -> None:
    """有反向笔 → ``confirmed``。"""
    from cpt.domain.signal import assess_first_sell

    signal = assess_first_sell(
        level=5,
        structure_id="level5:fx:z2",
        center_ids=("fx:z1", "fx:z2"),
        trend_direction=1,
        has_two_centers=True,
        has_divergence_leg=True,
        has_reversal_bi=True,
        divergence_status="detected",
        price=130.0,
        source_revision=0,
        event_time=1_700_000_000_000,
    )
    assert signal.status == "confirmed"
    assert signal.confirmed_time == 1_700_000_000_000


def test_sell_signal_rejects_invalid_trend_direction() -> None:
    """``trend_direction`` 既不是 1 也不是 -1 → ``ValueError``。"""
    from cpt.domain.signal import assess_first_sell

    with pytest.raises(ValueError, match="trend_direction"):
        assess_first_sell(
            level=5,
            structure_id="level5:empty",
            center_ids=(),
            trend_direction=2,  # 无效
            has_two_centers=False,
            has_divergence_leg=False,
            has_reversal_bi=False,
            price=100.0,
            source_revision=0,
            event_time=0,
        )
