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

_INSERT_SQL: Final[str] = (
    "INSERT INTO public.intraday_seal_snapshot (at, code, sealed, bid1_lots, limit_price) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (at, code) DO NOTHING"
)


def insert_seal_snapshot(conn: Any, rows: Sequence[dict[str, Any]], *, at: Any) -> int:
    """写一轮快照。返回真正插入的行数。

    ``rows`` 每项至少要有 ``code``；``sealed`` 缺省按 ``False``；
    ``bid1_lots`` / ``limit_price`` 缺省为 ``None``（判不出来的情况）。

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
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(at) FROM {_SNAPSHOT_TABLE}")  # noqa: S608 - 常量表名
        row = cur.fetchone()
        latest = row[0] if row else None
        if latest is None:
            return {"at": None, "rows": [], "counts": {"total": 0, "sealed": 0, "open_board": 0}}
        cur.execute(
            f"SELECT at, code, sealed, bid1_lots, limit_price FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            "WHERE at = %s ORDER BY sealed DESC, bid1_lots DESC NULLS LAST, code",
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
            f"SELECT at, sealed, bid1_lots, limit_price FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            "WHERE code = %s AND at >= %s AND at < %s ORDER BY at",
            (code, since, until),
        )
        # 带上 code：下游 track_summary 要它，缺了会在真数据上报 KeyError（测试里
        # 的假游标喂的是带 code 的 dict，所以只在实跑时才暴露）。
        return [
            {
                "at": r[0],
                "code": code,
                "sealed": r[1],
                "bid1_lots": r[2],
                "limit_price": r[3],
            }
            for r in cur.fetchall()
        ]


__all__ = [
    "insert_seal_snapshot",
    "load_latest_round",
    "load_seal_track",
    "load_session_rows",
]


def load_session_rows(conn: Any, *, start: Any, end: Any) -> list[dict[str, Any]]:
    """取 ``[start, end)`` 区间内的全部快照行（按时间升序），供当天回顾用。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT at, code, sealed, bid1_lots, limit_price FROM {_SNAPSHOT_TABLE} "  # noqa: S608
            "WHERE at >= %s AND at < %s ORDER BY at, code",
            (start, end),
        )
        return [
            {"at": r[0], "code": r[1], "sealed": r[2], "bid1_lots": r[3], "limit_price": r[4]}
            for r in cur.fetchall()
        ]
