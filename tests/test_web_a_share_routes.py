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
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cpt.web import a_share_routes

from tests.conftest import served


@pytest.fixture(autouse=True)
def _no_real_name_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认把证券名称查询掐掉。

    ``a_share_routes._names`` 直接调 ``fetch_security_names``（模块级函数），
    **不经过**测试里 monkeypatch 的 ``AShareLocalClient`` —— 不掐的话这些测试会
    去连真库：本机侥幸能过，CI（无 psycopg / 无 DB）行为就完全不同。需要验证
    名字的用例自己覆盖它。
    """
    monkeypatch.setattr(a_share_routes, "_names", lambda codes: {})


@pytest.fixture(autouse=True)
def _isolated_watchlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把自选落盘指到临时文件。

    ``pool_payload()`` 从 v2 起会读自选 —— 不隔离的话每个池子测试都会去读**真实的**
    ``~/.cache/cpt/watchlist.json``：本机因为恰好有一条手输而失败、CI（无该文件）却
    通过，是最典型的"本机假红 / 假绿"。需要验证自选的用例自己覆盖这个路径。
    """
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "watchlist.json")


class _FakeClient:
    """``AShareLocalClient`` duck type：只需要 ``_get_conn`` / ``close``。"""

    def __init__(
        self,
        rows: list[Any] | None = None,
        factors: list[Any] | None = None,
        strategy_rows: list[Any] | None = None,
    ) -> None:
        self._rows = rows if rows is not None else []
        self._factors = factors if factors is not None else []
        #: 策略表行，形状 (code, strategy, name, action, score, confidence, reason, model)
        self._strategy_rows = strategy_rows if strategy_rows is not None else []
        self.closed = False

    def _get_conn(self) -> _FakeClient:
        return self

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self._factors, self._strategy_rows)

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, rows: list[Any], factors: list[Any], strategy_rows: list[Any]) -> None:
        self._rows = rows
        self._factors = factors
        self._strategy_rows = strategy_rows
        self._result: list[Any] = []

    def execute(self, sql: str, *args: Any) -> None:
        flat = " ".join(sql.split()).lower()
        if "max(trade_date)" in flat:
            # 策略表的最新交易日。**必须先于下面那条判**：两条 SQL 都含
            # "public.strategy_signal"，顺序反了会把 max 查询当成取数查询。
            self._result = [(date(2026, 9, 24),)]
        elif "from public.strategy_signal" in flat:
            self._result = list(self._strategy_rows)
        elif "ref_adjust_factor" in flat and "distinct" in flat:
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
    with served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/snapshot")
    assert status == 400
    assert body["error"]["code"] == "code_required"


def test_snapshot_rejects_bad_width_k() -> None:
    with served() as base:
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
    with served() as base:
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


