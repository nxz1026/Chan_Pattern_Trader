"""数据源能力注册表与 ``/api/dashboard/sources`` 测试（R17）。

核心断言是**纪律**而不是功能：
- Wind 默认必须被标成 ``skipped``（一次探测 = 一次真实额度）；
- 任何源的失败都必须折叠成 ``status``，不能让 ``/api/dashboard/sources`` 500；
- 探活结果有缓存，别把看板刷成 DDoS。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from cpt.adapters import source_registry
from cpt.adapters.source_registry import (
    SOURCES,
    ProbeResult,
    capabilities_payload,
    clear_cache,
    probe_all,
    probe_source,
)

from tests.conftest import served


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _offline_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """**禁止真实网络**：把所有探针替换成瞬时假实现。

    真实探针会打 Binance / 腾讯 / 新浪 / 本地库，CI 无网会挂住十几秒；
    需要断言真实行为的用例在自己的 monkeypatch 里覆盖对应项即可。
    """
    for source_id in (
        "binance_futures",
        "ccxt",
        "wind",
        "tencent_kline",
        "sina_quote",
        "a_share_local",
    ):
        monkeypatch.setitem(
            source_registry._PROBES,  # noqa: SLF001
            source_id,
            lambda _quota, _sid=source_id: {"status": "ok", "stub": _sid},
        )


def test_declarations_are_unique_and_complete() -> None:
    ids = [source.id for source in SOURCES]
    assert len(ids) == len(set(ids)), f"数据源 id 重复：{ids}"
    for source in SOURCES:
        assert source.markets, f"{source.id} 未声明市场"
        assert source.provides, f"{source.id} 未声明能力"
        assert source.role in {"primary", "fallback", "local"}, source.role
        assert source.quota in {"none", "wind"}, source.quota


def test_wind_is_the_only_quota_source_and_is_a_share_primary() -> None:
    quota_sources = [source.id for source in SOURCES if source.quota != "none"]
    assert quota_sources == ["wind"]
    wind = next(source for source in SOURCES if source.id == "wind")
    assert wind.role == "primary"
    assert "a_share" in wind.markets


def test_crypto_has_a_dependency_free_primary_and_a_ccxt_fallback() -> None:
    crypto = [source for source in SOURCES if "crypto" in source.markets]
    primaries = [source for source in crypto if source.role == "primary"]
    assert [source.id for source in primaries] == ["binance_futures"]
    assert primaries[0].requires == (), "主通道必须零第三方依赖"
    fallback = next(source for source in crypto if source.id == "ccxt")
    assert fallback.requires == ("ccxt",)


def test_probe_source_reports_unknown_id() -> None:
    with pytest.raises(KeyError):
        probe_source("nope")


def test_probe_source_folds_exceptions_into_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_quota: bool) -> Mapping[str, Any]:
        raise RuntimeError("upstream exploded")

    monkeypatch.setitem(source_registry._PROBES, "tencent_kline", _boom)  # noqa: SLF001
    result = probe_source("tencent_kline")
    assert result.status == "unavailable"
    assert "RuntimeError" in result.detail
    assert "upstream exploded" in result.detail
    assert result.latency_ms >= 0.0


def test_wind_probe_is_skipped_without_quota_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """关键纪律：默认路径**不允许**碰 Wind。"""
    called: list[bool] = []

    def _spy(enabled: bool) -> Mapping[str, Any]:
        called.append(enabled)
        return {"status": "ok"}

    monkeypatch.setitem(source_registry._PROBES, "wind", _spy)  # noqa: SLF001
    result = probe_source("wind", use_cache=False)
    assert called == [False]
    assert result.status == "skipped"
    assert "quota_not_authorized" in result.detail

    result_quota = probe_source("wind", include_quota=True, use_cache=False)
    assert called == [False, True]
    assert result_quota.status == "ok"


def test_probe_cache_avoids_duplicate_work(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _spy(_quota: bool) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "ok", "hits": len(calls)}

    monkeypatch.setitem(source_registry._PROBES, "sina_quote", _spy)  # noqa: SLF001
    first = probe_source("sina_quote")
    second = probe_source("sina_quote")
    assert len(calls) == 1
    assert first == second

    probe_source("sina_quote", use_cache=False)
    assert len(calls) == 2


def test_probe_all_covers_every_declared_source() -> None:
    results = probe_all()
    assert [result.source_id for result in results] == [source.id for source in SOURCES]
    assert all(isinstance(result, ProbeResult) for result in results)


def test_probe_all_can_filter_by_market() -> None:
    results = probe_all(markets=("a_share",))
    ids = [result.source_id for result in results]
    assert "wind" in ids and "tencent_kline" in ids
    assert "ccxt" not in ids and "binance_futures" not in ids


def test_capabilities_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(source_registry._PROBES, "wind", lambda _q: {"status": "skipped"})  # noqa: SLF001
    payload = capabilities_payload()
    assert payload["schema_version"] == "sources.v1"
    assert payload["include_quota"] is False
    assert payload["known_dead_endpoints"], "已知不可用端点必须留档"
    by_id = {entry["id"]: entry for entry in payload["sources"]}
    assert set(by_id) == {source.id for source in SOURCES}
    entry = by_id["ccxt"]
    assert entry["markets"] == ["crypto"]
    assert entry["requires"] == ["ccxt"]
    assert entry["probe"]["status"] in {"ok", "unavailable", "skipped"}


# --------------------------------------------------------------- HTTP 路由


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return json.load(response)


def test_sources_route_returns_registry() -> None:
    with served() as base:
        payload = _get(f"{base}/api/dashboard/sources")
    assert payload["schema_version"] == "sources.v1"
    assert {entry["id"] for entry in payload["sources"]} == {source.id for source in SOURCES}


def test_sources_route_filters_by_market() -> None:
    with served() as base:
        payload = _get(f"{base}/api/dashboard/sources?markets=crypto")
    ids = {entry["id"] for entry in payload["sources"]}
    assert ids == {"binance_futures", "ccxt"}


def test_sources_route_defaults_to_no_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def _spy(enabled: bool) -> Mapping[str, Any]:
        seen.append(enabled)
        return {"status": "skipped" if not enabled else "ok"}

    monkeypatch.setitem(source_registry._PROBES, "wind", _spy)  # noqa: SLF001
    with served() as base:
        default = _get(f"{base}/api/dashboard/sources?markets=a_share")
        with_quota = _get(f"{base}/api/dashboard/sources?markets=a_share&include_quota=1")
    assert default["include_quota"] is False
    assert with_quota["include_quota"] is True
    assert seen == [False, True]


# ------------------------------------------------- 本地库缺整天检测（真实缺陷）


class _FakeCursor:
    """按 SQL 关键词返回预设行的假游标。"""

    def __init__(self, counts: dict[str, int], per_day: list[tuple[Any, int]]) -> None:
        self._counts = counts
        self._per_day = per_day
        self._result: list[tuple[Any, int]] = []

    def execute(self, sql: str, *args: Any) -> None:
        flat = " ".join(sql.split())
        if "group by date" in flat.lower():
            self._result = list(self._per_day)
        elif "from asel.ref_adjust_factor" in flat.lower():
            self._result = [(self._counts["factors"],)]
        else:
            self._result = [(self._counts["bars"],)]

    def fetchone(self) -> tuple[Any, ...]:
        return self._result[0] if self._result else (0,)

    def fetchall(self) -> list[tuple[Any, int]]:
        return list(self._result)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _install_fake_local(monkeypatch: pytest.MonkeyPatch, per_day: list[tuple[Any, int]]) -> None:
    from datetime import date

    cursor = _FakeCursor({"bars": sum(n for _, n in per_day), "factors": 49790}, per_day)

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def _get_conn(self) -> _FakeConn:
            return _FakeConn(cursor)

    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _FakeClient)
    # 覆盖 autouse 的通用假探针，把**真实**的本地库探针接回来（DB 已被假游标替换）。
    monkeypatch.setitem(
        source_registry._PROBES,  # noqa: SLF001
        "a_share_local",
        lambda _q: source_registry._probe_a_share_local(),  # noqa: SLF001
    )
    assert date(2026, 9, 22).weekday() == 1  # 周二，确认它不是周末


def test_local_probe_detects_missing_whole_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """只报总行数会让"少了一整个交易日"完全隐形 —— 必须报出来并降级。"""
    from datetime import date

    per_day = [
        (date(2026, 9, 18), 5210),
        (date(2026, 9, 21), 5210),
        # 2026-09-22（周二）整天缺失
        (date(2026, 9, 23), 5221),
        (date(2026, 9, 24), 5221),
    ]
    _install_fake_local(monkeypatch, per_day)
    result = probe_source("a_share_local", use_cache=False)
    assert result.status == "degraded"
    assert result.evidence["missing_weekdays"] == ["2026-09-22"]
    assert result.evidence["latest_date"] == "2026-09-24"
    # 上中位数（sorted[len//2]）：[5210, 5210, 5221, 5221] → 5221
    assert result.evidence["median_bars_per_day"] == 5221
    assert "2026-09-22" in result.detail


def test_local_probe_ignores_weekend_gap_and_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """周五 → 周一是正常间隔，不许误报成缺失。"""
    from datetime import date

    per_day = [
        (date(2026, 9, 17), 5210),
        (date(2026, 9, 18), 5210),
        (date(2026, 9, 21), 5210),
        (date(2026, 9, 22), 5210),
    ]
    _install_fake_local(monkeypatch, per_day)
    result = probe_source("a_share_local", use_cache=False)
    assert result.status == "ok"
    assert result.evidence["missing_weekdays"] == []
    assert result.detail == ""


def test_local_probe_flags_low_coverage_day(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    per_day = [
        (date(2026, 9, 21), 5210),
        (date(2026, 9, 22), 5210),
        (date(2026, 9, 23), 4000),  # 只有 77% 覆盖，但不缺整天
        (date(2026, 9, 24), 5210),
    ]
    _install_fake_local(monkeypatch, per_day)
    result = probe_source("a_share_local", use_cache=False)
    assert result.status == "ok"
    assert result.evidence["low_coverage_days"] == [{"date": "2026-09-23", "bars": 4000}]
