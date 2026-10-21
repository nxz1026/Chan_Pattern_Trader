"""结构事件流 ``public.cpt_structure_event`` 的读写（R26）。

**store 层不 commit** —— 事务边界归调用方（与 ``signal_event_store`` /
``dashboard_run_store`` / ``llm_call_store`` 同一约定）。

**当前状态从事件流派生**，不建状态表 —— 见迁移 SQL 的说明。派生走
:func:`cpt.domain.structure_events.state_from_event`，把 domain 类型接回来了：
R26 之前 ``StructureState`` 全仓零生产者、零消费者（见 progress-log R24 勘察）。

## 降级纪律：写侧 best-effort，读侧一律抛

R27-2 定的原纪律是「写侧吞、读侧抛」，但它给 :func:`latest_events` /
:func:`current_states` 开了一个口子，让它们也吞。**2026-10-06 关闭这个口子** ——
那两个函数吞掉的不是「写不写」，而是「查不到」和「没有」的区别，而 recorder
拿到的 ``{}`` 会让它把「读失败」当成「首次」，把全部结构重写成新事件。

更糟的是 PG 语义：任何一条语句失败都会让**整个事务进入 aborted 态**。recorder
传的常是**共享连接**（``a_share_snapshot`` 侧），吞掉异常意味着这条连接带着
aborted 态交还给调用方，后续每一条查询都撞 ``current transaction is aborted``，
整轮逐只降级 —— 一个读函数的降级，炸穿了整张快照的构造。

现在的规矩：

- :func:`append_events` —— **best-effort**，返回 0。调用方是 recorder（快照构造
  热路径），写事件失败不该让整张快照构造失败。但**必须 ERROR 级留痕**并区分
  「表不存在」与「DB 故障」，别让降级变成静默。
- :func:`latest_events` / :func:`current_states` —— **抛**。理由同
  :func:`timeline`：读侧吞异常就是撒谎，且在共享连接上会污染事务。
- :func:`timeline` / :func:`recent_events` —— **抛**。调用方是 HTTP 读接口，
  降级成空元组会让接口把「DB 挂了」谎报成「没有事件」。

判据一句话：**吞掉异常会改变答案**时就得抛。写侧吞掉只影响「有没有落库」，
读侧吞掉会让「查不到」变成「没有」—— 调用方无从区分。

**传进来的连接归谁？** 本层不 commit，事务边界在调用方；因此调用方**必须**
在用完后保证连接可继续使用（见 ``structure_event_recorder`` 的无条件
``rollback``）。store 层不做这件事，它不知道连接是不是共享的。

"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from cpt.domain.models import StructureEvent, StructureState
from cpt.domain.structure_events import state_from_event

_LOG = logging.getLogger(__name__)

__all__ = [
    "append_events",
    "current_states",
    "ensure_id_sequence",
    "ensure_sequence_at",
    "is_missing_table_error",
    "latest_events",
    "recent_events",
    "timeline",
]

_COLUMNS = (
    "id",
    "structure_id",
    "event_type",
    "status",
    "revision",
    "payload",
    "occurred_at",
    "created_at",
)


def _ms(value: Any) -> Any:
    """``datetime`` → Unix 毫秒。列是 timestamptz，出参必须能进 json。"""
    return int(value.timestamp() * 1000) if hasattr(value, "timestamp") else value


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        name: _ms(value) if name in ("occurred_at", "created_at") else value
        for name, value in zip(_COLUMNS, row, strict=False)
    }


#: PostgreSQL「表不存在」的 SQLSTATE。见
#: https://www.postgresql.org/docs/current/errcodes-appendix.html
_SQLSTATE_UNDEFINED_TABLE = "42P01"


def is_missing_table_error(exc: BaseException) -> bool:
    """``exc`` 是否是「表不存在」（PG SQLSTATE ``42P01``）。

    **给调用方用的**，因为只有调用方知道该怎么区分处置：表不存在通常意味着
    迁移没跑（部署问题，事件流本来就是空的，降级不算撒谎），而连接不通、
    权限不足这类真故障则不能当成「没有历史」。

    刻意**不** import psycopg 来做 ``isinstance``：本模块顶层只依赖 stdlib +
    domain，``psycopg`` 是 ``[db]`` extra（CI 不一定装）。改用驱动都提供的
    ``sqlstate`` 属性判断，缺属性时返回 ``False``（按「真故障」处理 ——
    这个方向是安全的：宁可当故障，也不要把真故障误报成「表没建」）。
    """
    return getattr(exc, "sqlstate", None) == _SQLSTATE_UNDEFINED_TABLE


#: ``id`` 是 ``bigserial``（迁移 ``2026-10-05_r26_structure_event.sql:31``），
#: 插入不写 id，取号来自这个序列。
_ID_SEQUENCE = "public.cpt_structure_event_id_seq"

#: 事件表全名。序列名/表名都是**编译期常量**，不接受外部输入 ——
#: ``setval`` 不是参数化能覆盖的函数（它收 regclass 字面量），所以这里用
#: f-string 拼 SQL 是安全的；**引入任何变量都必须改成 ``%s::regclass``**。
_ID_TABLE = "public.cpt_structure_event"


def ensure_sequence_at(conn: Any, *, table: str, sequence: str) -> int:
    """把 ``id`` 序列推到 ``max(id)``，返回推到的新值；无事可做返回 0。

    修的是**序列落后于表**这一类故障。2026-10-20/21 在生产上真实撞了两次：

    ``psycopg.errors.UniqueViolation: duplicate key value violates unique
    constraint "cpt_structure_event_pkey"`` / ``DETAIL: Key (id)=(900008)
    already exists``

    根因不是"序列坏了"，而是**有代码带 id 显式写入**（迁移脚本的
    ``INSERT ... SELECT id``、测试的 ``setval`` 回滚）。一旦显式 id 超过
    序列当前值，后续每次 ``nextval`` 都会落在已占用区间上，且**不会自愈** ——
    每次跑批都炸，直到有人手工 ``setval``。

    正确做法本是所有写入都不带 id、序列自行前进；但那管不住外部脚本。
    这里在写批之前做一次幂等的"推到表尾"，代价是每轮两条快照查询。

    实现要点：

    - ``setval(..., max(id), true)``：第三参 ``true`` 表示**下一个** ``nextval``
      返回 ``max+1``。写 ``max+1`` 会让间隔凭空多一个号，与回滚脚本
      ``2026-10-19_r67_symbol_in_id_rollback.sql:95`` 的写法保持一致。
    - 表为空时 ``COALESCE(max(id), 1)`` —— ``setval`` 不接 NULL。
    - **只在真落后时才写**：比较用 ``last_value``，落后才 ``setval``，
      免得每批都拿一把序列上的锁。
    - 用 ``pg_sequences.last_value`` 而不是 ``SELECT last_value FROM <seq>``：
      后者要序列的 SELECT 权限，而生产账户通常只有 USAGE。
      ``pg_sequences`` 对无权限的序列返回 NULL，此时按「要修」处理 ——
      宁可多做一次无效 setval，也不要漏修。
    - **不 commit**：事务边界归调用方，与同模块其它函数一致。

    :param conn: psycopg 连接（**不 commit**）。
    :param table: 表全名，如 ``public.cpt_structure_event``（**调用方常量**）。
    :param sequence: 序列全名，如 ``public.cpt_structure_event_id_seq``。
    :returns: ``setval`` 后的新值；序列已对齐、表为空、或查询不到序列时返回 0。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_value FROM pg_sequences WHERE schemaname = %s AND sequencename = %s",
            ("public", sequence.split(".")[-1]),
        )
        row = cur.fetchone()
        last_value = row[0] if row else None

        # 序列不存在（迁移没跑）就别 setval —— 那会报错，而这不是本函数要修的问题。
        # fetchone() 返回 None 是「没有这个序列」，与「有序列但查不到 last_value
        # （NULL，无权限）」必须分开：前者早退，后者照修。
        if row is None:
            return 0

        cur.execute(f"SELECT max(id) FROM {table}")  # noqa: S608 — 调用方常量表名
        max_row = cur.fetchone()
        max_id = max_row[0] if max_row else None

        if max_id is None:
            return 0

        # last_value 为 NULL（USAGE 有、SELECT 无）时按"可能落后"处理。
        if last_value is not None and last_value >= max_id:
            return 0

        cur.execute(
            f"SELECT setval('{sequence}', %s, true)",  # noqa: S608 — 调用方常量序列名
            (max_id,),
        )
        new_row = cur.fetchone()
        new_value = int(new_row[0]) if new_row and new_row[0] is not None else int(max_id)
        _LOG.warning(
            "结构事件 id 序列落后：last_value=%s < max(id)=%s，已 setval 到 %s"
            "（通常是迁移/测试带 id 显式写入所致）",
            last_value,
            max_id,
            new_value,
        )
        return new_value


