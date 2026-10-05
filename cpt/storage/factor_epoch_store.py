"""因子口径纪元（R45）。

## 这解决什么问题

2026-10-03 两次切表把 :table:`asel.ref_adjust_factor` 从「tx:fqkline 逐日比值」
换成「公司行动重算」。后果：切表前记录的 **41 条信号里 29 条变成
``invalidated``**。

那些 ``invalidated`` **不是**「信号失败了」，而是「结构在换口径后重算，
与旧口径判断不一致」。**两件事在没有标记时长得一模一样** ——
下一个看到「信号突然失效」的人会当成 bug 排查一轮。

## 为什么是单行表而不是给每行加列

加列会有**新旧混态**的坑：历史行要回填，新行靠写入方赋值 ——
只要有一处漏了，表里就同时存在「有纪元 / 没纪元」两种状态，比没有更糟。

口径切换**本质是一个时间点**，而 ``cpt_signal_event`` 本来就有
``created_at``，所以归属是**可推导**的：

    created_at <  switched_at  ⇒ 旧口径
    created_at >= switched_at  ⇒ 新口径

单行表只回答「切换发生在哪一刻」，不逐行打标。
"""

from __future__ import annotations

import logging
from typing import Any, Final

_LOG = logging.getLogger(__name__)

__all__ = [
    "EPOCH_DDL",
    "FactorEpoch",
    "current_epoch",
    "ensure_epoch_table",
    "is_legacy_event",
    "record_epoch",
]

#: 切表实测：旧口径台阶匹配率 22.63%，新口径 100.00%。
_OLD_MATCH_RATE: Final[float] = 0.2263
_NEW_MATCH_RATE: Final[float] = 1.0

EPOCH_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS public.cpt_factor_epoch (
    id             smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    switched_at    timestamptz NOT NULL,
    old_source     text NOT NULL,
    new_source     text NOT NULL,
    old_match_rate numeric,
    new_match_rate numeric,
    note           text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now()
)
"""


class FactorEpoch(dict[str, Any]):
    """一行纪元记录（``dict`` 子类，便于直接 JSON 化）。"""

    @property
    def switched_at(self) -> Any:
        return self.get("switched_at")

    @property
    def old_source(self) -> Any:
        return self.get("old_source")

    @property
    def new_source(self) -> Any:
        return self.get("new_source")

    @property
    def old_match_rate(self) -> Any:
        return self.get("old_match_rate")

    @property
    def new_match_rate(self) -> Any:
        return self.get("new_match_rate")

    @property
    def known(self) -> bool:
        return bool(self.get("switched_at"))


def ensure_epoch_table(conn: Any) -> None:
    """建表（幂等）。**不 commit** —— 事务边界归调用方。"""
    with conn.cursor() as cur:
        cur.execute(EPOCH_DDL)
        cur.execute(
            "COMMENT ON TABLE public.cpt_factor_epoch IS "
            "'因子口径纪元：switched_at 之前的事件属于旧口径，之后属于新口径'"
        )


def record_epoch(
    conn: Any,
    switched_at: Any,
    *,
    old_source: str = "tx:fqkline",
    new_source: str = "eastmoney:events",
    old_match_rate: float = _OLD_MATCH_RATE,
    new_match_rate: float = _NEW_MATCH_RATE,
    note: str = "",
) -> None:
    """登记（或更新）切换点。**幂等**：重复调用只更新同一行。

    ⚠️ ``ON CONFLICT DO UPDATE`` 而非 ``DO NOTHING`` —— 切换时刻允许被
    **修正**（实测发现切了两次：09:55:46Z 与 10:34:24Z，后者才是最终状态）。
    刻意不写死成一次：一次是巧合，两次是这轮的事实。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.cpt_factor_epoch
                (id, switched_at, old_source, new_source,
                 old_match_rate, new_match_rate, note)
            VALUES (1, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                switched_at    = EXCLUDED.switched_at,
                old_source     = EXCLUDED.old_source,
                new_source     = EXCLUDED.new_source,
                old_match_rate = EXCLUDED.old_match_rate,
                new_match_rate = EXCLUDED.new_match_rate,
                note           = EXCLUDED.note
            """,
            (switched_at, old_source, new_source, old_match_rate, new_match_rate, note),
        )


def current_epoch(conn: Any) -> FactorEpoch:
    """读当前纪元；表不存在或无记录 → 空 dict（``known`` 为 False）。

    **永不抛** —— 纪元是**说明性**的，缺了不该让看板 500。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT switched_at, old_source, new_source, "
                "       old_match_rate, new_match_rate, note "
                "FROM public.cpt_factor_epoch WHERE id = 1"
            )
            row = cur.fetchone()
        # ⚠️ 取值也必须在 try 内：测试替身常返回**短元组**（按查询顺序硬编码），
        # `row[5]` 会抛 IndexError 逃出函数 —— 而本函数的契约是**永不抛**。
        if not row or len(row) < 6:
            return FactorEpoch()
        return FactorEpoch(
            switched_at=row[0],
            old_source=row[1],
            new_source=row[2],
            old_match_rate=row[3],
            new_match_rate=row[4],
            note=row[5],
        )
    except Exception as exc:  # noqa: BLE001 — 缺表/无权限/行数不足都不该拖垮看板
        _LOG.info("读取因子纪元失败（不影响主流程）: %s", exc)
        return FactorEpoch()


def is_legacy_event(created_at: Any, epoch: FactorEpoch) -> bool:
    """事件是否属于**旧口径**。

    :param created_at: 事件的写入墙钟。
    :param epoch: :func:`current_epoch` 的结果；``known`` 为 False 时一律判「新口径」
        （没有纪元记录就无从说起，判成旧口径会凭空污染统计）。
    """
    if not epoch.known or created_at is None:
        return False
    try:
        return bool(created_at < epoch.switched_at)
    except TypeError:  # 类型不可比（naive vs aware）时保守判新口径
        return False
