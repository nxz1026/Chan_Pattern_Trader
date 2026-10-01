"""``cpt.domain.structure_events`` 测试（纯函数，零 IO）。

守的是三条：
1. **确定性 id** —— 幂等重放的前提；
2. **无变化不产生事件** —— 这是热路径常态，每轮都写会淹掉事件流；
3. **revision 只在真产生事件时递增** —— 否则「100 次重算 = revision 100」，
   revision 就失去了「改了多少次」的意义。
"""

from __future__ import annotations

import pytest
from cpt.domain.models import Bi, StructureEvent, StructureState
from cpt.domain.structure_events import (
    diff_states,
    state_from_event,
    state_to_payload,
    states_from_structures,
    structure_id_of,
)


def _state(
    *,
    market: str = "cn",
    kind: str = "bi",
    level: int = 5,
    start_time: int = 1_700_000_000_000,
    end_time: int = 1_700_003_600_000,
    status: str = "forming",
    revision: int = 1,
    sid: str | None = None,
) -> StructureState:
    return StructureState(
        id=sid or structure_id_of(market, kind, level, start_time),  # type: ignore[arg-type]
        level=level,
        kind=kind,  # type: ignore[arg-type]
        direction=1,
        start_time=start_time,
        end_time=end_time,
        status=status,  # type: ignore[arg-type]
        revision=revision,
        first_seen_at=start_time,
        confirmed_at=end_time if status == "confirmed" else None,
        invalidated_at=end_time if status == "invalidated" else None,
        source_ids=("a", "b"),
    )


# --------------------------------------------------------------------------- #
# 确定性 id
# --------------------------------------------------------------------------- #


def test_structure_id_is_deterministic() -> None:
    """幂等重放的全部前提：同输入必同 id。"""
    assert structure_id_of("cn", "bi", 5, 1000) == structure_id_of("cn", "bi", 5, 1000)
    assert structure_id_of("cn", "bi", 5, 1000) == "cn:bi:5:1000"


def test_structure_id_distinguishes_kind_and_level_and_start() -> None:
    """不同的 kind / level / 起点 = 不同的结构。"""
    assert structure_id_of("cn", "bi", 5, 1000) != structure_id_of("cn", "zhongshu", 5, 1000)
    assert structure_id_of("cn", "bi", 5, 1000) != structure_id_of("cn", "bi", 30, 1000)
    assert structure_id_of("cn", "bi", 5, 1000) != structure_id_of("cn", "bi", 5, 2000)


def test_structure_id_distinguishes_markets() -> None:
    """R27-4 的核心：**同 kind / 同 level / 同起点，不同市场 = 不同结构**。

    没有这一条，两个市场只要在时间轴上撞上就会静默合并 —— 而且不会报错，
    因为 id「确实」同输入同输出，只是这个「同」跨了市场。
    """
    assert structure_id_of("cn", "bi", 5, 1000) != structure_id_of("crypto", "bi", 5, 1000)
    assert structure_id_of("crypto", "bi", 5, 1000) == "crypto:bi:5:1000"


def test_structure_id_rejects_unknown_market() -> None:
    """拼错的市场键必须**报错**，不能静默兜底。

    静默兜底会写出永远匹配不上的 id：每轮都在写新 ``created``，页面看起来一切
    正常，实际状态机从来没推进过 —— 比直接报错难查得多。
    """
    with pytest.raises(ValueError, match="未知 market"):
        structure_id_of("bond", "bi", 5, 1000)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# states_from_structures：market 真的进了 id
# --------------------------------------------------------------------------- #


def _one_bi() -> Bi:
    return Bi(
        level=5,
        direction=1,
        start_time=1_700_000_000_000,
        end_time=1_700_003_600_000,
        high=10.0,
        low=9.0,
        source_ids=("a",),
        power_price=1.0,
        power_volume=1.0,
        length=5,
    )


def test_states_carry_market_prefix() -> None:
    """同一批结构，两个市场必须算出**不同**的 id。"""
    cn = states_from_structures(market="cn", bis=[_one_bi()])
    crypto = states_from_structures(market="crypto", bis=[_one_bi()])
    assert cn[0].id == "cn:bi:5:1700000000000"
    assert crypto[0].id == "crypto:bi:5:1700000000000"
    # 除 id 以外内容完全相同 —— 证明前缀只影响身份，不影响结构本身
    assert cn[0].kind == crypto[0].kind
    assert cn[0].start_time == crypto[0].start_time


def test_states_market_is_required() -> None:
    """``market`` 必须**必填**。

    给默认值等于留一个后门给下一个调用方 —— 而这个后门一旦有人踩，症状是
    「每轮都在写新 created」，看板上完全正常，要到事件流涨到离谱才可能被察觉。
    """
    with pytest.raises(TypeError):
        states_from_structures(bis=[_one_bi()])  # type: ignore[call-arg]


def test_states_are_still_deterministic_per_market() -> None:
    """加了前缀**不能**破坏幂等：同市场同输入必同 id。"""
    a = states_from_structures(market="crypto", bis=[_one_bi()])
    b = states_from_structures(market="crypto", bis=[_one_bi()])
    assert [s.id for s in a] == [s.id for s in b]


# --------------------------------------------------------------------------- #
# created
# --------------------------------------------------------------------------- #


