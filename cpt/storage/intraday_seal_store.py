"""盘中封板快照的落库与读取（R90）。

表是 ``public.intraday_seal_snapshot``，建表语句见
``scripts/migrations/2026-10-22_r90_intraday_seal_snapshot.sql``。

## 谁写、谁读

- **写**：``scripts/intraday_collectd.py`` 每 2 分钟一轮，写 CPT **判定完的结论**
  （``seal_strength()`` 的输出）。写失败只记 warning，不改变采集轮次的结果 ——
  行情采到了就是采到了，落库失败是另一件事，由巡检单独抓。
- **读**：emotion-core 的触发判据。**判据不许在两边各写一份**（ADR 0003 §E2），
  所以这里只存结论、不存原始五档。

## 三条口径（下游必须照做）

1. **没行 = 判不了**，不是「没封板」。停牌、缺档位、榜单里没出现，都不写行。
2. ``sealed=False`` 的行是「采到了、这一轮没封在板上」（比如冲到板附近又回落）。
3. 一轮可能有 30~40 行；下游按 ``at`` 分组取「最新一轮」，
   **不要按 ``code`` 单点查最新** —— 轮次是整体，混轮次会造出并不存在的状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

_SNAPSHOT_TABLE: Final[str] = "public.intraday_seal_snapshot"

#: 探针行的代码前缀。真实采集只写 6 位数字代码；产品测试 / 手工验证用的哨兵行
#: 一律以 ``\x01`` 开头。SQL 的 ``LIKE`` 里 ``\x01`` 没有通配含义，不必转义。
PROBE_CODE_PREFIX: Final[str] = "\x01"

#: 「这一行是真实采集轮」的 SQL 片段。**读取一律带上它**，否则一条探针行就能让
#: 盘中面板显示一个有模有样的假封板（R91 生产实测：117 行里 18 行是探针）。
#:
#: ``%`` 必须写成 ``%%``：这些 SQL 全走 ``psycopg`` 的参数化路径，裸写 ``%`` 会被
#: 当成占位符报 ``only '%s', '%b', '%t' are allowed as placeholders, got '%'``。
_REAL_ROWS: Final[str] = f"code NOT LIKE '{PROBE_CODE_PREFIX}%%'"

#: CPT **读侧**用到的 5 列，三个 ``load_*`` 统一用这一份。
#:
#: ⚠️ 写侧是 9 列（R93 补了 ``price`` / ``prev_close`` / ``distance_pct`` / ``is_st``），
#: 但那 4 列是 **emotion-core N5 的输入**，CPT 自己不读 —— 契约
#: `docs/intraday-seal-contract.md` §2.1 给两边列了两份 SELECT。早先这里写的是
#: 9 列、三个读函数却只映射前 5 个，等于每行白取 4 列（2026-10-29 审计）。
#: 要那 4 列的下游**直接查表**（契约 §2.1 第 90 行那份 9 列 SELECT），不要改这里。
_SELECT_COLS: Final[str] = "at, code, sealed, bid1_lots, limit_price"

_INSERT_SQL: Final[str] = (
    "INSERT INTO public.intraday_seal_snapshot ("
    "at, code, sealed, bid1_lots, limit_price, price, prev_close, distance_pct, is_st) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (at, code) DO NOTHING"
)


def insert_seal_snapshot(conn: Any, rows: Sequence[dict[str, Any]], *, at: Any) -> int:
    """写一轮快照。返回真正插入的行数。

    ``rows`` 每项至少要有 ``code``；``sealed`` 缺省按 ``False``；
    其余数值列（``bid1_lots`` / ``limit_price`` / ``price`` / ``prev_close`` /
    ``distance_pct`` / ``is_st``）缺省为 ``None``（判不出来的情况，**不许填 0**）。

    R93 起写 9 列：``at, code, sealed, bid1_lots, limit_price, price, prev_close,
    distance_pct, is_st``。后 4 列是 emotion-core N5 要的，其中
    ``distance_pct`` 和 ``is_st`` 是**判定结果**不是原始字段 ——
    判据只许有一处（ADR 0003 §E2）。

    ``ON CONFLICT DO NOTHING``：同一时刻重复写（例如手动重跑同一轮）不报错也不重复。
    **不 commit** —— 事务边界归调用方。
    """
    if not rows:
        return 0
    params = [
        (
            at,
            row["code"],
            bool(row.get("sealed", False)),
            row.get("bid1_lots"),
            row.get("limit_price"),
            row.get("price"),
            row.get("prev_close"),
            row.get("distance_pct"),
            row.get("is_st"),
        )
        for row in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(_INSERT_SQL, params)
        return len(params)


def load_latest_round(conn: Any) -> dict[str, Any]:
    """取**最新一轮**的全部行，供下游判据用。

    返回 ``{"at": datetime | None, "rows": [...], "counts": {...}}``；
    表空时 ``at=None``、``rows=[]``。``counts`` 与 :func:`seal_board` 同口径，
    但**只用这一轮的行**算 —— 跨轮混算会把「上一轮封着、这一轮开板」读成两只票。

    轮次由「最新一行 ``at``」界定；采集是每 2 分钟整轮写入，一轮的 ``at`` 相同。

    ## 为什么排除 ``PROBE_CODE_PREFIX`` 开头的行

    真实写入方 ``scripts/intraday_collectd.py`` 只认 6 位数字代码。库里出现别的东西
    = 有人在往里塞探针（手工验证、产品测试），**不是真实采集轮**。若不排除，一条
    探针行会让整个盘中面板显示「1 只封板、封单 5 万手」—— 看着像真的，其实是假的。
    这是 R91 在生产库上实测踩到的：117 行里 18 行是探针。
    """
    where = _REAL_ROWS
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(at) FROM {_SNAPSHOT_TABLE} WHERE {where}")  # noqa: S608
        row = cur.fetchone()
        latest = row[0] if row else None
        if latest is None:
            return {"at": None, "rows": [], "counts": {"total": 0, "sealed": 0, "open_board": 0}}
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            f"WHERE at = %s AND {where} "
            "ORDER BY sealed DESC, bid1_lots DESC NULLS LAST, code",
            (latest,),
        )
        rows = [
            {
                "at": r[0],
                "code": r[1],
                "sealed": r[2],
                "bid1_lots": r[3],
                "limit_price": r[4],
            }
            for r in cur.fetchall()
        ]
    sealed = sum(1 for r in rows if r["sealed"])
    return {
        "at": latest,
        "rows": rows,
        "counts": {"total": len(rows), "sealed": sealed, "open_board": len(rows) - sealed},
    }


def load_seal_track(conn: Any, code: str, *, since: Any, until: Any) -> list[dict[str, Any]]:
    """某只票在 ``[since, until)`` 区间里的封板轨迹（按时间升序）。

    只用于回看（「几点开板、开了几次」），**不用于实时判据** —— 实时判据只认最新一轮。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            f"WHERE code = %s AND at >= %s AND at < %s AND {_REAL_ROWS} ORDER BY at",
            (code, since, until),
        )
        # 带上 code：下游 track_summary 要它，缺了会在真数据上报 KeyError（测试里
        # 的假游标喂的是带 code 的 dict，所以只在实跑时才暴露）。
        #
        # ⚠️ 下标必须跟着 ``_SELECT_COLS`` 走：``at, code, sealed, bid1_lots,
        # limit_price, ...`` —— ``code`` 插在第 2 列，所以 sealed 从 r[2] 起。
        # 2026-10-29 审计实测：这里曾写成 r[1]/r[2]/r[3]（``code`` 加入前的下标），
        # 真库返回 ``sealed='600293'``、``bid1_lots=True``；假游标喂 4 元组所以单测全绿。
        return [
            {
                "at": r[0],
                "code": code,
                "sealed": r[2],
                "bid1_lots": r[3],
                "limit_price": r[4],
            }
            for r in cur.fetchall()
        ]


__all__ = [
    "PROBE_CODE_PREFIX",
    "insert_seal_snapshot",
    "load_latest_round",
    "load_seal_track",
    "load_session_rows",
]


def load_session_rows(conn: Any, *, start: Any, end: Any) -> list[dict[str, Any]]:
    """取 ``[start, end)`` 区间内的全部快照行（按时间升序），供当天回顾用。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            f"WHERE at >= %s AND at < %s AND {_REAL_ROWS} ORDER BY at, code",
            (start, end),
        )
        return [
            {"at": r[0], "code": r[1], "sealed": r[2], "bid1_lots": r[3], "limit_price": r[4]}
            for r in cur.fetchall()
        ]
