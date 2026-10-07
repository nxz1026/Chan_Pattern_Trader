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
from datetime import UTC, datetime, timedelta
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
    "recent_closes",
    "snapshot_payload",
    "submit_llm_explain",
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

#: 取"最近两根收盘价"时向前回溯的自然日数。放 30 天而不是 3~5 天：A 股有周末、
#: 长假和停牌，窗口太窄会让窗口内不足两根而白白降级；30 天足够覆盖任何常规假期，
#: 又不会把查询代价抬起来（日线一天一行）。
CLOSE_LOOKBACK_DAYS: int = 30

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


def build_recommendation(
    code: str, *, level: str | None = None, history_days: int = 0
) -> dict[str, Any]:
    """推荐块：动作 + 参考价 + 依据。**永不抛异常**。

    R45 新增。买卖与价格由 :mod:`cpt.application.recommendation` **纯确定性**算出
    —— 刻意**不经 LLM**：让模型生成「买/卖 + 价格」会不可复现、不可测，
    且在最要紧的输出上引入幻觉风险。LLM 只负责给这段结果**配人话**
    （另一条路由，且失败时前端照常显示这里的确定性结果）。

    走的是**同一份快照**而不是另查一次 ——
    推荐与图上画的结构必须来自同一次计算，否则两边会打架。
    """
    from cpt.application.recommendation import build_recommendation as _build  # noqa: PLC0415

    try:
        payload = snapshot_payload(code)
    except Exception as exc:  # noqa: BLE001 — 看板主路径，不能因推荐块崩掉
        return {
            "available": False,
            "action": "hold",
            "action_label": "观望",
            "headline": "推荐不可用",
            "reason": f"{type(exc).__name__}: {exc}",
            "price": None,
            "disclaimer": "结构状态翻译，非投资建议",
        }
    out = _build(payload)
    if level:
        out["level"] = level

    # ⚠️ 快照里的 K 线是**后复权价**（实测 600519 茅台显示 8886，而实际约 1400），
    # 拿它当「参考价」给用户是**不可挂单**的 —— 一个「买卖 + 价格」的面板
    # 给的是后复权价，等于给了一个他下不了单的数字。
    # ⇒ 补一个**不复权收盘价**（raw_close）作为可执行参考价，
    #    后复权价保留在 raw 里以备核对。
    out["raw_close"] = _raw_close(_normalize(code))
    # 复权倍率 = 后复权价 ÷ 不复权收盘价。用户能自己换算：
    #   真实可成交价 = 显示价；图上/K 线上的复权价 = 真实价 × 倍率。
    # 2026-10-07 实测：600519 8886.54 / 1258.62 = 7.06；
    #                 000002 1311.70 / 4.26 = 307.9（送转频繁）。
    # 倍率是**逐股**的（累计分红送转），所以必须现算，不能写死在文档里。
    _adj, _raw = out.get("price"), out.get("raw_close")
    if isinstance(_adj, int | float) and isinstance(_raw, int | float) and _raw > 0:
        out["price_ratio"] = round(float(_adj) / float(_raw), 4)
    else:
        out["price_ratio"] = None
    out["history"] = _signal_history(_normalize(code))
    out["level"] = level or ""

    # P2：留痕。**best-effort** —— 留痕失败不该让推荐接口 500，
    # 但**必须留日志**：静默丢会让「历史」悄悄变空而没人知道。
    #
    # ⚠️ 写**没有**去重：同一只票每次刷新写一行，这是**时间序列**不是状态。
    # 「最新一条」由读端排序表达。
    _persist_recommendation(code, out, level or "")
    if history_days:
        out["recommendation_history"] = _recommendation_history(code, days=history_days)
    return out