def test_first_run_creates_every_structure() -> None:
    """首次跑（无历史）→ 全部记 created，revision 从 1 起。"""
    states = (_state(kind="bi"), _state(kind="zhongshu", start_time=999))
    events = diff_states({}, states)
    assert len(events) == 2
    assert {e.event_type for e in events} == {"created"}
    assert all(e.revision == 1 for e in events)


def test_no_change_produces_no_event() -> None:
    """**热路径常态**：每轮快照都跑一遍，不变就不该写。"""
    state = _state()
    assert diff_states({state.id: state}, [state]) == ()


def test_same_state_id_differs_only_in_irrelevant_field_no_event() -> None:
    """id 相同、状态区间都相同 → 无事件（不能靠字段顺序或 object 身份判变化）。"""
    prior = _state(revision=3)
    current = _state(revision=99)  # 传入的 revision 差异不该自己触发事件
    assert diff_states({prior.id: prior}, [current]) == ()


# --------------------------------------------------------------------------- #
# 状态跃迁
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("forming", "confirmed", "confirmed"),
        ("forming", "invalidated", "invalidated"),
        ("confirmed", "invalidated", "invalidated"),
        ("invalidated", "forming", "reclassified"),
    ],
)
def test_status_transitions(old: str, new: str, expected: str) -> None:
    prior = _state(status=old, revision=2)
    current = _state(status=new, revision=2)
    (event,) = diff_states({prior.id: prior}, [current])
    assert event.event_type == expected
    assert event.revision == 3, "revision 只在这里 +1"


def test_kind_change_is_a_different_structure_not_a_reclassification() -> None:
    """同起点但 kind 不同 = **两个结构**，不是「一个结构改了类型」。

    因为 ``structure_id_of`` 把 ``kind`` 算进了 id。这条把设计意图钉住：
    起点相同的中枢和笔是两个东西，各自 created。
    """
    bi = _state(kind="bi", status="confirmed")
    zs = _state(kind="zhongshu", status="confirmed")
    assert bi.id != zs.id

    events = diff_states({bi.id: bi}, [zs])
    assert [e.event_type for e in events] == ["created"]


def test_reclassified_means_invalidated_structure_comes_back() -> None:
    """``reclassified`` 的真实含义：被 invalidated 的结构重新 forming。"""
    prior = _state(status="invalidated", revision=2)
    current = _state(status="forming", revision=2)
    (event,) = diff_states({prior.id: prior}, [current])
    assert event.event_type == "reclassified"


def test_end_time_growth_is_updated() -> None:
    """笔在延伸：状态没变、区间变长 → updated（别让「延伸」无迹可寻）。"""
    prior = _state(end_time=1_000, status="forming", revision=1)
    current = _state(end_time=2_000, status="forming", revision=1)
    (event,) = diff_states({prior.id: prior}, [current])
    assert event.event_type == "updated"
    assert event.revision == 2


# --------------------------------------------------------------------------- #
# revision 语义
# --------------------------------------------------------------------------- #


def test_revision_only_increments_when_event_produced() -> None:
    """连续多轮无变化，revision 不动；真变了才 +1。"""
    state = _state(status="forming", revision=5)
    sid = state.id

    for _ in range(3):
        assert diff_states({sid: state}, [state]) == ()
    assert state.revision == 5, "无事件时 revision 不该被消费"

    confirmed = _state(status="confirmed", revision=5)
    (event,) = diff_states({sid: state}, [confirmed])
    assert event.revision == 6


# --------------------------------------------------------------------------- #
# 消失的结构不记事件
# --------------------------------------------------------------------------- #


def test_vanished_structure_is_not_an_event() -> None:
    """曾经有、这次没有 → **不记**。

    消失的原因很多（级别切换、递归参数变了、bars 被重算），没把握之前一律记
    invalidated 会污染事件流。真需要时加 ``expired`` 这一类，而不是先猜。
    """
    gone = _state(kind="bi", start_time=111)
    assert diff_states({gone.id: gone}, []) == ()


# --------------------------------------------------------------------------- #
# payload 往返
# --------------------------------------------------------------------------- #


def test_payload_round_trip() -> None:
    """事件 → 状态：``StructureState`` 真的有出口了。"""
    original = _state(status="confirmed", revision=3)
    event = StructureEvent(
        event_type="confirmed",
        structure_id=original.id,
        revision=original.revision,
        payload=state_to_payload(original),
        occurred_at=original.end_time,
    )
    restored = state_from_event(event)
    assert restored == original


def test_payload_is_json_safe() -> None:
    """payload 要直接进 jsonb —— tuple 转 list，否则适配器会拒。"""
    payload = state_to_payload(_state())
    assert isinstance(payload["source_ids"], list)
    import json

    assert json.dumps(payload, ensure_ascii=False)


def test_state_from_event_tolerates_legacy_payload() -> None:
    """旧版本写进去的 payload 缺字段时也要读得出合法状态，不能抛。"""
    event = StructureEvent(
        event_type="updated",
        structure_id="bi:5:legacy",
        revision=7,
        payload={},  # 全空
        occurred_at=1234,
    )
    state = state_from_event(event)
    assert state.id == "bi:5:legacy"
    assert state.revision == 7
    assert state.first_seen_at == 1234
    assert state.status == "forming"
