"""D 类待接线模块的后端接线集成测试（契约 C1–C6 + C7 + 守门用例）。

入口一律走**生产构造**：``cpt.web.app`` 的真 HTTP handler（``tests.conftest.served``）
与 ``cpt.web.__main__`` 的真 provider。原因见 ``docs/pending-wiring.md`` §约束 2/4 ——
直接调模块函数的用例在「传参错、根本没接线」时照样全绿，那种测试不算守门。

末条 ``test_production_entrypoint_calls_wired_projections`` 是**故意会因忘接线变红**的：
它 monkeypatch 三个**使用点**（不是源模块），只要有人摘掉调用，spy 就收不到。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

import pytest
from cpt.application.dashboard_runs import clear_runs, record_run
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar
from cpt.web.__main__ import _FixtureProvider

from tests.conftest import served

# ------------------------------------------------------------------ 基础工具


def _request(url: str, method: str = "GET") -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, exc.read()[:200]


def _provider(limit: int = 120) -> _FixtureProvider:
    return _FixtureProvider(symbol="BTCUSDT", interval="1h", limit=limit)


@pytest.fixture(autouse=True)
def _isolated_run_ring() -> Any:
    """运行历史是**进程级**状态：不清会串到别的用例里去。"""
    clear_runs()
    yield
    clear_runs()


def _bar(index: int, closed: bool = True):
    return make_canonical_bar(
        open_time=index * 300000,
        open=10.0,
        high=12.0 + index,
        low=8.0 + index,
        close=11.0 + index,
        close_time=index * 300000 + 299999,
        is_closed=closed,
    )


def _fake_run(run_id: str, *, candles: int = 300, base: float = 1.0) -> dict[str, Any]:
    """一份能进环形缓冲的合成快照：字段足够 ``build_run_index`` 与 compare/multi-run 使用。"""
    return {
        "schema_version": "dashboard.v2",
        "market": {"symbol": f"SYM-{run_id}", "bar_count": candles, "last_price": base},
        "candles": [
            {"open_time": 1_700_000_000_000 + i * 60_000, "close": base + i} for i in range(candles)
        ],
        "overlays": {
            # id 带上 run_id：overlays 是「dict 里套 4 个 list」，只有内容不同才会
            # 进入 diff，从而验证递归摘要化（只判顶层 list 的实现会从这儿漏数组）。
            "fractals": [{"id": f"{run_id}-f{i}", "level": 1} for i in range(5)],
            "bis": [],
            "zhongshus": [],
            "trend_types": [],
        },
        "signal": None,
        "events": [],
        "data_quality": {"stale": False, "gap": False},
        "runtime": {
            "mode": "research",
            "status": "confirmed",
            "data_source": "test",
            # run_id 必须显式给：build_run_index 取 `runtime.run_id or dataset_hash`，
            # 不给就会退化成 dataset_hash，用例里的 "run-a" 将查不到本体。
            "run_id": run_id,
        },
        "reproducibility": {"dataset_hash": f"hash-{run_id}"},
    }


def _all_strings(value: Any) -> list[str]:
    """递归收集所有字符串，用来断言响应里不存在超长巨串。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _all_strings(entry)]
    if isinstance(value, (list, tuple)):
        return [item for entry in value for item in _all_strings(entry)]
    return []


# ------------------------------------------------------------------ C1 export


def test_export_route_slices_requested_range() -> None:
    provider = _provider()
    bars = provider._bars  # noqa: SLF001 — 取真实 K 线时间轴
    start_ms = int(bars[10].open_time)
    end_ms = int(bars[40].open_time)
    with served(provider) as base:
        status, body = _request(f"{base}/api/dashboard/export?start_ms={start_ms}&end_ms={end_ms}")
    assert status == 200
    assert body["schema_version"] == "dashboard_export.v1"
    assert body["available"] is True
    # R32：原来是全等断言 ``slice == {start_ms, end_ms}``。现在信封里多带了
    # 「切了什么 / 没切什么」的声明（纯新增，start_ms/end_ms 语义不变），
    # 所以改成**分别**校验原有两键 —— 不能因为加字段就把原契约放掉。
    assert body["slice"]["start_ms"] == start_ms
    assert body["slice"]["end_ms"] == end_ms
    assert body["slice"]["sliced_blocks"] == ["candles", "market"]
    assert body["slice"]["candle_count"] == 31
    assert body["slice"]["source_bar_count"] == 120
    assert "bar_index" in body["slice"]["unsliced_blocks_note"]
    assert body["candle_count"] == 31  # 闭区间 [10, 40]
    assert len(body["snapshot"]["candles"]) == 31
    # 切片不得改动源快照：market.bar_count 被重算成窗口根数
    assert body["snapshot"]["market"]["bar_count"] == 31
    # R32：market 的时间跨度也必须跟着切片走，不能还挂着完整窗口的跨度
    assert body["snapshot"]["market"]["first_open_time"] == start_ms
    assert body["snapshot"]["market"]["last_open_time"] == end_ms


