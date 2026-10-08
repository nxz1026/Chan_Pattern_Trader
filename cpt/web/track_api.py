"""我的追踪 — HTTP 端点（段 1）。

5 个端点（与 :mod:`cpt.web.trade_api` 同款 GET/POST 风格）：

- ``GET  /api/dashboard/track``                      列表（活跃）+ 回收站
- ``POST /api/dashboard/track/add     {code, note?}`` 加入（幂等，重复加入不报错）
- ``POST /api/dashboard/track/remove  {code}``      软删（90 天回收站可恢复）
- ``POST /api/dashboard/track/restore {code}``      从回收站复活
- ``GET  /api/dashboard/track/{code}/advice``       完整建议（算点 + 持久化快照）
- ``GET  /api/dashboard/track/{code}/history?days=30`` 历史快照

## 用户识别

每个端点由调用方（:func:`cpt.web.app`）从 ``X-CPT-User`` 头提取 ``user_id`` 后传入。
**单用户部署**不传头时 default = ``"default"``。**多用户部署**时鉴权由上游网关负责。

## 失败语义

- 缺 psycopg / DB 不可达：抛（caller 折成 503 + reason）
- 重复加入：返回 200 而非 409（幂等写）
- 软删一个不存在的：返回 204
- 建议点计算失败 / 数据不足：在 ``advice`` 内显式 ``null`` 并写明 reason（**不**抛）

⚠️ DB 抖动是**降级**不是故障：列表端点 catch 后返 ``{available: false, reason: ...}``；
advice 端点 catch 后返 503（advice 拿不到就该告诉用户）—— 两种**不同处置**对应
两种上游含义（"暂时没有" vs "今天没东西可建议"）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from cpt.adapters.a_share_local import AShareLocalClient
from cpt.storage import track_store

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_USER_ID",
    "MAX_USER_ID_LEN",
    "SPEAK_TTL_HOURS",
    "extract_user_id",
    "handle_track_add",
    "handle_track_advice",
    "handle_track_history",
    "handle_track_list",
    "handle_track_maintenance",
    "handle_track_remove",
    "handle_track_restore",
    "handle_track_speak",
    "track_store",
]

#: 缺省用户标识（单用户部署）
DEFAULT_USER_ID: Final[str] = "default"
#: ``user_id`` 长度上限（避免日志/索引被异常长 header 撑爆）
MAX_USER_ID_LEN: Final[int] = 64
#: 「讲人话」缓存有效期（小时）。TTL 内同 ``(user_id, code)`` 不重提 LLM：
#: 由 :func:`cpt.application.llm_cases.summarize_recommendation` 的 ``request_hash``
#: 幂等机制保证（同一 digest 直接返 ``status=duplicate``）。
SPEAK_TTL_HOURS: Final[int] = 6


# ── 用户识别 ──────────────────────────────────────────────────────────


def extract_user_id(headers: Any) -> str:
    """从 ``BaseHTTPRequestHandler.headers`` 提 ``user_id``。

    - 缺 ``X-CPT-User`` 头 → :data:`DEFAULT_USER_ID`
    - 头是空串 / 全空白 / 含非法字符（不是 ``[A-Za-z0-9._-]``）→ 同样 default
    - 超长截断到 :data:`MAX_USER_ID_LEN`
    """
    try:
        raw = headers.get("X-CPT-User", DEFAULT_USER_ID)
    except Exception:  # noqa: BLE001
        return DEFAULT_USER_ID
    if not isinstance(raw, str):
        return DEFAULT_USER_ID
    raw = raw.strip()
    if not raw:
        return DEFAULT_USER_ID
    # 截断
    if len(raw) > MAX_USER_ID_LEN:
        raw = raw[:MAX_USER_ID_LEN]
    # 字符白名单：足够覆盖"用户名"和将来加鉴权时的 sub
    if not all(c.isalnum() or c in "._-" for c in raw):
        return DEFAULT_USER_ID
    return raw


# ── 客户端懒加载（与 a_share_routes / trade_api 同款） ──────────────────


def _client() -> AShareLocalClient:
    return AShareLocalClient()


def _conn() -> Any:
    return _client()._get_conn()  # noqa: SLF001 — 同 trade_api 用法


# ── 列表与回收站 ──────────────────────────────────────────────────────


def handle_track_list(user_id: str) -> tuple[dict[str, Any], int]:
    """活跃 + 回收站一并返。DB 故障时降级 ``available=false``，不抛 500。"""
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            active = track_store.list_active(conn, user_id)
            removed = track_store.list_removed(conn, user_id)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("track list degraded for user=%s: %s", user_id, exc)
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
            "items": [],
            "recycle": [],
        }, 200
    return {
        "available": True,
        "user_id": user_id,
        "items": active,
        "recycle": removed,
    }, 200


# ── 增删 ──────────────────────────────────────────────────────────────


def handle_track_add(user_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """加入。重复加入同一活跃票是 200（幂等）；复活从回收站加同一票也是 200。"""
    code = (body.get("code") or "").strip()
    if not code:
        return {"error": "code required", "field": "code"}, 400
    note = body.get("note")
    if note is not None:
        note = str(note).strip() or None
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            row = track_store.add(conn, user_id, bare, note=note)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track add failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503
    return {"ok": True, "item": row}, 200


def handle_track_remove(user_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """软删。找不到 / 本来就删过 → 204（DELETE 风格的幂等）。"""
    code = (body.get("code") or "").strip()
    if not code:
        return {"error": "code required", "field": "code"}, 400
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            removed = track_store.remove(conn, user_id, bare)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track remove failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503
    return ({"ok": True, "removed": removed}, 200) if removed else ({"ok": True}, 204)


def handle_track_restore(user_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """从回收站复活。回收站里没有 → 404。"""
    code = (body.get("code") or "").strip()
    if not code:
        return {"error": "code required", "field": "code"}, 400
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            restored = track_store.restore(conn, user_id, bare)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track restore failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503
    if not restored:
        return {"error": "not_in_recycle", "code": bare}, 404
    return {"ok": True, "code": bare}, 200


# ── 建议与历史 ──────────────────────────────────────────────────────


def handle_track_advice(user_id: str, code: str) -> tuple[dict[str, Any], int]:
    """完整建议：current + algorithm + suggested_points（含 reference + confirmed）。

    - 先查重：用户没追踪这票 → 404
    - 然后取 snapshot + recommendation + 算点
    - 写一条快照（**advice 端点触发即写**，避免用户刷列表看不到变化）
    - LLM 段**不**调（段 2 才接 UI 上的"再讲一次"按钮）
    """
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400

    # 1. 确认在追踪
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            active = track_store.list_active(conn, user_id)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track advice store read failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503

    if not any(r["code"] == bare for r in active):
        return {"error": "not_tracked", "code": bare}, 404
    note = next((r["note"] for r in active if r["code"] == bare), None)

    # 2. 取快照 + 建议
    from cpt.web import a_share_routes  # noqa: PLC0415

    try:
        snapshot = a_share_routes.snapshot_payload(bare)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "snapshot_unavailable",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }, 502
    try:
        rec = a_share_routes.build_recommendation(bare)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "recommendation_unavailable",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }, 502

    # 3. 算点
    from cpt.application import track_points  # noqa: PLC0415

    try:
        points = track_points.compute_points(snapshot, rec, note=note)
    except Exception as exc:  # noqa: BLE001 — 算点失败不该让 advice 整段炸
        _LOG.exception("track points compute failed code=%s", bare)
        points = {
            "buy": {"reference": None, "confirmed": None},
            "sell": {"reference": None, "confirmed": None},
            "stop_loss_reference": None,
            "reason": f"compute_failed: {type(exc).__name__}",
        }

    payload: dict[str, Any] = {
        "code": bare,
        "as_of": _now_iso(),
        "current": {
            "price": rec.get("price"),
            "raw_close": rec.get("raw_close"),
            "price_ratio": rec.get("price_ratio"),
            "action": rec.get("action"),
            "action_label": rec.get("action_label"),
            "status": rec.get("status"),
            "headline": rec.get("headline"),
            "reason": rec.get("reason"),
        },
        "explain_algorithm": {
            "rule": "一买"
            if rec.get("signal_type") == "first_buy"
            else ("一卖" if rec.get("signal_type") == "first_sell" else "无"),
            "status": rec.get("status"),
            "facts": {
                "has_reversal_bi": _safe_bool(snapshot, "signal", "has_reversal_bi"),
                "divergence_status": rec.get("divergence_status"),
            },
            "triggers_to_confirm": ["反向笔收盘确认"]
            if rec.get("status") == "structure_ready"
            else [],
            "what_would_invalidate": _invalidation_triggers(snapshot, rec),
        },
        "suggested_points": points,
        "human": None,  # 段 2 UI 上"再讲一次"才会调 LLM
        "disclaimer": "结构状态翻译与参考位，不构成投资建议。T+1 持仓层由交易机负责。",
    }

    # 4. 写快照（与 last_current_json 同步）
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            track_store.record_snapshot(
                conn,
                user_id,
                bare,
                payload,
                current_summary=payload["current"],
            )
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        # 快照写失败**不**让 advice 端点 503（advice 本身已算好）。
        # 写日志 + 在 payload 里标 advised=true 但 snapshot_recorded=false。
        _LOG.warning("track advice snapshot write failed user=%s code=%s: %s", user_id, bare, exc)
        payload["snapshot_recorded"] = False
    else:
        payload["snapshot_recorded"] = True

    return payload, 200


def handle_track_speak(user_id: str, code: str) -> tuple[dict[str, Any], int]:
    """「再讲一次人话」—— 调 LLM 重写人话（异步，**立刻返回** ``call_id``）。

    与 :func:`handle_track_advice` 同一前置链（查追踪 → 取数据 → 算 rec），
    但**不**写 ``cpt_track_snapshot``（人话是异步结果，写库要等 worker 回来；
    由前端轮询 LLM 状态拿结果后自行 update UI，不污染确定性快照历史）。

    节流：``SPEAK_TTL_HOURS`` 内同 ``(user_id, code)`` 不重提 LLM。
    实现机制是复用 :func:`cpt.application.llm_cases.summarize_recommendation`
    的 ``request_hash`` 幂等 —— 同一 digest 走 ``status=duplicate`` 分支。

    :returns: ``{"ok": True, "call_id", "status", "reason", "expires_in_hours"}``。
    """
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400

    # 1. 必须在追踪
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            active = track_store.list_active(conn, user_id)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track speak store read failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503

    if not any(r["code"] == bare for r in active):
        return {"error": "not_tracked", "code": bare}, 404
    user_note = next((r.get("note") for r in active if r["code"] == bare), None)

    # 2. 取快照 + 建议（与 advice 同源）
    from cpt.web import a_share_routes  # noqa: PLC0415

    try:
        snapshot = a_share_routes.snapshot_payload(bare)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "snapshot_unavailable",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }, 502
    try:
        rec = a_share_routes.build_recommendation(bare)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "recommendation_unavailable",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }, 502

    # 3. LLM 入队 —— note 作为「用户偏好」段拼进 disclaimer，
    # 让模型语气贴近用户意图，但不参与动作判断（结构确定性结论来自 rec）。
    from cpt.application import llm_cases  # noqa: PLC0415

    note_segment = f"用户偏好：{user_note}。" if user_note else ""
    disclaimer = (
        f"{note_segment}结构状态翻译与参考位，不构成投资建议。"
        "T+1 持仓层由交易机负责。"
    )
    subject_id = f"track:{user_id}:{bare}"

    try:
        audit_conn = _conn()
        try:
            result = llm_cases.summarize_recommendation(
                audit_conn,
                code=bare,
                name=str(snapshot.get("name") or bare),
                action_label=str(rec.get("action_label") or ""),
                headline=str(rec.get("headline") or ""),
                reason=str(rec.get("reason") or ""),
                price=rec.get("price"),
                disclaimer=disclaimer,
                subject_id=subject_id,
            )
        finally:
            audit_conn.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("track speak llm enqueue failed user=%s code=%s", user_id, bare)
        return {
            "error": "llm_unavailable",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }, 503

    return {
        "ok": True,
        "code": bare,
        "call_id": result.get("call_id"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "expires_in_hours": SPEAK_TTL_HOURS,
    }, 200


def handle_track_history(
    user_id: str, code: str, *, days: int = track_store.SNAPSHOT_RETENTION_DAYS
) -> tuple[dict[str, Any], int]:
    """历史快照（默认 30 天）。"""
    try:
        bare = _normalize_code(code)
    except ValueError as exc:
        return {"error": str(exc), "field": "code"}, 400
    days = max(1, min(int(days), track_store.SNAPSHOT_RETENTION_DAYS))
    since = datetime.now(UTC) - timedelta(days=days)
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            snaps = track_store.list_snapshots(conn, user_id, bare, since=since)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        _LOG.exception("track history failed user=%s code=%s", user_id, bare)
        return {"error": "store_unavailable", "detail": str(exc)[:160]}, 503
    return {
        "code": bare,
        "since": since.isoformat(),
        "snapshots": snaps,
    }, 200


def handle_track_maintenance(
    *,
    snapshot_retention_days: int = track_store.SNAPSHOT_RETENTION_DAYS,
    recycle_retention_days: int = track_store.RECYCLE_RETENTION_DAYS,
) -> tuple[dict[str, Any], int]:
    """日常维护：删过期快照 + 删过期回收站。**给 cron / 手动调**，不在用户路径上。

    返回每步的删除行数。
    """
    try:
        conn = _conn()
        try:
            track_store.ensure_table(conn)
            snap_n = track_store.prune_snapshots(conn, snapshot_retention_days)
            rec_n = track_store.prune_removed(conn, recycle_retention_days)
        finally:
            conn.close()
    except track_store.TrackStoreError as exc:
        return {"ok": False, "error": str(exc)[:160]}, 503
    return {
        "ok": True,
        "pruned_snapshots": snap_n,
        "pruned_removed": rec_n,
    }, 200


# ── 内部 ──────────────────────────────────────────────────────────────


def _normalize_code(code: str) -> str:
    """与现有 a_share_routes._normalize 一致；放在这里避免 web / storage 循环依赖。"""
    from cpt.adapters.a_share_public import ASharePublicError, normalize_code  # noqa: PLC0415

    try:
        normalized = normalize_code(code)
    except ASharePublicError as exc:
        raise ValueError(str(exc)) from exc
    # 与 a_share_routes 一致：自选里统一存 6 位裸码
    return normalized[2:] if len(normalized) > 6 else normalized


def _safe_bool(payload: dict[str, Any], *keys: str) -> bool | None:
    cur: Any = payload
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, bool) else None


def _invalidation_triggers(snapshot: dict[str, Any], rec: dict[str, Any]) -> list[str]:
    """根据当前结构给"什么情况下失效"的描述。**人话层面给用户看**，不是触发器。"""
    triggers: list[str] = []
    zhongshus = list((snapshot.get("overlays") or {}).get("zhongshus") or [])
    if zhongshus:
        # 最近一个中枢的下沿：跌破就意味着"结构下移"——失效条件
        last = zhongshus[-1]
        low = last.get("low") if isinstance(last, dict) else getattr(last, "low", None)
        if isinstance(low, (int, float)):
            triggers.append(f"价格跌破最近中枢下沿 {float(low):.2f}（后复权）")
    if rec.get("status") == "invalidated":
        triggers.append("信号已 invalidated（结构已不成立）")
    return triggers


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── 路由注册辅助（给 app.py 用） ──────────────────────────────────────


def route_for(path: str) -> tuple[str, str] | None:
    """把 URL 路径解析成 ``(handler_name, suffix)``，供 :mod:`cpt.web.app` 调度。

    返回 ``None`` 表示该路径不归 ``track`` 管。

    - ``/api/dashboard/track``                    → ``("list", "")``
    - ``/api/dashboard/track/600519/advice``      → ``("advice", "600519")``
    - ``/api/dashboard/track/600519/history``     → ``("history", "600519")``
    - ``/api/dashboard/track/maintenance``       → ``("maintenance", "")``
    """
    p = urlsplit(path).path
    parts = [seg for seg in p.split("/") if seg]
    if not parts or parts[0] != "api":
        return None
    if len(parts) < 3 or parts[1] != "dashboard" or parts[2] != "track":
        return None
    # /api/dashboard/track[/...]
    if len(parts) == 3:
        return ("list", "")
    if len(parts) == 4 and parts[3] == "maintenance":
        return ("maintenance", "")
    if len(parts) == 5 and parts[4] in ("advice", "history"):
        return (parts[4], parts[3])
    return None