def _recommendation_history(code: str, *, days: int) -> dict[str, Any]:
    """读回这只票的历史推荐（按口径纪元分组）。

    失败 ⇒ ``available:False`` + 写明原因，**不返回空列表冒充「没有历史」**。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.storage.recommendation_store import (  # noqa: PLC0415
        RecommendationPersistError,
        recent_recommendations,
    )

    client = None
    try:
        client = AShareLocalClient()
        rows = recent_recommendations(client._get_conn(), code=code, days=days)  # noqa: SLF001
    except RecommendationPersistError as exc:
        return {
            "available": False,
            "reason": "recommendation_history_unavailable",
            "detail": str(exc),
            "count": 0,
            "items": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "reason": "recommendation_history_error",
            "detail": f"{type(exc).__name__}: {exc}",
            "count": 0,
            "items": [],
        }
    finally:
        if client is not None:
            client.close()

    epochs = sorted({r["factor_epoch"] for r in rows if r.get("factor_epoch")})
    return {
        "available": bool(rows),
        "count": len(rows),
        "days": days,
        "epochs": [e.isoformat() if hasattr(e, "isoformat") else str(e) for e in epochs],
        "items": [
            {
                **r,
                "created_at": r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else str(r["created_at"]),
                "factor_epoch": r["factor_epoch"].isoformat()
                if hasattr(r["factor_epoch"], "isoformat")
                else r["factor_epoch"],
            }
            for r in rows
        ],
    }


def _persist_recommendation(code: str, rec: dict[str, Any], level: str) -> None:
    """把这次推荐落一行。**失败只记日志**，不让它带崩推荐接口。"""
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.storage.factor_epoch_store import current_epoch  # noqa: PLC0415
    from cpt.storage.recommendation_store import (  # noqa: PLC0415
        RecommendationPersistError,
        append_recommendation,
        ensure_table,
    )

    rec = {**rec, "code": code, "level": level}
    client = None
    try:
        client = AShareLocalClient()
        conn = client._get_conn()  # noqa: SLF001
        ensure_table(conn)
        epoch = None
        try:
            epoch = current_epoch(conn).switched_at
        except Exception:  # noqa: BLE001 — 没有纪元就存 NULL，不该因此丢掉整条留痕
            epoch = None
        append_recommendation(conn, rec, epoch=epoch)
        conn.commit()  # store 层不 commit（边界归调用方），同 llm_cases
    except RecommendationPersistError as exc:
        _LOG.warning("推荐留痕未写入 code=%s: %s", code, exc)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("推荐留痕异常 code=%s: %s", code, exc)
    finally:
        if client is not None:
            client.close()


def _signal_history(code: str) -> dict[str, Any]:
    """信号历史 + **口径分组**（R45 P2）。

    因子表 R45 一天切了 4 次，而推荐是当天才有的 ⇒ 「切表前推荐长什么样」
    当时**没有答案**。``cpt_signal_event`` 一直在记状态变迁，
    配合 ``cpt_factor_epoch.switched_at`` 就能分清旧口径 / 新口径。

    读失败**降级为不可用**，但不假装「确实没有历史」——
    与 ``load_signal_events`` 自己抛 ``SignalEventError`` 的口径一致。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.application.recommendation import build_history  # noqa: PLC0415
    from cpt.storage.factor_epoch_store import current_epoch  # noqa: PLC0415
    from cpt.storage.signal_event_store import (  # noqa: PLC0415
        SignalEventError,
        load_signal_events,
    )

    # ⚠️ R45 修：**client 构造必须在 try 里面**。
    # 原来它写在 try 之前 ⇒ 构造一抛就**穿出去**，而调用方
    # `build_recommendation` 的兜底会把**整个推荐**降级 ——
    # 可「信号历史」只是**装饰**（今天 R45 自己加的），
    # 它坏了不该让「动作 + 参考价」一起没。
    client = None
    try:
        client = AShareLocalClient()
        conn = client._get_conn()  # noqa: SLF001
        epoch_ms = None
        try:
            ep = current_epoch(conn)
            epoch_ms = int(ep.switched_at.timestamp() * 1000)
        except Exception:  # noqa: BLE001 — 没有纪元就不分组，不该因此丢掉历史
            epoch_ms = None
        try:
            events = load_signal_events(conn, days=90, code=code)
        except SignalEventError as exc:
            return {
                "available": False,
                "reason": "signal_history_unavailable",
                "detail": str(exc),
                "count": 0,
                "items": [],
            }
        return build_history(events, epoch_ms=epoch_ms)
    except Exception as exc:  # noqa: BLE001 — 历史是锦上添花，不能带崩推荐
        return {
            "available": False,
            "reason": "signal_history_error",
            "detail": f"{type(exc).__name__}: {exc}",
            "count": 0,
            "items": [],
        }
    finally:
        if client is not None:
            client.close()


