"""R10 / I：错误处理边界——/health 真实健康状态 + 上游降级留痕。

覆盖：
1. provider 有 ``health()`` 时 /health 返回真实状态（不再永远 ok）
2. provider 无 ``health()`` 时退回静态只读声明
3. ``health()`` 自身抛异常时也要能返回（ok=False, degraded=True）
4. ``_RealtimeProvider`` 上游不可达时：快照带 degraded 标记 + health 报 degraded
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from cpt.web.__main__ import _RealtimeProvider

from tests.conftest import served

_STATIC_SNAPSHOT = {
    "schema_version": "dashboard.v2",
    "market": {"symbol": "BTCUSDT"},
    "runtime": {"symbol": "BTCUSDT", "data_source": "fixture"},
}


def _get(base: str, path: str) -> tuple[int, dict[str, object]]:
    with urllib.request.urlopen(f"{base}{path}") as response:
        return response.status, json.load(response)


class _HealthProvider:
    """最小 provider：有 snapshot_payload 与 health。"""

    def __init__(self, health: dict[str, object] | Exception) -> None:
        self._health = health

    def snapshot_payload(self) -> dict[str, object]:
        return dict(_STATIC_SNAPSHOT)

    def health(self) -> dict[str, object]:
        if isinstance(self._health, Exception):
            raise self._health
        return dict(self._health)


def test_health_returns_real_provider_state() -> None:
    provider = _HealthProvider(
        {
            "ok": False,
            "read_only": True,
            "degraded": True,
            "consecutive_failures": 3,
            "last_error": "upstream_fetch_failed:URLError",
        }
    )
    with served(provider) as base:
        status, payload = _get(base, "/api/dashboard/health")
        assert status == 200
        assert payload["ok"] is False
        assert payload["degraded"] is True
        assert payload["consecutive_failures"] == 3
        assert payload["last_error"] == "upstream_fetch_failed:URLError"


def test_health_falls_back_without_provider_health() -> None:
    # 无 health 能力的 provider（如 dict / fixture）：退回静态只读声明
    with served(lambda: dict(_STATIC_SNAPSHOT)) as base:
        status, payload = _get(base, "/api/dashboard/health")
        assert status == 200
        assert payload == {"ok": True, "read_only": True, "degraded": False}


def test_health_survives_probe_failure() -> None:
    provider = _HealthProvider(RuntimeError("probe blew up"))
    with served(provider) as base:
        status, payload = _get(base, "/api/dashboard/health")
        assert status == 200
        assert payload["ok"] is False
        assert payload["degraded"] is True
        assert payload["last_error"] == "health_probe_failed:RuntimeError"


class _FailingClient:
    """模拟上游不可达。"""

    def fetch_validated_klines(self, *args: object, **kwargs: object) -> object:
        raise OSError("upstream unreachable")


@pytest.mark.usefixtures("_no_network")
def test_realtime_provider_marks_degraded_on_upstream_failure() -> None:
    provider = _RealtimeProvider(symbol="BTCUSDT", interval="1h", limit=10, poll_seconds=3600.0)
    try:
        # 换掉真实客户端，避免任何网络 I/O
        provider._client = _FailingClient()  # type: ignore[assignment]
        provider.force_refresh()

        snapshot = provider.snapshot_payload()
        runtime = snapshot["runtime"]
        assert runtime["degraded"] is True
        assert str(runtime["degraded_reason"]).startswith("upstream_fetch_failed:")
        # 降级快照仍是 schema 完整的空结构，不能崩
        assert snapshot["candles"] == []

        health = provider.health()
        assert health["ok"] is False
        assert health["degraded"] is True
        assert health["consecutive_failures"] >= 1
        assert str(health["last_error"]).startswith("upstream_fetch_failed:")
        assert health["symbol"] == "BTCUSDT"
    finally:
        provider.stop()


@pytest.fixture
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止后台轮询线程发起真实网络请求。"""

    class _ImmediateClient:
        def fetch_validated_klines(self, *args: object, **kwargs: object) -> object:
            raise OSError("network disabled in tests")

    monkeypatch.setattr("cpt.web.__main__.BinanceFuturesClient", _ImmediateClient)
