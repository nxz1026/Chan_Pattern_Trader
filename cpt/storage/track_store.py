"""我的追踪 — 持久化（R57 新增）。

按多人设计：每行带 ``user_id``，由 API 层从 ``X-CPT-User`` 头提取；
当前部署是单用户，缺省 ``"default"``，未来加鉴权不用改表。

## 失败语义

读失败 / 写失败**抛** :class:`TrackStoreError`，**不**用空列表冒充「没有追踪」。
理由与 ``RecommendationPersistError`` 一致：「业务事实可以为空，故障必须抛」。

## 保留与清理

- 快照（``cpt_track_snapshot``）保留 **30 天**（按段 1 决定）；
- 软删行（``cpt_track.removed_at``）保留 **90 天**（回收站，段 1 决定）。
- 清理由 ``prune_snapshots`` / ``prune_removed`` 提供，**调用方负责调度**
  （CPT 这边在每天的 :func:`cpt.web.track_api.handle_track_maintenance` 里调，
  实际触发点由部署侧的 cron 决定 —— 本模块不写 cron）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

_LOG = logging.getLogger(__name__)

__all__ = [
    "SNAPSHOT_RETENTION_DAYS",
    "RECYCLE_RETENTION_DAYS",
    "TrackStoreError",
    "add",
    "ensure_table",
    "list_active",
    "list_removed",
    "list_snapshots",
    "prune_removed",
    "prune_snapshots",
    "record_snapshot",
    "remove",
    "restore",
]

#: 快照保留天数（按段 1 决定；改值要同步测试）
SNAPSHOT_RETENTION_DAYS: Final[int] = 30
#: 软删行保留天数（回收站；改值要同步测试）
RECYCLE_RETENTION_DAYS: Final[int] = 90


class TrackStoreError(RuntimeError):
    """追踪库读写失败。**读失败绝不用空列表冒充「没有追踪」。**"""


def ensure_table(conn: Any) -> None:
    """幂等建表 + 索引。冷启动时调一次即可。

    复合主键 ``(user_id, code)`` 让「同票不能重复在追踪」在 DB 层兜底；
    部分索引只覆盖「未软删」「已软删」两条热路径，避免全表扫。
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.cpt_track (
                user_id           TEXT        NOT NULL,
                code              TEXT        NOT NULL,
                added_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                note              TEXT,
                last_advice_at    TIMESTAMPTZ,
                last_current_json JSONB,
                removed_at        TIMESTAMPTZ,
                PRIMARY KEY (user_id, code)
            )
        """)
        # 「活跃」列表按 added_at DESC，热路径用部分索引
        cur.execute("""
            CREATE INDEX IF NOT EXISTS cpt_track_active_idx
                ON public.cpt_track (user_id, added_at DESC)
                WHERE removed_at IS NULL
        """)
        # 「回收站」按 removed_at DESC
        cur.execute("""
            CREATE INDEX IF NOT EXISTS cpt_track_removed_idx
                ON public.cpt_track (user_id, removed_at DESC)
                WHERE removed_at IS NOT NULL
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.cpt_track_snapshot (
                id       BIGSERIAL    PRIMARY KEY,
                user_id  TEXT         NOT NULL,
                code     TEXT         NOT NULL,
                as_of    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                payload  JSONB        NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS cpt_track_snapshot_lookup_idx
                ON public.cpt_track_snapshot (user_id, code, as_of DESC)
        """)


# ── CRUD ──────────────────────────────────────────────────────────────


def add(conn: Any, user_id: str, code: str, note: str | None = None) -> dict[str, Any]:
    """加入追踪。**幂等**：

    - 已经在追踪 → 返回原行，``added_at`` 不动；
    - 在回收站 → **复活**（清 ``removed_at``、重置 ``added_at = now``），返回新行；
    - 全新 → 新建一行。

    返回的 dict 是**写入后的最新状态**。``last_advice_at`` 留空，由
    :func:`record_snapshot` 维护。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.cpt_track (user_id, code, note)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, code) DO UPDATE
                  SET removed_at = NULL,
                      added_at = CASE
                          WHEN public.cpt_track.removed_at IS NOT NULL THEN NOW()
                          ELSE public.cpt_track.added_at
                      END,
                      note = COALESCE(EXCLUDED.note, public.cpt_track.note)
                RETURNING user_id, code, added_at, note, last_advice_at, last_current_json,
                          removed_at
                """,
                (user_id, code, note),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(f"add(user_id={user_id!r}, code={code!r}) failed: {exc}") from exc
    if not row:
        raise TrackStoreError("add returned no row")
    return _row_to_dict(row)


def remove(conn: Any, user_id: str, code: str) -> bool:
    """软删。返回 ``True`` 表示**确实从活跃变为已删**（而非本来就删过/不存在）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.cpt_track
                SET removed_at = NOW()
                WHERE user_id = %s AND code = %s AND removed_at IS NULL
                """,
                (user_id, code),
            )
            affected = cur.rowcount
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(f"remove(user_id={user_id!r}, code={code!r}) failed: {exc}") from exc
    return bool(affected > 0)


def restore(conn: Any, user_id: str, code: str) -> bool:
    """从回收站复活。返回是否真改了状态。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.cpt_track
                SET removed_at = NULL,
                    added_at = NOW()
                WHERE user_id = %s AND code = %s AND removed_at IS NOT NULL
                """,
                (user_id, code),
            )
            affected = cur.rowcount
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(f"restore(user_id={user_id!r}, code={code!r}) failed: {exc}") from exc
    return bool(affected > 0)


def list_active(conn: Any, user_id: str) -> list[dict[str, Any]]:
    """活跃追踪：按 ``added_at`` 倒序。失败抛，不用空列表。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, code, added_at, note, last_advice_at, last_current_json,
                       removed_at
                FROM public.cpt_track
                WHERE user_id = %s AND removed_at IS NULL
                ORDER BY added_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise TrackStoreError(f"list_active(user_id={user_id!r}) failed: {exc}") from exc
    return [_row_to_dict(r) for r in rows]


