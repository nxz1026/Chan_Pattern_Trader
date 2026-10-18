"""P2-10 回归：R21 的「状态推进」不许再以死守卫的形式回到生产路径。

背景：``_derive_first_buy_signal`` / ``_derive_first_sell_signal`` 里曾有一条

    if signal is not None and facts.has_reversal_bi and signal.status == "structure_ready":
        signal = transition_first_buy(signal, reversal_closed=True, ...)

守卫**恒为假**：``assess_first_buy`` 的判定顺序是「``has_reversal_bi`` 为真 ⇒ 直接
``confirmed``」，两个条件互斥。它不仅是死代码，还是**冗余回声** —— 就算走到，得到的
也是 ``assess_first_buy`` 已经给出的同一个 ``confirmed``。

**为什么这类死代码值得单独钉**：它让「R21 已接线」的自述看起来有代码支撑，而真正的
缺口（``alert`` / ``candidate`` 不可达，需要盘中反向 K 线）被这条空接线盖住了。
下面三组分别钉：① 域层前提（守卫互斥）② 生产路径不再二次改写信号 ③ 文档别再声称已接。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Signal
from cpt.domain.signal import assess_first_buy

# --------------------------------------------------------------------------- #
# 夹具：签名必须与真 dataclass 一致（P2-10 的病根之一就是「替身不同域」）
# --------------------------------------------------------------------------- #

_LEVEL = RulesConfig().levels[0]  # 不 monkeypatch 级别，直接用生产的那个


def _bi(direction: int, start: int, end: int, level: int = _LEVEL) -> Bi:
    return Bi(
        level=level,
        direction=direction,
        start_time=start,
        end_time=end,
        high=110.0,
        low=90.0,
        source_ids=(f"fx:{end}",),
        power_price=5.0,
        power_volume=100.0,
        length=3,
    )


def _bar(open_time_ms: int, close: float) -> CanonicalBar:
    return CanonicalBar(
        open_time=open_time_ms,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=100.0,
        close_time=open_time_ms + 24 * 3600 * 1000 - 1,
        quote_volume=200.0,
        trade_count=1,
        taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0,
        is_closed=True,
    )


def _bars(n: int = 3) -> list[CanonicalBar]:
    base = 1_700_000_000_000
    return [_bar(base + i * 24 * 3600 * 1000, 100.0 + i) for i in range(n)]


def _signal(signal_type: str, status: str) -> Signal:
    return Signal(
        signal_id=f"{signal_type}:{_LEVEL}:level{_LEVEL}:zs1",
        level=_LEVEL,
        signal_type=signal_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        structure_id=f"level{_LEVEL}:zs1",
        center_ids=("zs0", "zs1"),
        divergence_status="detected",
        alert_time=None,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=100.0,
        source_revision=0,
    )


def _fake_facts() -> object:
    return type(
        "FakeFacts",
        (),
        {
            "structure_id": f"level{_LEVEL}:zs1",
            "center_ids": ("zs0", "zs1"),
            "has_two_centers": True,
            "has_divergence_leg": True,
            "has_reversal_bi": True,
            "divergence_status": "detected",
        },
    )()


# --------------------------------------------------------------------------- #
# ① 域层前提：``has_reversal_bi`` 与 ``structure_ready`` 互斥
# --------------------------------------------------------------------------- #

_BASE = {
    "level": _LEVEL,
    "structure_id": "s",
    "center_ids": ("a", "b"),
    "trend_direction": -1,
    "divergence_status": "detected",
}
_PREV_STATUSES = (None, "alert", "candidate", "structure_ready", "confirmed", "invalidated")


def test_guard_condition_is_unreachable_across_the_whole_input_space() -> None:
    """⚠️ 核心：穷举入参空间，``has_reversal_bi and status == "structure_ready"`` 命中 0 次。

    这就是「死代码」结论的全部依据 —— 不是「看起来到不了」，是全枚举。若将来
    ``assess_first_buy`` 的判定顺序被改动（例如把反向笔拆成两级），这条会红，提醒
    重新评估是否需要恢复推进接线。
    """
    hits: list[tuple[bool, bool, bool, str | None]] = []
    for has_two_centers in (False, True):
        for has_divergence_leg in (False, True):
            for has_reversal_bi in (False, True):
                for prev_status in _PREV_STATUSES:
                    previous = None
                    if prev_status is not None:
                        seed = assess_first_buy(
                            has_two_centers=True,
                            has_divergence_leg=True,
                            has_reversal_bi=False,
                            event_time=0,
                            previous=None,
                            **_BASE,
                        )
                        previous = replace(seed, status=prev_status)
                    signal = assess_first_buy(
                        has_two_centers=has_two_centers,
                        has_divergence_leg=has_divergence_leg,
                        has_reversal_bi=has_reversal_bi,
                        event_time=1000,
                        previous=previous,
                        **_BASE,
                    )
                    if has_reversal_bi and signal.status == "structure_ready":
                        hits.append(
                            (has_two_centers, has_divergence_leg, has_reversal_bi, prev_status)
                        )
    assert hits == [], f"守卫不再是恒假了（命中 {len(hits)} 次）：{hits}"


def test_has_reversal_bi_is_directly_promoted_to_confirmed_by_the_domain_function() -> None:
    """守卫想做的推进，``assess_first_buy`` **已经做了** —— 所以删它不是丢功能。"""
    signal = assess_first_buy(
        has_two_centers=True,
        has_divergence_leg=True,
        has_reversal_bi=True,
        event_time=1000,
        previous=None,
        **_BASE,
    )
    assert signal.status == "confirmed"
    assert signal.confirmed_time == 1000, "确认时间该被域函数自己写上"


def test_the_guard_branch_would_have_been_a_noop() -> None:
    """反证：就算把守卫强行走到，``transition`` 得到的也是同一个 ``confirmed``。

    构造一个域层造不出的 ``structure_ready + has_reversal_bi`` 组合喂给
    ``transition_first_buy``：``reversal_closed=True`` 且结构有效 ⇒ ``confirmed``，
    与 ``assess_first_buy`` 的输出**同状态、同时间戳**。故那条接线是冗余回声。
    """
    from cpt.domain.signal import transition_first_buy

    produced = assess_first_buy(
        has_two_centers=True,
        has_divergence_leg=True,
        has_reversal_bi=True,
        event_time=1000,
        previous=None,
        **_BASE,
    )
    forced_ready = replace(produced, status="structure_ready", confirmed_time=None)
    advanced = transition_first_buy(
        forced_ready, reversal_closed=True, structure_valid=True, event_time=1000
    )
    assert advanced.status == produced.status == "confirmed"
    assert advanced.confirmed_time == produced.confirmed_time
    assert advanced.signal_id == produced.signal_id


# --------------------------------------------------------------------------- #
# ② 生产路径：不再有本地推进 ⇒ 不调 transition
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "derive_name, direction",
    [("_derive_first_buy_signal", -1), ("_derive_first_sell_signal", 1)],
)
def test_production_path_calls_no_transition_function(monkeypatch, derive_name, direction) -> None:
    """生产推导函数**不调** ``transition_first_buy/sell``：用 spy 盯住调用点。

    为什么用 spy 而不是断言「结果状态未变」：结果未变也可能来自「调了但恰好同值」，
    而这里要钉的是**接线本身不在** —— spy 未触发即证。

    域侧故意固定返回 ``structure_ready``（配 ``has_reversal_bi=True``）：这正是当年让
    死守卫「看起来在工作」的那个组合。若谁把它接回来，spy 立刻抛。注意
    ``a_share_snapshot`` **没有**模块级 ``transition_*`` 名字（已随 P2-10 删除导入），
    故用 ``raising=False`` 建一个属主，靠 monkeypatch 恢复。
    """
    from cpt.application import a_share_snapshot as mod
    from cpt.application import first_buy_bridge as bridge

    called: list[str] = []

    def spy(name: str):
        def _spy(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"{derive_name} 不该再调 {name}")

        return _spy

    import cpt.domain.signal as signal_mod

    monkeypatch.setattr(mod, "transition_first_buy", spy("transition_first_buy"), raising=False)
    monkeypatch.setattr(mod, "transition_first_sell", spy("transition_first_sell"), raising=False)
    # ⚠️ 必须**同时**打在域模块上：如果有人把接线写成函数内惰性导入
    # （``from cpt.domain.signal import transition_first_buy``），只打模块属性会被
    # 绕过 —— 实测把守卫插回去时 ``mod`` 上的 spy 一次都没触发（是下面那条 status
    # 断言兜住的）。两处都打，spy 才真的跑不掉。
    monkeypatch.setattr(signal_mod, "transition_first_buy", spy("transition_first_buy"))
    monkeypatch.setattr(signal_mod, "transition_first_sell", spy("transition_first_sell"))
    # 一买用模块级导入，一卖在函数内**惰性导入**（a_share_snapshot.py:732）⇒
    # 两条路径的补丁点不同，且都必须打到「真函数将被解析的那个名字空间」。
    monkeypatch.setattr(mod, "derive_first_buy_facts", lambda **kwargs: _fake_facts())
    monkeypatch.setattr(bridge, "derive_first_sell_facts", lambda **kwargs: _fake_facts())
    monkeypatch.setattr(
        mod, "assess_first_buy", lambda **kwargs: _signal("first_buy", "structure_ready")
    )
    # assess_first_sell 同样是惰性导入（a_share_snapshot.py:733），不是模块属性 ⇒
    # 打在 cpt.domain.signal 上，否则 AttributeError。
    monkeypatch.setattr(
        signal_mod, "assess_first_sell", lambda **kwargs: _signal("first_sell", "structure_ready")
    )
    monkeypatch.setattr(mod, "load_previous_signal", lambda conn, signal_id: None)

    signal, _prev = getattr(mod, derive_name)(
        [_bi(direction, 0, 10)], [], _bars(), client=None, code="000002"
    )

    assert called == [], f"生产路径仍在调状态推进：{called}"
    assert signal is not None, f"{derive_name} 没产出信号（夹具没走到评估点？）"
    # 原样透传：没有本地 replace/推进
    assert signal.status == "structure_ready", "本函数不该二次改写域层给的状态"


# --------------------------------------------------------------------------- #
# ③ 文档：不许再声称「已接」
# --------------------------------------------------------------------------- #


def test_docs_no_longer_claim_the_transition_is_wired() -> None:
    """反回归：三处文档都改了口径（否则下一个人还会以为它在工作）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    signal_py = (root / "cpt/domain/signal.py").read_text(encoding="utf-8")
    assert "状态推进**没有生产" in signal_py
    assert "状态推进**也已接**" not in signal_py

    snapshot_py = (root / "cpt/application/a_share_snapshot.py").read_text(encoding="utf-8")
    assert "没有调用点" in snapshot_py

    pending = (root / "docs/pending-wiring.md").read_text(encoding="utf-8")
    assert "已接（R21，本条为过期描述已更正）" not in pending
    assert "没有生产调用点" in pending
