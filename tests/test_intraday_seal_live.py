"""R91：**在真库上**测「读取不该被探针行带偏」。

## 为什么这组测试必须连真库

R90 落库之后，生产表 ``public.intraday_seal_snapshot`` 里混进了 18 行探针数据
（``600519 / bid1_lots=50000 / limit_price=1100``，产品测试写的）。结果是：
**盘中面板显示「1 只封板、封单 5 万手、强度 1.00 正常」**，看着像真的，实际是假的。

这类问题用假游标测不出来 —— 假游标喂什么就返回什么，SQL 里少一个 ``WHERE``
它也不会红。只有对着真库跑才发现「读回来的和期望的不是同一批行」。

## 怎么做到「连真库但不污染真库」

1. 一个连接、一个事务，**最后一定 ``rollback()``**；
2. 插入的哨兵行代码以 ``\\x01`` 开头 —— 与真实 6 位数字代码不可能撞；
3. 断言的期望值**全从插入的数据推导**，不写死任何生产数字 —— 否则这个测试会随
   生产数据变化而假红。

连不上库就 ``pytest.skip``（与 ``test_migration_r67_symbol_in_id`` 同款约定）：
本仓有整份跳过 DB 类测试的先例，CI 里没有库。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from cpt.storage.intraday_seal_store import (
    PROBE_CODE_PREFIX,
    load_latest_round,
    load_seal_track,
    load_session_rows,
)


@pytest.fixture()
def live_conn() -> Iterator[Any]:
    """真库连接，**事务最后一定回滚**。连不上就跳过。"""
    try:
        import psycopg
        from cpt.adapters.a_share_local import connection_kwargs
    except Exception as exc:  # noqa: BLE001 - 驱动缺失是环境问题
        pytest.skip(f"没有 psycopg / 连接配置：{exc}")
    try:
        conn = psycopg.connect(**connection_kwargs())
    except Exception as exc:  # noqa: BLE001 - 库不可达同样跳过
        pytest.skip(f"连不上库：{exc}")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _probe_code(tag: str) -> str:
    """哨兵代码：``\\x01`` 前缀 + 随机串，保证不撞任何真实代码。"""
    return f"{PROBE_CODE_PREFIX}{tag}-{uuid.uuid4().hex[:8]}"


def _insert(conn: Any, *, at: dt.datetime, code: str, sealed: bool, lots: float | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.intraday_seal_snapshot "
            "(at, code, sealed, bid1_lots, limit_price) VALUES (%s, %s, %s, %s, %s)",
            (at, code, sealed, lots, None),
        )


def test_latest_round_ignores_probe_rows_beyond_the_real_latest(
    live_conn: Any,
) -> None:
    """探针行的时间戳比真实最新一轮还新时，**不许**被当成最新一轮。

    这正是生产上的实际情形：产品测试写进去的那 18 行是收盘后才写的，
    ``max(at)`` 落在它们身上，于是面板把「一只假封板」当成了当前行情。
    """
    real_before = load_latest_round(live_conn)
    real_at = real_before["at"]
    real_codes = {r["code"] for r in real_before["rows"]}

    if real_at is None:
        pytest.skip("表里没有真实轮次，这条无从验证")

    probe_at = real_at + dt.timedelta(minutes=30)
    probe_code = _probe_code("latest")
    _insert(live_conn, at=probe_at, code=probe_code, sealed=True, lots=50_000.0)

    after = load_latest_round(live_conn)
    assert after["at"] == real_at, (
        f"最新一轮被探针行带偏了：{after['at']} != {real_at}。"
        "load_latest_round 的 WHERE 少了真实行过滤"
    )
    assert probe_code not in {r["code"] for r in after["rows"]}
    assert {r["code"] for r in after["rows"]} == real_codes


def test_latest_round_counts_do_not_move_when_a_probe_row_is_added(
    live_conn: Any,
) -> None:
    """加一条探针行，``counts`` 一个数都不许变 —— 包括 sealed / open_board。"""
    before = load_latest_round(live_conn)
    if before["at"] is None:
        pytest.skip("表里没有真实轮次")
    probe_code = _probe_code("counts")
    # 故意插在**真实最新那一轮的时间戳上**，撑大 total 的可能性最大
    _insert(live_conn, at=before["at"], code=probe_code, sealed=False, lots=None)
    after = load_latest_round(live_conn)
    assert after["counts"] == before["counts"], (
        f"探针行改变了 counts：{before['counts']} -> {after['counts']}"
    )
    assert probe_code not in {r["code"] for r in after["rows"]}


def test_seal_track_does_not_return_probe_rows(live_conn: Any) -> None:
    """按票回看同样要过滤 —— 否则探针行会伪造出一段封板轨迹。"""
    code = _probe_code("track")
    at = dt.datetime(2026, 10, 22, 2, 0, tzinfo=dt.UTC)
    _insert(live_conn, at=at, code=code, sealed=True, lots=123_456.0)
    _insert(live_conn, at=at + dt.timedelta(minutes=2), code=code, sealed=False, lots=None)
    track = load_seal_track(
        live_conn,
        code,
        since=at - dt.timedelta(minutes=1),
        until=at + dt.timedelta(minutes=3),
    )
    assert track == [], f"探针行泄漏进按票回看：{track}"


def test_session_rows_do_not_return_probe_rows(live_conn: Any) -> None:
    """当天回顾同理。"""
    code = _probe_code("session")
    at = dt.datetime(2026, 10, 22, 2, 0, tzinfo=dt.UTC)
    _insert(live_conn, at=at, code=code, sealed=True, lots=1.0)
    rows = load_session_rows(
        live_conn,
        start=at - dt.timedelta(minutes=1),
        end=at + dt.timedelta(minutes=1),
    )
    assert all(r["code"] != code for r in rows), "探针行泄漏进当天回顾"


def test_real_rows_survive_the_filter(live_conn: Any) -> None:
    """过滤器不能把真实行一起滤掉 —— 「过滤太狠」和「没过滤」一样是 bug。"""
    real = load_latest_round(live_conn)
    if real["at"] is None:
        pytest.skip("表里没有真实轮次")
    assert real["rows"], "真实最新一轮读出来是空的：过滤条件写错了"
    assert all(not str(r["code"]).startswith(PROBE_CODE_PREFIX) for r in real["rows"])
    six_digit = [r for r in real["rows"] if len(str(r["code"])) == 6 and str(r["code"]).isdigit()]
    assert six_digit, f"真实行里没有一只是 6 位代码：{[r['code'] for r in real['rows']][:5]}"


def test_r93_price_columns_round_trip(live_conn: Any) -> None:
    """R93 补的 4 列必须能真写进去、真读出来（类型对得上、NULL 不被换成 0）。"""
    code = _probe_code("r93")
    at = dt.datetime(2026, 10, 22, 2, 5, tzinfo=dt.UTC)
    with live_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.intraday_seal_snapshot "
            "(at, code, sealed, bid1_lots, limit_price, price, prev_close, distance_pct, is_st) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (at, code, True, 12345.0, 9.49, 9.49, 8.63, 0.0, False),
        )
    # 直接查，**不走 load_***：那三个函数都带 _REAL_ROWS 过滤，哨兵行会被自己滤掉。
    # 这里验的是「列能不能写进去」而不是「过滤器生不生效」。
    with live_conn.cursor() as cur:
        cur.execute(
            "SELECT price, prev_close, distance_pct, is_st "
            "FROM public.intraday_seal_snapshot WHERE at = %s AND code = %s",
            (at, code),
        )
        row = cur.fetchone()
    assert row is not None, "探针行没写进去"
    price, prev_close, distance_pct, is_st = row
    assert price == 9.49
    assert prev_close == 8.63
    assert distance_pct == 0.0
    assert is_st is False
