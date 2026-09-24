"""A 股三条 HTTP 路由的载荷构造（R17-3）。

放在 ``cpt.web`` 下而不是 ``cpt.web.app`` 里，是为了让 ``app.py``（通用 HTTP 外壳，
零业务依赖）不必顶层 import psycopg / 本地库客户端 —— 这些只在真的访问 A 股路由时
才按需导入，加密侧看板不受影响（CI 无 psycopg 也要能跑）。

三条路由：
- ``snapshot``：v2 snapshot（与加密侧同构 ⇒ 四个画布直接复用）；
- ``pool``：热门池（``hot_rank`` 最新日 ∪ ``ladder_day`` 连板），并标注每只是否
  **有复权因子** —— 这是 A 股能不能画出来的前提；
- ``watchlist``：自选读写（``WatchlistStore`` JSON 落盘，单进程锁）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_WIDTH_K",
    "DEFAULT_WATCHLIST_PATH",
    "InvalidCodeError",
    "pool_payload",
    "snapshot_payload",
    "watchlist_add",
    "watchlist_payload",
    "watchlist_remove",
]

#: 默认展示根数（与 ``cpt.application.a_share_snapshot.DEFAULT_WIDTH_K`` 同值）。
DEFAULT_WIDTH_K: int = 120

#: 自选落盘位置。**不进仓库**：这是运行时状态，不是代码资产。
DEFAULT_WATCHLIST_PATH: Path = Path(
    os.getenv("CPT_WATCHLIST", "~/.cache/cpt/watchlist.json")
).expanduser()

_MARKET: str = "A"


class InvalidCodeError(ValueError):
    """代码格式不合法 → 路由返回 400（而不是让 handler 抛异常断连接）。"""


def _store(path: Path | None = None) -> Any:
    from cpt.adapters.a_share_pool import WatchlistStore  # noqa: PLC0415

    return WatchlistStore(path or DEFAULT_WATCHLIST_PATH)


def _normalize(code: str) -> str:
    """校验并归一化 A 股代码（``600519`` / ``600519.SH`` / ``sh600519``）。"""
    from cpt.adapters.a_share_public import ASharePublicError, normalize_code  # noqa: PLC0415

    try:
        normalized = normalize_code(code)
    except ASharePublicError as exc:
        raise InvalidCodeError(str(exc)) from exc
    # 自选里统一存 6 位裸码，与 daily_bar.code 一致
    return normalized[2:]


def snapshot_payload(code: str, *, width_k: int = DEFAULT_WIDTH_K) -> dict[str, Any]:
    """构造 A 股 v2 snapshot。失败时返回 degraded 占位快照（不抛）。

    **按需补因子在这里显式开启**（``default=True``）：用户输入代码 → 本地没有因子
    就去腾讯拉一次并落库 → 重新生成快照。application 层的默认是关闭的，所以直接
    调用 ``build_ashare_snapshot`` 的代码（含测试）不会联网、不会写库。
    """
    from cpt.application.a_share_snapshot import (  # noqa: PLC0415
        build_ashare_snapshot,
        factor_ensurer_from_env,
    )

    return build_ashare_snapshot(
        _normalize(code),
        width_k=width_k,
        ensure_factors=factor_ensurer_from_env(default=True),
    )


def _factor_codes() -> set[str]:
    """带复权因子的代码集合（A 股能否画出来的前提）。"""
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415

    client = AShareLocalClient()
    try:
        with client._get_conn().cursor() as cur:  # noqa: SLF001 — 就是要碰真连接
            cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor")
            return {str(row[0]) for row in cur.fetchall()}
    finally:
        client.close()


def pool_payload() -> dict[str, Any]:
    """热门池 + 每只是否有复权因子。

    ``drawable`` 字段是**刻意**加的：热门池 100 只里只有 94 只有因子，剩下 6 只
    点进去必然是空图。把这件事在列表阶段就说清楚，比让用户对着空画布猜要好。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.adapters.a_share_pool import fetch_hot_pool  # noqa: PLC0415

    client = AShareLocalClient()
    try:
        conn = client._get_conn()  # noqa: SLF001
        entries = fetch_hot_pool(conn)
    finally:
        client.close()

    try:
        factors = _factor_codes()
        factor_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — 因子表读不到也要能出池子
        factors = set()
        factor_error = f"{type(exc).__name__}: {exc}"

    items = [
        {
            "code": entry.code,
            "source": entry.source,
            "rank": entry.rank,
            "cont_days": entry.cont_days,
            "as_of": entry.as_of,
            "drawable": entry.code in factors,
        }
        for entry in entries
    ]
    return {
        "schema_version": "a_share_pool.v1",
        "as_of": items[0]["as_of"] if items else None,
        "count": len(items),
        "drawable_count": sum(1 for item in items if item["drawable"]),
        "factor_error": factor_error,
        "items": items,
    }


def _entries_payload() -> dict[str, Any]:
    entries = _store().list()
    return {
        "schema_version": "a_share_watchlist.v1",
        "market": _MARKET,
        "count": len(entries),
        "items": [
            {"code": entry.code, "market": entry.market, "added_at": entry.added_at}
            for entry in entries
        ],
    }


def watchlist_payload() -> dict[str, Any]:
    """自选列表（只返回 A 股，crypto 自选不混进来）。"""
    payload = _entries_payload()
    payload["items"] = [item for item in payload["items"] if item["market"] == _MARKET]
    payload["count"] = len(payload["items"])
    return payload


def watchlist_add(code: str) -> dict[str, Any]:
    """加入自选（幂等）。"""
    normalized = _normalize(code)
    _store().add(normalized, _MARKET)
    return watchlist_payload()


def watchlist_remove(code: str) -> dict[str, Any]:
    """移出自选；返回移除后的列表 + 是否真的移除了。"""
    normalized = _normalize(code)
    removed = _store().remove(normalized, _MARKET)
    payload = watchlist_payload()
    payload["removed"] = removed
    return payload
