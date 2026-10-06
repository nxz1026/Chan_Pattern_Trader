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


def _rollback_quietly(conn: Any) -> None:
    """回滚传入的连接，**绝不**把异常抛出去。

    这是救连接的最后一道：连接已经坏了，rollback 自己也可能失败（比如连接
    已断）。这里失败就只记日志 —— 快照是热路径，救不回来也不能带崩它。
    """
    try:
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — 救不回来也不能带崩快照
        _LOG.warning("结构事件 rollback 失败（连接可能已断）: %s", exc)


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

    表不存在（迁移没跑）或连接不通时，``append_events`` 降级返回 0，
    ``current_states`` 读失败会抛，但**本函数照样把算出的事件返回**。

    理由：``snapshot.events`` 回答的是「本次计算里什么结构变了」，这个事实与
    **能不能落库是两件事**。DB 故障不该把一个真实的数据字段清空 —— 那会让
    看板在一次数据库抖动后突然少显示一批变化，比「没落库」更难解释。落库本身
    仍是 best-effort 审计，失败只进日志。

    代价要说清：这些事件**没有**进事件流，跨重启的追溯里查不到。所以
    ``status`` 会出现「快照说有 created、库里没有」的状态。

    ## 共享连接必须被救回来（2026-10-06 修）

    两条降级路径都会让传入的连接停在 ``current transaction is aborted``：

    1. ``current_states`` 读失败抛 —— 走下面的 ``except``。
    2. ``append_events`` 写失败**吞掉**并返回 0 —— 异常不冒泡，**原来的
       ``except`` 根本不会执行**（这就是 R45 补的 rollback 防御形同虚设的原因）。

    PG 里任何一条语句失败都会把整个事务打成 aborted，而调用方
    （``a_share_snapshot``）传进来的是**共享连接**、后面还有十余处查询要跑。
    一次事件写入失败会连带整轮快照逐只降级。

    修法：**失败时**无条件 rollback 传入的连接。**注意不是「每次都 rollback」**
    —— 热路径常态是「无变化、不写库、不 commit」（上面 ``if not events`` 的
    早退），此时共享连接上可能还挂着调用方自己的未提交事务，无差别 rollback
    会把别人的工作销毁掉。所以只在「本次真的碰过库且碰坏了」时救。
    """
    from cpt.domain.structure_events import diff_states, states_from_structures
    from cpt.storage.structure_event_store import (
        append_events,
        current_states,
        is_missing_table_error,
    )

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
            try:
                previous = current_states(db, [s.id for s in states])
            except Exception as exc:
                # 读失败分两种，处置完全不同（2026-10-06）：
                #
                # - **表不存在**（42P01，多半是迁移没跑）：事件流本来就是空的，
                #   「无历史」是**真的**，降级不算撒谎，快照照常产出变化。
                # - **真 DB 故障**（连不通 / 权限 / 其它）：我们根本不知道历史
                #   长什么样。此时若也降级成 ``{}``，``diff_states`` 会把**全部**
                #   结构当成新建重写一遍 —— 把「读失败」谎报成「首次」。
                #   所以这里重新抛出，交给下面的 except：本轮不产出事件。
                #
                # 注意 PG 语义：这条读失败已经把事务打成 aborted，两条分支
                # 都必须 rollback（except 分支统一处理）。
                if not is_missing_table_error(exc):
                    raise
                _LOG.warning("结构事件表不存在（42P01，按「无历史」处理，迁移可能没跑）: %s", exc)
                previous = {}
            events = diff_states(previous, states)
            if not events:
                return ()  # 热路径常态：不写库
            if fingerprint is not None and symbol:
                events = _with_cause(
                    db, events, market=market, symbol=symbol, fingerprint=fingerprint
                )
            written = append_events(db, events)
            if written == 0:
                # 写侧 best-effort 降级：异常被 store 吞掉、不冒泡，所以 except
                # 分支进不来，只能在这里判。PG 里那条失败语句已把事务打成
                # aborted，不救连接，调用方后续每条查询都失败。
                _LOG.warning("结构事件写入降级为 0 条（表缺失或 DB 故障），已回滚传入连接")
                _rollback_quietly(db)
            else:
                db.commit()  # ← store 层不 commit，边界在这里
            return events
    except Exception as exc:  # noqa: BLE001 — 旁路失败不影响快照
        _LOG.warning("结构事件记录失败（不影响快照）: %s", exc)
        # 自己开的连接由 _connection 的 finally: close() 兜住；只有调用方传进来
        # 复用的那条需要在这里救。
        if conn is not None:
            _rollback_quietly(conn)
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
