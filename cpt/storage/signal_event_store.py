"""信号事件持久化：``public.cpt_signal_event`` append-only 事件流。

当前状态 = 同 ``signal_id`` 的最新事件。本模块负责：

1. ``load_previous_signal``：从事件流重建最新 ``Signal``，喂给
   ``assess_first_buy(previous=...)`` 做状态推进。
2. ``record_signal_event``：状态跃迁时 append 一条事件。同 status 不重复写
   （30s 轮询不产生垃圾行）。

写入层不直接依赖 ``cpt.domain.signal`` 的状态机逻辑——它只接收已经算好的
``Signal`` 对象，判断 ``status`` 是否变化后 append。状态怎么算、怎么推进，
是 ``cpt.domain.signal`` 与 ``cpt.application.first_buy_bridge`` 的职责。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from cpt.domain.models import Signal

_LOG = logging.getLogger(__name__)

__all__ = [
    "latest_status",
    "load_previous_signal",
    "load_signal_events",
    "load_trade_decisions",
    "record_signal_event",
    "SignalEventError",
]


class SignalEventError(RuntimeError):
    """信号事件持久化失败。"""


def _ms_to_timestamptz(ms: int | None) -> datetime | None:
    """Unix 毫秒 → UTC ``datetime``；``None`` / ``<= 0`` 视为未知。"""
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _timestamptz_to_ms(dt: datetime | None) -> int | None:
    """UTC ``datetime`` → Unix 毫秒；``None`` → ``None``。"""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _row_to_signal(row: tuple[Any, ...]) -> Signal:
    """从事件行重建 ``Signal``。

    列顺序必须与 ``load_previous_signal`` 的 SELECT 一致：
    ``signal_id, level, signal_type, status, structure_id, center_ids,
    divergence_status, alert_time, candidate_time, confirmed_time,
    invalidated_time, price, source_revision``
    """
    return Signal(
        signal_id=row[0],
        level=row[1],
        signal_type=row[2],
        status=row[3],
        structure_id=row[4],
        center_ids=tuple(row[5]) if row[5] else (),
        divergence_status=row[6],
        alert_time=_timestamptz_to_ms(row[7]),
        candidate_time=_timestamptz_to_ms(row[8]),
        confirmed_time=_timestamptz_to_ms(row[9]),
        invalidated_time=_timestamptz_to_ms(row[10]),
        price=row[11],
        source_revision=row[12],
    )


def latest_status(conn: Any, signal_id: str) -> str | None:
    """只取 ``signal_id`` 最新事件的 ``status``；无事件 → ``None``。

    R24 新增。为什么不直接用 :func:`load_previous_signal`：调用方
    （``a_share_snapshot._attach_signal_change``）只需要 status 这一个字段，
    没必要在 application 层为它构造一个完整 ``Signal`` 再拆开看。

    投影只有 **1 列**，比 ``load_previous_signal`` 的 13 列稳得多 —— 表加列 /
    改列都不会波及这里，测试替身也只需给一个元组。

    排序列是 ``id DESC``（bigserial 追加顺序）。**注意**：本表没有 ``event_time``
    这一列，时间语义对应的是 ``transition_time``；R24 之前 application 层曾内联
    写过 ``ORDER BY event_time``，PG 报 ``column "event_time" does not exist``，
    被 except 吞掉只记 debug —— 后果是「信号状态变化检测」自 R21 起一直是死的。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM public.cpt_signal_event "
                "WHERE signal_id = %s ORDER BY id DESC LIMIT 1",
                (signal_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        _LOG.warning("读取信号最新状态失败 %s: %s", signal_id, exc)
        raise SignalEventError(f"读取信号最新状态失败: {exc}") from exc
    return row[0] if row else None


def load_previous_signal(conn: Any, signal_id: str) -> Signal | None:
    """加载 ``signal_id`` 的最新状态（最新事件），返回 ``Signal`` 或 ``None``。

    返回的 ``Signal`` 可直接作为 ``assess_first_buy(..., previous=...)`` 的入参。
    无事件 → ``None``（首次评估）。

    ## R45 修：读失败必须**抛**，不能返回 ``None``

    原实现在 ``except`` 里 ``return None``，理由是「事件流不是硬依赖」。但那个
    理由只成立一半，漏了 PG 的事务语义：

        实测（PostgreSQL 18.6）：事务里一条语句失败后，**同一连接**的后续语句
        全部报 ``current transaction is aborted, commands ignored until end of
        transaction block`` —— 连接进入 aborted 态，必须 ROLLBACK 才能再用。

    调用方（``a_share_snapshot`` 第 456 / 538 行）为此专门写了
    ``_rollback_quietly``，注释里就写着「冒出去会让整个快照 500，并且把连接留在
    aborted 态连累后面所有查询」。**但那段防御是死代码** —— 函数自己先吞了异常，
    调用方的 ``except`` 永远不会触发，连接就一直烂着，后面每一个查询都失败。

    修法：与 :func:`latest_status` / :func:`load_signal_events` 保持一致，读失败
    抛 :class:`SignalEventError`。调用方的 ``try/except`` 随即生效：rollback +
    ``previous`` 保持 ``None``（降级为首次评估）—— 这才是它本来想要的语义。

    ⚠️ ``tests/test_a_share_rollback.py::test_load_previous_signal_failure_does_not_propagate``
    是 monkeypatch 掉本函数让它抛的，只证明了「调用方会兜底」，**证明不了真函数
    会抛**。那种测法对 mock 有效、对真实路径无效 —— 真实路径当时恰恰是坏的。
    回归见 ``tests/test_storage_failure_semantics.py``。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT signal_id, level, signal_type, status, structure_id,
                          center_ids, divergence_status, alert_time, candidate_time,
                          confirmed_time, invalidated_time, price, source_revision
                   FROM public.cpt_signal_event
                   WHERE signal_id = %s
                   ORDER BY id DESC LIMIT 1""",
                (signal_id,),
            )
            row = cur.fetchone()
        return _row_to_signal(row) if row else None
    except Exception as exc:
        # 「库读不到」与「确实没有历史」是两件事，前者不能冒充后者。
        # 必须抛 —— 吞掉会把连接留在 aborted 态（见上方实测），后续查询全废。
        _LOG.warning("加载信号历史失败 %s: %s", signal_id, exc)
        raise SignalEventError(f"加载信号历史失败: {exc}") from exc


