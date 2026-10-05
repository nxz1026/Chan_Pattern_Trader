"""推荐留痕的持久化（R45 P2）。

## 为什么需要它

:func:`cpt.application.recommendation.build_recommendation` **每次请求实时算、
零留痕**。而因子表在 R45 一天内切了 **4 次**、口径变过 2 次 ——
「切表前推荐长什么样」这个问题**当时没有答案，现在也补不回来**。

⇒ 至少让**今后的**这类变化看得见：每次推荐落一行，
带当时的 ``factor_epoch``（口径纪元），于是
「同一只票在切表前后推荐变了没」是**查表**而不是推断。

## 纪律：失败**必须抛**，且**永不静默**

- 写失败**抛** ``RecommendationPersistError``，不返回空、不假装成功 ——
  这与 store 层其他函数一致（「业务事实可以返回，故障必须抛」）。
  唯一例外是**调用方**（web 层）决定 best-effort 吞掉，
  因为留痕失败不该让用户的推荐接口 500。
- 读失败**抛**，不返回空列表冒充「没有历史」。

⚠️ 写**没有** ``ON CONFLICT`` 去重：同一只票每次刷新都写一行，
这是**时间序列**不是状态；「最新一条」用读端的排序表达。
"""

from __future__ import annotations

import logging
from typing import Any, Final

_LOG = logging.getLogger(__name__)

__all__ = [
    "KIND_ASK",
    "RecommendationPersistError",
    "append_recommendation",
    "ensure_table",
    "recent_recommendations",
    "REC_COLUMNS",
]

#: 留痕的 kind。**只有**用户主动看推荐页才写 —— 看板每 30s 轮询一次，
#: 那是机器流量不是人的意图，混进来会让「历史」变成噪音。
KIND_ASK: Final[str] = "ask"

REC_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "code",
    "level",
    "action",
    "price",
    "signal_status",
    "signal_type",
    "headline",
    "reason",
    "data_bars",
    "factor_epoch",
    "created_at",
)


class RecommendationPersistError(RuntimeError):
    """推荐留痕读写失败。**读失败绝不用空列表冒充「没有历史」。**"""


def ensure_table(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.cpt_recommendation (
                id bigserial PRIMARY KEY,
                code text NOT NULL,
                level text NOT NULL DEFAULT '',
                action text NOT NULL,
                price double precision,
                signal_status text NOT NULL DEFAULT 'none',
                signal_type text NOT NULL DEFAULT '',
                headline text NOT NULL DEFAULT '',
                reason text NOT NULL DEFAULT '',
                data_bars integer NOT NULL DEFAULT 0,
                factor_epoch timestamptz,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)


def append_recommendation(conn: Any, rec: dict[str, Any], *, epoch: Any = None) -> int:
    """写一行留痕。成功返回 ``id``。

    :param rec: :func:`build_recommendation` 的产物。
    :param epoch: 当时的因子口径纪元（``switched_at``）；没有就存 NULL。
    :raises RecommendationPersistError: 写失败（**不吞**）。
    """
    quality = rec.get("data_quality") or {}
    sql = """
        INSERT INTO public.cpt_recommendation
            (code, level, action, price, signal_status, signal_type,
             headline, reason, data_bars, factor_epoch)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    params = (
        str(rec.get("code") or ""),
        str(rec.get("level") or ""),
        str(rec.get("action") or "hold"),
        rec.get("raw_close") if rec.get("raw_close") is not None else rec.get("price"),
        str(rec.get("status") or "none"),
        str(rec.get("signal_type") or ""),
        str(rec.get("headline") or ""),
        str(rec.get("reason") or ""),
        int(quality.get("bars") or 0),
        epoch,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("推荐留痕写入失败: %s", exc)
        raise RecommendationPersistError(f"推荐留痕写入失败: {exc}") from exc


def recent_recommendations(
    conn: Any, *, code: str, days: int = 30, limit: int = 100
) -> list[dict[str, Any]]:
    """读最近 ``days`` 天的留痕，**时间倒序**。

    :raises RecommendationPersistError: 读失败（**不返回空列表冒充「没历史」**）。
    """
    sql = """
        SELECT id, code, level, action, price, signal_status, signal_type,
               headline, reason, data_bars, factor_epoch, created_at
        FROM public.cpt_recommendation
        WHERE code = %s
          AND created_at >= now() - make_interval(days => %s)
        ORDER BY created_at DESC, id DESC
        LIMIT %s
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (code, max(1, int(days)), max(1, int(limit))))
            rows = cur.fetchall() or ()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("推荐留痕读取失败 code=%s: %s", code, exc)
        raise RecommendationPersistError(f"推荐留痕读取失败: {exc}") from exc
    return [
        {
            "id": r[0],
            "code": r[1],
            "level": r[2],
            "action": r[3],
            "price": r[4],
            "signal_status": r[5],
            "signal_type": r[6],
            "headline": r[7],
            "reason": r[8],
            "data_bars": r[9],
            "factor_epoch": r[10],
            "created_at": r[11],
        }
        for r in rows
    ]