def ensure_id_sequence(conn: Any) -> int:
    """把 ``public.cpt_structure_event`` 的 ``id`` 序列推到 ``max(id)``。

    :func:`ensure_sequence_at` 的本表绑定版。**序列自愈只有这一条路径** ——
    其它 bigserial 表（如 ``cpt_signal_event``）应当直接调
    :func:`ensure_sequence_at` 并传自己的常量，**不要**另写一份 setval。
    """
    return ensure_sequence_at(conn, table=_ID_TABLE, sequence=_ID_SEQUENCE)


def append_events(conn: Any, events: Sequence[StructureEvent]) -> int:
    """追加一批事件，返回**真正写入**的条数。

    **空批次是 no-op，不碰存储** —— 这是热路径常态：每轮快照都会 diff 一次，
    而绝大多数轮次没有结构变化。发了 SQL 再回来什么都不写是纯浪费。

    :param conn: psycopg 连接（**不 commit**）。
    :returns: 写入条数。

    # gate: allow-silent: 写失败不阻断 —— 调用方
    # ``structure_event_recorder.record_structure_events`` 的 docstring 明确写了
    # 「``append_events`` 写失败被吞，但本函数照样把算出的事件返回」：
    # 结构事件是**观测产物**，算出来了就返回，存不进去不该让快照 500。
    # 调用方已按此契约在 except 分支补上 rollback（R45）。
    """
    if not events:
        return 0

    rows = [
        (
            ev.structure_id,
            ev.event_type,
            str(ev.payload.get("status", "forming")),
            ev.revision,
            json.dumps(ev.payload, ensure_ascii=False, default=str),
            ev.occurred_at,
        )
        for ev in events
    ]
    try:
        # 先自愈序列再写。序列落后于 max(id) 时，本批的每一次 nextval 都会
        # 落在已占用区间上 ⇒ UniqueViolation，且**不会自愈**，每轮都炸。
        # 放在 INSERT 之前的理由：撞了之后再修等于这批已经废了，还得重跑。
        ensure_id_sequence(conn)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.cpt_structure_event "
                "(structure_id, event_type, status, revision, payload, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, to_timestamp(%s / 1000.0))",
                rows,
            )
            return len(rows)
    except Exception as exc:
        # best-effort：写事件失败不该让整张快照构造失败（调用方 recorder 兜底）。
        # 但**必须留痕** —— 原来这里是 warning 且不带 exc_info，栈被丢掉，
        # 「表没建」和「DB 挂了」两种完全不同的现场在日志里长得一模一样。
        # 表不存在（42P01）几乎总是部署漏跑迁移，DB 故障则是运维问题，
        # 处置路径不同，日志必须能分开。
        if is_missing_table_error(exc):
            _LOG.error(
                "写入结构事件失败 %s 条：表 public.cpt_structure_event 不存在"
                "（42P01，通常是迁移没跑）",
                len(rows),
                exc_info=True,
            )
        else:
            _LOG.error("写入结构事件失败 %s 条", len(rows), exc_info=True)
        return 0