def test_export_route_rejects_bad_range() -> None:
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/export?start_ms=abc&end_ms=10")
        assert status == 400
        assert body["error"]["code"] == "invalid_range"

        status, body = _request(f"{base}/api/dashboard/export?start_ms=200&end_ms=100")
        assert status == 400
        assert body["error"]["code"] == "invalid_range"

        status, _ = _request(f"{base}/api/dashboard/export?start_ms=1")
        assert status == 400  # 缺参 = 非整数


def test_export_route_degrades_when_snapshot_missing() -> None:
    """空快照 ⇒ available:false + 机器可读 reason，不许 500、不许伪造 candles。"""
    with served(lambda: {}) as base:
        status, body = _request(f"{base}/api/dashboard/export?start_ms=1&end_ms=2")
    assert status == 200
    assert body == {
        "schema_version": "dashboard_export.v1",
        "available": False,
        "reason": "snapshot_unavailable",
    }


# ------------------------------------------------------------------ C2 levels


def test_levels_route_returns_tree_in_fixture_mode() -> None:
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/levels")
    assert status == 200
    assert body["schema_version"] == "dashboard_levels.v1"
    assert body["available"] is True
    assert body["levels"], "fixture 快照有分型/笔/中枢，级别树不该为空"
    levels = {row["level"] for row in body["levels"]}
    assert levels, "级别键必须来自结构自带的 level 字段"
    for row in body["levels"]:
        assert set(row) == {"level", "parent_level", "elements"}


def test_levels_route_degrades_without_overlays() -> None:
    with served(lambda: {"market": {"symbol": "BTCUSDT"}}) as base:
        status, body = _request(f"{base}/api/dashboard/levels")
    assert status == 200
    assert body == {
        "schema_version": "dashboard_levels.v1",
        "available": False,
        "reason": "overlays_unavailable",
    }


# ------------------------------------------------------------------ C3 compare


def test_compare_route_summarizes_sequence_differences() -> None:
    record_run(_fake_run("run-a", base=1.0))
    record_run(_fake_run("run-b", base=2.0))
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/compare?left=run-a&right=run-b")
    assert status == 200
    assert body["schema_version"] == "dashboard_compare.v1"
    assert body["available"] is True
    assert body["left_dataset_hash"] == "hash-run-a"
    assert body["right_dataset_hash"] == "hash-run-b"

    by_field = {entry["field"]: entry for entry in body["differences"]}
    assert "candles" in by_field, "两份快照的 K 线不同，必须出现在 diff 里"

    # 序列型字段必须降级成 count + hash，而不是整段原始数组。
    summary = by_field["candles"]["left"]
    assert summary["__summary__"] == "candles"
    assert summary["count"] == 300
    assert re.fullmatch(r"[0-9a-f]{12}", summary["hash"])
    assert isinstance(by_field["candles"]["right"], dict)

    # 顶层 dict（overlays/market）里的 list 也必须递归摘要化——
    # overlays 是「dict 里套 4 个 list」，只判顶层 list 会从这儿漏出去。
    assert isinstance(by_field["overlays"]["left"], dict)
    assert isinstance(by_field["overlays"]["left"]["fractals"], dict)
    assert by_field["overlays"]["left"]["fractals"]["count"] == 5

    # 硬门槛：任何字符串都不允许长到前端会卡死的程度。
    longest = max((len(text) for text in _all_strings(body["differences"])), default=0)
    assert longest < 1000, f"diff 里出现超长字符串（{longest} 字符），摘要化没生效"
    assert len(json.dumps(body["differences"], ensure_ascii=False)) < 6000


