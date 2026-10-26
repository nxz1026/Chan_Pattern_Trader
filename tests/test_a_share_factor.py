"""后复权因子重算路径测试（R37/R64/R96）。

按需取因子（腾讯 hfq / raw 相除那条路径）已经在 R96 整个删掉了：
``build_factor_rows`` / ``fetch_factor_rows`` / ``OnDemandFactorFetcher`` /
``FactorRow`` / ``upsert_factor_rows`` / ``factor_source_ref`` / ``SOURCE_TX``
等等全没了，连同 ``factor_ensurer_from_env`` 与 ``build_ashare_snapshot`` 的
``ensure_factors`` 参数一起（用户拍板「不仅要关，而且要删」）。

这个文件现在只测**重算路径**：``promote_recompute_factors`` 把暂存表
``asel.ref_adjust_factor_v2`` 的「按公司行动算出来的真·分段常数因子」搬进生产表。
"""

from __future__ import annotations

from typing import Any

from cpt.adapters.a_share_factor import (
    PROMOTE_SOURCE_URL,
    promote_recompute_factors,
)


class _PromoteCursor:
    """按脚本预置的 ``fetchone()`` 返回值回答，并记录每一条 SQL。"""

    def __init__(self, log: list[tuple[str, Any]], script: list[tuple]) -> None:
        self._log = log
        self._script = script
        self.rowcount = 0
        self._row: tuple | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))
        self._row = self._script.pop(0) if self._script else (None,)

    def fetchone(self) -> tuple | None:
        return self._row

    def __enter__(self) -> _PromoteCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _PromoteConn:
    def __init__(self, script: list[tuple]) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0
        self._script = list(script)

    def cursor(self) -> _PromoteCursor:
        return _PromoteCursor(self.log, self._script)

    def commit(self) -> None:
        self.commits += 1


def test_promote_dry_run_counts_without_writing() -> None:
    """dry_run 只数不写：不出现 INSERT，也不 commit。"""
    conn = _PromoteConn([(3393715, 93073)])
    assert promote_recompute_factors(conn, dry_run=True) == (3393715, 93073)
    sql = conn.log[0][0]
    assert "INSERT" not in sql
    assert "LEFT JOIN asel.ref_adjust_factor" in sql
    assert conn.commits == 0


def test_promote_updates_only_rows_beyond_tolerance() -> None:
    """判据必须是**带容差的数值比较**，不能用 ``IS DISTINCT FROM``。

    numeric ↔ double precision 的表示差异会让同一个 Python float 看起来不同 ——
    实测容差 1e-9 会白写 1,806,097 行、``IS DISTINCT FROM`` 更是 2,248,057 行，
    而真实修正只有 1e-8 那一簇 92,924 行。
    """
    conn = _PromoteConn([(200_000, 51, "000002", "2024-01-05"), (0, 0, None, None)])
    assert promote_recompute_factors(conn, batch=200_000) == (200_000, 51)
    sql = conn.log[0][0]
    assert "ON CONFLICT (code, trade_date) DO UPDATE" in sql
    assert "abs(asel.ref_adjust_factor.hfq_factor - EXCLUDED.hfq_factor)" in sql
    assert "1e-08" in sql
    assert "RETURNING 1" in sql
    # 来源标记要能区分「按公司行动重算」与「按需拉的腾讯比值」
    assert conn.log[0][1][3] == PROMOTE_SOURCE_URL
    # 每批各自 commit（含最后那批扫到空、用来收尾的事务）—— 一个事务锁全表是事故
    assert conn.commits == 2


def test_promote_paginates_by_keyset() -> None:
    """按 ``(code, trade_date)`` 键集分批：一个事务里锁住全表是事故。"""
    conn = _PromoteConn(
        [
            (200_000, 3, "000002", "2024-01-05"),
            (200_000, 0, "600519", "2024-03-01"),
            (0, 0, None, None),
        ]
    )
    assert promote_recompute_factors(conn) == (400_000, 3)
    # 第二批必须从上一批的尾部继续（游标推进），而不是从头再扫
    assert conn.log[1][1][:2] == ("000002", "2024-01-05")
    assert conn.log[2][1][:2] == ("600519", "2024-03-01")
    assert conn.commits == 3