#: ``pg_advisory_xact_lock`` 的命名空间键（审计 M17）。
#:
#: 锁键 = ``(hashtext(_SIGNAL_EVENT_LOCK_NAMESPACE), hashtext(signal_id))`` ——
#: 同一 ``signal_id`` 上的写入被串行化，不同票互不阻塞。命名空间用固定字符串，
#: 避免与仓里其它 advisory lock 使用者撞键。
_SIGNAL_EVENT_LOCK_NAMESPACE = "cpt_signal_event"


def record_signal_event(
    conn: Any,
    signal: Signal,
    prev_status: str | None,
    code: str,
    event_time: int,
) -> bool:
    """记录信号事件。只在 ``status`` 变化时 append，返回 ``True`` 表示已写入。

    :param conn: psycopg 连接（不 commit，由调用方控制事务边界）。
    :param signal: 当前评估后的 ``Signal``。
    :param prev_status: 上一状态（``None`` = 首次评估）。
    :param code: A 股 6 位代码。
    :param event_time: 本次评估的事件时间（Unix 毫秒）。
    :returns: ``True`` 表示写了一条新事件；``False`` 表示 status 未变、跳过。

    **并发安全（审计 M17）**：原来只用 ``signal.status == prev_status`` 做纯
    应用层去重，**先读后写不是原子的**。``ThreadingHTTPServer`` + 「每请求一连」
    下，两个请求可以各自读到同一旧状态、各写一条 ``X→Y``，污染「当前状态 =
    最新事件」这个真相源。r21 迁移**刻意不加** ``UNIQUE(signal_id, status)``
    （同一 status 可合法再现，见迁移注释），所以用**事务级 advisory lock**
    串行化而不是加约束/改表：按 ``signal_id`` 取 ``pg_advisory_xact_lock``，
    **在锁内重读最新事件**，已是本次目标状态就跳过。锁随本事务 commit/rollback
    自动释放，不泄漏、不需要显式解锁。
    """
    if signal.status == prev_status:
        return False

    transition_time = _ms_to_timestamptz(event_time)
    if transition_time is None:
        transition_time = datetime.now(UTC)

    try:
        with conn.cursor() as cur:
            # ① 事务级 advisory lock：锁键 = hashtext(命名空间)+hashtext(signal_id)。
            #    r21 故意不加 UNIQUE，串行化是最贴合既有取舍的手段。
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (_SIGNAL_EVENT_LOCK_NAMESPACE, signal.signal_id),
            )
            # ② 必须在**锁内**重读再决定，否则串行化毫无意义：第二个事务拿到锁
            #    时，对方可能已经提交了同一跃迁。
            cur.execute(
                "SELECT status FROM public.cpt_signal_event "
                " WHERE signal_id = %s ORDER BY id DESC LIMIT 1",
                (signal.signal_id,),
            )
            row = cur.fetchone()
            current_status = row[0] if row else None
            if current_status == signal.status:
                # 本次跃迁已经是最新事件（并发事务写入 / 此前已落库）—— 不重复 append
                return False
            cur.execute(
                """INSERT INTO public.cpt_signal_event
                     (signal_id, code, signal_type, level, structure_id,
                      prev_status, status, transition_time, price, source_revision,
                      center_ids, divergence_status, alert_time, candidate_time,
                      confirmed_time, invalidated_time)
                   VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)""",
                (
                    signal.signal_id,
                    code,
                    signal.signal_type,
                    signal.level,
                    signal.structure_id,
                    prev_status,
                    signal.status,
                    transition_time,
                    signal.price,
                    signal.source_revision,
                    list(signal.center_ids),
                    signal.divergence_status,
                    _ms_to_timestamptz(signal.alert_time),
                    _ms_to_timestamptz(signal.candidate_time),
                    _ms_to_timestamptz(signal.confirmed_time),
                    _ms_to_timestamptz(signal.invalidated_time),
                ),
            )
    except Exception as exc:
        _LOG.warning("记录信号事件失败 %s: %s", signal.signal_id, exc)
        raise SignalEventError(f"记录信号事件失败: {exc}") from exc

    return True


