"""Trade API：把 CPT 的缠论信号以「决策批次」投喂给 LKL-Trade 交易机。

与 emotion-core 的 ``presentation/trade_api.py`` **协议完全一致**（同一个交易机
客户端要能对接两个上游），差别只在三处：

1. **数据源**：本模块读 ``cpt_signal_event``（CPT 自己的结构事件流），
   经 :func:`cpt.storage.signal_event_store.load_trade_decisions` 取数。
   本模块**不写 SQL**（R46 分层门禁：SQL 只许在 ``cpt/adapters`` / ``cpt/storage``）。
2. **状态目录独立**：默认 ``/home/ubuntu/trade_cpt``，**绝不能**与 emotion-core 的
   ``/home/ubuntu/trade`` 共用——那里的 ``state.json`` 用 ``for_date`` 做键
   （``decisions[for_date]``），两个上游共用会**互相覆盖决策缓存**：
   谁先生成 batch_id，另一个的决策就永远发不出去，且不报错。
3. **哪些状态算「可下单」是配置项**，不是硬编码——这是业务决策，见
   :data:`DEFAULT_TRADE_STATUSES` 的说明。

路由（在 ``app.py`` 里注册，且必须在加密 provider 分发**之前**，
理由同 A 股路由：否则 provider 不可达时这里根本走不到）::

    GET  /api/trade/decisions?date=YYYY-MM-DD  -> {batch_id, for_date, actions[]}
    POST /api/trade/results                    -> {status: "ok", batch_id}
    GET  /api/trade/results?date=YYYY-MM-DD    -> 最近一次回执
    GET  /api/trade/health                     -> {ok: true, service, source}
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs

from cpt.domain.market_time import market_today

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TRADE_STATUSES",
    "TradeStateError",
    "handle_trade_decisions",
    "handle_trade_health",
    "handle_trade_results_get",
    "handle_trade_results_post",
    "trade_dir",
]

#: 决策源目录。**必须与 emotion-core 的分开**，否则 state.json 的
#: ``decisions[for_date]`` 缓存互相覆盖（见模块 docstring）。
DEFAULT_TRADE_DIR = "/home/ubuntu/trade_cpt"

#: 允许进入决策批次的状态白名单。
#:
#: ⚠️ **为什么含 ``structure_ready``**：`confirmed`（反向笔已确认）在真实数据上
#: **一买历史 0 条**，只用它的话端点永远返回空 actions，接通了也看不到东西。
#: ``structure_ready`` 是「两个中枢 + 背驰段成立、方向向下」，是当前唯一能产出
#: 买入决策的状态（2026-09-30 实测 11 条）。
#:
#: ⚠️ **它的代价（必须知道再上实盘）**：``structure_ready`` 到 ``confirmed`` 之间
#: 还隔着反向笔，而反向笔的门槛在这批数据上从未达成——历史上到达过
#: ``structure_ready`` 的 9 个 signal_id，**8 个最终转 ``invalidated``**（约 89%）。
#: 叠加 A 股 T+1（当日买入不可卖），按 ``structure_ready`` 买入意味着相当一部分
#: 会被套至少一个交易日。
#:
#: **当前定位是「接出来看得见」，不是「拿来真下单」。** 交易机侧保持不接
#: （未设 ``GM_SOURCE=cpt`` 的实例，LKL-Trade 默认 dry 演练），所以这批决策
#: 不会产生任何真实委托。要上实盘前请先把下面的白名单调回只有 ``confirmed``。
#:
#: ⚠️ 别指望「等 ``alert``/``candidate`` 接上再上实盘」——那两个状态在生产路径上
#: **不可达**（``assess_first_buy`` 从不产出 ``alert``，``_advance`` 只消费它），
#: 生产 ``status`` 恒为 ``{structure_ready, confirmed, invalidated}``。真要那两档
#: 得新增盘中反向 K 线的实时能力，不是补线。详见 ``docs/pending-wiring.md``。
DEFAULT_TRADE_STATUSES: tuple[str, ...] = ("confirmed", "structure_ready")

#: 回执文件 / ``processed`` 列表的保留上限（``R59（审计 M2）``）。
#:
#: 修前两个都**只涨不消**：``results_*.json`` 每个 batch 一个文件（交易机
#: 一天可能回传多次），``state["processed"]`` 每条回执追加一个 batch_id ——
#: 跑一年就是几万个文件 + 几万条 list，而 ``state.json`` 是每次决策都要整体读写的。
#:
#: - ``_RESULTS_RETENTION_DAYS``：只保留最近 N 天的回执**文件**。回执是审计留痕，
#:   ``GET /api/trade/results?date=`` 只查某一天，且**生产查询的都是今天**；
#:   90 天足够覆盖"翻上季度账"的需求，再老的就该进归档而不是留在热目录。
#: - ``_MAX_PROCESSED_BATCHES``：``processed`` 只保留最近 N 个 batch_id（FIFO）。
#:   ⚠️ 幂等性代价必须说清：超过上限的老 batch 若**再次**回传，会被当成新批次
#:   重写一个结果文件。上限取 5000（远超任何真实回放窗口：交易机重试发生在
#:   分钟级），**不破坏实际幂等**；同时``_prune_processed`` 只裁最老的，
#:   最近的一定在。
_RESULTS_RETENTION_DAYS = 90
_MAX_PROCESSED_BATCHES = 5000

#: 建议股数。⚠️ LKL-Trade 目前**只支持市价单**（``OrderType_Market, price=0``），
#: 所以这里的 ``price`` 只能进 ``reason`` 供审计，**不会**成为委托价。
DEFAULT_VOLUME = 100

#: 信号类型 → 交易动作。CPT 的 ``first_buy`` / ``first_sell`` 直接对应开仓 / 清仓。
_ACTION_MAP: dict[str, tuple[str, str]] = {
    "first_buy": ("BUY", "OPEN_POS"),
    "first_sell": ("SELL", "CLOSE_ALL"),
}

_state_lock = Lock()


class TradeStateError(RuntimeError):
    """决策状态目录损坏或不可读——必须让调用方看见，不静默降级成「无信号」。"""


def _today() -> date:
    """市场时区（北京）下的今天（``R59（审计 L11 同类残留）``）。

    修前这里是 ``date.today()`` —— **宿主本地时区**。生产机是 ``Etc/UTC``，
    于是北京时间 00:00-08:00 之间接口默认查的是**前一天**的决策/回执：
    交易机在北京凌晨拉一次，拿到的是昨天那一批，且看不出任何异常。
    统一走 :func:`cpt.domain.market_time.market_today`（域层唯一口径）。
    """
    return market_today()


def trade_dir() -> Path:
    p = Path(os.environ.get("CPT_TRADE_DIR") or DEFAULT_TRADE_DIR).expanduser()
    return p


def _state_file() -> Path:
    return trade_dir() / "state.json"


def _statuses() -> tuple[str, ...]:
    raw = os.environ.get("CPT_TRADE_BUY_STATUSES") or ""
    picked = tuple(s.strip() for s in raw.replace("，", ",").split(",") if s.strip())
    return picked or DEFAULT_TRADE_STATUSES


def _volume() -> int:
    try:
        return max(int(os.environ.get("CPT_TRADE_VOLUME") or DEFAULT_VOLUME), 0)
    except ValueError:
        return DEFAULT_VOLUME


def _load_state() -> dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {"decisions": {}, "processed": []}
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise TradeStateError(f"决策状态文件损坏: {p}") from exc
    if not isinstance(loaded, dict):
        raise TradeStateError(f"决策状态文件不是对象: {p}")
    data: dict[str, Any] = loaded
    data.setdefault("decisions", {})
    data.setdefault("processed", [])
    return data


def _save_state(state: dict[str, Any]) -> None:
    d = trade_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _state_file())


def _prune_results_files(*, today: date | None = None) -> int:
    """删掉过期回执文件，返回删除数（``R59（审计 M2）``）。

    只碰**文件名严格匹配** ``results_YYYY-MM-DD_*.json`` 且日期早于
    ``today - _RESULTS_RETENTION_DAYS`` 的文件 —— 目录里别的东西（包括
    ``state.json`` 与交易机自己的文件）一律不动。这是删除操作，
    所以宁可少删（多占几 KB）也不能删错：
    「文件名不像回执」就跳过，绝不按大小/时间乱扫。

    为什么不只靠 ``processed`` 上限：那是**幂等键**的裁剪，与磁盘占用是两件事；
    两者都做，且各自的保留期独立可解释。
    """
    cutoff = (today or _today()) - timedelta(days=_RESULTS_RETENTION_DAYS)
    removed = 0
    for path in trade_dir().glob("results_*.json"):
        stamp = path.name[len("results_") :].split("_", 1)[0]
        try:
            when = date.fromisoformat(stamp)
        except ValueError:
            continue
        if when >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            _LOG.warning("过期回执删除失败 %s: %s", path.name, exc)
    if removed:
        _LOG.info("清理过期回执 %d 个（保留 %d 天）", removed, _RESULTS_RETENTION_DAYS)
    return removed


def _prune_processed(state: dict[str, Any]) -> None:
    """把 ``processed`` 裁到最近 :data:`_MAX_PROCESSED_BATCHES` 条（``R59（审计 M2）``）。

    裁剪**必须在锁内、且与 ``_save_state`` 同一次写**完成，否则并发回执会用
    陈旧副本把别的 batch 盖回去（幂等键丢失 → 重复执行）。
    """
    processed = state.get("processed")
    if not isinstance(processed, list) or len(processed) <= _MAX_PROCESSED_BATCHES:
        return
    state["processed"] = processed[-_MAX_PROCESSED_BATCHES:]


def _fetch_signals(for_date: str) -> tuple[dict[str, Any], ...]:
    """从库取当日可下单信号。延迟导入：``web`` 层不顶层拖 psycopg。

    取连接的写法与 ``a_share_routes`` 全仓一致（``client._get_conn()``），
    连接生命周期沿用同一约定。
    """
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
    from cpt.storage.signal_event_store import load_trade_decisions  # noqa: PLC0415

    client = AShareLocalClient()
    return load_trade_decisions(
        client._get_conn(),  # noqa: SLF001
        for_date=for_date,
        statuses=_statuses(),
    )


def _build_actions(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in rows:
        action, exec_ = _ACTION_MAP.get(str(row["signal_type"]), (None, None))
        if action is None:
            # 未知信号类型**不猜**——猜错会把「不是买点」的东西当买点发出去。
            _LOG.warning("未识别的信号类型 %r（code=%s），跳过", row["signal_type"], row["code"])
            continue
        price = row.get("price")
        reason = str(row.get("signal_id") or "")
        if price is not None:
            # ⚠️ 2026-10-07 实测：`cpt_signal_event.price` 是**后复权价**，不是可成交价。
            # 2026-09-30 实测：600519 信号价 8886.54 vs daily_bar.close 1258.62（7.06×）、
            # 000002 1311.70 vs 4.26（307.9×）、301047 333.34 vs 159.00（2.10×）——
            # 倍数因股而异（累计分红送转）。
            # 市价单不受影响（价格不进委托），但**绝不能**当限价或展示价用，故标注清楚。
            reason = (
                f"{reason} 后复权参考价{float(price):.2f}（非可成交价）"
                if reason
                else f"后复权参考价{float(price):.2f}（非可成交价）"
            )
        actions.append(
            {
                "code": str(row["code"]).zfill(6),
                "action": action,
                "exec": exec_,
                "volume": _volume(),
                "reason": reason,
            }
        )
    return actions


def handle_trade_decisions(query_str: str) -> tuple[dict[str, Any], int]:
    """``GET /api/trade/decisions?date=YYYY-MM-DD``"""
    q = parse_qs(query_str)
    for_date_str = q.get("date", [_today().isoformat()])[0]
    try:
        for_date = date.fromisoformat(for_date_str)
    except ValueError:
        # R59（审计 M4）：不再把请求串原样回显（``f"invalid date: {for_date_str}"``）。
        # 这不是路径穿越（已核算不可利用），但**回显输入**没有任何诊断价值，
        # 而错误体是唯一回给交易机/浏览器的字段；原文进日志。
        _LOG.warning("决策查询的 date 非法（已忽略）: %r", for_date_str[:64])
        return {"error": "invalid date"}, 400

    key = for_date.isoformat()
    with _state_lock:
        try:
            state = _load_state()
        except TradeStateError as exc:
            _LOG.error("%s", exc)
            return {"error": "trade state unavailable"}, 503
        cached = state["decisions"].get(key)
        if cached is not None:
            _LOG.info("返回缓存决策 batch=%s 来源=cpt", cached.get("batch_id"))
            return cached, 200

        try:
            rows = _fetch_signals(key)
        except Exception as exc:  # noqa: BLE001 — 读不到 ≠ 没信号，必须分开说
            _LOG.error("取交易决策失败 %s: %s", key, exc)
            # R59（审计 M4）：只回稳定文案，异常类名/原文只进服务端日志。
            return {"error": "decision source unavailable"}, 503

        payload: dict[str, Any] = {"batch_id": "", "for_date": key, "actions": []}
        actions = _build_actions(rows)
        if actions:
            payload = {
                "batch_id": str(uuid.uuid4()),
                "for_date": key,
                "actions": actions,
                "source": "cpt",
            }
            state["decisions"][key] = payload
            try:
                _save_state(state)
            except OSError as exc:
                # 决策已生成但没缓存成——**仍然返回**，否则交易机会以为今天没信号；
                # 代价是下次请求会生成新 batch_id（交易机按 batch 幂等，不会重下）。
                _LOG.error("决策缓存落盘失败（仍返回本次决策）: %s", exc)
        _LOG.info("生成决策 %s（来源=cpt，%d 条，状态白名单=%s）", key, len(actions), _statuses())
        return payload, 200


def handle_trade_results_post(data: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """``POST /api/trade/results`` —— 交易机回传执行结果。

    入参是**已解析的 dict**（沿用 ``app.py`` 的 ``_read_json_body()`` 约定），
    ``None`` 表示 body 读不到或不是合法 JSON —— 由这里回 400，与仓内其他写路由一致。
    """
    if not data or not isinstance(data, dict):
        _LOG.warning("results body 不可解析或为空")
        return {"error": "invalid or empty JSON body"}, 400

    batch_id = str(data.get("batch_id") or "")
    for_date = str(data.get("for_date") or "")
    trades = data.get("trades") or []
    if not batch_id or not for_date:
        return {"error": "missing batch_id or for_date"}, 400

    # R59（审计 M2）：``for_date`` 以前**完全不校验**，直接进文件名
    # （``results_{for_date}_{ts}.json``）与 ``state["decisions"]`` 的键。
    # 路径穿越已核算不可利用（``Path`` 不接受含分隔符的写文件名），但非法形状会被
    # 原样写进目录/日志，且让「按日期查回执」永远查不到 —— 现在与 decisions 同口径
    # 用 ``date.fromisoformat`` 归一化，并限制在"回执是近期审计留痕"的合理范围内。
    try:
        for_date_day = date.fromisoformat(for_date)
    except ValueError:
        _LOG.warning("回执 for_date 非法 batch=%s for_date=%r", batch_id, for_date[:64])
        return {"error": "invalid for_date"}, 400
    today = _today()
    if not (today - timedelta(days=_RESULTS_RETENTION_DAYS)) <= for_date_day <= today:
        # 允许 90 天窗口（=保留期）：再早的回执文件已被清理，收下只会立刻成为孤儿；
        # 未来的日期则一定是交易机时钟或字段拼错。
        _LOG.warning("回执 for_date 超出保留窗口 batch=%s for_date=%s", batch_id, for_date)
        return {"error": "for_date out of retention window"}, 400
    for_date = for_date_day.isoformat()

    with _state_lock:
        try:
            state = _load_state()
        except TradeStateError as exc:
            _LOG.error("%s", exc)
            return {"error": "trade state unavailable"}, 503
        if batch_id in state["processed"]:
            _LOG.info("batch %s 已处理，幂等返回", batch_id)
            return {"status": "ok", "batch_id": batch_id, "note": "idempotent"}, 200

        try:
            d = trade_dir()
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            out = d / f"results_{for_date}_{ts}.json"
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            _LOG.error("结果落盘失败: %s", exc)
            return {"error": "results not persisted"}, 503

        state["processed"].append(batch_id)
        # R59（审计 M2）：两个"只涨不消"的地方都在这里收口 —— ``processed`` 裁上限、
        # 过期回执文件删除。都放在**已经持有 _state_lock 且紧随成功写入之后**，
        # 失败只记日志：清理是维护动作，不该让已落盘的回执变成 500。
        _prune_processed(state)
        try:
            _save_state(state)
        except OSError as exc:
            _LOG.error("processed 落盘失败（结果已存，幂等会失效）: %s", exc)
        try:
            _prune_results_files(today=today)
        except OSError as exc:
            _LOG.warning("过期回执清理失败: %s", exc)

    _LOG.info("收到回执 batch=%s 共 %d 笔", batch_id, len(trades))
    return {"status": "ok", "batch_id": batch_id}, 200


def handle_trade_results_get(query_str: str) -> tuple[dict[str, Any], int]:
    """``GET /api/trade/results?date=YYYY-MM-DD``"""
    q = parse_qs(query_str)
    for_date_str = q.get("date", [_today().isoformat()])[0]
    # R59（审计 M2）：``for_date`` 以前直接拼进 glob 模式
    # （``results_{for_date}_*.json``）—— 与 decisions 同口径先归一化，
    # 非法回 400 而不是 glob 出一个空列表冒充"今天没有回执"。
    try:
        for_date = date.fromisoformat(for_date_str).isoformat()
    except ValueError:
        _LOG.warning("回执查询的 date 非法（已忽略）: %r", for_date_str[:64])
        return {"error": "invalid date"}, 400
    files = sorted(trade_dir().glob(f"results_{for_date}_*.json"), reverse=True)
    if not files:
        return {"for_date": for_date, "trades": []}, 200
    try:
        return json.loads(files[0].read_text(encoding="utf-8")), 200
    except (ValueError, OSError) as exc:
        # 回执文件半截/被手工改坏：回 503 说清楚"存储有问题"，
        # 而不是抛出去让 handler 变成 500（也没有原文回显，M4）。
        _LOG.error("回执文件不可读 %s: %s", files[0].name, exc)
        return {"error": "results unreadable"}, 503


def handle_trade_health() -> tuple[dict[str, Any], int]:
    """``GET /api/trade/health``

    R59（审计 L5）：``state_dir`` 以前回**绝对路径**（``/home/ubuntu/trade_cpt``）——
    这个端点无鉴权、经 nginx 对外，等于把主机布局与运行账户名直接告诉扫描者。
    这里只回目录**名**（``trade_cpt``），保留"能看出指向哪个目录"的排障价值，
    又不再是可被拿去拼路径的绝对位置；``state_present`` 仍是布尔，判断"接线了吗"
    不依赖这个字段。

    被拒方案：①删掉整个键 —— 有的探针只拿它做展示/比对，删了会变成 ``missing``
    噪音，而审计只要求"不回绝对路径"，相对名已满足；②回 ``os.path.relpath`` ——
    它相对的是**进程 CWD**（server 由 systemd 起，CWD 不稳定），反而不可解释。
    """
    d = trade_dir()
    return {
        "ok": True,
        "service": "cpt_trade_api",
        "source": "cpt",
        "state_dir": d.name or str(d),
        "state_present": (d / "state.json").exists(),
        "statuses": list(_statuses()),
        "volume": _volume(),
    }, 200