def latest_events(conn: Any, structure_ids: Sequence[str]) -> dict[str, StructureEvent]:
    """取每个 ``structure_id`` 的**最新一条事件**。

    用 ``DISTINCT ON``（PG 的「每组取一行」），一次查询拿完，别在 Python 里
    拉全表再挑。

    :returns: ``structure_id -> StructureEvent``；不存在的 id **不在结果里**
        （区别于「存在但 payload 为空」——调用方据此判首次）。
    :raises Exception: 读库失败**原样抛出**。理由同 :func:`timeline`，而且
        更硬：recorder 传进来的常是**共享连接**，本函数吞掉异常等于把一条
        aborted 事务交还给调用方，PG 语义下它后续每条查询都会失败
        （2026-10-06 修）。表不存在也照抛 —— 「读不到」和「没有」必须分开。
    """

    ids = [str(i).strip() for i in structure_ids if str(i).strip()]
    if not ids:
        return {}

    sql = f"""
        SELECT DISTINCT ON (structure_id) {", ".join(_COLUMNS)}
          FROM public.cpt_structure_event
         WHERE structure_id = ANY(%s)
         ORDER BY structure_id, revision DESC
    """
    # 不吞：见 :raises:。空 ids 的早退是**契约**（不查库），与读失败不是一回事。
    with conn.cursor() as cur:
        cur.execute(sql, (list(ids),))
        found = cur.fetchall() or ()

    out: dict[str, StructureEvent] = {}
    for row in found:
        record = _row_to_dict(row)
        payload = record.get("payload")
        out[record["structure_id"]] = StructureEvent(
            event_type=record["event_type"],
            structure_id=record["structure_id"],
            revision=int(record["revision"]),
            payload=payload if isinstance(payload, dict) else {},
            occurred_at=int(record["occurred_at"]),
        )
    return out


