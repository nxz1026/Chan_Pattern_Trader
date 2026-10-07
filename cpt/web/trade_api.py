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
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs

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
#: 不会产生任何真实委托。要上实盘前请先把下面的白名单调回只有 ``confirmed``，
#: 或先补上 ``transition_first_buy`` 的 ``alert``/``candidate`` 中间档。
DEFAULT_TRADE_STATUSES: tuple[str, ...] = ("confirmed", "structure_ready")

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
            # 只作审计信息：交易机目前只发市价单，price 不会成为委托价。
            reason = f"{reason} 参考价{float(price):.2f}" if reason else f"参考价{float(price):.2f}"
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
    for_date_str = q.get("date", [date.today().isoformat()])[0]
    try:
        for_date = date.fromisoformat(for_date_str)
    except ValueError:
        return {"error": f"invalid date: {for_date_str}"}, 400

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
            return {"error": f"decision source unavailable: {type(exc).__name__}"}, 503

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
        try:
            _save_state(state)
        except OSError as exc:
            _LOG.error("processed 落盘失败（结果已存，幂等会失效）: %s", exc)

    _LOG.info("收到回执 batch=%s 共 %d 笔", batch_id, len(trades))
    return {"status": "ok", "batch_id": batch_id}, 200


def handle_trade_results_get(query_str: str) -> tuple[dict[str, Any], int]:
    """``GET /api/trade/results?date=YYYY-MM-DD``"""
    q = parse_qs(query_str)
    for_date = q.get("date", [date.today().isoformat()])[0]
    files = sorted(trade_dir().glob(f"results_{for_date}_*.json"), reverse=True)
    if not files:
        return {"for_date": for_date, "trades": []}, 200
    return json.loads(files[0].read_text(encoding="utf-8")), 200


def handle_trade_health() -> tuple[dict[str, Any], int]:
    """``GET /api/trade/health``"""
    d = trade_dir()
    return {
        "ok": True,
        "service": "cpt_trade_api",
        "source": "cpt",
        "state_dir": str(d),
        "state_present": (d / "state.json").exists(),
        "statuses": list(_statuses()),
        "volume": _volume(),
    }, 200
