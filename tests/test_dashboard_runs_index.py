"""运行索引（R20 ⑤ 接线）守门用例。

**入口纪律**：用例一律打**真 HTTP server**（``tests/conftest.py::served``），
不直接调 ``dashboard_runs.record_run`` —— 直接调只能证明「函数本身没坏」，
证明不了生产会不会走它（R19 立下的规矩，本轮第三次沿用）。而且这一条在本轮
尤其关键：接线第一版做在**领域层**（``build_dashboard_snapshot_v2`` 里调
``record_run``），单测全绿，真跑才炸出
``test_provider_caches_snapshot_within_ttl`` 变红（snapshot 变非确定性）。
**领域层保持确定性**这条单测守不住当时那个 bug，所以必须打 HTTP 层。

接线前应当红的用例：``test_http_snapshot_carries_run_index`` 等全部 HTTP 用例
（``runs`` 恒为 ``[]``）。

刻意**没有**的用例：不锁死「两条相同数据只记一条」之外的记账节奏。realtime
模式 30s 一轮、请求远密于构建，ring 里的条数与真实构建次数不是一一对应，
锁死条数等于把实现细节固化成契约。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.application.dashboard_runs import RUN_RING_SIZE, clear_runs
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar

from tests.conftest import served

DAY_MS = 24 * 3600 * 1000
START = 1706745600000  # 2024-02-01T00:00:00Z（与 tests/fixtures 同一基准）


def _bars(n: int = 40) -> list[CanonicalBar]:
    """一段确定的日线（交替涨跌，保证 zigzag 能出笔）。"""
    out: list[CanonicalBar] = []
    price = 100.0
    for i in range(n):
        price *= 1.02 if i % 2 == 0 else 0.98
        open_ms = START + i * DAY_MS
        out.append(
            CanonicalBar(
                open_time=open_ms,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=1000.0,
                close_time=open_ms + DAY_MS - 1,
                quote_volume=price * 1000.0,
                trade_count=100,
                taker_buy_base_volume=500.0,
                taker_buy_quote_volume=price * 500.0,
                is_closed=True,
            )
        )
    return out


def _v2(
    symbol: str,
    *,
    runtime: dict[str, Any] | None = None,
    n: int = 40,
) -> dict[str, Any]:
    """领域层快照（``market.symbol`` 显式带上——生产路径也是这么覆盖的）。

    ``n`` 用来制造**不同的 dataset_hash**：``dataset_hash`` 只覆盖 bars、不含
    symbol（``dashboard_reproducibility.py:71``），所以「同长度同内容的 bars」=
    「同一次运行」，改 symbol 不会被记成新的一条。真实里两只票的 bars 不可能
    一样，测试必须照着这个事实造数据，否则会去重逻辑把用例卡死。
    """
    snap = build_dashboard_snapshot_v2(
        RulesConfig(),
        _bars(n),
        mode="research",
        status="confirmed",
        data_source="fixture",
        runtime=runtime if runtime is not None else {"symbol": symbol, "interval_ms": DAY_MS},
    )
    snap["market"]["symbol"] = symbol
    return snap


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}") as response:
        return json.load(response)


@pytest.fixture(autouse=True)
def _clean_ring() -> Iterator[None]:
    """环形缓冲是**进程级状态**：不清会跨用例串味，测试必须互相独立。"""
    clear_runs()
    yield
    clear_runs()


def _serve(symbol: str, *, n: int = 40, runtime: dict[str, Any] | None = None) -> Any:
    """起一个真 server 并**立刻打一次主快照**（记账只发生在主快照出口）。

    ``/api/dashboard/runs`` 是纯读取路由：不先访问 ``/api/dashboard/snapshot``，
    ring 就是空的 —— 这条本身也是口径的一部分（面板先开着、服务刚重启时，
    运行索引理应是空的而不是报错）。
    """
    context = served(lambda: _v2(symbol, n=n, runtime=runtime))
    base = context.__enter__()
    _get(base, "/api/dashboard/snapshot")
    return base, context


def test_http_snapshot_carries_run_index() -> None:
    """生产 HTTP 响应里必须带上运行索引——这是整个 R20 ⑤ 的核心断言。"""
    with served(lambda: _v2("BTCUSDT")) as base:
        payload = _get(base, "/api/dashboard/snapshot")
    assert payload["runs"], "HTTP 响应里 runs 为空，realtime 模式跑一天也是空的"
    assert payload["runs"][0]["symbol"] == "BTCUSDT"


def test_http_runs_route_serves_ring_history() -> None:
    """``/api/dashboard/runs`` 必须独立可取，且与主快照里的口径一致。

    前端不 fetch 这个路由（只读 ``snapshot.runs``），但它本来就是对外契约里的
    一条路由，``tests/test_dashboard_http.py`` 也逐条断言它的存在。
    """
    base, context = _serve("BTCUSDT")
    try:
        payload = _get(base, "/api/dashboard/runs")
    finally:
        context.__exit__(None, None, None)
    assert "runs" in payload
    assert payload["runs"], "runs 路由返回空列表"


def test_every_run_row_has_both_timestamp_field_names() -> None:
    """时间戳必须**两个字段名都非空且同值**。

    这是本轮修掉的隐藏 bug 的守门：``dashboard_runs`` 出 ``generated_at`` 而前端
    ``dashboard.js`` 读 ``created_at``；因为 ``runs`` 恒空、循环体从未执行，
    这个不一致潜伏了很久。
    """
    base, context = _serve("BTCUSDT")
    try:
        rows = _get(base, "/api/dashboard/runs")["runs"]
    finally:
        context.__exit__(None, None, None)
    assert rows
    for row in rows:
        assert row["generated_at"] is not None
        assert row["created_at"] == row["generated_at"]


def test_timestamp_falls_back_when_runtime_omits_generated_at() -> None:
    """A 股主看板的 runtime 只有 ``as_of_ms``、没有 ``generated_at``。

    不兜底的话运行索引里 A 股那条永远空白——而 A 股恰是用户主要看的面板
    （判据同 R19：数据接上不等于信息可用）。
    """
    as_of = START + 5 * DAY_MS
    base, context = _serve("600519", runtime={"as_of_ms": as_of})
    try:
        rows = _get(base, "/api/dashboard/runs")["runs"]
    finally:
        context.__exit__(None, None, None)
    assert rows[0]["generated_at"] == as_of


def test_timestamp_falls_back_to_wall_clock_as_last_resort() -> None:
    """runtime 什么都没有时，用当前时间而不是留空。"""
    before = datetime.now(UTC).timestamp() * 1000
    base, context = _serve("BTCUSDT", runtime={})
    try:
        rows = _get(base, "/api/dashboard/runs")["runs"]
    finally:
        context.__exit__(None, None, None)
    assert rows[0]["generated_at"] >= before


def test_runs_are_newest_first() -> None:
    """运行索引按时间**倒序**（最新的在最上面）。

    两次不同的 bars 各记一条；顺序是「最后记的在最前」。
    """
    base, first = _serve("BTCUSDT", n=40)
    try:
        _get(base, "/api/dashboard/snapshot")
    finally:
        first.__exit__(None, None, None)
    with served(lambda: _v2("ETHUSDT", n=42)) as base:
        rows = _get(base, "/api/dashboard/snapshot")["runs"]
    assert [row["symbol"] for row in rows] == ["ETHUSDT", "BTCUSDT"]


def test_repeated_requests_of_same_snapshot_do_not_duplicate_runs() -> None:
    """同一份快照被反复请求，只记**一条**。

    realtime 模式 30s 内可能有十几次请求命中同一份缓存 snapshot；不去重的话
    「一次运行」会被记成十几条，运行索引立刻失去意义。
    """
    with served(lambda: _v2("BTCUSDT")) as base:
        for _ in range(4):
            payload = _get(base, "/api/dashboard/snapshot")
    assert len(payload["runs"]) == 1, f"同一份快照被记成 {len(payload['runs'])} 条"


def test_ring_drops_oldest_past_capacity() -> None:
    """环形缓冲有上限，不能无限涨（realtime 每 30s 一轮）。"""
    base, first = _serve("OLDEST", n=40)
    first.__exit__(None, None, None)
    for i in range(RUN_RING_SIZE):
        base, context = _serve(f"SYM{i}", n=41 + i)
        context.__exit__(None, None, None)
    base, context = _serve("NEWEST", n=200)
    try:
        rows = _get(base, "/api/dashboard/runs")["runs"]
    finally:
        context.__exit__(None, None, None)
    assert len(rows) == RUN_RING_SIZE
    symbols = {row["symbol"] for row in rows}
    assert "OLDEST" not in symbols, "超出容量后最旧的一条应被挤出"


def test_child_field_response_is_left_alone() -> None:
    """子字段响应（``/api/dashboard/reproducibility``）**不注入** runs。

    注入点按 ``"market" in payload`` 过滤：``reproducibility`` 里没有 market，
    无端塞一个 runs 键会让消费者以为这份响应也带运行历史。
    """
    with served(lambda: _v2("BTCUSDT")) as base:
        payload = _get(base, "/api/dashboard/reproducibility")
    assert "runs" not in payload


def test_domain_layer_snapshot_stays_deterministic() -> None:
    """领域层快照**必须保持同输入同输出**。

    这是本轮返工的原因，也是最容易被破坏的一条：接线第一版把 ring 内容写进
    ``v2["runs"]``，同参数两次构建返回不等，直接打破
    ``tests/test_web_a_share.py::test_provider_caches_snapshot_within_ttl`` 守的
    缓存语义。本用例把这条性质钉在领域层，防止下次接线又踩回来。
    """
    first = _v2("BTCUSDT")
    second = _v2("BTCUSDT")
    assert first == second, "领域层 snapshot 混入进程级状态后不再可缓存"
    assert first["runs"] == [], "runs 属于 HTTP 响应层视图，不进领域层快照"
