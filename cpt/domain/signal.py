"""一买状态机（``docs/rules.md`` §8.2 / §8.6，纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的「结构序列 → ``Signal[]``」环节，是 CPT 自研
三个核心模块之一（另两个：:mod:`cpt.domain.trend_type`、:mod:`cpt.domain.recursion`）。

规则口径按 ``docs/rules.md`` §8.2（一买状态）与 §8.6（信号字段、只追加语义）：

1. **只处理向下走势**：一买是向下走势末端的多头信号，``trend_direction`` 必须为
   ``-1``；``+1``（向上）是合法取值但结构准备恒不成立——本模块不为向上走势编造
   买点，也不抛异常把它当成调用错误。
2. **结构准备条件**：``has_two_centers``（当前级别存在至少两个同级中枢）与
   ``has_divergence_leg``（第二中枢之后存在继续沿原趋势方向运行的一笔，即背驰段）
   同时成立，才算结构准备就绪（§8.2 第 1、2、3 条）。
3. **背驰不是硬门槛**：``divergence_status`` 只记录 ``chanlun-pro`` 的背驰比较
   结果，取值 ``not_checked`` / ``not_detected`` / ``detected``；它**不参与**结构
   准备判断（§8.2「背驰是否成立不作为一买候选的硬门槛」）。
4. **反向结构是确认门槛**：结构准备就绪且 ``has_reversal_bi``（背驰段之后出现反向
   新笔，§8.2 第 4 条）成立时进入 ``confirmed``；否则停在结构侧状态。
5. **状态阶梯只升不降**：``structure_ready → alert → candidate → confirmed``，而
   ``invalidated`` 是终态——已失效信号不被后续入参复活。``alert``（反向K线盘中
   出现）与 ``candidate``（反向K线收盘后仍成立）需要盘中/收盘信息，本函数没有
   盘中参数，因此**不主动产生**这两个状态，只保持 ``previous`` 已有的取值；
   ``alert → candidate → confirmed`` 的推进由 :func:`transition_first_buy`
   （收盘后调用）承担。
6. **时间戳**：``event_time`` 只写入与本次产出状态对应的字段（``alert_time`` /
   ``candidate_time`` / ``confirmed_time`` / ``invalidated_time``）；``previous``
   上已有的时间戳一律保留（§8.6「历史最终结果不能覆盖实时预警」）。
   ``structure_ready`` 没有对应时间字段，且「首次准备不自动 alert/candidate」，
   故其四个时间戳均为 ``None``。
7. **信号标识**：``signal_id`` 固定为 ``first_buy:{level}:{structure_id}``，同级别
   同结构恒等、重复调用可复现；它同时是仓储 upsert 的主键
   （``cpt.storage.repository.SQLiteRepository.upsert_signal``），使一条信号随状态
   演进原地更新，而不是每步产生新行。

本模块是纯函数状态机：**不**检测背驰（只记录三态）、**不**自行判断结构准备以外的
结构有效性（``has_two_centers`` / ``has_divergence_leg`` / ``structure_valid`` 由
:func:`cpt.domain.trend_type.classify_trend` 与级联重构流程提供）、**不**写事件与
仓储、**不**下单（§8.2「CPT 只输出信号和状态，不自动下单」）。对输入只读，重复
调用同输入必同输出。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Final, Literal, cast

from cpt.domain.models import DivergenceStatus, Signal, SignalStatus

__all__ = ["assess_first_buy", "transition_first_buy"]

#: 本模块产出的信号类型（``Signal.signal_type``，``docs/rules.md`` §8.6）。
_SIGNAL_TYPE: Final[str] = "first_buy"

#: 走势方向：向下（一买唯一适用方向）/ 向上。
_DOWN: Final[int] = -1
_UP: Final[int] = 1

#: ``trend_direction`` 合法取值；方向未定（``0``）不构成一买输入。
_DIRECTIONS: Final[tuple[int, int]] = (_UP, _DOWN)

#: 一买状态阶梯（``Signal.status``，``docs/rules.md`` §8.2）。
_STATUS_STRUCTURE_READY: Final[str] = "structure_ready"
_STATUS_ALERT: Final[str] = "alert"
_STATUS_CANDIDATE: Final[str] = "candidate"
_STATUS_CONFIRMED: Final[str] = "confirmed"
_STATUS_INVALIDATED: Final[str] = "invalidated"

#: 状态词表（``docs/architecture.md`` §6 与 §8.2 一致）。
_STATUSES: Final[tuple[str, ...]] = (
    _STATUS_STRUCTURE_READY,
    _STATUS_ALERT,
    _STATUS_CANDIDATE,
    _STATUS_CONFIRMED,
    _STATUS_INVALIDATED,
)

#: 背驰状态词表（``docs/rules.md`` §8.2 / §9.6 冻结）。
_DIVERGENCE_STATUSES: Final[tuple[str, ...]] = ("not_checked", "not_detected", "detected")


def _validate_level(level: int) -> None:
    """校验信号级别：必须 ``>= 0``。"""
    if level < 0:
        raise ValueError(f"level 必须 >= 0, 实测 {level!r}")


def _validate_event_time(event_time: int) -> None:
    """校验事件时间：必须 ``>= 0``（Unix 毫秒）。"""
    if event_time < 0:
        raise ValueError(f"event_time 必须 >= 0, 实测 {event_time!r}")


def _validate_divergence_status(divergence_status: str) -> None:
    """校验背驰状态取值。"""
    if divergence_status not in _DIVERGENCE_STATUSES:
        raise ValueError(
            f"divergence_status 必须是 {_DIVERGENCE_STATUSES} 之一, 实测 {divergence_status!r}"
        )


def _validate_previous(previous: Signal | None, level: int | None = None) -> None:
    """校验上一步信号：类型、状态、背驰三态，以及（可选）级别一致。"""
    if previous is None:
        return
    if previous.signal_type != _SIGNAL_TYPE:
        raise ValueError(
            f"previous.signal_type 必须是 {_SIGNAL_TYPE!r}, 实测 {previous.signal_type!r}"
        )
    if previous.status not in _STATUSES:
        raise ValueError(f"previous.status 必须是 {_STATUSES} 之一, 实测 {previous.status!r}")
    _validate_divergence_status(previous.divergence_status)
    if level is not None and previous.level != level:
        raise ValueError(f"previous.level({previous.level}) 必须与 level({level}) 同级")


def _signal_id(level: int, structure_id: str) -> str:
    """确定性信号标识：同级别同结构恒等（仓储 upsert 主键）。"""
    return f"{_SIGNAL_TYPE}:{level}:{structure_id}"


def _resolve_price(price: float, fallback: float) -> float:
    """价格解析：``price <= 0`` 视为「未提供」，沿用 ``fallback``。

    非有限值（``nan`` / ``inf``）与负值视为非法入参。
    """
    if not math.isfinite(price) or price < 0.0:
        raise ValueError(f"price 必须是有限非负值, 实测 {price!r}")
    return price if price > 0.0 else fallback


def _structure_ready(trend_direction: int, has_two_centers: bool, has_divergence_leg: bool) -> bool:
    """§8.2 结构准备：向下走势 + 两个同级中枢 + 背驰段。"""
    return trend_direction == _DOWN and has_two_centers and has_divergence_leg


def _stamp(existing: int | None, event_time: int) -> int | None:
    """时间戳保留语义：已有值不覆盖，缺失时写入 ``event_time``。"""
    return existing if existing is not None else event_time


def _times(
    previous: Signal | None, status: str, event_time: int
) -> tuple[int | None, int | None, int | None, int | None]:
    """按目标 ``status`` 生成 ``(alert, candidate, confirmed, invalidated)`` 时间戳四元组。

    只填目标状态对应的时间字段，其余字段沿用 ``previous``（历史不可覆盖）。
    """
    alert = previous.alert_time if previous is not None else None
    candidate = previous.candidate_time if previous is not None else None
    confirmed = previous.confirmed_time if previous is not None else None
    invalidated = previous.invalidated_time if previous is not None else None
    if status == _STATUS_ALERT:
        alert = _stamp(alert, event_time)
    elif status == _STATUS_CANDIDATE:
        candidate = _stamp(candidate, event_time)
    elif status == _STATUS_CONFIRMED:
        confirmed = _stamp(confirmed, event_time)
    elif status == _STATUS_INVALIDATED:
        invalidated = _stamp(invalidated, event_time)
    return alert, candidate, confirmed, invalidated


def assess_first_buy(
    level: int,
    structure_id: str,
    center_ids: Sequence[str],
    trend_direction: int,
    has_two_centers: bool,
    has_divergence_leg: bool,
    has_reversal_bi: bool,
    divergence_status: str = "not_checked",
    price: float = 0.0,
    source_revision: int = 0,
    event_time: int = 0,
    previous: Signal | None = None,
) -> Signal:
    """按结构事实评估一买信号，返回不可变 :class:`~cpt.domain.models.Signal`。

    :param level: 信号级别，必须 ``>= 0``。
    :param structure_id: 承载该信号的结构标识（走势类型 / 结构元素 id），非空；
        与 ``level`` 共同决定 ``signal_id``。
    :param center_ids: 参与一买判定的中枢 id 序列，按给定顺序原样写入
        （去重与排序属调用方职责，本函数不重排、不猜测）。
    :param trend_direction: 走势方向，必须 ``1`` 或 ``-1``；只有 ``-1``（向下）
        才可能进入结构准备。
    :param has_two_centers: 当前级别是否已存在至少两个同级中枢（§8.2 第 1 条）。
    :param has_divergence_leg: 第二中枢之后是否存在继续沿原趋势方向运行的一笔，
        即背驰段（§8.2 第 2、3 条）。
    :param has_reversal_bi: 背驰段之后是否已出现反向新笔（§8.2 第 4 条）。
    :param divergence_status: 背驰比较结果，取值 ``not_checked`` /
        ``not_detected`` / ``detected``；不是结构准备门槛。
    :param price: 当前价格；``<= 0`` 视为未提供，沿用 ``previous.price``
        （无 ``previous`` 时为 ``0.0``）。
    :param source_revision: 承载结构的 revision，原样记录（本函数不自增、不回填）。
    :param event_time: 本次评估的事件时间（Unix 毫秒），写入目标状态对应的时间戳。
    :param previous: 上一步信号；``None`` 表示首次评估。必须同级别、同
        ``signal_type``，状态与背驰取值合法。
    :raises ValueError: ``level < 0``；``trend_direction`` 不是 ``1`` / ``-1``；
        ``structure_id`` 为空；``source_revision < 0``；``event_time < 0``；
        ``divergence_status`` 不在三态内；``price`` 为负或非有限值；
        ``previous`` 的 ``signal_type`` / ``status`` / ``divergence_status``
        非法或与 ``level`` 不同级。
    :returns: 评估后的不可变信号。状态取值见模块 docstring 第 5 条：结构准备就绪
        且 ``has_reversal_bi`` 成立时为 ``confirmed``（首次评估亦然，等价于
        ``structure_ready → confirmed``）；就绪且无反向笔时，无 ``previous`` 为
        ``structure_ready``；就绪且无反向笔且 ``previous`` 为 ``confirmed`` 保持
        ``confirmed``，为 ``alert`` / ``candidate`` 则保持原状态；不满足结构准备
        或 ``previous`` 已 ``invalidated`` 时为 ``invalidated``。本函数不产生
        ``alert`` —— 它需要盘中反向K线信息（§8.2），由实时引擎在
        :func:`transition_first_buy` 之外路径标注。
    """
    _validate_level(level)
    if trend_direction not in _DIRECTIONS:
        raise ValueError(f"trend_direction 必须是 1 或 -1, 实测 {trend_direction!r}")
    if not structure_id:
        raise ValueError("structure_id 必须为非空字符串")
    if source_revision < 0:
        raise ValueError(f"source_revision 必须 >= 0, 实测 {source_revision!r}")
    _validate_event_time(event_time)
    _validate_divergence_status(divergence_status)
    _validate_previous(previous, level)

    if previous is not None and previous.status == _STATUS_INVALIDATED:
        status = _STATUS_INVALIDATED
    elif not _structure_ready(trend_direction, has_two_centers, has_divergence_leg):
        status = _STATUS_INVALIDATED
    elif has_reversal_bi:
        status = _STATUS_CONFIRMED
    elif previous is None:
        status = _STATUS_STRUCTURE_READY
    else:
        status = previous.status

    alert_time, candidate_time, confirmed_time, invalidated_time = _times(
        previous, status, event_time
    )
    return Signal(
        signal_id=_signal_id(level, structure_id),
        level=level,
        signal_type=cast("Literal['first_buy']", _SIGNAL_TYPE),
        status=cast(SignalStatus, status),
        structure_id=structure_id,
        center_ids=tuple(center_ids),
        divergence_status=cast(DivergenceStatus, divergence_status),
        alert_time=alert_time,
        candidate_time=candidate_time,
        confirmed_time=confirmed_time,
        invalidated_time=invalidated_time,
        price=_resolve_price(price, previous.price if previous is not None else 0.0),
        source_revision=source_revision,
    )


def transition_first_buy(
    previous: Signal,
    reversal_closed: bool,
    structure_valid: bool,
    event_time: int,
    price: float | None = None,
) -> Signal:
    """推进已存在的一买信号（收盘后调用），返回新的不可变信号。

    :param previous: 上一步信号；必须 ``signal_type="first_buy"``、状态与背驰取值
        合法（级别随信号自身，本函数不校验跨级别）。
    :param reversal_closed: 反向K线是否收盘后仍成立（§8.2「反向K线收盘后仍成立」）。
    :param structure_valid: 一买结构（两个同级中枢 + 背驰段）是否仍然成立。
    :param event_time: 本次事件时间（Unix 毫秒），必须 ``>= 0``。
    :param price: 当前价格；``None`` 表示沿用 ``previous.price``；``<= 0`` 同样
        视为未提供。负值或非有限值抛 ``ValueError``。
    :raises ValueError: ``event_time < 0``；``price`` 为负或非有限值；
        ``previous`` 的 ``signal_type`` / ``status`` / ``divergence_status`` 非法。
    :returns: 状态推进后的信号，``signal_id`` / ``structure_id`` / ``center_ids`` /
        ``divergence_status`` / ``source_revision`` 原样保持。推进规则：
        ``confirmed`` 在结构仍有效时保持 ``confirmed``，结构失效转 ``invalidated``；
        ``structure_ready`` / ``alert`` / ``candidate`` 在 ``reversal_closed`` 且
        结构有效时转 ``confirmed``，否则保持原状态（其中 ``alert`` 无反向结构时转
        ``candidate``，即收盘后仍成立）；``invalidated`` 保持 ``invalidated``。
    """
    _validate_previous(previous)
    _validate_event_time(event_time)

    if previous.status == _STATUS_INVALIDATED:
        status = _STATUS_INVALIDATED
    elif previous.status == _STATUS_CONFIRMED:
        status = _STATUS_CONFIRMED if structure_valid else _STATUS_INVALIDATED
    elif reversal_closed:
        status = _STATUS_CONFIRMED if structure_valid else previous.status
    elif previous.status == _STATUS_ALERT:
        status = _STATUS_CANDIDATE
    else:
        status = previous.status

    alert_time, candidate_time, confirmed_time, invalidated_time = _times(
        previous, status, event_time
    )
    resolved_price = previous.price if price is None else _resolve_price(price, previous.price)
    return replace(
        previous,
        status=cast(SignalStatus, status),
        alert_time=alert_time,
        candidate_time=candidate_time,
        confirmed_time=confirmed_time,
        invalidated_time=invalidated_time,
        price=resolved_price,
    )
