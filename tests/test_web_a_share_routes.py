"""A 股 HTTP 路由测试（R17-3）。**不联网、不碰真库**：注入假客户端。

这组测试的重点不是"能返回 200"，而是**失败语义**：
- 缺复权因子必须报 ``no_factor``，不能报成 ``db_error``（会把排查方向带偏）；
- 代码格式非法必须回 **JSON 体 400**，不能走 ``send_error`` ——
  ``BaseHTTPRequestHandler`` 把 message 写进只能 latin-1 编码的状态行，中文消息会
  抛 ``UnicodeEncodeError`` 并**直接断连接**（实测 ``RemoteDisconnected``）；
- A 股路由不能被加密 provider 的失败短路掉。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cpt.web import a_share_routes
from cpt.web.app import serve_snapshot


class _FakeClient:
    """``AShareLocalClient`` duck type：只需要 ``_get_conn`` / ``close``。"""

    def __init__(self, rows: list[Any] | None = None, factors: list[Any] | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._factors = factors if factors is not None else []
        self.closed = False

    def _get_conn(self) -> _FakeClient:
        return self

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self._factors)

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, rows: list[Any], factors: list[Any]) -> None:
        self._rows = rows
        self._factors = factors
        self._result: list[Any] = []

    def execute(self, sql: str, *args: Any) -> None:
        flat = " ".join(sql.split()).lower()
        if "ref_adjust_factor" in flat and "distinct" in flat:
            self._result = [(code,) for code in self._factors]
        elif "max(date)" in flat:
            # hot_rank / ladder_day 的 max(date) 都要给 **date 对象**：适配器会对它
            # 调 .isoformat()，给字符串会在测试里炸（假游标"看起来对"是最常见的
            # 假绿来源）。同时不能返回 None，否则 fetchone()[0] 直接崩。
            self._result = [(date(2026, 9, 24),)]
        elif "from public.hot_rank" in flat:
            self._result = list(self._rows)
        elif "from public.ladder_day" in flat:
            self._result = []
        else:
            self._result = []

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@contextmanager
def _served(provider: Any = None) -> Iterator[str]:
    if provider is None:
        provider = lambda: {"schema_version": "dashboard.v2", "candles": []}  # noqa: E731
    server = serve_snapshot(provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(url: str, method: str = "GET") -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, exc.read()[:120]


# --------------------------------------------------------------------- 快照


def test_snapshot_requires_code() -> None:
    with _served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/snapshot")
    assert status == 400
    assert body["error"]["code"] == "code_required"


def test_snapshot_rejects_bad_width_k() -> None:
    with _served() as base:
        for value in ("abc", "3", "5000"):
            status, body = _request(
                f"{base}/api/dashboard/a-share/snapshot?code=002614&width_k={value}"
            )
            assert status == 400, value
            assert body["error"]["code"].startswith("width_k"), value


def test_invalid_code_returns_json_400_not_a_dropped_connection() -> None:
    """回归：中文错误消息曾让 ``send_error`` 抛 UnicodeEncodeError 并断开连接。

    ``send_error`` 把 message 写进 HTTP 状态行（只能 latin-1），所以任何中文提示
    都会把 400 变成 RemoteDisconnected —— 浏览器侧只看到"网络错误"，看不到原因。
    """
    with _served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/snapshot?code=abc")
    assert status == 400
    assert body["error"]["code"] == "invalid_code"
    assert "abc" in body["error"]["message"]


def test_snapshot_reason_no_factor_is_not_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺因子必须报 no_factor：报成 db_error 会让人去查数据库而不是查因子表。"""
    from cpt.adapters.a_share_local import AShareNoFactorError
    from cpt.application.a_share_snapshot import build_ashare_snapshot

    class _NoFactorClient(_FakeClient):
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise AShareNoFactorError("区间内所有日期都缺因子（123 日）")

    snapshot = build_ashare_snapshot("600519", client=_NoFactorClient())
    assert snapshot["runtime"]["degraded_reason"] == "no_factor"
    assert snapshot["data_quality"]["reason"] == "no_factor"
    assert snapshot["runtime"]["degraded"] is True


