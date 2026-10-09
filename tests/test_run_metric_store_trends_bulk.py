"""``run_metric_store.waterline_trends_bulk``：一条 SQL 取代 inspection 的 N+1（R59 / 审计 M5）。

## 为什么要有这个文件

``/api/dashboard/inspection`` 原来对每个 distinct ``(market, symbol)`` 调一次
``waterline_trend``，上限 20 个 key 就是 20 次往返。批量版用一条 ``LATERAL``
语句按 key 各取 ``limit`` 行 —— **语义必须与逐个调用完全一致**，否则前端看到的
趋势会悄悄变形（少一条变化、顺序反了、limit 口径不同）。

分两层验证：

1. **离线**（假连接，沿用 ``tests/test_run_metric_store_prune.py`` 的范式）：
   SQL 形状、参数展平、重复 key 去重、空 keys 不发 SQL、按 key 分组、
   组内仍按时间升序做 diff、未请求的 key 被忽略。
2. **真库**（``CPT_REQUIRE_DB=1`` 时连不上就是红，同 ``tests/test_track.py``）：
   ``waterline_trends_bulk`` 与逐个 ``waterline_trend`` 的返回值**完全相等**，
   含 ``samples``/``first_seen``/``last_seen``/``latest``/``changes`` 与 limit 截断。

真库用例只写自家 ``market`` 前缀的 marker 行、收尾按该前缀删除；请对测试库运行
（CI 的 ``cpt_test`` / 本地临时集群）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cpt.storage import run_metric_store as rms

# ── 离线：假连接 ────────────────────────────────────────────────────


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.calls.append((" ".join(sql.split()), list(params)))

    def fetchall(self) -> list[Any]:
        return list(self._conn.rows)


class _Conn:
    """只记录 SQL/参数、返回预置行的假连接（不碰 DB）。"""

    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, list[Any]]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _metric(market: str, symbol: str, observed_at: int, **fields: Any) -> tuple[Any, ...]:
    """按 ``_COLUMNS`` 顺序造一行。

    ⚠️ 假游标的 ``fetchall`` 必须返回**行元组**：``_row_to_dict`` 是
    ``zip(_COLUMNS, row)`` 的位置映射，喂 dict 会把字典的**键名**当成列值
    （第一版就这么错了：所有 key 都读成 ``observed_at``，趋势全是零样本）。
    """
    values: dict[str, Any] = {
        "market": market,
        "symbol": symbol,
        "observed_at": observed_at,
        **fields,
    }
    return tuple(values.get(name) for name in rms._COLUMNS)  # noqa: SLF001 — 测试要对齐真实列序


def test_empty_keys_never_touches_the_database() -> None:
    conn = _Conn()
    assert rms.waterline_trends_bulk(conn, []) == {}  # type: ignore[arg-type]
    assert conn.calls == []


def test_one_query_per_call_with_flattened_params() -> None:
    """重复 key 去重；参数展平成 (m, s, m, s, …) + 末尾 capped limit。"""
    conn = _Conn()
    keys = [("a_share", "600000"), ("a_share", "600000"), ("crypto", "BTCUSDT")]

    out = rms.waterline_trends_bulk(conn, keys, limit=99999)

    assert len(conn.calls) == 1, "批量版必须只发一条 SQL"
    sql, params = conn.calls[0]
    assert "CROSS JOIN LATERAL" in sql
    assert "VALUES" in sql
    assert "::text" in sql
    assert sql.count("LIMIT %s") == 1
    # 每行一个 key 两个参数，末尾是 capped limit（与 recent_metrics 同上限 1000）
    assert params == ["a_share", "600000", "crypto", "BTCUSDT", 1000]
    assert set(out) == {("a_share", "600000"), ("crypto", "BTCUSDT")}
    assert out[("a_share", "600000")] == {
        "market": "a_share",
        "symbol": "600000",
        "samples": 0,
        "changes": [],
    }


def test_limit_is_capped_to_at_least_one() -> None:
    conn = _Conn()
    rms.waterline_trends_bulk(conn, [("a_share", "600000")], limit=0)
    assert conn.calls[0][1] == ["a_share", "600000", 1]


def test_groups_rows_by_key_and_keeps_ascending_diff_order() -> None:
    """假游标按 SQL 的口径给行（组内时间倒序）；输出仍按升序算 diff。"""
    rows = [
        _metric("a_share", "600000", 300, bar_count=3, health="ok"),
        _metric("a_share", "600000", 200, bar_count=2, health="ok"),
        _metric("a_share", "600000", 100, bar_count=1, health="ok"),
        # 没被请求的 key：既不进结果、也不能混进别人的趋势
        _metric("other", "999999", 300, bar_count=9),
    ]
    conn = _Conn(rows)

    out = rms.waterline_trends_bulk(conn, [("a_share", "600000"), ("a_share", "300001")], limit=2)

    trend = out[("a_share", "600000")]
    assert trend["samples"] == 3
    assert trend["first_seen"] == 100
    assert trend["last_seen"] == 300
    assert trend["latest"]["bar_count"] == 3
    # 升序两两比较 ⇒ at 是**后一行**的时间；只有真变了的字段才进 changed
    assert [c["at"] for c in trend["changes"]] == [200, 300]
    assert trend["changes"][0]["changed"] == {"bar_count": [1, 2]}
    # 请求了但库里没有的 key → 零样本形态，绝不静默缺席
    assert out[("a_share", "300001")] == {
        "market": "a_share",
        "symbol": "300001",
        "samples": 0,
        "changes": [],
    }


def test_offline_equivalence_with_per_key_waterline_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一份行：批量结果与逐个 ``waterline_trend`` **逐字段相等**。

    两条路径拿到的行形状不同：``waterline_trend`` 走 ``recent_metrics``（返回
    ``_row_to_dict`` 之后的 **dict**），批量版直接吃游标返回的**行元组**。
    这里用同一份原始行喂两边，等价性才有意义。
    """
    raw = {
        ("a_share", "600000"): (
            _metric("a_share", "600000", 300, bar_count=3, health="ok", dataset_hash="d2"),
            _metric("a_share", "600000", 200, bar_count=2, health="ok", dataset_hash="d2"),
            _metric("a_share", "600000", 100, bar_count=1, health="degraded", dataset_hash="d1"),
        ),
        ("crypto", "BTCUSDT"): (
            _metric("crypto", "BTCUSDT", 500, bar_count=7, distractor="ignored"),
            _metric("crypto", "BTCUSDT", 400, bar_count=7, distractor="ignored"),
        ),
    }
    as_dicts = {
        key: tuple(rms._row_to_dict(row) for row in rows)  # noqa: SLF001 — 复刻 recent_metrics 的出参
        for key, rows in raw.items()
    }
    monkeypatch.setattr(
        rms,
        "recent_metrics",
        lambda conn, *, market=None, symbol=None, limit=200: as_dicts[(market, symbol)],
    )
    conn = _Conn([row for rows in raw.values() for row in rows])

    bulk = rms.waterline_trends_bulk(conn, list(raw), limit=5)

    for key in raw:
        assert bulk[key] == rms.waterline_trend(conn, market=key[0], symbol=key[1], limit=5)
    # 只有 distractor 一个字段变化、但它不在 tracked 里 ⇒ 没有变化条目
    assert bulk[("crypto", "BTCUSDT")]["changes"] == []
    assert bulk[("a_share", "600000")]["changes"][0]["changed"]["health"] == ["degraded", "ok"]


