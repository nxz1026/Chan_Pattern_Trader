"""``cpt.web.track_api.handle_track_speak`` 纯逻辑测试。

## 覆盖

1. 用户未追踪该 code → 404 ``{"error": "not_tracked"}``
2. 用户追踪中、note 为空 → 调 LLM 时 ``disclaimer`` 不带「用户偏好」段
3. 用户追踪中、note 非空 → 调 LLM 时 ``disclaimer`` 含「用户偏好：<note>。」段
4. LLM 入队失败 → 503 ``{"error": "llm_unavailable"}``
5. LLM 入队返回 ``available=False, status=duplicate`` → 仍 200，
   ``status`` 字段透传，便于前端判「6h 内已写过」并直接显示上次 human

不连真 DB、不发真 SIG——所有外部依赖通过 monkeypatch 替换。
"""

from __future__ import annotations

from typing import Any

import pytest
from cpt.web import track_api

# ── mock 工具 ────────────────────────────────────────────────────


class _Conn:
    def close(self) -> None:
        pass


def _patch_db_h(monkeypatch: pytest.MonkeyPatch, active_rows: list[dict[str, Any]]) -> None:
    """mock track_store.ensure_table + list_active。"""

    def fake_ensure(conn: Any) -> None:
        return None

    def fake_list_active(conn: Any, user_id: str) -> list[dict[str, Any]]:
        return active_rows

    def fake_conn() -> _Conn:
        return _Conn()

    monkeypatch.setattr(track_api.track_store, "ensure_table", fake_ensure)
    monkeypatch.setattr(track_api.track_store, "list_active", fake_list_active)
    monkeypatch.setattr(track_api, "_conn", fake_conn)


def _patch_snapshot_rec(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: dict[str, Any] | None = None,
    rec: dict[str, Any] | None = None,
    snapshot_exc: Exception | None = None,
    rec_exc: Exception | None = None,
) -> None:
    """mock a_share_routes 的两个函数（speak handler 延迟导入）。"""
    if snapshot_exc is not None:

        def fake_snapshot(code: str) -> Any:
            raise snapshot_exc
    else:
        def fake_snapshot(code: str) -> dict[str, Any]:
            return snapshot or {
                "name": "贵州茅台",
                "overlays": {"zhongshus": [], "bis": []},
                "signal": {"has_reversal_bi": False},
            }

    if rec_exc is not None:

        def fake_rec(code: str) -> Any:
            raise rec_exc
    else:
        def fake_rec(code: str) -> dict[str, Any]:
            return rec or {
                "price": 1500.0,
                "raw_close": 1500.0,
                "price_ratio": 1.0,
                "action": "watch",
                "action_label": "观望",
                "status": "structure_ready",
                "headline": "结构就绪",
                "reason": "中枢已形成",
                "signal_type": "structure_ready",
            }

    monkeypatch.setattr("cpt.web.a_share_routes.snapshot_payload", fake_snapshot)
    monkeypatch.setattr("cpt.web.a_share_routes.build_recommendation", fake_rec)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    """mock llm_cases.summarize_recommendation。"""
    import cpt.application.llm_cases as cases

    def fake_summarize(conn: Any, **kwargs: Any) -> dict[str, Any]:
        # 透传 kwargs 让我们能断言行
        fake_summarize.last_kwargs = kwargs  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr(cases, "summarize_recommendation", fake_summarize)


# ── 测试 ──────────────────────────────────────────────────────────


def test_speak_404_when_code_not_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db_h(monkeypatch, active_rows=[{"code": "000002", "note": None}])
    _patch_snapshot_rec(monkeypatch)
    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 404
    assert payload == {"error": "not_tracked", "code": "600519"}


def test_speak_passes_note_into_disclaimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db_h(monkeypatch, active_rows=[{"code": "600519", "note": "长期持有"}])
    _patch_snapshot_rec(monkeypatch)
    _patch_llm(
        monkeypatch,
        {
            "available": True,
            "call_id": "call-1",
            "status": "queued",
            "reason": "",
        },
    )
    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 200
    assert payload["ok"] is True
    assert payload["call_id"] == "call-1"
    # 关键断言：note 进 disclaimer
    import cpt.application.llm_cases as cases
    last = cases.summarize_recommendation.last_kwargs  # type: ignore[attr-defined]
    assert "长期持有" in last["disclaimer"]
    assert last["subject_id"] == "track:u:600519"


def test_speak_without_note_omits_preference_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db_h(monkeypatch, active_rows=[{"code": "600519", "note": None}])
    _patch_snapshot_rec(monkeypatch)
    _patch_llm(
        monkeypatch,
        {
            "available": True,
            "call_id": "call-2",
            "status": "queued",
            "reason": "",
        },
    )
    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 200
    import cpt.application.llm_cases as cases
    last = cases.summarize_recommendation.last_kwargs  # type: ignore[attr-defined]
    assert "用户偏好" not in last["disclaimer"]
    assert "结构状态翻译" in last["disclaimer"]


def test_speak_503_when_llm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db_h(monkeypatch, active_rows=[{"code": "600519", "note": None}])
    _patch_snapshot_rec(monkeypatch)

    import cpt.application.llm_cases as cases

    def boom(conn: Any, **kwargs: Any) -> Any:
        raise RuntimeError("queue down")

    monkeypatch.setattr(cases, "summarize_recommendation", boom)

    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 503
    assert payload["error"] == "llm_unavailable"


def test_speak_passes_through_duplicate_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6h 内同 (user_id, code) 重提应拿 duplicate —— status 字段透传 200。"""
    _patch_db_h(monkeypatch, active_rows=[{"code": "600519", "note": "长期持有"}])
    _patch_snapshot_rec(monkeypatch)
    _patch_llm(
        monkeypatch,
        {
            "available": False,
            "call_id": "call-old",
            "status": "duplicate",
            "reason": "same_request_in_flight_or_done",
        },
    )
    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 200
    assert payload["status"] == "duplicate"
    assert payload["call_id"] == "call-old"
    # 绝对值：6h 是文档与前端的对外契约（mutate 时必须改文档）
    assert payload["expires_in_hours"] == 6


def test_speak_400_on_invalid_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db_h(monkeypatch, active_rows=[])
    payload, status = track_api.handle_track_speak("u", "abc")
    assert status == 400
    assert "error" in payload


def test_speak_passes_cache_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ R52：真 6h 窗口靠 ``cache_bucket`` 进提示词改变 ``request_hash``。

    不传的话幂等键与提示词无关地**永久**相同，"再讲一次人话"永远只会拿回旧缓存
    （R45 的文档承诺是 6h，实现却是永久）。
    """
    _patch_db_h(monkeypatch, active_rows=[{"code": "600519", "note": None}])
    _patch_snapshot_rec(monkeypatch)
    _patch_llm(
        monkeypatch,
        {"available": True, "call_id": "call-3", "status": "queued", "reason": ""},
    )
    payload, status = track_api.handle_track_speak("u", "600519")
    assert status == 200

    import cpt.application.llm_cases as cases

    last = cases.summarize_recommendation.last_kwargs  # type: ignore[attr-defined]
    assert last["cache_bucket"] == track_api._speak_bucket()
    # 桶宽度 = 对外承诺的 TTL
    assert track_api.SPEAK_TTL_HOURS == 6