def submit_llm_summarize(code: str, rec: dict[str, Any]) -> dict[str, Any]:
    """提交「给推荐配人话」的 LLM 请求。**入队即返回**，不等模型。

    ⚠️ 传的**只有** :func:`cpt.application.recommendation` 算出的那几行
    （动作 / 结论 / 依据 / 参考价）—— **不含结构明细**。
    模型因此没有机会产出与确定性结果**矛盾**的判断。
    看板上「卡片」与「人话」并排显示，两者矛盾时没人知道该信哪个。

    LLM 不可用 / 未启用 / 重复提交 → 返回 ``available: False``，
    前端照常显示确定性结果，**只把摘要那一行藏起来**。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.application.llm_cases import summarize_recommendation  # noqa: PLC0415
    from cpt.storage.llm_call_store import LLMCallError  # noqa: PLC0415

    # ⚠️ 构造在 try **里面**（同 ``submit_llm_explain``）：无 DB 配置时构造函数
    # 就抛，放在外面会绕过这里承诺的「LLM 不可用 → available=False」。
    client = None
    try:
        client = AShareLocalClient()
        names = _names([code])
        return summarize_recommendation(
            client._get_conn(),
            code=code,
            name=names.get(code, ""),
            action_label=str(rec.get("action_label") or ""),
            headline=str(rec.get("headline") or ""),
            reason=str(rec.get("reason") or ""),
            price=rec.get("raw_close") if rec.get("raw_close") is not None else rec.get("price"),
            disclaimer=str(rec.get("disclaimer") or ""),
        )
    except LLMCallError as exc:
        # ``finish_call`` 现在与 ``enqueue_call`` 同口径：写失败**抛**
        # （见 ``llm_call_store.LLMCallError``）。本路由的契约是「LLM 只是旁路，
        # 失败不抛给 HTTP」，所以在这里折成与 ``submit_llm_explain`` 相同的信封，
        # 而不是让一条审计写失败把整页变成 500。
        _LOG.warning("提交 LLM 摘要失败（审计落库失败）%s: %s", code, exc)
        return {
            "available": False,
            "call_id": "",
            "status": "error",
            "reason": f"llm_submit_failed:{type(exc).__name__}",
        }
    finally:
        if client is not None:
            client.close()


def _raw_close(code: str) -> float | None:
    """不复权收盘价（``public.daily_bar.close``，未复权）。

    与快照的 ``candles[-1].close`` **口径不同**，别混用：
    快照那份是后复权价，用于画图（复权后价格连续，结构才连得上）。
    """
    # R46：SQL 下沉到 adapters.a_share_local（web 层不再直接写 SQL，
    # 过 scripts/check_sql_layering.py 分层门禁）。
    from cpt.adapters.a_share_local import fetch_latest_raw_close  # noqa: PLC0415

    return fetch_latest_raw_close(code)


def _factor_codes() -> set[str]:
    """带复权因子的代码集合（A 股能否画出来的前提）。

    R24：SQL 已下沉到 ``adapters.a_share_local.fetch_factor_codes``。
    web 层不再直接碰连接 —— 它只调函数。
    """
    from cpt.adapters.a_share_local import fetch_factor_codes  # noqa: PLC0415

    return fetch_factor_codes()


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
    from cpt.adapters.a_share_local import AShareLocalClient, AShareLocalError  # noqa: PLC0415
    from cpt.adapters.a_share_pool import fetch_hot_pool  # noqa: PLC0415
    from cpt.adapters.strategy_signal import fetch_strategy_top  # noqa: PLC0415

    db_error: str | None = None
    entries: list[Any] = []
    picks: list[Any] = []
    strategy_error: str | None = None
    # ⚠️ 构造**必须在 try 里面**（R45 已修过 ``_signal_history`` 的同一处）：
    # 无 DB 配置 / 缺 psycopg 时 ``AShareLocalClient()`` 自己就抛，那种情况下
    # 没有连接可关，但**降级路径必须照样跑到** —— 构造在 try 外面时异常会直接
    # 冒出去，这个函数承诺的「三个来源各自失败互不影响」当场失效。
    client = None
    try:
        client = AShareLocalClient()
        conn = client._get_conn()  # noqa: SLF001
        entries = fetch_hot_pool(conn, limit=hot_limit)
        try:
            picks = fetch_strategy_top(conn, limit=strategy_limit)
        except Exception as exc:  # noqa: BLE001 — 策略表读不到也要能出池子
            picks = []
            strategy_error = f"{type(exc).__name__}: {exc}"
    except AShareLocalError as exc:  # noqa: BLE001 — 缺 psycopg/DB 不可达时降级
        db_error = f"{type(exc).__name__}: {exc}"
        entries = []
        picks = []
        strategy_error = None
    finally:
        if client is not None:
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
        "db_error": db_error,
        "items": merged,
    }


def recent_closes(code: str) -> tuple[float, float] | None:
    """取 ``code`` 的最近两根日线收盘价，返回 ``(prev_close, last_close)``。

    顺序是**时间升序**（前一根在前），与调用方算涨跌幅的直觉一致。

    降级为 ``None`` 而不是抛异常：调用方会**逐只**问价，任何一只票取不到价（连不上
    DB、窗口内不足两根、缺复权因子……）都不该把整张列表搞挂 —— 同
    :func:`_names` / :func:`_manual_entries` 的"装饰性/局部失败不拖垮主视图"哲学一致。

    读的是 :class:`AShareLocalClient` 既有的日线接口（后复权口径与画布同源），不另写
    SQL：口径一旦分成两份，列表上的涨跌幅和画布上的 K 线迟早对不上。

    惰性 import：``AShareLocalClient`` 的构造会在无 DB 配置时抛错，且它的模块链会去
    找 psycopg —— CI 没装 psycopg，顶层 import 会让本模块整个导入失败（连 crypto 看板
    都起不来）。放到函数内，导入失败/无 DB 时正好走下面的 ``except`` 降级。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415

    end = datetime.now(UTC)
    start_ms = int((end - timedelta(days=CLOSE_LOOKBACK_DAYS)).timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    # ⚠️ 构造在 try **里面**：无 DB 配置时构造函数就抛（见上面 docstring），
    # 构造在外面会让「取不到价 → None」这个降级承诺当场失效、直接冒出去。
    client = None
    try:
        client = AShareLocalClient()
        bars = client.fetch_validated_klines(code, start_ms, end_ms).bars
    except Exception as exc:  # noqa: BLE001 — 逐票失败只代表这一票没价
        _LOG.debug("最近收盘价读取失败 code=%s: %s", code, exc)
        return None
    finally:
        if client is not None:
            client.close()  # 无论成败都要放连接，池子里每只票都会走这里

    if len(bars) < 2:
        return None
    # 接口本身已按日期升序返回，这里再排一次是为把"顺序=时间升序"变成显式契约，
    # 上游实现换了排序也不会悄悄反过来。
    ordered = sorted(bars, key=lambda bar: bar.open_time)
    return (float(ordered[-2].close), float(ordered[-1].close))


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


def submit_llm_explain(code: str, structure: dict[str, Any]) -> dict[str, Any]:
    """提交一次「规则解释」请求，**立刻返回**（不等模型）。

    R25。LLM 是旁路增强：未启用 / 缺 key / 表不存在都只是 ``available=False``，
    **不抛异常** —— 主看板照常出图。UI 拿 ``call_id`` 去轮询
    ``/api/dashboard/llm/calls?call_id=...``。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.application.llm_cases import explain_structure  # noqa: PLC0415

    normalized = _normalize(code)
    try:
        names = _names([normalized])
    except Exception:  # noqa: BLE001 — 名字是装饰，取不到就用代码
        names = {}
    client = None
    try:
        client = AShareLocalClient()
        return explain_structure(  # noqa: SLF001
            client._get_conn(),  # noqa: SLF001
            code=normalized,
            name=names.get(normalized, ""),
            market="a_share",
            structure=structure,
            subject_id=str(structure.get("id", "")),
        )
    except Exception as exc:  # noqa: BLE001 — 旁路失败不拖垮写接口
        _LOG.warning("提交 LLM 解释失败 %s: %s", normalized, exc)
        return {
            "available": False,
            "call_id": "",
            "status": "error",
            "reason": f"llm_submit_failed:{type(exc).__name__}",
        }
    finally:
        if client is not None:
            client.close()