def current_states(conn: Any, structure_ids: Sequence[str]) -> dict[str, StructureState]:
    """从事件流派生「当前状态」。**``StructureState`` 的生产出口在这里。**"""
    return {sid: state_from_event(ev) for sid, ev in latest_events(conn, structure_ids).items()}


def timeline(conn: Any, structure_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
    """某结构的事件时间线，**revision 升序**（最早在前）。

    :raises Exception: 读库失败**原样抛出**，不降级成空元组。

    ## 为什么这条读接口不 best-effort

    本模块的写侧（:func:`append_events`）刻意吞异常 —— 写事件失败不该让快照构造
    失败，那由调用方的 try/except 兜。但**读接口吞掉异常就是在撒谎**：DB 挂掉时
    返回空元组，HTTP 层只能报 ``available=true, count=0``，把「查不到」说成
    「没有」。前端无法区分这两者，而这两者的处置完全不同（前者该重试/告警，
    后者是正常结果）。

    降级义务因此上移到 HTTP 层：它捕获异常并回 ``available=false`` + reason，
    这才是前端能据以决策的形状。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_structure_event "
            "WHERE structure_id = %s ORDER BY revision ASC LIMIT %s",
            (structure_id, max(1, min(int(limit), 500))),
        )
        rows = cur.fetchall() or ()
    return tuple(_row_to_dict(row) for row in rows)


#: :func:`recent_events` 的 limit 上限。与 :func:`timeline` 同一个量级：
#: 这张表只在**结构真的变了**时才追加一行，正常轮询绝大多数时候不写，
#: 所以几百行足够覆盖「最近发生了什么」，不必给无限。
_RECENT_LIMIT_CAP = 500


def recent_events(
    conn: Any,
    *,
    limit: int = 50,
    event_type: str | None = None,
    kind: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """最近的��构事件，**occurred_at 倒序**（最新在前），跨所有结构。

    :func:`timeline` 回答「**这一个**结构经历了什么」，本函数回答「**最近**
    发生了什么」—— 前者是 R27 UI 的详情面板，后者是它的列表页。

    :param event_type: 按事件类型过滤（``created`` / ``updated`` / …）。命中
        ``idx_cpt_structure_event_kind_level`` 的前导列。
    :param kind: 按结构类型过滤（``bi`` / ``fractal`` / ``zhongshu`` / …），
        取自 ``payload->>'kind'``。**无索引** —— 见下方取舍。
    :returns: 事件字典元组。
    :raises Exception: 读库失败原样抛出 —— 理由同 :func:`timeline`（读接口
        吞异常会把「查不到」谎报成「没有」），降级由 HTTP 层负责。

    ## 为什么 ``kind`` 过滤不加索引

    表只在结构真变了才追加一行，增长极慢（部署首日 725 行，绝大多数轮次零写入），
    而加一列就把「表不存 market / 不存 kind」这个 R26 就定下的口径破坏掉了 ——
    kind 本来就能从 payload 现抽。加索引前先问「真的慢吗」，答案是还没有。

    真慢了再说：届时加的是 ``CREATE INDEX ... ON ... ((payload->>'kind'))``，
    表达式索引，不需要新增列，迁移也只多一行。
    """
    sql = f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_structure_event"
    where: list[str] = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = %s")
        params.append(str(event_type))
    if kind:
        where.append("payload->>'kind' = %s")
        params.append(str(kind))
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 倒序键用 (occurred_at, id) 而不是裸 occurred_at：同毫秒批量写入时
    # occurred_at 会并列，只按它排序会让同一批次的相对顺序随机漂移，
    # 翻页时可能漏行或重复行。id 是 bigserial 单调，追加它就稳定了。
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT %s"
    params.append(max(1, min(int(limit), _RECENT_LIMIT_CAP)))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or ()
    return tuple(_row_to_dict(row) for row in rows)
