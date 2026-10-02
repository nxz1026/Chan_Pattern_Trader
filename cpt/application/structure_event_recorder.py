"""结构事件流的记录（R26 接线，跨市场共享）。

**这是 ``application`` 层唯一碰「结构事件」的地方。** 两个市场共用：

- **A 股**：``a_share_snapshot`` 自带 ``AShareLocalClient``，把连接传进来；
- **加密**：``_RealtimeProvider`` 没有 PG 客户端，��用 :mod:`cpt.adapters._dbconfig`
  现开一条（那是全仓 PG 连接的**唯一权威实现**，不依赖 asel）。

## 全程 best-effort

事件流是旁路增强，**任何一步失败都只降级、不抛出、也不影响快照本体** —— 与
``_attach_signal_change`` / ``_attach_dual_compare`` 同一纪律。

## 事务边界

``cpt/storage/*`` 一律**不 commit**（本仓约定），所以提交义务在这里。这个坑
踩过三次：R23 的 ``app.py::_persist_run``、R25 的 ``llm_cases`` 两处。每次的
现场都一样 —— 函数正常返回、HTTP 200、日志零告警、表 0 行。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from cpt.domain.models import Bi, Fractal, StructureEvent, TrendType, ZhongShu
from cpt.domain.structure_events import MarketKey

_LOG = logging.getLogger(__name__)

__all__ = ["record_structure_events"]


@contextmanager
def _connection(conn: Any | None) -> Iterator[Any]:
    """有连接就用；没有就现开一条（加密路径）。

    :mod:`cpt.adapters._dbconfig` 是全仓 PG 连接的唯一权威实现，且不依赖
    ``asel`` 包 —— 所以加密侧用它不算越界，``application -> adapters`` 本来
    就是允许的依赖方向。**这里不写任何 SQL**，SQL 只在 ``cpt/storage/``。
    """
    if conn is not None:
        yield conn
        return

    import psycopg  # noqa: PLC0415 — 惰性导入：CI 不装 [db] extra

    from cpt.adapters._dbconfig import connection_kwargs  # noqa: PLC0415

    opened = psycopg.connect(**connection_kwargs())
    try:
        yield opened
    finally:
        opened.close()


def record_structure_events(
    *,
    market: MarketKey,
    fractals: Sequence[Fractal] = (),
    bis: Sequence[Bi] = (),
    zhongshus: Sequence[ZhongShu] = (),
    trend_types: Sequence[TrendType] = (),
    conn: Any | None = None,
    symbol: str = "",
    fingerprint: dict[str, str] | None = None,
) -> tuple[StructureEvent, ...]:
    """diff 出本轮结构变化 → append 到事件流 → 返回事件。

    :param market: 写进 ``structure_id`` 的市场前缀（``cn`` / ``crypto``）。**必填**：
        不带市场前缀的 id 会让两个市场在同 level 上撞同一 ``start_time`` 时静默合并。
    :param conn: 复用的连接；``None`` 表示自己开一条（加密路径）。
    :param symbol: 标的代码。**归因必需**（``cpt_run_metric`` 按 market+symbol 存
        指纹，而 ``structure_id`` 本身**不含代码** —— 它是
        ``{market}:{kind}:{level}:{start_time}``）。留空则不归因。
    :param fingerprint: **本轮**的算法指纹四件套（``config_hash`` /
        ``dataset_hash`` / ``rules_version`` / ``backend``）。给了且 ``symbol``
        非空就归因「这次结构变化是什么原因」，写进每条事件的 ``payload["cause"]``；
        否则**不归因**（payload 里没有 ``cause`` 这个键），而不是归一个假原因。
    :returns: 本轮产生的事件；无变化 / 计算失败时返回空元组。

    **空批次是常态**：每轮快照都 diff，而同一根 K 线上的结构大多不变。
    此时不产生事件、不写库、不 commit。

    ## 写库失败时仍然返回事件 —— 这是刻意的

    表不存在（迁移没跑）或连接不通时，``current_states`` 降级成 ``{}``，
    ``append_events`` 写失败被吞，但**本函数照样把算出的事件返回**。

    理由：``snapshot.events`` 回答的是「本次计算里什么结构变了」，这个事实与
    **能不能落库是两件事**。DB 故障不该把一个真实的数据字段清空 —— 那会让
    看板在一次数据库抖动后突然少显示一批变化，比「没落库」更难解释。落库本身
    仍是 best-effort 审计，失败只进日志。

    代价要说清：这些事件**没有**进事件流，跨重启的追溯里查不到。所以
    ``status`` 会出现「快照说有 created、库里没有」的状态。
    """
    from cpt.domain.structure_events import diff_states, states_from_structures
    from cpt.storage.structure_event_store import append_events, current_states

    try:
        states = states_from_structures(
            market=market,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            trend_types=trend_types,
        )
        if not states:
            return ()
    except Exception as exc:  # noqa: BLE001 — 纯计算失败也不该影响快照
        _LOG.warning("结构状态构造失败（不影响快照）: %s", exc)
        return ()

    try:
        with _connection(conn) as db:
            previous = current_states(db, [s.id for s in states])
            events = diff_states(previous, states)
            if not events:
                return ()  # 热路径常态：不写库
            if fingerprint is not None and symbol:
                events = _with_cause(
                    db, events, market=market, symbol=symbol, fingerprint=fingerprint
                )
            append_events(db, events)
            db.commit()  # ← store 层不 commit，边界在这里
            return events
    except Exception as exc:  # noqa: BLE001 — 旁路失败不影响快照
        _LOG.warning("结构事件记录失败（不影响快照）: %s", exc)
        return ()


def _with_cause(
    conn: Any,
    events: Sequence[StructureEvent],
    *,
    market: str,
    symbol: str,
    fingerprint: dict[str, str],
) -> tuple[StructureEvent, ...]:
    """给每条事件标上「这次结构变化是什么原因」，返回**新**事件元组。

    ## 为什么要这一项

    ``cpt_structure_event`` 记了「**什么**结构变了」，却没说「**为什么**」。
    而这四种原因对 Loop/LLM 的价值天差地别：「输入数据换了（K 线/因子）」该去查
    数据源，「算法自己变了」该怀疑代码 —— 混在一起就等于没归因。

    ## 判据从哪来

    拿**上一轮**运行落的指纹（``cpt_run_metric`` 最近一条 ``kind='run'``）与本轮
    指纹逐项比。判定顺序按「确定性」从高到低，见
    :func:`cpt.application.run_metric.explain_cause`：后端 → 配置 → 数据 → 残差。

    ## 为什么返回新元组而不是原地改

    ``StructureEvent`` 是 ``frozen=True``（``cpt/domain/models.py``）。所以只能
    :func:`dataclasses.replace` 造新的 —— 顺手保证了「归因失败就原样返回」很容易写。

    ## 两处刻意的留白

    - **归不出就不加这个键**（而不是加 ``""``）。首次运行没有前值可比；写个空串
      会让前端必须区分「原因不明」和「原因算出来是空」—— 后者不该存在。缺键本身
      就是信号。
    - **本函数不抛**。归因是增强，标不上就原样返回事件。
    """
    from cpt.application.run_metric import explain_cause
    from cpt.storage.run_metric_store import latest_run_fingerprint

    try:
        previous = latest_run_fingerprint(conn, market=market, symbol=symbol)
        cause = explain_cause(previous, fingerprint) if previous else ""
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("结构变化归因失败 %s/%s: %s", market, symbol, exc)
        return tuple(events)
    if not cause:
        return tuple(events)
    return tuple(replace(event, payload={**event.payload, "cause": cause}) for event in events)