# ── 真库：等价性（连不上则 skip；``CPT_REQUIRE_DB=1`` 时是红） ────────


def _require_db() -> bool:
    return os.environ.get("CPT_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


def _open_pg_conn() -> Any:
    try:
        import psycopg  # noqa: F401 — 只探可用性
    except ImportError as exc:
        message = f"psycopg 不可用（需要 `pip install -e '.[db]'`）: {exc}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)

    from cpt.adapters.a_share_local import AShareLocalClient

    try:
        return AShareLocalClient()._get_conn()  # noqa: SLF001 — 同 track_api 用法
    except Exception as exc:  # noqa: BLE001
        message = f"DB not reachable: {type(exc).__name__}: {str(exc)[:120]}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def metric_db() -> Any:
    """建表（幂等）后的连接 + 本用例专属的 ``market`` marker；收尾删干净。"""
    conn = _open_pg_conn()
    rms.ensure_table(conn)
    conn.commit()
    marker = f"bulk-{uuid.uuid4().hex[:8]}"
    yield conn, marker
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.cpt_run_metric WHERE market = %s", (marker,))
    conn.commit()
    conn.close()


def test_bulk_equals_per_key_on_real_database(metric_db: tuple[Any, str]) -> None:
    """真库上逐 key 对比：``LATERAL`` + ``LIMIT`` 的口径必须与旧实现一致。"""
    conn, marker = metric_db
    base = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for symbol_index, symbol in enumerate(("600000", "600001", "600002")):
        for step in range(5):
            rows.append(
                {
                    "market": marker,
                    "symbol": symbol,
                    "kind": rms.KIND_RUN,
                    "observed_at": base + timedelta(days=step, minutes=symbol_index),
                    "bar_count": step + symbol_index,
                    "bi_count": step,
                    "health": "ok" if step < 3 else "degraded",
                    "dataset_hash": f"ds-{symbol_index % 2}",
                }
            )
    assert rms.append_metrics(conn, rows) == len(rows)
    conn.commit()

    keys = [(marker, "600000"), (marker, "600001"), (marker, "600002"), (marker, "600009")]
    bulk = rms.waterline_trends_bulk(conn, keys, limit=3)
    expected = {
        key: rms.waterline_trend(conn, market=key[0], symbol=key[1], limit=3) for key in keys
    }

    assert bulk == expected
    assert bulk[(marker, "600000")]["samples"] == 3
    assert bulk[(marker, "600000")]["latest"]["bar_count"] == 4
    assert bulk[(marker, "600009")] == {
        "market": marker,
        "symbol": "600009",
        "samples": 0,
        "changes": [],
    }
    # 同样的夹紧规则也走 SQL（limit=0 ⇒ 每 key 1 行）
    single = rms.waterline_trends_bulk(conn, [(marker, "600001")], limit=0)
    assert single[(marker, "600001")] == rms.waterline_trend(
        conn, market=marker, symbol="600001", limit=0
    )
    assert single[(marker, "600001")]["samples"] == 1