def test_ashare_routes_survive_broken_crypto_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股路由不能依赖加密 provider：provider 抛异常时它必须照常工作。

    **必须把 ``AShareLocalClient`` 换成假客户端。** 本用例的意图是"加密 provider 挂了
    A 股路由照样工作"，与数据库无关；用真客户端会走 ``_get_conn()`` → 顶层
    ``import psycopg``，而 psycopg 只在 ``.[db]`` extra 里 —— CI 只装
    ``requirements-dev.txt``（不含任何 extra），于是 CI 直接
    ``ModuleNotFoundError``；本机因为 ``.venv`` 恰好装了 psycopg 才"过"。
    这正是本文件 ``_no_real_name_lookup`` docstring 里警告过的
    "本机侥幸能过、CI 行为完全不同"，当时守住了 ``_names`` 与自选落盘，
    **漏了 ``_get_conn`` 这条路径** —— 结果 CI 从这个用例开始一路红，
    后面的静态门禁与 vulture 全被 skip（2026-09-25 排查 P0-1）。
    """
    monkeypatch.setattr(
        "cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: _FakeClient()
    )

    def _boom() -> dict[str, Any]:
        raise RuntimeError("crypto upstream down")

    with served(_boom) as base:
        status, body = _request(f"{base}/api/dashboard/a-share/pool")
    assert status == 200
    assert body["schema_version"] == "a_share_pool.v2"


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
    # v2 起来源明细收进 `hot` 子对象（一条候选可以同时来自多个源，见 _merge_sources）
    assert by_code["000592"]["hot"]["rank"] == 2
    assert by_code["000592"]["sources"] == ["hot_rank"]
    assert by_code["000592"]["group"] == "hot"


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


# --------------------------------------------------------------- 三源合并（v2）


def _strategy_row(
    code: str,
    action: str,
    score: int,
    confidence: float,
    *,
    strategy: str = "bull_trend",
    name: str = "默认多头趋势",
) -> tuple:
    """真实行形状：``(code, strategy, name, action, score, confidence, reason, model)``。

    ⚠️ ``name`` 是**策略名称**，不是股票名。``confidence`` 用 ``Decimal``
    —— 真库是 ``numeric(4,3)``，用 float 造数据会把这个坑测没。
    """
    return (code, strategy, name, action, score, Decimal(str(confidence)), "理由", "m")


def test_pool_merges_hot_manual_and_strategy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """三源合并 + 每只标注全部来源（2026-09-25 需求）。"""
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    a_share_routes.watchlist_add("600519")

    fake = _FakeClient(
        rows=[("002119", 1), ("000592", 2)],
        factors=["002119", "600519"],
        strategy_rows=[_strategy_row("000498", "BUY", 88, 0.92)],
    )
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()

    assert payload["schema_version"] == "a_share_pool.v2"
    by_code = {item["code"]: item for item in payload["items"]}
    assert set(by_code) == {"002119", "000592", "600519", "000498"}
    # 手输排第一：用户自己的选择不能被算法分组盖掉
    assert [item["code"] for item in payload["items"]][0] == "600519"
    assert by_code["600519"]["sources"] == ["manual"]
    assert by_code["002119"]["sources"] == ["hot_rank"]
    assert by_code["000498"]["sources"] == ["strategy"]
    assert by_code["000498"]["strategy"]["combined"] == 90.0
    assert by_code["000498"]["strategy"]["action"] == "BUY"
    assert payload["groups"] == {"manual": 1, "hot": 2, "strategy": 1}
    assert payload["strategy_error"] is None
    assert payload["watchlist_error"] is None


def test_pool_dedups_code_present_in_multiple_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一代码出现在两个源里只出一条，但 ``sources`` 要列出两处。

    不去重的话下拉会出现两个 value 相同的 option，选中哪个都一样，
    而 ``selected`` 归属还会变得随机 —— 正是"选错票"最容易发生的地方。
    """
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    fake = _FakeClient(
        rows=[("000592", 2)],
        factors=[],
        strategy_rows=[_strategy_row("000592", "WATCH", 55, 0.60)],
    )
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["sources"] == ["hot_rank", "strategy"]
    assert item["group"] == "hot"  # 热门池优先级高于策略
    assert item["hot"]["rank"] == 2
    assert item["strategy"]["score"] == 55


