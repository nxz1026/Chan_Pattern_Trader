"""advice 端点回填人话 + 「再讲一次」时间桶 —— 纯逻辑测试。

## 覆盖

1. ``handle_track_advice`` 把最近一条 ``status=ok`` 的 ``result_text`` 回填进 ``human``
2. 最新一行不是 ok（error / queued）时**继续往下找**，别把失败行当人话
3. 缓存读失败 → ``human=None`` 但 advice 照常 200（旁路**不得**拖垮主链路）
4. ``human`` 结构：``text / call_id / model / purpose / generated_at / generated_at_iso``
5. ``_ms_to_iso`` 坏值返 ``None``（不能抛）
6. ``_speak_bucket`` 同窗口稳定、跨窗口变化、宽度 = :data:`SPEAK_TTL_HOURS`

## 为什么这些用例必须存在

R52 之前 ``handle_track_advice`` 把 ``human`` 写死 ``None``，而人话**已经**
生成过并躺在 ``cpt_llm_call`` 里 —— 页面永远显示「暂无人话」。
第 1/2 条把这个契约钉死；第 3 条钉死「读缓存是旁路」这一降级语义。

不连真 DB：``_conn`` / ``recent_calls`` / ``track_store`` / ``a_share_routes`` 全部 mock。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cpt.web import track_api

# ── mock 工具 ─────────────────────────────────────────────────────


class _Conn:
    def close(self) -> None:
        pass


def _row(
    *,
    call_id: str,
    status: str,
    result_text: str | None,
    created_at: int = 1_791_460_442_658,
    finished_at: int | None = 1_791_460_449_826,
    model: str = "agnes-3.0-flash",
) -> dict[str, Any]:
    """一条 ``cpt_llm_call`` 行（键名与 ``_COLUMNS`` 对齐）。"""
    return {
        "call_id": call_id,
        "purpose": "summarize_recommendation",
        "subject_id": "track:u:000002",
        "status": status,
        "request_hash": "deadbeef",
        "result_text": result_text,
        "error_text": None,
        "model": model,
        "prompt_tokens": 310,
        "completion_tokens": 55,
        "created_at": created_at,
        "finished_at": finished_at,
    }


def _capture_recent(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> Any:
    def fake_recent(conn: Any, *, limit: int = 20, subject_id: str | None = None) -> Any:
        calls.append({"limit": limit, "subject_id": subject_id})
        return tuple(rows)

    return fake_recent


def _patch_advice_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_rows: list[dict[str, Any]] | None = None,
    recent: Any = None,
) -> dict[str, Any]:
    """把 advice 端点用到的全部外部依赖换掉。"""
    captured: dict[str, Any] = {"recent_calls": [], "snapshot": None}

    def fake_ensure(conn: Any) -> None:
        return None

    def fake_list_active(conn: Any, user_id: str) -> list[dict[str, Any]]:
        return active_rows if active_rows is not None else [{"code": "000002", "note": None}]

    def fake_record(conn: Any, user_id: str, code: str, payload: Any, **kw: Any) -> None:
        captured["snapshot"] = payload

    monkeypatch.setattr(track_api.track_store, "ensure_table", fake_ensure)
    monkeypatch.setattr(track_api.track_store, "list_active", fake_list_active)
    monkeypatch.setattr(track_api.track_store, "record_snapshot", fake_record)
    monkeypatch.setattr(track_api, "_conn", lambda: _Conn())

    monkeypatch.setattr(
        "cpt.web.a_share_routes.snapshot_payload",
        lambda code: {
            "name": "贵州茅台",
            "overlays": {"zhongshus": [], "bis": []},
            "signal": {"has_reversal_bi": False},
        },
    )
    monkeypatch.setattr(
        "cpt.web.a_share_routes.build_recommendation",
        lambda code: {
            "price": 1500.0,
            "raw_close": 1500.0,
            "price_ratio": 1.0,
            "action": "watch",
            "action_label": "观望",
            "status": "structure_ready",
            "headline": "结构就绪",
            "reason": "中枢已形成",
            "signal_type": "structure_ready",
        },
    )
    if recent is not None:
        monkeypatch.setattr("cpt.storage.llm_call_store.recent_calls", recent)
    return captured


# ── advice 回填 human ─────────────────────────────────────────────


def test_advice_fills_human_from_ok_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """最近一条 ok 的 result_text 必须出现在 advice 的 human 里。"""
    text = "000002 当前结构判断为观望，因为之前的一买信号已经失效。不构成投资建议。"
    calls: list[dict[str, Any]] = []
    _patch_advice_deps(
        monkeypatch,
        recent=_capture_recent(
            [_row(call_id="call-ok", status="ok", result_text=text)], calls
        ),
    )

    payload, status = track_api.handle_track_advice("u", "000002")

    assert status == 200
    human = payload["human"]
    assert human is not None, "库里有人话，advice 就不能再回 null"
    assert human["text"] == text
    assert human["call_id"] == "call-ok"
    assert human["model"] == "agnes-3.0-flash"
    assert human["purpose"] == "summarize_recommendation"
    # 毫秒（与 app.py 时间轴口径一致）+ ISO 双份
    assert human["generated_at"] == 1_791_460_449_826
    assert human["generated_at_iso"] == "2026-10-08T11:54:09Z"
    # 查询键必须是**完整** subject（后端精确匹配）
    assert calls == [{"limit": 20, "subject_id": "track:u:000002"}]


def test_advice_human_skips_newer_non_ok_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """最新行是 error / running 时往下找 —— 不能把失败行当人话。"""
    calls: list[dict[str, Any]] = []
    _patch_advice_deps(
        monkeypatch,
        recent=_capture_recent(
            [
                _row(call_id="call-bad", status="error", result_text=None),
                _row(call_id="call-run", status="running", result_text=None),
                _row(call_id="call-ok", status="ok", result_text="上一版人话。"),
            ],
            calls,
        ),
    )

    payload, status = track_api.handle_track_advice("u", "000002")

    assert status == 200
    assert payload["human"]["call_id"] == "call-ok"
    assert payload["human"]["text"] == "上一版人话。"


def test_advice_human_skips_ok_row_with_blank_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """ok 但 result_text 是空白 → 不算人话，继续找。"""
    _patch_advice_deps(
        monkeypatch,
        recent=_capture_recent(
            [
                _row(call_id="call-blank", status="ok", result_text="   \n "),
                _row(call_id="call-ok", status="ok", result_text="真正的人话。"),
            ],
            [],
        ),
    )

    payload, status = track_api.handle_track_advice("u", "000002")

    assert status == 200
    assert payload["human"]["call_id"] == "call-ok"


def test_advice_human_is_none_when_cache_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 旁路降级：读缓存炸了，advice 必须照常 200，``human=None``。"""
    from cpt.storage.llm_call_store import LLMCallError

    def boom(conn: Any, **kw: Any) -> Any:
        raise LLMCallError("库读不到")

    captured = _patch_advice_deps(monkeypatch, recent=boom)

    payload, status = track_api.handle_track_advice("u", "000002")

    assert status == 200
    assert payload["human"] is None
    assert payload["code"] == "000002"
    # 主链路照常走完（快照也写了）
    assert payload["snapshot_recorded"] is True
    assert captured["snapshot"] is not None


