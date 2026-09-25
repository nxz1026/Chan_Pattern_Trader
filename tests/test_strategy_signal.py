"""``cpt.adapters.strategy_signal`` 测试。**不碰真库**：用假游标。

重点覆盖三件容易错的事（都在真实数据上踩到过）：

1. ``confidence`` 是 ``numeric(4,3)`` → psycopg 回 ``Decimal``。**不转 float** 会
   在排序时抛 ``TypeError``、在 JSON 序列化时抛 ``TypeError``。
2. 口径是**先滤动作再综合排序**（``PASS`` 不进候选）。本数据里 score 与 confidence
   负相关，只按综合分排会把"低分高置信"的 PASS 票拉进来。
3. 同一 ``code`` 可能有多行（表的唯一键含 ``strategy`` / ``prompt_hash``）——
   不去重会让同一只票在下拉里出现两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from cpt.adapters.strategy_signal import (
    ELIGIBLE_ACTIONS,
    combined_score,
    fetch_strategy_top,
)

# 真实行的形状：(code, strategy, name, action, score, confidence, reason, model)
# ⚠️ name 是**策略名称**，不是股票名。
R_000498 = ("000498", "bull_trend", "默认多头趋势", "BUY", 88, Decimal("0.920"), "放量突破", "m")
R_000710 = ("000710", "bull_trend", "默认多头趋势", "PASS", 55, Decimal("0.700"), "—", "m")
R_000504 = ("000504", "bull_trend", "默认多头趋势", "PASS", 55, Decimal("0.800"), "—", "m")
R_000753 = ("000753", "bull_trend", "默认多头趋势", "WATCH", 55, Decimal("0.600"), "—", "m")
R_000678 = ("000678", "bull_trend", "默认多头趋势", "WATCH", 45, Decimal("0.600"), "—", "m")
# 低分高置信的 PASS（真实数据里 000607 就是这种）—— 必须被滤掉
R_000607 = ("000607", "bull_trend", "默认多头趋势", "PASS", 20, Decimal("0.900"), "—", "m")


class _FakeCursor:
    def __init__(self, source: list[tuple], max_row: tuple | None) -> None:
        self.source = source
        self.max_row = max_row
        self._result: list[Any] = []
        self.executed: list[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        flat = " ".join(sql.split()).lower()
        if flat.startswith("select max(trade_date)"):
            self._result = [] if self.max_row is None else [self.max_row]
        elif flat.startswith("select code, strategy"):
            self._result = list(self.source)
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class FakeConn:
    rows: list[tuple]
    max_row: tuple | None = (date(2026, 9, 24),)
    cursor_obj: _FakeCursor = field(init=False)

    def __post_init__(self) -> None:
        # 复用**同一个**游标实例：``fetch_strategy_top`` 内部是 ``with conn.cursor()``，
        # 外部拿不到那个对象，而测试要断言"到底发出去过哪些 SQL"。每次新建游标
        # 会让 ``executed`` 永远为空。
        self.cursor_obj = _FakeCursor(source=self.rows, max_row=self.max_row)

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj


# --------------------------------------------------------------------------- #
# 综合分
# --------------------------------------------------------------------------- #


def test_combined_score_normalizes_confidence_to_0_100() -> None:
    """置信 0.92 要当成 92 分参与，不能当成 0.92 —— 否则置信等于没算。"""
    assert combined_score(88, 0.92) == 90.0
    assert combined_score(55, 0.60) == 57.5
    assert combined_score(0, 0.0) == 0.0
    assert combined_score(100, 1.0) == 100.0


def test_eligible_actions_excludes_pass() -> None:
    assert ELIGIBLE_ACTIONS == frozenset({"BUY", "WATCH"})
    assert "PASS" not in ELIGIBLE_ACTIONS


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def test_fetch_strategy_top_filters_out_pass() -> None:
    """PASS 不进候选 —— 这是用户拍板的口径 D 的前半句。"""
    picks = fetch_strategy_top(FakeConn(rows=[R_000498, R_000710, R_000607]))
    assert [p.code for p in picks] == ["000498"]
    assert all(p.action in ELIGIBLE_ACTIONS for p in picks)


def test_fetch_strategy_top_sorts_by_combined_desc() -> None:
    picks = fetch_strategy_top(FakeConn(rows=[R_000678, R_000498, R_000753]))
    # 综合分：000498=90.0 > 000753=57.5 > 000678=52.5
    assert [p.code for p in picks] == ["000498", "000753", "000678"]
    assert picks[0].combined == 90.0


def test_fetch_strategy_top_respects_limit() -> None:
    rows = [R_000498, R_000753, R_000678]
    assert len(fetch_strategy_top(FakeConn(rows=rows), limit=2)) == 2
    assert len(fetch_strategy_top(FakeConn(rows=rows), limit=None)) == 3


def test_fetch_strategy_top_dedups_same_code_keeping_best() -> None:
    """同一 code 两行（不同 prompt_hash / strategy）只留综合分高的那条。

    不去重的话下拉里会出现两个 value 相同的 option，选中哪个都一样 ——
    正是"选错票"最容易发生的地方。
    """
    weaker = ("000498", "other", "别的策略", "WATCH", 30, Decimal("0.500"), "—", "m")
    picks = fetch_strategy_top(FakeConn(rows=[weaker, R_000498]))
    assert [p.code for p in picks] == ["000498"]
    assert picks[0].score == 88
    assert picks[0].strategy == "bull_trend"


def test_fetch_strategy_top_returns_empty_when_table_empty() -> None:
    """空表：``SELECT max(trade_date)`` 回 NULL 行，必须返回 [] 而不是抛。"""
    assert fetch_strategy_top(FakeConn(rows=[], max_row=(None,))) == []
    assert fetch_strategy_top(FakeConn(rows=[], max_row=None)) == []


def test_fetch_strategy_top_converts_decimal_confidence_to_float() -> None:
    """回归：``confidence`` 是 numeric → ``Decimal``，不转 float 会炸。

    ``Decimal`` 与 ``float`` 混算抛 ``TypeError``，且 ``json.dumps`` 无法序列化
    ``Decimal`` —— 后端路由会直接 500。
    """
    import json

    picks = fetch_strategy_top(FakeConn(rows=[R_000498]))
    assert isinstance(picks[0].confidence, float)
    assert isinstance(picks[0].combined, float)
    # 能整体序列化（路由要把它塞进 JSON）
    json.dumps({"confidence": picks[0].confidence, "combined": picks[0].combined})


def test_fetch_strategy_top_uses_strategy_name_not_stock_name() -> None:
    """``name`` 列是**策略名称**。这里钉住语义，防止有人把它当股票名显示。"""
    picks = fetch_strategy_top(FakeConn(rows=[R_000498]))
    assert picks[0].strategy_name == "默认多头趋势"
    assert picks[0].strategy == "bull_trend"


def test_fetch_strategy_top_handles_null_name_and_model() -> None:
    row = ("000498", "bull_trend", None, "BUY", 88, Decimal("0.920"), None, None)
    picks = fetch_strategy_top(FakeConn(rows=[row]))
    assert picks[0].strategy_name == ""
    assert picks[0].model is None
    assert picks[0].reason == ""


def test_fetch_strategy_top_sets_trade_date_from_max() -> None:
    picks = fetch_strategy_top(FakeConn(rows=[R_000498], max_row=(date(2026, 9, 24),)))
    assert picks[0].trade_date == "2026-09-24"


def test_fetch_strategy_top_sql_has_deterministic_tie_break() -> None:
    """SQL 的 ``ORDER BY`` 必须有 tie-break，否则"同 code 多行且综合分相等"时
    保留哪一行取决于数据库返回顺序 —— 同一份数据两次跑可能给出不同的候选。

    当前库里只有 1 个 strategy、且无同 code 多行，**触发不了**；但唯一键含
    ``strategy`` / ``prompt_hash``，加策略或加 prompt 版本后立刻就会遇到。
    """
    conn = FakeConn(rows=[R_000498])
    fetch_strategy_top(conn)
    # 第 0 条是 ``SELECT max(trade_date)``，取数那条是最后一条
    sql = " ".join(conn.cursor().executed[-1][0].split())
    assert "ORDER BY code, score DESC, confidence DESC" in sql