def test_snapshot_reason_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from cpt.adapters.a_share_local import AShareNoDataError
    from cpt.application.a_share_snapshot import build_ashare_snapshot

    class _NoDataClient(_FakeClient):
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise AShareNoDataError("public.daily_bar 无数据")

    snapshot = build_ashare_snapshot("999999", client=_NoDataClient())
    assert snapshot["runtime"]["degraded_reason"] == "no_data"


def test_snapshot_reason_db_error_still_distinct() -> None:
    from cpt.application.a_share_snapshot import build_ashare_snapshot

    class _BrokenClient(_FakeClient):
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection refused")

    snapshot = build_ashare_snapshot("002614", client=_BrokenClient())
    assert snapshot["runtime"]["degraded_reason"] == "db_error:RuntimeError"


def test_ashare_routes_survive_broken_crypto_provider() -> None:
    """A 股路由不能依赖加密 provider：provider 抛异常时它必须照常工作。"""

    def _boom() -> dict[str, Any]:
        raise RuntimeError("crypto upstream down")

    with _served(_boom) as base:
        status, body = _request(f"{base}/api/dashboard/a-share/pool")
    assert status == 200
    assert body["schema_version"] == "a_share_pool.v1"


# ----------------------------------------------------------------------- 池


def test_pool_marks_drawable_and_uses_union(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(rows=[("002119", 1), ("000592", 2)], factors=["002119"])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 2
    assert payload["drawable_count"] == 1
    by_code = {item["code"]: item for item in payload["items"]}
    # 缺因子的票**仍然列出但标 drawable=false**：直接过滤会让人以为池子少了票
    assert by_code["002119"]["drawable"] is True
    assert by_code["000592"]["drawable"] is False
    assert by_code["000592"]["rank"] == 2


def test_pool_survives_factor_table_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """因子表读不到也要能出池子（否则 A 股入口整体不可用）。"""

    def _boom() -> set[str]:
        raise RuntimeError("factor table locked")

    fake = _FakeClient(rows=[("002119", 1)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    monkeypatch.setattr(a_share_routes, "_factor_codes", _boom)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 1
    assert payload["drawable_count"] == 0
    assert "factor table locked" in payload["factor_error"]


# --------------------------------------------------------------------- 自选


def test_watchlist_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPT_WATCHLIST", str(tmp_path / "wl.json"))
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")

    assert a_share_routes.watchlist_payload()["count"] == 0
    after_add = a_share_routes.watchlist_add("600519")
    assert [item["code"] for item in after_add["items"]] == ["600519"]
    # 幂等
    assert a_share_routes.watchlist_add("600519.SH")["count"] == 1
    after_remove = a_share_routes.watchlist_remove("sh600519")
    assert after_remove["removed"] is True
    assert after_remove["count"] == 0
    assert a_share_routes.watchlist_remove("600519")["removed"] is False


def test_watchlist_route_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    with _served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/watchlist?code=002614", "POST")
        assert status == 200
        assert [item["code"] for item in body["items"]] == ["002614"]
        status, body = _request(f"{base}/api/dashboard/a-share/watchlist?code=002614", "DELETE")
        assert status == 200
        assert body["removed"] is True
        status, body = _request(f"{base}/api/dashboard/a-share/watchlist")
        assert status == 200
        assert body["count"] == 0


def test_watchlist_route_rejects_bad_code_with_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    with _served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/watchlist?code=zzz", "POST")
    assert status == 400
    assert body["error"]["code"] == "invalid_code"


def test_unknown_ashare_subpath_is_404() -> None:
    with _served() as base:
        status, _ = _request(f"{base}/api/dashboard/a-share/nope")
    assert status == 404


def test_unsupported_method_on_ashare_watchlist_is_rejected() -> None:
    """未实现的方法必须被拒。

    实测是 **501**（``BaseHTTPRequestHandler`` 找不到 ``do_PUT`` 时的标准行为），
    不是 405 —— 405 只在我显式实现了该方法但路由不匹配时出现。两种都算"被拒"，
    但这里钉住实际值，避免以后有人误以为 PUT 是"已实现但未授权"。
    """
    with _served() as base:
        status, _ = _request(f"{base}/api/dashboard/a-share/watchlist?code=002614", "PUT")
    assert status == 501