def test_advice_human_is_none_when_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_advice_deps(monkeypatch, recent=_capture_recent([], []))
    payload, status = track_api.handle_track_advice("u", "000002")
    assert status == 200
    assert payload["human"] is None


def test_latest_human_falls_back_to_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """``finished_at`` 为空（还在跑但被判 ok 的边界）时用 ``created_at``。"""
    _patch_advice_deps(
        monkeypatch,
        recent=_capture_recent(
            [
                _row(
                    call_id="call-ok",
                    status="ok",
                    result_text="人话。",
                    created_at=1_791_460_442_658,
                    finished_at=None,
                )
            ],
            [],
        ),
    )
    payload, status = track_api.handle_track_advice("u", "000002")
    assert status == 200
    assert payload["human"]["generated_at"] == 1_791_460_442_658


# ── 时间桶 ────────────────────────────────────────────────────────


def test_ms_to_iso_rejects_garbage() -> None:
    assert track_api._ms_to_iso(None) is None
    assert track_api._ms_to_iso("x") is None
    assert track_api._ms_to_iso(True) is None
    assert track_api._ms_to_iso(1_791_460_449_826) == "2026-10-08T11:54:09Z"


def test_speak_bucket_is_stable_within_window_and_changes_across() -> None:
    """同桶稳定（→ duplicate 读缓存）、跨桶变化（→ 真调 LLM）。"""
    base = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    width = track_api.SPEAK_TTL_HOURS
    assert track_api.SPEAK_TTL_HOURS == 6  # 对外契约

    assert track_api._speak_bucket(base) == track_api._speak_bucket(
        base + timedelta(hours=width - 1, minutes=59)
    )
    assert track_api._speak_bucket(base) != track_api._speak_bucket(
        base + timedelta(hours=width)
    )
    # 桶起点可读（排查时能直接对上库里的提示词）
    assert track_api._speak_bucket(base).endswith("Z")
    assert track_api._speak_bucket(base) == "2026-10-08T12:00:00Z"


def test_speak_bucket_matches_request_hash_partition() -> None:
    """桶进提示词 → hash 变 → 唯一索引放行。这里直接验 hash 层的等价性。"""
    from cpt.llm.prompts import summarize_request
    from cpt.storage.llm_call_store import request_hash

    def digest(bucket: str) -> str:
        req = summarize_request(
            code="000002",
            name="贵州茅台",
            action_label="观望",
            headline="结构就绪",
            reason="中枢已形成",
            price=1500.0,
            disclaimer="不构成投资建议。",
            subject_id="track:u:000002",
            cache_bucket=bucket,
        )
        return request_hash(req.purpose, req.system, req.user)

    a = track_api._speak_bucket(datetime(2026, 10, 8, 6, 30, tzinfo=UTC))
    b = track_api._speak_bucket(datetime(2026, 10, 8, 13, 30, tzinfo=UTC))
    assert a != b
    assert digest(a) == digest(a)
    assert digest(a) != digest(b)


def test_summarize_request_without_bucket_keeps_prompt_unchanged() -> None:
    """主看板不传桶 → 提示词与 R45 完全一致（向后兼容，不能多出系统标记）。"""
    from cpt.llm.prompts import summarize_request

    req = summarize_request(
        code="000002",
        name="贵州茅台",
        action_label="观望",
        headline="结构就绪",
        reason="中枢已形成",
        price=1500.0,
        disclaimer="不构成投资建议。",
    )
    assert "cache-bucket" not in req.user
    assert req.user.endswith("附注：不构成投资建议。")