#: 历史投影列顺序，必须与 ``load_signal_events`` 的 SELECT 一致。
_HISTORY_COLUMNS = (
    "id",
    "signal_id",
    "code",
    "signal_type",
    "level",
    "structure_id",
    "prev_status",
    "status",
    "transition_time",
    "price",
    "source_revision",
    "center_ids",
    "divergence_status",
    "alert_time",
    "candidate_time",
    "confirmed_time",
    "invalidated_time",
    "created_at",
)

#: 这些列在库里是 ``timestamptz``，出参统一转 Unix 毫秒。
_HISTORY_TIME_COLUMNS = frozenset(
    {
        "transition_time",
        "alert_time",
        "candidate_time",
        "confirmed_time",
        "invalidated_time",
        "created_at",
    }
)


def _history_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """把一条事件行投影成普通 ``dict``（无 psycopg 类型，可直接 JSON 化）。

    取 5 个必需字段之外的完整列，是因为历史面板要展示跃迁前后状态与价格；
    时间列统一用 ``_timestamptz_to_ms`` 转毫秒，与前端其余时间轴口径一致。
    """
    out: dict[str, Any] = {}
    for name, value in zip(_HISTORY_COLUMNS, row, strict=False):
        if name in _HISTORY_TIME_COLUMNS:
            out[name] = _timestamptz_to_ms(value)
        elif name == "center_ids":
            out[name] = list(value) if value else []
        else:
            out[name] = value
    return out


