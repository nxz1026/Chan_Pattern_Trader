"""级联重构：尾部状态替换、重构事件与依赖滞后扫描（``engine`` 层纯函数）。

对应 ``docs/architecture.md`` §3.2 的 ``engine/rebuild.py``（「尾部失效结构弹出 +
回溯重算 + 级联事件」）、``docs/rules.md`` §9.2 与 ``docs/architecture.md`` §8.2
冻结约定：

- **确认即冻结，重构走新事件**：已确认结构绝不原地修改，失效即发 ``invalidated``
  事件；
- **候选结构就地更新**（``revision + 1``）；
- 高层结构记录 ``source_structure_id + source_revision``；低级别 ``revision``
  变化时扫描依赖滞后的高层结构并标记——这是「无未来函数」的执行机制。

三个函数的职责边界：

1. :func:`rebuild_tail` —— 以调用方重算出的 ``replacement_states`` 作为最新尾部
   状态，把「旧状态集 → 新状态集」合并为一个新的不可变状态元组。冻结状态
   （``confirmed`` / ``open_end`` / ``closed``）只保留 + 追加，永不原地变形；
   其余状态就地 ``revision + 1`` 替换；同 ``id`` 同内容即幂等，不产生新版本。
2. :func:`make_rebuild_events` —— 对「重构前 / 重构后」两个状态集做 ``id`` +
   ``revision`` 差异比较，产出只追加的
   :class:`~cpt.domain.models.StructureEvent`。
3. :func:`scan_stale_dependents` —— 按 ``source_ids`` 的版本化引用约定，找出依赖
   了滞后 ``source_revision`` 的高层状态。

本模块位于 engine 层但保持纯函数：无 I/O、无系统时钟、无随机；时间一律由调用方以
``occurred_at`` 注入（``docs/architecture.md`` §9.2）。对输入只读，同输入必同输出，
因此可单测、可幂等重放。

已知限制（``StructureState`` 数据模型缺口，非本模块可修复）：

- :class:`~cpt.domain.models.StructureState` 没有独立的 ``source_revision``
  字段，无法直接表达「本状态基于上游哪个 revision」。因此：(a)
  :func:`scan_stale_dependents` 只能依赖 ``source_ids`` 中的版本化引用约定；
  (b) :func:`make_rebuild_events` 的 ``payload["source_revision"]`` 只能记录该
  状态自身的 ``revision``（本模块可提供的最接近信息）。缺口应在修复数据模型
  （新增字段）后收紧，而不是在本模块猜测。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Final, cast

from cpt.domain.models import EventType, StructureEvent, StructureState, StructureStatus

__all__ = ["make_rebuild_events", "rebuild_tail", "scan_stale_dependents"]

#: 已冻结状态：确认即冻结，绝不原地修改（``docs/rules.md`` §9.2）。
_FROZEN_STATUSES: Final[tuple[str, ...]] = ("confirmed", "open_end", "closed")

#: ``StructureState.status`` 合法词表（``docs/rules.md`` §8.1 / §8.6）。
_STATUSES: Final[tuple[str, ...]] = ("forming", "confirmed", "invalidated", "open_end", "closed")

#: 冻结结构被重构后，追加版本的初始状态（§9.4：候选态命名 ``forming``）。
_APPENDED_STATUS: Final[str] = "forming"

#: 失效态取值：失效即发 ``invalidated`` 事件（``docs/rules.md`` §9.2）。
_INVALIDATED: Final[str] = "invalidated"

#: 已确认态取值：用于 ``confirmed_at`` 的 engine 时间注入。
_CONFIRMED: Final[str] = "confirmed"

#: 版本化来源引用分隔符：``<source_id>@r<revision>``（:func:`scan_stale_dependents` 约定）。
_REVISION_SEPARATOR: Final[str] = "@r"

#: 来源引用的 ``kind`` 前缀分隔符，与 ``b:`` / ``fx:`` / ``bi:`` 引用风格一致。
_KIND_PREFIX_SEPARATOR: Final[str] = ":"

#: 未设置时间哨兵：epoch 毫秒 0 不是真实行情观测时间（``docs/rules.md`` §8.5 UTC）。
_UNSET_TIME: Final[int] = 0

#: 事件类型（``docs/rules.md`` §9.4 统一词表；本模块只负责这两个）。
_EVENT_UPDATED: Final[str] = "updated"
_EVENT_INVALIDATED: Final[str] = "invalidated"

#: 事件载荷键（§9.2 要求记录 ``source_revision`` 与 ``source_ids``）。
_PAYLOAD_SOURCE_REVISION: Final[str] = "source_revision"
_PAYLOAD_SOURCE_IDS: Final[str] = "source_ids"
_PAYLOAD_STATUS: Final[str] = "status"


def _validate_occurred_at(occurred_at: int) -> None:
    """校验重构时间：必须 ``>= 0``（Unix 毫秒）。"""
    if occurred_at < 0:
        raise ValueError(f"occurred_at 必须 >= 0, 实测 {occurred_at!r}")


def _validate_state(state: StructureState) -> None:
    """校验单个状态：``id`` 非空、``status`` 在词表内、``revision >= 0``。

    不校验 ``level``：级别链由 ``RulesConfig.levels`` 决定，属调用方职责。
    """
    if not state.id:
        raise ValueError("StructureState.id 不能为空")
    if state.status not in _STATUSES:
        raise ValueError(f"StructureState.status 非法 {state.status!r}, 允许 {_STATUSES!r}")
    if state.revision < 0:
        raise ValueError(f"StructureState.revision 必须 >= 0, 实测 {state.revision!r}")


def _validate_states(states: Sequence[StructureState]) -> None:
    """校验状态序列中的每一项。"""
    for state in states:
        _validate_state(state)


def _is_frozen(status: str) -> bool:
    """是否已冻结状态（确认即冻结，``docs/rules.md`` §9.2）。"""
    return status in _FROZEN_STATUSES


def _current(states: Sequence[StructureState]) -> dict[str, StructureState]:
    """按 ``id`` 取当前状态：``revision`` 最大者；同 ``revision`` 保留先出现者。"""
    current: dict[str, StructureState] = {}
    for state in states:
        existing = current.get(state.id)
        if existing is None or state.revision > existing.revision:
            current[state.id] = state
    return current


def _same_content(previous: StructureState, replacement: StructureState) -> bool:
    """内容同一性判定：边界 / 状态 / 方向 / 来源一致即视为同一状态。

    ``revision`` 与展示用元数据（``level`` / ``kind`` / 时间戳）不参与比较——它们
    不构成结构变化。
    """
    return (
        previous.start_time == replacement.start_time
        and previous.end_time == replacement.end_time
        and previous.status == replacement.status
        and previous.direction == replacement.direction
        and previous.source_ids == replacement.source_ids
    )


def _differs(left: StructureState, right: StructureState) -> bool:
    """事件差异判定：``revision`` 为主动因，其余结构字段差异同样触发。"""
    return left.revision != right.revision or _same_content(left, right) is False


def _resolve_time(
    existing: int | None, status: str, frozen_status: str, occurred_at: int
) -> int | None:
    """时间戳注入：状态等于 ``frozen_status`` 而时间未设置时用 ``occurred_at`` 补齐。"""
    if existing is not None:
        return existing
    return occurred_at if status == frozen_status else None


def _adopt(replacement: StructureState, occurred_at: int) -> StructureState:
    """新 ``id``：沿用替换状态字段，仅在 ``first_seen_at`` 未设置时盖 engine 时间。"""
    if replacement.first_seen_at > _UNSET_TIME:
        return replacement
    return replace(replacement, first_seen_at=occurred_at)


def _promote(
    replacement: StructureState, previous: StructureState, occurred_at: int
) -> StructureState:
    """未确认状态就地更新：``revision`` 单调 +1，状态时间戳按替换状态补齐。"""
    return replace(
        replacement,
        revision=max(previous.revision, replacement.revision) + 1,
        first_seen_at=previous.first_seen_at,
        confirmed_at=_resolve_time(
            replacement.confirmed_at, replacement.status, _CONFIRMED, occurred_at
        ),
        invalidated_at=_resolve_time(
            replacement.invalidated_at, replacement.status, _INVALIDATED, occurred_at
        ),
    )


def _revive_frozen(replacement: StructureState, previous: StructureState) -> StructureState:
    """冻结状态被重构：追加新版本，状态回落 ``forming``，确认 / 失效时间清空。

    追加版本是未确认候选，不写确认 / 失效时间，因此不需要 ``occurred_at``。
    """
    return replace(
        replacement,
        revision=max(previous.revision, replacement.revision) + 1,
        status=cast(StructureStatus, _APPENDED_STATUS),
        first_seen_at=previous.first_seen_at,
        confirmed_at=None,
        invalidated_at=None,
    )


def _dedupe(states: Sequence[StructureState]) -> tuple[StructureState, ...]:
    """稳定去重：保持首次出现顺序，丢弃完全相同的后续副本。"""
    seen: set[StructureState] = set()
    result: list[StructureState] = []
    for state in states:
        if state in seen:
            continue
        seen.add(state)
        result.append(state)
    return tuple(result)


def rebuild_tail(
    previous_states: Sequence[StructureState],
    replacement_states: Sequence[StructureState],
    occurred_at: int,
) -> tuple[StructureState, ...]:
    """把重算出的尾部状态合并回当前状态集，返回新的不可变状态元组。

    参数：
        previous_states: 重构前的当前状态集。可含同一 ``id`` 的多个 ``revision``
            记录（见规则 3 的追加语义）；同 ``id`` 的当前状态取 ``revision`` 最大者。
        replacement_states: 调用方回溯重算得到的尾部状态，作为最新尾部状态。
        occurred_at: 本次重构时间（Unix 毫秒，engine 注入）。

    语义（``docs/rules.md`` §9.2 / ``docs/architecture.md`` §8.2）：

    1. **同内容即幂等**：同一 ``id`` 且边界（``start_time`` / ``end_time``）、
       ``status``、``direction``、``source_ids`` 与当前状态一致时，保留旧状态、
       丢弃替换副本——重复重构不产生新 ``revision``。
    2. **未确认状态就地更新**：``status ∉ {confirmed, open_end, closed}`` 的当前
       状态被 ``revision + 1`` 的替换状态就地替换（含 ``invalidated``：§9.2 的
       「确认即冻结」只冻结已确认三元组）。
    3. **冻结状态只追加**：``status ∈ {confirmed, open_end, closed}`` 的当前状态
       原样保留；内容变化时另追加一个 ``revision + 1``、``status = forming`` 的
       新状态（§9.4 术语：候选态命名 ``forming``），``confirmed_at`` /
       ``invalidated_at`` 清空。冻结结构因此永不原地变形，变化只体现在新版本与
       事件上。
    4. **新 ``id`` 追加到尾部**：``previous_states`` 中不存在的 ``id`` 按
       ``replacement_states`` 顺序追加，字段沿用替换状态。
    5. **``revision`` 单调**：新版本 ``revision = max(旧, 替换) + 1``；替换状态
       自带更大 ``revision`` 时不会被回退。
    6. **时间注入**：``first_seen_at`` 沿用旧状态（同一结构 ``id`` 的首次出现时间）；
       新 ``id`` 沿用替换状态，仅当其为 ``0``（未设置）时用 ``occurred_at`` 补齐；
       ``status`` 为 ``confirmed`` / ``invalidated`` 而对应时间未设置时用
       ``occurred_at`` 补齐。
    7. **顺序**：先按 ``previous_states`` 原顺序输出（就地替换落在原位），再按
       ``replacement_states`` 顺序输出追加的冻结新版本与新 ``id``（尾部）；最后
       稳定去重（保持首次出现顺序）。

    不校验 ``level``；对输入只读。

    Raises:
        ValueError: ``occurred_at < 0``，或状态 ``id`` 为空 / ``status`` 不在词表 /
            ``revision < 0``。
    """
    _validate_occurred_at(occurred_at)
    _validate_states(previous_states)
    _validate_states(replacement_states)

    current = _current(previous_states)
    replacing = _current(replacement_states)
    rank: dict[str, int] = {}
    for index, state in enumerate(replacement_states):
        rank.setdefault(state.id, index)

    result: list[StructureState] = []
    tail: list[tuple[int, StructureState]] = []
    for state in previous_states:
        if current.get(state.id) is not state:
            # 同一 id 的低 revision 历史记录：原样保留，不参与替换。
            result.append(state)
            continue
        replacement = replacing.get(state.id)
        if replacement is None or _same_content(state, replacement):
            # 无替换或内容一致：保留当前状态，替换副本在去重语义下被丢弃。
            result.append(state)
            continue
        if _is_frozen(state.status):
            result.append(state)
            tail.append((rank[state.id], _revive_frozen(replacement, state)))
        else:
            result.append(_promote(replacement, state, occurred_at))

    for state in replacement_states:
        if state is not replacing.get(state.id) or state.id in current:
            continue
        tail.append((rank[state.id], _adopt(state, occurred_at)))

    tail.sort(key=lambda item: item[0])
    return _dedupe([*result, *(state for _, state in tail)])


def _event(
    event_type: str, state: StructureState, occurred_at: int, revision: int | None = None
) -> StructureEvent:
    """构造只追加事件：``payload`` 记录 ``source_revision`` / ``source_ids`` / ``status``。"""
    return StructureEvent(
        event_type=cast(EventType, event_type),
        structure_id=state.id,
        revision=state.revision if revision is None else revision,
        payload={
            _PAYLOAD_SOURCE_REVISION: state.revision,
            _PAYLOAD_SOURCE_IDS: state.source_ids,
            _PAYLOAD_STATUS: state.status,
        },
        occurred_at=occurred_at,
    )


def make_rebuild_events(
    before: Sequence[StructureState],
    after: Sequence[StructureState],
    occurred_at: int,
) -> tuple[StructureEvent, ...]:
    """比对重构前后状态集，产出只追加的重构事件（``updated`` / ``invalidated``）。

    参数：
        before: 重构前的状态集（``rebuild_tail`` 的 ``previous_states``）。
        after: 重构后的状态集（``rebuild_tail`` 的返回值或调用方的当前状态投影）。
        occurred_at: 本次重构时间（Unix 毫秒，engine 注入）。

    语义（``docs/rules.md`` §9.2 / §9.4）：

    1. 逐 ``id`` 比较当前状态（``revision`` 最大者）：
       ``revision``、``status`` 或结构字段（边界 / 方向 / ``source_ids``）有差异即
       产出一条 ``updated``，``revision`` 取 ``after`` 的版本号。
    2. ``after`` 中当前状态为 ``invalidated``、而 ``before`` 不是：产出
       ``invalidated``（失效即发事件，§9.2）；``before`` 中已失效则不再重复产出。
    3. ``before`` 中存在而 ``after`` 中消失的 ``id``：产出 ``invalidated``，
       ``revision`` 取旧当前状态 ``revision + 1``（每次变化递增 ``revision``）。
    4. ``payload`` 至少含 ``source_revision`` 与 ``source_ids``（另含 ``status``）。
       因 ``StructureState`` 无独立 ``source_revision`` 字段，
       ``payload["source_revision"]`` 记录该状态自身的 ``revision``（模块 docstring
       已声明该限制）。
    5. 事件只追加：不修改输入、不删除历史事件；同一 ``id`` 只比较当前状态，
       历史版本不产生事件。
    6. 排序稳定：``updated`` 按 ``after`` 中当前状态的出现顺序，随后
       ``invalidated`` 按 ``before`` 中当前状态的出现顺序。
    7. ``created`` / ``confirmed`` / ``closed`` / ``reclassified`` 属前向管线
       （``engine/events.py``）职责，不在本函数产出。``after`` 中新增 ``id`` 按
       ``updated`` 记录——本函数只报告当前状态集投影的差异。

    Raises:
        ValueError: ``occurred_at < 0``，或任一状态 ``id`` 为空 / ``status`` 不在
            词表 / ``revision < 0``。
    """
    _validate_occurred_at(occurred_at)
    _validate_states(before)
    _validate_states(after)

    before_current = _current(before)
    after_current = _current(after)

    events: list[StructureEvent] = []
    handled: set[str] = set()
    for state in after:
        if after_current.get(state.id) is not state or state.id in handled:
            continue
        handled.add(state.id)
        previous = before_current.get(state.id)
        if state.status == _INVALIDATED and (previous is None or previous.status != _INVALIDATED):
            events.append(_event(_EVENT_INVALIDATED, state, occurred_at))
            continue
        if previous is not None and not _differs(previous, state):
            continue
        events.append(_event(_EVENT_UPDATED, state, occurred_at))

    for state in before:
        if before_current.get(state.id) is not state or state.id in handled:
            continue
        handled.add(state.id)
        events.append(_event(_EVENT_INVALIDATED, state, occurred_at, revision=state.revision + 1))

    return tuple(events)


def _parse_versioned_reference(entry: str) -> tuple[str, int] | None:
    """解析版本化引用 ``[*kind*:]<source_id>@r<revision>`` → ``(source_id, revision)``。

    分隔符取最后一处 ``@r``；缺少该后缀（如裸 ``id``）或后缀非十进制数字时返回
    ``None``——裸引用无法判断滞后，见 :func:`scan_stale_dependents` 语义 4。
    """
    target, separator, digits = entry.rpartition(_REVISION_SEPARATOR)
    if not separator or not target or not digits.isdigit():
        return None
    return target, int(digits)


def _referenced_revision(entry: str, source_id: str) -> int | None:
    """条目引用 ``source_id`` 时返回其编码 ``revision``，否则 ``None``。

    接受两种写法：``<source_id>@r<n>``（引用 id 原样）与
    ``<kind>:<source_id>@r<n>``（带 ``kind`` 前缀，与 ``b:`` / ``fx:`` / ``bi:``
    引用一致）。
    """
    parsed = _parse_versioned_reference(entry)
    if parsed is None:
        return None
    target, revision = parsed
    if target == source_id or target.endswith(_KIND_PREFIX_SEPARATOR + source_id):
        return revision
    return None


def scan_stale_dependents(
    states: Sequence[StructureState],
    changed_source_id: str,
    changed_source_revision: int,
) -> tuple[StructureState, ...]:
    """返回依赖了滞后 ``source_revision`` 的状态（按输入顺序过滤，稳定）。

    参数：
        states: 待扫描的状态集（调用方通常传入当前状态投影）。
        changed_source_id: 发生变化的上游结构 ``id``。
        changed_source_revision: 该上游结构的最新 ``revision``。

    语义（``docs/rules.md`` §9.2「无未来函数」执行机制）：

    1. 只考察 ``id != changed_source_id`` 的状态——结构不是自身的依赖。
    2. 依赖关系记录在 ``source_ids`` 中，版本化引用约定为
       ``[*kind*:]<source_id>@r<revision>``（如 ``"bi:7@r3"``、``"source:s1@r1"``）；
       ``kind`` 前缀与 ``b:`` / ``fx:`` / ``bi:`` 引用风格一致。
    3. 某条 ``source_ids`` 项指向 ``changed_source_id`` 且编码 ``revision``
       **小于** ``changed_source_revision`` 时，该状态标记为依赖滞后；编码
       ``revision`` 不小于上游最新版本时视为已同步。
    4. **``StructureState`` 没有 ``source_revision`` 字段**，因此裸 ``id`` 引用
       （如 ``"bi:7"``，不含 ``@r<revision>`` 后缀）无法判断滞后与否：本函数
       **不标记**（不猜测、不假设 ``0``），保持纯函数与幂等；该缺口需在数据模型
       补字段后收紧。
    5. 只读过滤：不修改状态、不删除、不排序；同 ``id`` 的多个版本按输入原样
       返回（调用方决定如何标记已确认状态——已确认结构只能发 ``invalidated``
       事件，不得原地修改）。

    Raises:
        ValueError: ``changed_source_id`` 为空，或 ``changed_source_revision < 0``。
    """
    if not changed_source_id:
        raise ValueError("changed_source_id 不能为空")
    if changed_source_revision < 0:
        raise ValueError(f"changed_source_revision 必须 >= 0, 实测 {changed_source_revision!r}")

    stale: list[StructureState] = []
    for state in states:
        if state.id == changed_source_id:
            continue
        for entry in state.source_ids:
            revision = _referenced_revision(entry, changed_source_id)
            if revision is not None and revision < changed_source_revision:
                stale.append(state)
                break
    return tuple(stale)
