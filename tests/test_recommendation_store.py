"""推荐留痕的**失败语义**（R45 P2）。

## 为什么补

留痕是「让切表效果可回看」的唯一凭据。而它最危险的失效形态是
**静默**：

- 写失败被吞 ⇒ 历史悄悄变空，没人知道；
- 读失败返回空列表 ⇒ 前端显示「没有历史」，而真相是**库读不到**。

⇒ 这两个必须**抛**，由 web 层决定 best-effort 吞不吞（留痕不该让推荐 500）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.storage import recommendation_store as rs  # noqa: E402


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *e: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.calls.append((" ".join(sql.split())[:60], params))
        if self.conn.raise_on_execute:
            raise self.conn.raise_on_execute

    def fetchone(self):  # noqa: ANN201
        # ⚠️ 不能靠「SQL 里有 RETURNING」判断 —— store 把 SQL 压成了 60 字符，
        # ``RETURNING`` 早被截掉 ⇒ 永远判不到 ⇒ 返回 None（第一版就栽在这）。
        # 改成：写操作（``calls[-1]`` 是 INSERT）才给 id。
        return (7,) if self.conn.calls and self.conn.calls[-1][0].startswith("INSERT") else None

    def fetchall(self) -> list:
        return list(self.conn.rows)


class _Conn:
    def __init__(self, *, raise_on_execute: Exception | None = None, rows: list | None = None):
        self.calls: list = []
        self.raise_on_execute = raise_on_execute
        self.rows = rows or []

    def cursor(self) -> _Cur:
        """store 用的是 ``with conn.cursor() as cur`` —— 假连接必须有它。"""
        return _Cur(self)


REC = {
    "code": "600519",
    "level": "1d",
    "action": "hold",
    "raw_close": 1258.62,
    "status": "invalidated",
    "signal_type": "first_buy",
    "headline": "信号已失效",
    "reason": "一买已失效（不取反方向）",
    "data_quality": {"bars": 122, "min_bars": 30, "sufficient": True},
}


def test_append_returns_id_and_writes_all_fields() -> None:
    c = _Conn()
    rid = rs.append_recommendation(c, REC, epoch="2026-10-04T05:47:41+00:00")
    assert rid == 7
    sql, params = c.calls[-1]
    assert "INSERT INTO public.cpt_recommendation" in sql
    # 用**不复权**价落库（能挂单的那个），不是后复权价
    assert 1258.62 in params, f"落库价格不对：{params}"
    assert "2026-10-04" in str(params[-1])


def test_append_prefers_raw_close_over_price() -> None:
    """⚠️ 两套价格并存时**必须**用不复权那个 —— 后复权价挂不了单。"""
    c = _Conn()
    rs.append_recommendation(c, {**REC, "raw_close": 100.0, "price": 8886.5})
    assert 100.0 in c.calls[-1][1]
    assert 8886.5 not in c.calls[-1][1]


def test_write_failure_raises_not_swallowed() -> None:
    """写失败**必须抛** —— 静默会让历史悄悄变空而没人知道。"""
    c = _Conn(raise_on_execute=RuntimeError("db down"))
    with pytest.raises(rs.RecommendationPersistError):
        rs.append_recommendation(c, REC)


def test_read_failure_raises_not_empty_list() -> None:
    """⚠️ 读失败**绝不用空列表冒充「没有历史」**。"""
    c = _Conn(raise_on_execute=RuntimeError("db down"))
    with pytest.raises(rs.RecommendationPersistError):
        rs.recent_recommendations(c, code="600519")


def test_read_orders_newest_first() -> None:
    c = _Conn(
        rows=[
            (2, "600519", "1d", "buy", 1.0, "confirmed", "first_buy", "h", "r", 122, None, None),
            (1, "600519", "1d", "hold", 1.0, "invalidated", "first_buy", "h", "r", 122, None, None),
        ]
    )
    out = rs.recent_recommendations(c, code="600519")
    assert [r["id"] for r in out] == [2, 1], "必须是时间倒序（最新在前）"
    assert out[0]["action"] == "buy"


def test_read_empty_is_empty_list_not_error() -> None:
    """确实没有历史 ⇒ 空列表，**不是**错误。"""
    out = rs.recent_recommendations(_Conn(rows=[]), code="600519")
    assert out == []


def test_days_and_limit_are_clamped() -> None:
    """days/limit 必须夹到 ≥1 —— 0 或负数会产出 `make_interval(days => 0)`。"""
    c = _Conn()
    rs.recent_recommendations(c, code="600519", days=0, limit=0)
    _, params = c.calls[-1]
    assert params[1] >= 1 and params[2] >= 1, f"未夹紧：{params}"