def load_trade_decisions(
    conn: Any,
    *,
    for_date: str,
    statuses: tuple[str, ...],
    limit: int = 50,
) -> tuple[dict[str, Any], ...]:
    """交易机决策取数：**某个自然日内状态发生跃迁**的信号事件。

    与 :func:`load_signal_events` 的区别：后者是「回看 N 天的滚动窗口」，
    给页面看历史用；这里要的是**某个指定日当天新发生的**决策，否则交易机
    每天都会把同一条老信号重复下单（``ref`` 含日期 ⇒ 每天都是新的一笔）。

    ⚠️ 2026-10-07 新增，为对接 LKL-Trade 的 Trade API。

    取「当天跃迁」而非「截至当天最新状态」是刻意的：后者会让 09-30 的一条
    confirmed 一买被重复投喂到每一个后续交易日。

    :param for_date: ``YYYY-MM-DD``（按 ``transition_time`` 的**服务器时区**日期切分）。
    :param statuses: 允许的状态白名单，如 ``("confirmed",)``。
    :returns: ``dict`` 元组，含 ``code`` / ``signal_type`` / ``status`` / ``level`` /
        ``price`` / ``transition_time`` / ``signal_id``。
    读失败抛 :class:`SignalEventError`——「库读不到」不能冒充「今天没信号」。
    """
    if not statuses:
        return ()
    # ⚠️ 2026-10-07：statuses 必须包成 **list** 才能被 psycopg 适配成 PG 数组。
    # 传 tuple 会被适配成 `('confirmed')` 这样的文本，`status = ANY($3)` 直接报
    # ``malformed array literal: "(confirmed)"``。
    # 这个坑很隐蔽：单元测试把 `_fetch_signals` 整个 mock 掉了，恰好绕开这一行，
    # 只有真机调用才暴露 —— 所以下面 test_trade_decisions_sql_params 补了回归。
    params: list[Any] = [for_date, for_date, list(statuses), limit]
    sql = """SELECT code, signal_type, status, level, price, signal_id,
                     transition_time, confirmed_time
              FROM public.cpt_signal_event
              WHERE transition_time >= %s::date
                AND transition_time <  (%s::date + interval '1 day')
                AND status = ANY(%s)
              ORDER BY transition_time DESC, id DESC
              LIMIT %s"""

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("加载交易决策失败 for_date=%s statuses=%s: %s", for_date, statuses, exc)
        raise SignalEventError(f"加载交易决策失败: {exc}") from exc

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        code, signal_type, status, level, price, signal_id, ts, confirmed = row
        # 同一 (code, signal_type) 当天多条跃迁只保留最新的一条，
        # 否则同一只票会因多次状态变化被投喂多笔（ref 只含 code+action）。
        key = (str(code), str(signal_type))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "code": str(code),
                "signal_type": str(signal_type),
                "status": str(status),
                "level": level,
                "price": price,
                "signal_id": signal_id,
                "transition_time": ts,
                "confirmed_time": confirmed,
            }
        )
    return tuple(out)


def load_signal_events(
    conn: Any, *, days: int = 30, code: str | None = None
) -> tuple[dict[str, Any], ...]:
    """读最近 ``days`` 天的信号事件，**时间倒序**（最新在前）。

    :param conn: psycopg 连接（只读，不开事务）。
    :param days: 回看天数，走 ``make_interval(days => %s)`` 参数化下推给库，
        不在 Python 侧过滤（避免全表拉取后本地裁剪）。
    :param code: 非空时只取该 6 位代码的事件；空串按「不过滤」处理。
    :returns: 普通 ``dict`` 元组，至少含 ``signal_id`` / ``code`` / ``status`` /
        ``divergence_status`` / ``transition_time``（Unix 毫秒）。

    读失败**必须**抛 ``SignalEventError``，不返回 ``None``、不返回伪造的空
    元组——「库读不到」与「确实没有事件」是两件事，前者不能冒充后者。由调用方
    （``cpt/web/app.py``）捕获后降级成
    ``{"available": false, "reason": "signal_history_unavailable"}``，
    面板据此显式提示历史不可用，而不是显示一条骗人的空历史。
    """
    params: list[Any] = [days]
    where = ["transition_time >= now() - make_interval(days => %s)"]
    if code:
        where.append("code = %s")
        params.append(code)
    sql = f"""SELECT {", ".join(_HISTORY_COLUMNS)}
           FROM public.cpt_signal_event
           WHERE {" AND ".join(where)}
           ORDER BY transition_time DESC, id DESC"""

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or ()
        return tuple(_history_row_to_dict(row) for row in rows)
    except Exception as exc:
        _LOG.warning("加载信号事件历史失败 days=%s code=%s: %s", days, code, exc)
        raise SignalEventError(f"加载信号事件历史失败: {exc}") from exc