def test_compare_route_requires_ids_and_degrades_on_unknown_body() -> None:
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/compare?left=only-one")
        assert status == 400
        assert body["error"]["code"] == "missing_run_id"

        status, body = _request(f"{base}/api/dashboard/compare?left=nope&right=nope2")
        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "run_body_unavailable"


# ------------------------------------------------------------- C4 multi-run


def test_multi_run_route_aligns_runs_by_open_time() -> None:
    record_run(_fake_run("m1", candles=5))
    record_run(_fake_run("m2", candles=5))
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/multi-run?run_ids=m1,m2")
    assert status == 200
    assert body["schema_version"] == "dashboard_multi_run.v1"
    assert body["available"] is True
    assert body["run_count"] == 2
    assert len(body["timestamps"]) == 5
    assert body["points"][0]["open_time"] == body["timestamps"][0]
    assert {"run_0", "run_1"} <= set(body["points"][0])


def test_multi_run_route_validates_ids_and_degrades() -> None:
    with served(_provider()) as base:
        for raw in ("only-one", "a,b,c,d,e,f", ""):
            status, body = _request(f"{base}/api/dashboard/multi-run?run_ids={raw}")
            assert status == 400, raw
            assert body["error"]["code"] == "invalid_run_ids"

        status, body = _request(f"{base}/api/dashboard/multi-run?run_ids=x1,x2")
        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "run_body_unavailable"


# ---------------------------------------------------------- C5 signal-stats


class _StubLocalClient:
    """AShareLocalClient 的 duck type：本用例只验证路由，不碰真库。"""

    def _get_conn(self) -> object:
        return object()

    def close(self) -> None:
        pass


def test_signal_stats_route_reports_basis_and_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    import cpt.adapters.a_share_local as local
    import cpt.storage.signal_event_store as store

    calls: list[dict[str, Any]] = []

    def fake_loader(conn: object, *, days: int = 30, code: str | None = None):
        calls.append({"conn": conn, "days": days, "code": code})
        return (
            {"signal_id": "s1", "code": "600519", "status": "confirmed", "transition_time": 1},
            {"signal_id": "s2", "code": "600519", "status": "alert", "transition_time": 2},
            {"signal_id": "s3", "code": "600519", "status": "invalidated", "transition_time": 3},
        )

    monkeypatch.setattr(local, "AShareLocalClient", _StubLocalClient)
    monkeypatch.setattr(store, "load_signal_events", fake_loader)

    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/signal-stats?days=7&code=600519")

    assert status == 200
    assert body["schema_version"] == "dashboard_signal_stats.v1"
    assert body["available"] is True
    # basis 必须自报口径：这是「状态跃迁事件分布」，不是「当前若干只票的快照」。
    assert body["basis"] == "signal_event_transitions"
    assert body["days"] == 7
    assert body["stats"]["total"] == 3
    assert body["stats"]["status_counts"] == {"confirmed": 1, "alert": 1, "invalidated": 1}
    assert body["stats"]["invalidated_count"] == 1
    assert calls and calls[0]["days"] == 7 and calls[0]["code"] == "600519"


def test_signal_stats_route_degrades_when_history_unavailable() -> None:
    """不 patch：本机/CI 都取不到事件表 ⇒ 必须是 available:false，绝不 500、绝不伪造 0。"""
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/signal-stats?days=30")
    assert status == 200
    assert body["schema_version"] == "dashboard_signal_stats.v1"
    assert body["available"] is False
    assert body["reason"] == "signal_history_unavailable"
    assert body["basis"] == "signal_event_transitions"
    assert "stats" not in body


def test_signal_stats_route_rejects_non_integer_days() -> None:
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/signal-stats?days=thirty")
    assert status == 400
    assert body["error"]["code"] == "invalid_days"


# ------------------------------------------------------------- C6 watchlist


