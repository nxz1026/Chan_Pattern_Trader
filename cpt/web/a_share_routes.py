"""A 股三条 HTTP 路由的载荷构造（R17-3）。

放在 ``cpt.web`` 下而不是 ``cpt.web.app`` 里，是为了让 ``app.py``（通用 HTTP 外壳，
零业务依赖）不必顶层 import psycopg / 本地库客户端 —— 这些只在真的访问 A 股路由时
才按需导入，加密侧看板不受影响（CI 无 psycopg 也要能跑）。

三条路由：
- ``snapshot``：v2 snapshot（与加密侧同构 ⇒ 四个画布直接复用）；
- ``pool``：**三源合并**的候选池（2026-09-25 起）= 热门池 Top5（``hot_rank`` 最新日
  ∪ ``ladder_day`` 连板）∪ 手输自选 ∪ 策略观察综合 Top5（``public.strategy_signal``）；
  每只标注**全部来源**（``sources``）与是否**有复权因子**（``drawable``，A 股能不能
  画出来的前提）；
- ``watchlist``：自选读写（``WatchlistStore`` JSON 落盘，单进程锁）。手输的代码走
  这里持久化 —— 此前它只写进 URL 查询串，第二次登录就丢。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HOT_TOP",
    "DEFAULT_STRATEGY_TOP",
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

#: 热门池在下拉里只留 Top5（2026-09-25 需求：热门池收敛，别把 100 只全倒进下拉）。
DEFAULT_HOT_TOP: int | None = 5

#: 策略观察候选同样只留 Top5。
DEFAULT_STRATEGY_TOP: int | None = 5

#: 合并去重后的分组顺序。**手输排第一**：用户自己的选择绝不能被算法分组盖掉 ——
#: 否则他会以为手输又丢了，而"手输丢失"正是这次要修的问题。策略排最后，它是外部
#: 观察信号，不是本系统的判断。
_GROUP_ORDER: tuple[str, ...] = ("manual", "hot", "strategy")

#: 具体来源标签 → 分组（``hot_rank`` 与 ``ladder_day`` 同属"热门池"）。
_GROUP_OF: dict[str, str] = {
    "manual": "manual",
    "hot_rank": "hot",
    "ladder_day": "hot",
    "strategy": "strategy",
}

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


def _names(codes: list[str]) -> dict[str, Any]:
    """批量查证券名称；**失败返回空字典**（名字是装饰，不该让池子/自选报错）。"""
    from cpt.adapters.a_share_local import fetch_security_names  # noqa: PLC0415

    try:
        return fetch_security_names(codes)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("证券名称批量查询失败: %s", exc)
        return {}


def _manual_entries() -> tuple[list[Any], str | None]:
    """A 股手输（自选）条目 + 读失败原因。

    读不到**不让整个池子挂**：热门池和策略仍然可用，前端只需少一组。这与
    ``_factor_codes`` / ``_names`` 的处理哲学一致 —— 装饰性来源失败不该拖垮主视图。

    注意与 ``_entries_payload``（自选路由本身）**刻意不同**：那条路由读失败就该报错，
    因为用户点的是"看我的自选"，静默返回空列表会让他以为自选被清空了。
    """
    try:
        return [e for e in _store().list() if e.market == _MARKET], None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def _merge_sources(
    hot_entries: list[Any], strategy_picks: list[Any], manual_entries: list[Any]
) -> list[dict[str, Any]]:
    """三源合并去重：同一代码只出一条，``sources`` 列出它的**全部**来源。

    去重是必须的，不是优化：``000592`` 可以同时是热门池 #2 和策略 42 分。不去重的
    话下拉里会出现两个 ``value`` 相同的 option，选中哪个结果一样，而 ``selected``
    归属还会变得随机 —— 正是"选错票"最容易发生的地方。

    合并后每条的 ``group`` 取 ``sources`` 的第一个（即 ``_GROUP_ORDER`` 里优先级
    最高的那个），前端据此分组渲染。
    """
    slots: dict[str, dict[str, Any]] = {}

    def slot(code: str) -> dict[str, Any]:
        return slots.setdefault(code, {"hot": None, "strategy": None, "manual": None})

    for entry in hot_entries:
        slot(entry.code)["hot"] = {
            "source": entry.source,
            "rank": entry.rank,
            "cont_days": entry.cont_days,
            "as_of": entry.as_of,
        }
    for pick in strategy_picks:
        slot(pick.code)["strategy"] = {
            "action": pick.action,
            "score": pick.score,
            "confidence": pick.confidence,
            "combined": pick.combined,
            "strategy": pick.strategy,
            # ⚠️ 策略名称，**不是股票名**（见 cpt.adapters.strategy_signal 的 docstring）
            "strategy_name": pick.strategy_name,
            "trade_date": pick.trade_date,
            "reason": pick.reason,
            "model": pick.model,
        }
    for entry in manual_entries:
        slot(entry.code)["manual"] = {"added_at": entry.added_at}

    merged: list[dict[str, Any]] = []
    for code, parts in slots.items():
        sources: list[str] = []
        if parts["manual"] is not None:
            sources.append("manual")
        if parts["hot"] is not None:
            sources.append(parts["hot"]["source"])  # hot_rank | ladder_day
        if parts["strategy"] is not None:
            sources.append("strategy")
        merged.append({"code": code, "sources": sources, "group": _GROUP_OF[sources[0]], **parts})
    merged.sort(key=_pool_sort_key)
    return merged


def _pool_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """组内排序键：手输按加入时间、热门池按名次、策略按综合分。"""
    group = _GROUP_ORDER.index(item["group"])
    if item["group"] == "manual":
        return (group, item["manual"]["added_at"] or "", item["code"])
    if item["group"] == "hot":
        hot = item["hot"]
        rank = hot["rank"] if hot["rank"] is not None else 9999
        return (group, rank, -(hot["cont_days"] or 0), item["code"])
    strategy = item["strategy"]
    return (group, -strategy["combined"], -strategy["score"], item["code"])


def pool_payload(
    *,
    hot_limit: int | None = DEFAULT_HOT_TOP,
    strategy_limit: int | None = DEFAULT_STRATEGY_TOP,
) -> dict[str, Any]:
    """A 股下拉的候选池 = **热门池 Top5 ∪ 手输（自选）∪ 策略综合 Top5**。

    ``drawable`` 字段是**刻意**加的：池子里没有复权因子的票点进去必然是空图（本地
    因子表未覆盖，按需拉取也可能失败）。在列表阶段就说清楚，比让用户对着空画布猜好。

    三个来源各自的失败**互不影响**（``factor_error`` / ``strategy_error`` /
    ``watchlist_error`` 分别记录）：A 股入口是主视图，任何一个上游抖动都不该让它整体
    不可用。前端只在对应字段非空时提示。

    自选读的是 ``_store()``（服务端 JSON，见 ``DEFAULT_WATCHLIST_PATH``）——
    **不是** localStorage。手输的代码必须跨登录保留，这是本次改动的起因。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.adapters.a_share_pool import fetch_hot_pool  # noqa: PLC0415
    from cpt.adapters.strategy_signal import fetch_strategy_top  # noqa: PLC0415

    client = AShareLocalClient()
    try:
        conn = client._get_conn()  # noqa: SLF001
        entries = fetch_hot_pool(conn, limit=hot_limit)
        try:
            picks = fetch_strategy_top(conn, limit=strategy_limit)
            strategy_error: str | None = None
        except Exception as exc:  # noqa: BLE001 — 策略表读不到也要能出池子
            picks = []
            strategy_error = f"{type(exc).__name__}: {exc}"
    finally:
        client.close()

    try:
        factors = _factor_codes()
        factor_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — 因子表读不到也要能出池子
        factors = set()
        factor_error = f"{type(exc).__name__}: {exc}"

    manual, watchlist_error = _manual_entries()
    merged = _merge_sources(entries, picks, manual)

    names = _names([item["code"] for item in merged])
    for item in merged:
        code = item["code"]
        # 名字（如 002119 → 康强电子）。下拉里只给六位数字太容易看岔。
        item["name"] = names[code].name if code in names else ""
        item["board"] = names[code].board if code in names else None
        item["drawable"] = code in factors

    as_of = entries[0].as_of if entries else (picks[0].trade_date if picks else None)
    return {
        "schema_version": "a_share_pool.v2",
        "as_of": as_of,
        "count": len(merged),
        "drawable_count": sum(1 for item in merged if item["drawable"]),
        "groups": {
            group: sum(1 for item in merged if item["group"] == group) for group in _GROUP_ORDER
        },
        "factor_error": factor_error,
        "strategy_error": strategy_error,
        "watchlist_error": watchlist_error,
        "items": merged,
    }


def _entries_payload() -> dict[str, Any]:
    entries = _store().list()
    names = _names([entry.code for entry in entries])
    return {
        "schema_version": "a_share_watchlist.v1",
        "market": _MARKET,
        "count": len(entries),
        "items": [
            {
                "code": entry.code,
                "name": names[entry.code].name if entry.code in names else "",
                "market": entry.market,
                "added_at": entry.added_at,
            }
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