def test_pool_manual_beats_hot_in_grouping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """手输的票同时也在热门池时归到「手输」组 —— 否则用户会以为手输又丢了。"""
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    a_share_routes.watchlist_add("002119")
    fake = _FakeClient(rows=[("002119", 1)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    item = a_share_routes.pool_payload()["items"][0]
    assert item["sources"] == ["manual", "hot_rank"]
    assert item["group"] == "manual"


def test_pool_caps_hot_pool_at_top5(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """热门池 100 只收敛到 Top5（2026-09-25 需求）。"""
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")
    fake = _FakeClient(rows=[(f"00000{i}", i) for i in range(1, 11)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 5
    assert [item["hot"]["rank"] for item in payload["items"]] == [1, 2, 3, 4, 5]


def test_pool_survives_strategy_table_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """策略表读不到也要能出池子（策略只是三个来源之一）。"""
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", tmp_path / "wl.json")

    class _BrokenCursor(_FakeCursor):
        def execute(self, sql: str, *args: Any) -> None:
            if "strategy_signal" in " ".join(sql.split()).lower():
                raise RuntimeError("strategy_signal missing")
            super().execute(sql, *args)

    class _BrokenClient(_FakeClient):
        def cursor(self) -> Any:
            return _BrokenCursor(self._rows, self._factors, self._strategy_rows)

    fake = _BrokenClient(rows=[("002119", 1)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 1
    assert "strategy_signal missing" in payload["strategy_error"]
    assert payload["groups"]["strategy"] == 0


def test_pool_survives_corrupt_watchlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自选文件损坏不能让整个池子挂 —— 但必须在 ``watchlist_error`` 里说清。

    与自选路由本身**刻意不同**：那条路由读失败就该报错（用户点的是"看我的自选"，
    静默返回空列表会让他以为自选被清空了）。
    """
    bad = tmp_path / "wl.json"
    bad.write_text("not json {", encoding="utf-8")
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", bad)
    fake = _FakeClient(rows=[("002119", 1)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 1
    assert "自选文件读取失败" in payload["watchlist_error"]


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
    with served() as base:
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
    with served() as base:
        status, body = _request(f"{base}/api/dashboard/a-share/watchlist?code=zzz", "POST")
    assert status == 400
    assert body["error"]["code"] == "invalid_code"


def test_unknown_ashare_subpath_is_404() -> None:
    with served() as base:
        status, _ = _request(f"{base}/api/dashboard/a-share/nope")
    assert status == 404


def test_unsupported_method_on_ashare_watchlist_is_rejected() -> None:
    """未实现的方法必须被拒。

    实测是 **501**（``BaseHTTPRequestHandler`` 找不到 ``do_PUT`` 时的标准行为），
    不是 405 —— 405 只在我显式实现了该方法但路由不匹配时出现。两种都算"被拒"，
    但这里钉住实际值，避免以后有人误以为 PUT 是"已实现但未授权"。
    """
    with served() as base:
        status, _ = _request(f"{base}/api/dashboard/a-share/watchlist?code=002614", "PUT")
    assert status == 501


# ------------------------------------------------------------------ 证券名称


def test_pool_items_carry_security_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """下拉里要显示名称：只有六位数字时 002119/002219 这种一眼看岔。"""
    from cpt.adapters.a_share_local import SecurityName

    fake = _FakeClient(rows=[("002119", 1), ("000592", 2)], factors=["002119"])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        a_share_routes,
        "_names",
        lambda codes: {
            "002119": SecurityName("002119", "康强电子", "主板"),
            "000592": SecurityName("000592", "平潭发展", "主板"),
        },
    )
    by_code = {item["code"]: item for item in a_share_routes.pool_payload()["items"]}
    assert by_code["002119"]["name"] == "康强电子"
    assert by_code["002119"]["board"] == "主板"
    assert by_code["000592"]["name"] == "平潭发展"


def test_pool_survives_name_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """名字查不到只是少个装饰，**不能让整个池子挂掉**。"""

    def _boom(codes: list[str]) -> dict[str, Any]:
        raise RuntimeError("security_master locked")

    fake = _FakeClient(rows=[("002119", 1)], factors=[])
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: fake)
    monkeypatch.setattr(a_share_routes, "_names", _boom)
    # _names 自己吞异常（见其实现），所以这里模拟的是"它返回空"的等价路径
    monkeypatch.setattr(a_share_routes, "_names", lambda codes: {})
    payload = a_share_routes.pool_payload()
    assert payload["count"] == 1
    assert payload["items"][0]["name"] == ""


def test_names_helper_swallows_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_names`` 内部必须吞掉异常并返回空字典。"""
    import cpt.adapters.a_share_local as local

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no db")

    monkeypatch.setattr(local, "fetch_security_names", _boom)
    assert a_share_routes._names(["600519"]) == {}


def test_clean_security_name_strips_padding() -> None:
    """老行情源按 4 字宽补齐，``深 赛 格`` / ``ST 中 侨`` / ``万  科Ａ`` 要去掉空白。

    实测全部 80 条含空白的名称里，没有一条是"ASCII 单词间的有意义空格"，
    所以删除是安全的（折叠成单空格则 ``深 赛 格`` 原样不变，没用）。
    """
    from cpt.adapters.a_share_local import _clean_security_name

    assert _clean_security_name("深 赛 格") == "深赛格"
    assert _clean_security_name("ST 中 侨") == "ST中侨"
    assert _clean_security_name("万  科Ａ") == "万科Ａ"
    assert _clean_security_name("TCL 通讯") == "TCL通讯"
    assert _clean_security_name("贵州茅台") == "贵州茅台"
    # 非字符串 / 空 → 空串（调用方据此判定"没名字"）
    assert _clean_security_name(None) == ""
    assert _clean_security_name("") == ""
    assert _clean_security_name("   ") == ""


def test_fetch_security_names_empty_input_does_not_connect() -> None:
    """空输入直接返回，**不建连接**（否则每次空池子都要连一次库）。"""
    from cpt.adapters.a_share_local import fetch_security_names

    def _boom() -> Any:
        raise AssertionError("不该建连接")

    assert fetch_security_names([], conn_factory=_boom) == {}