def list_removed(conn: Any, user_id: str) -> list[dict[str, Any]]:
    """回收站：只列 **90 天内**的软删行。"""
    cutoff = datetime.now(UTC) - timedelta(days=RECYCLE_RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, code, added_at, note, last_advice_at, last_current_json,
                       removed_at
                FROM public.cpt_track
                WHERE user_id = %s
                  AND removed_at IS NOT NULL
                  AND removed_at >= %s
                ORDER BY removed_at DESC
                """,
                (user_id, cutoff),
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise TrackStoreError(f"list_removed(user_id={user_id!r}) failed: {exc}") from exc
    return [_row_to_dict(r) for r in rows]


# ── 快照 ──────────────────────────────────────────────────────────────


def record_snapshot(
    conn: Any,
    user_id: str,
    code: str,
    payload: dict[str, Any],
    *,
    current_summary: dict[str, Any] | None = None,
) -> int:
    """写一条快照 + 同步更新 ``cpt_track.last_advice_at`` 与 ``last_current_json``。

    :param payload: 完整建议 JSON（``/track/{code}/advice`` 端点的整份输出）。
    :param current_summary: 紧凑版（只放 ``current`` 子集），存到 ``last_current_json`` 以便
        列表接口直接读、不必 JOIN。**省略时**用 ``payload.get("current")``。
    :returns: 新快照的 ``id``。
    """
    summary = current_summary if current_summary is not None else payload.get("current") or {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.cpt_track_snapshot (user_id, code, payload)
                VALUES (%s, %s, %s::jsonb)
                RETURNING id
                """,
                (user_id, code, json.dumps(payload, ensure_ascii=False)),
            )
            row = cur.fetchone()
            snap_id = int(row[0]) if row else 0
            cur.execute(
                """
                UPDATE public.cpt_track
                SET last_advice_at = NOW(),
                    last_current_json = %s::jsonb
                WHERE user_id = %s AND code = %s AND removed_at IS NULL
                """,
                (json.dumps(summary, ensure_ascii=False), user_id, code),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(
            f"record_snapshot(user_id={user_id!r}, code={code!r}) failed: {exc}"
        ) from exc
    return snap_id


def list_snapshots(
    conn: Any, user_id: str, code: str, *, since: datetime | None = None
) -> list[dict[str, Any]]:
    """该用户对该票的快照历史。``since`` 默认 ``now - SNAPSHOT_RETENTION_DAYS``。"""
    if since is None:
        since = datetime.now(UTC) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code, as_of, payload
                FROM public.cpt_track_snapshot
                WHERE user_id = %s AND code = %s AND as_of >= %s
                ORDER BY as_of DESC
                """,
                (user_id, code, since),
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise TrackStoreError(
            f"list_snapshots(user_id={user_id!r}, code={code!r}) failed: {exc}"
        ) from exc
    return [{"id": int(r[0]), "code": r[1], "as_of": _iso(r[2]), "payload": r[3]} for r in rows]


# ── 清理 ──────────────────────────────────────────────────────────────


def prune_snapshots(conn: Any, retention_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """删 ``as_of < now - retention_days`` 的快照。返回删除行数。"""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.cpt_track_snapshot WHERE as_of < %s",
                (cutoff,),
            )
            affected = cur.rowcount
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(f"prune_snapshots failed: {exc}") from exc
    return int(affected)


def prune_removed(conn: Any, retention_days: int = RECYCLE_RETENTION_DAYS) -> int:
    """删 ``removed_at < now - retention_days`` 的回收站行。返回删除行数。"""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM public.cpt_track
                WHERE removed_at IS NOT NULL AND removed_at < %s
                """,
                (cutoff,),
            )
            affected = cur.rowcount
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise TrackStoreError(f"prune_removed failed: {exc}") from exc
    return int(affected)


# ── 内部 ──────────────────────────────────────────────────────────────


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """``cpt_track`` 7 列的 SELECT 行转 dict。"""
    return {
        "user_id": row[0],
        "code": row[1],
        "added_at": _iso(row[2]),
        "note": row[3],
        "last_advice_at": _iso(row[4]),
        "last_current": row[5] if row[5] is not None else None,
        "removed_at": _iso(row[6]),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