def test_watchlist_route_maps_pool_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import cpt.web.a_share_routes as routes
    import cpt.web.app as web_app

    monkeypatch.setattr(
        routes,
        "pool_payload",
        lambda **_: {
            "schema_version": "a_share_pool.v2",
            "as_of": "2026-09-30",
            "db_error": None,
            "items": [
                {"code": "600519", "drawable": True},
                {"code": "000001", "drawable": False},
            ],
        },
    )
    monkeypatch.setattr(
        routes,
        "recent_closes",
        lambda code: {"600519": (10.0, 11.0)}.get(code),
    )
    monkeypatch.setattr(web_app, "_latest_signal_statuses", lambda: {"600519": "candidate"})

    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/watchlist")

    assert status == 200
    assert body["schema_version"] == "dashboard_watchlist.v1"
    assert body["available"] is True
    assert body["as_of"] == "2026-09-30"
    rows = {row["symbol"]: row for row in body["rows"]}
    assert rows["600519"]["last_price"] == 11.0
    assert rows["600519"]["change_pct"] == pytest.approx(10.0)
    assert rows["600519"]["signal_status"] == "candidate"
    assert rows["600519"]["alert"] is True  # candidate 属于「警示中」
    assert rows["600519"]["available"] is True
    # 取不到价的票：诚实的 None，且不拖垮整个列表
    assert rows["000001"]["last_price"] is None
    assert rows["000001"]["change_pct"] is None
    assert rows["000001"]["available"] is False


def test_watchlist_route_degrades_when_pool_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import cpt.web.a_share_routes as routes

    def boom(**_: Any) -> dict[str, Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(routes, "pool_payload", boom)
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/watchlist")
    assert status == 200
    assert body == {
        "schema_version": "dashboard_watchlist.v1",
        "available": False,
        "reason": "pool_unavailable",
    }

    # 库连不上但没条目 ⇒ 同样是「不可用」，不能伪装成「今天确实没票」。
    monkeypatch.setattr(
        routes, "pool_payload", lambda **_: {"items": [], "db_error": "OperationalError: down"}
    )
    with served(_provider()) as base:
        status, body = _request(f"{base}/api/dashboard/watchlist")
    assert status == 200
    assert body["available"] is False
    assert body["reason"] == "pool_unavailable"


# ------------------------------------------------- 守门：接线本身必须被调用


def test_production_entrypoint_calls_wired_projections(monkeypatch: pytest.MonkeyPatch) -> None:
    """**忘接线就会红**：spy 挂在三个**使用点**上，摘掉调用即 assert 失败。

    挂使用点而不是源模块（``dashboard_quality`` / ``dashboard_watch`` / ``dashboard_levels``）：
    源模块里换掉函数体不会影响已经 ``from ... import`` 绑定的引用，那种探针测不出「没接」。
    """
    import cpt.application.dashboard as dashboard_mod
    import cpt.application.dashboard_levels as levels_mod
    import cpt.application.dashboard_quality as quality_mod
    import cpt.application.dashboard_snapshot_v2 as v2_mod
    import cpt.application.dashboard_watch as watch_mod

    calls: list[str] = []

    def spy(name: str, real: Any):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(dashboard_mod, "quality_report", spy("quality", quality_mod.quality_report))
    monkeypatch.setattr(v2_mod, "watch_metrics", spy("watch", watch_mod.watch_metrics))
    monkeypatch.setattr(v2_mod, "level_tree", spy("levels", levels_mod.level_tree))

    snapshot = build_dashboard_snapshot_v2(
        RulesConfig(), [_bar(0), _bar(1)], fractals=(), bis=(), zhongshus=(), trend_types=()
    )
    assert calls.count("quality") == 1
    assert calls.count("watch") == 1
    # 四类结构全空时 `_level_tree_payload` 直接回 overlays_unavailable（诚实降级），
    # 不调 level_tree —— 下面用真 provider 证明确有结构时它会被调。
    assert calls.count("levels") == 0
    assert set(snapshot) >= {"data_quality", "watch_metrics", "level_tree"}
    assert snapshot["level_tree"] == {"available": False, "reason": "overlays_unavailable"}

    # 同一条链路的**真 provider**（HTTP 服务真实使用的那个）也必须走到。
    calls.clear()
    live = _provider(limit=60).snapshot_payload()
    assert set(calls) == {"quality", "watch", "levels"}
    assert {"severity", "gap_count", "out_of_order_count", "gaps", "out_of_order"} <= set(
        live["data_quality"]
    )
    assert live["watch_metrics"]["available"] is True
    assert live["level_tree"]["available"] is True
