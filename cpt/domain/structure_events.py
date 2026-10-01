"""结构事件的跃迁判定（纯函数，零 IO）。

**为什么在 domain**：「forming → confirmed 该记什么事件」是**规则口径**，不是编排细节。
`docs/rules.md` §8.6 定的「事件只追加、不原地改写」在这里落地。

## 三层模型与本模块的关系

`rules.md` §8.6 的「当前状态 + 不可变事件 + 信号」里：

- **信号**那一层 R21 已落地（``cpt_signal_event`` / ``signal_event_store``）；
- **结构**这一层就是本模块 + ``structure_event_store``；
- **当前状态**不是一张表，而是**从事件流派生** —— 同 ``structure_id`` 的
  最新一条。刻意不建状态表：两张表必然出现「状态表说 A、事件表说 B」的
  不一致，而事件流是唯一真相。R21 已经是这个形态（``cpt_signal_event`` 没有
  配一张 signal 状态表），保持一致。

## 幂等的前提

``StructureState.id`` 必须**确定性生成** —— 同一份 bars 重算必得同一个 id。
本模块用 ``f"{kind}:{level}:{start_time}"``：

- ``kind`` 来自结构类型（fractal/bi/zhongshu/trend_type）；
- ``level`` 来自级别；
- ``start_time`` 是**结构的起始时刻**，不随行情推进而变（变的会另起一个 id，
  这正是我们要的：那是两个结构，不是一个结构的两次修订）。

domain 已验证零时钟零随机（``git grep 'datetime\\.now|random\\.' cpt/domain`` 零命中），
所以「同输入必同输出 → 同 id → 幂等重放」这条链是真的，不是口号。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from cpt.domain.models import (
    Bi,
    EventType,
    Fractal,
    StructureEvent,
    StructureKind,
    StructureState,
    StructureStatus,
    TrendType,
    ZhongShu,
)

__all__ = [
    "EVENT_FOR_STATUS",
    "diff_states",
    "state_from_event",
    "state_to_payload",
    "states_from_structures",
    "structure_id_of",
]


def structure_id_of(kind: StructureKind, level: int, start_time: int) -> str:
    """结构业务主键。**确定性**是幂等重放的全部前提，见模块 docstring。"""
    return f"{kind}:{level}:{start_time}"


#: 状态跃迁 → 事件类型。
#:
#: 只列**有意义的**跃迁；``updated``（同状态内容微调）不落事件，
#: 否则每根新 K 线都会给同一结构加一条 revision，事件流会被噪声淹没。
#: 这也是 R21 信号侧的做法：只有 status 变化才 append。
EVENT_FOR_STATUS: Mapping[tuple[StructureStatus, StructureStatus], EventType] = {
    ("forming", "confirmed"): "confirmed",
    ("forming", "invalidated"): "invalidated",
    ("confirmed", "invalidated"): "invalidated",
    ("invalidated", "forming"): "reclassified",
}


def _event_type_for(previous: StructureState, current: StructureState) -> EventType | None:
    """两个版本之间该记什么事件；``None`` = 无事件。

    **刻意没有「kind 变了 → reclassified」这条分支**：``structure_id_of`` 把
    ``kind`` 算进了 id，所以同一个 id 必然同 kind。换个 kind 意味着这是**另一个
    结构**（起点相同的中枢和笔），在 ``diff_states`` 眼里是 ``created`` 而非
    重分类 —— 加那条分支只会写出一段永远走不到的死代码。

    ``reclassified`` 的真实含义在 ``EVENT_FOR_STATUS``：被 invalidated 的结构
    重新 forming。
    """
    mapped = EVENT_FOR_STATUS.get((previous.status, current.status))
    if mapped is not None:
        return mapped
    if previous.status == current.status and previous.end_time != current.end_time:
        # 状态没变但区间长了 —— 典型的是「笔还在延伸」。记 updated，
        # 断的是「同一结构被反复重算却毫无痕迹」这件事。
        return "updated"
    return None


def diff_states(
    previous: Mapping[str, StructureState],
    current: Sequence[StructureState],
) -> tuple[StructureEvent, ...]:
    """对比「库里的最新状态」与「本次算出的结构」，产出要追加的事件。

    :param previous: ``structure_id -> StructureState``，来自事件流的最新一条。
        **没有历史**（首次跑）时传空 dict —— 那时全部结构都记 ``created``。
    :param current: 本次快照算出的全部结构状态。
    :returns: 待 append 的事件；无变化时返回空元组（**不产生写**，这是热路径的常态）。

    **只处理 ``current`` 里有的结构。** 「曾经存在、这次消失了」不记事件 ——
    消失的原因是多种多样的（级别切换、递归链参数变了、bars 被重算），
    在没有把握之前把它们一律记成 ``invalidated`` 会污染事件流。真正需要时
    再单独加一类 ``expired``，而不是先猜。
    """
    events: list[StructureEvent] = []
    for state in current:
        sid = state.id
        prior = previous.get(sid)
        if prior is None:
            events.append(
                StructureEvent(
                    event_type="created",
                    structure_id=sid,
                    revision=1,
                    payload=state_to_payload(state),
                    occurred_at=state.first_seen_at,
                )
            )
            continue

        event_type = _event_type_for(prior, state)
        if event_type is None:
            continue

        # revision 单调递增：只在**真的产生事件**时 +1。
        # 没有事件的版本不占号，否则「同一结构 100 次重算 = revision 100」，
        # revision 就失去了「改了多少次」的意义。
        #
        # payload 必须**同步写入新 revision** —— 否则事件行 revision=7 而 payload
        # 里还是上一版的 1，从 payload 派生的状态就自带过期 revision。
        stamped = replace(state, revision=prior.revision + 1)
        events.append(
            StructureEvent(
                event_type=event_type,
                structure_id=sid,
                revision=prior.revision + 1,
                payload=state_to_payload(stamped),
                occurred_at=state.confirmed_at or state.invalidated_at or state.end_time,
            )
        )
    return tuple(events)


def states_from_structures(
    *,
    fractals: Sequence[Fractal] = (),
    bis: Sequence[Bi] = (),
    zhongshus: Sequence[ZhongShu] = (),
    trend_types: Sequence[TrendType] = (),
) -> tuple[StructureState, ...]:
    """把一次算出的结构集合转成 ``StructureState`` 序列（纯函数）。

    **这一步此前全仓不存在** —— ``StructureState`` 定义了却没人构造它，
    因为「domain 对象 → 可持久化的状态」这个转换压根没人写。R26 补上。

    ## status 怎么定

    domain 的 ``Bi`` / ``ZhongShu`` 本身**不带 status**（它们描述「是什么」，
    不描述「现在确没确认」）。这里按可判定的规则取：

    - **分型**恒 ``confirmed`` —— 三根固定 K 线构成，天然已定型；
    - **笔 / 中枢**：序列里**最后一个**记 ``forming``（后续 K 线可能让它延伸），
      其余记 ``confirmed``。这是缠论里「笔会被延伸」的直接体现；
    - **走势类型**按 ``TrendKind`` 判：``forming`` / ``open_end`` → ``forming``，
      ``reclassified`` → ``confirmed``（重分类本身就是确认过的结论），其余 → ``confirmed``。

    取值都在 ``StructureStatus`` 词表内，且**确定性**（只依赖输入顺序与内容，
    不读时钟）。
    """
    states: list[StructureState] = []

    for f in fractals:
        states.append(
            StructureState(
                id=structure_id_of("fractal", f.level, f.start_time),
                level=f.level,
                kind="fractal",
                direction=1 if f.kind == "top" else -1,
                start_time=f.start_time,
                end_time=f.end_time,
                status="confirmed",
                revision=1,
                first_seen_at=f.start_time,
                confirmed_at=f.end_time,
                invalidated_at=None,
                source_ids=tuple(f.source_ids),
            )
        )

    # 笔与中枢的形状一致，但**刻意写成两个块而不是
    # `for group, kind in ((bis, "bi"), (zhongshus, "zhongshu"))``** ——
    # 异构循环会让 mypy 把 `group` 推成 `object`，随后每个属性访问都报错，
    # 最后只能靠一堆 `type: ignore` 盖住（实测会盖出 10 条错误）。
    for index, b in enumerate(bis):
        forming = index == len(bis) - 1
        states.append(
            StructureState(
                id=structure_id_of("bi", b.level, b.start_time),
                level=b.level,
                kind="bi",
                direction=b.direction,
                start_time=b.start_time,
                end_time=b.end_time,
                status="forming" if forming else "confirmed",
                revision=1,
                first_seen_at=b.start_time,
                confirmed_at=None if forming else b.end_time,
                invalidated_at=None,
                source_ids=tuple(b.source_ids),
            )
        )

    for index, z in enumerate(zhongshus):
        forming = index == len(zhongshus) - 1
        states.append(
            StructureState(
                id=structure_id_of("zhongshu", z.level, z.start_time),
                level=z.level,
                kind="zhongshu",
                # 中枢是**连续三笔的重叠区间**（见 zhongshu.build_zhongshus），
                # 它本身没有方向 —— 方向属于构成它的笔。`BarLike.direction` 的
                # 词表里 0 就是「中性/未定」，这里如实填 0，不自造约定。
                direction=0,
                start_time=z.start_time,
                end_time=z.end_time,
                status="forming" if forming else "confirmed",
                revision=1,
                first_seen_at=z.start_time,
                confirmed_at=None if forming else z.end_time,
                invalidated_at=None,
                source_ids=tuple(z.bi_ids),
            )
        )

    for t in trend_types:
        forming = t.kind in ("forming", "open_end")
        states.append(
            StructureState(
                id=structure_id_of("trend_type", t.level, t.start_time),
                level=t.level,
                kind="trend_type",
                direction=t.direction,
                start_time=t.start_time,
                end_time=t.end_time,
                status="forming" if forming else "confirmed",
                revision=1,
                first_seen_at=t.start_time,
                confirmed_at=None if forming else t.end_time,
                invalidated_at=None,
                source_ids=tuple(t.source_ids),
            )
        )

    return tuple(states)


def state_to_payload(state: StructureState) -> dict[str, object]:
    """``StructureState`` → 可直接进 jsonb 的 dict。

    tuple 字段（``source_ids``）转 list，否则 psycopg 的 jsonb 适配会拒。
    """
    return {
        "id": state.id,
        "level": state.level,
        "kind": state.kind,
        "direction": state.direction,
        "start_time": state.start_time,
        "end_time": state.end_time,
        "status": state.status,
        "revision": state.revision,
        "first_seen_at": state.first_seen_at,
        "confirmed_at": state.confirmed_at,
        "invalidated_at": state.invalidated_at,
        "source_ids": list(state.source_ids),
    }


def _as_int(value: object, default: int) -> int:
    """``object`` → ``int`` 的收窄取值。类型对不上就用 default，不抛。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_opt_int(value: object) -> int | None:
    """可空整数（``confirmed_at`` / ``invalidated_at`` 用）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_str(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def state_from_event(event: StructureEvent) -> StructureState:
    """事件 → ``StructureState``（当前状态的派生入口）。

    payload 里缺字段时用事件的 revision / occurred_at 兜底，
    这样**即便 payload 是旧版本写进去的**也能读出合法状态。
    """
    payload = event.payload
    raw_sources = payload.get("source_ids")
    source_ids = (
        tuple(str(x) for x in raw_sources) if isinstance(raw_sources, (list, tuple)) else ()
    )
    return StructureState(
        id=_as_str(payload.get("id"), event.structure_id),
        level=_as_int(payload.get("level"), 0),
        kind=_as_str(payload.get("kind"), "bi"),  # type: ignore[arg-type]
        direction=_as_int(payload.get("direction"), 0),
        start_time=_as_int(payload.get("start_time"), 0),
        end_time=_as_int(payload.get("end_time"), 0),
        status=_as_str(payload.get("status"), "forming"),  # type: ignore[arg-type]
        revision=_as_int(payload.get("revision"), event.revision),
        first_seen_at=_as_int(payload.get("first_seen_at"), event.occurred_at),
        confirmed_at=_as_opt_int(payload.get("confirmed_at")),
        invalidated_at=_as_opt_int(payload.get("invalidated_at")),
        source_ids=source_ids,
    )
