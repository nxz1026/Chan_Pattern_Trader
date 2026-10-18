"""巡检里的「因子批次停摆」这一条（R67，P0-1 的**复发探测**）。**离线**。

## 为什么单开这条检查

P0-1（2026-10-17）：``factor_recompute.py`` 的账本按「股票」而不是「交易日」记账，
日跑从此空转，因子表最新日**冻结**在 10-15，全市场静默退到不复权价。而当时
**所有既有检查都是绿的** —— 它们看的全是「单只标的的水位 / stale」，没有一条回答
「因子表整体有没有跟着日历往前走」。

## 判据为什么不是 ``max(trade_date)``

按需补因子（``FactorEnsurer``，热池 50 ∪ 自选）**每天**都在往生产表写少量**当前**
交易日的行 ⇒ ``max(trade_date)`` 被这几十行顶到最新，滞后算出来是 0。
2026-10-17 现场就是这样：``max`` ＝ 10-16 而**只有 2 行**，5208 只仍停在 10-15。
所以判据取「最新一个**批次级**覆盖日」（当日覆盖 ≥ ``FACTOR_BATCH_MIN_CODES``）。

## 为什么正文里不能出现 K 线日与滞后天数

停摆期间 ``bar_max`` 和 ``lag_days`` **每天都在长**，写进 ``problems`` 就等于把
状态指纹（:func:`_state_key`）变成每日一变 ⇒ 一天一条「还是坏的」，正是 R63 要消掉
的噪音（``docs/known-traps.md`` 第 49 号：单调量写进 problems = 指纹自毁）。
所以正文只放停摆时**冻结**的批次覆盖日。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_inspection_factor_mod", ROOT / "scripts" / "run_inspection.py"
)
assert _spec is not None and _spec.loader is not None
ri = importlib.util.module_from_spec(_spec)
sys.modules["run_inspection_factor_mod"] = ri
_spec.loader.exec_module(ri)


class _Cur:
    """按顺序吐出 ``fetchone()`` 的结果，并记下 SQL 与参数。"""

    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.sql: list[str] = []
        self.params: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(sql)
        self.params.append(params)

    def fetchone(self) -> Any:
        return self.values.pop(0) if self.values else None


class _Conn:
    def __init__(self, values: list[Any]) -> None:
        self.cur = _Cur(values)

    def cursor(self) -> _Cur:
        return self.cur


# ── _factor_lag：只认批次级覆盖日，按交易日数滞后 ───────────────────────


def test_factor_lag_counts_trading_days(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn([(date(2026, 10, 15),), (date(2026, 10, 20),)])
    seen: dict[str, Any] = {}

    def fake_open_days(c: Any, start: date, end: date) -> list[date]:
        seen["start"], seen["end"] = start, end
        return [date(2026, 10, 16), date(2026, 10, 19)]  # 周末不算

    monkeypatch.setattr(ri, "open_days_between", fake_open_days)
    assert ri._factor_lag(conn) == ("2026-10-15", 2)
    # 起点必须是「批次覆盖日的次日」，否则会把已覆盖的那天算成滞后
    assert seen == {"start": date(2026, 10, 16), "end": date(2026, 10, 20)}
    assert "public.daily_bar" in conn.cur.sql[1]


def test_batch_coverage_guard_is_in_the_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这条用例专门钉住「假新鲜」**：判据必须带覆盖率闸门。

    去掉 ``HAVING count(DISTINCT code) >= 500`` 就退回 ``max(trade_date)``，
    于是按需补因子的几十行会让一条停摆的流水线看起来一切正常 —— 2026-10-17 的现场。
    """
    monkeypatch.setattr(ri, "open_days_between", lambda c, s, e: [])
    conn = _Conn([(date(2026, 10, 15),), (date(2026, 10, 16),)])
    ri._factor_lag(conn)

    sql = conn.cur.sql[0]
    assert "count(DISTINCT code)" in sql
    assert "HAVING" in sql
    assert "asel.ref_adjust_factor" in sql
    assert conn.cur.params[0] == (ri.FACTOR_BATCH_MIN_CODES,)
    assert 100 < ri.FACTOR_BATCH_MIN_CODES < 5200, "闸门要大于按需日写入量、小于全市场"


def test_factor_lag_is_none_when_unreadable() -> None:
    """新增检查读不到就静默跳过 —— 不让它自己变成一条新噪音。"""

    class _Boom:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    assert ri._factor_lag(_Boom()) is None


def test_factor_lag_is_none_when_no_batch_day_or_no_bars() -> None:
    # 从没跑过批次（HAVING 全被过滤掉）
    assert ri._factor_lag(_Conn([(None,), (date(2026, 10, 16),)])) is None
    # 本地一根 K 线都没有
    assert ri._factor_lag(_Conn([(date(2026, 10, 15),), (None,)])) is None


# ── build_report 接线 ───────────────────────────────────────────────────


def _report(monkeypatch: pytest.MonkeyPatch, lag: tuple[str, int] | None) -> dict[str, Any]:
    """用最小假环境跑一遍 build_report（不碰 DB、不探数据源）。"""
    monkeypatch.setattr(ri, "_recent_rows", lambda conn: [])
    monkeypatch.setattr(ri, "_factor_lag", lambda conn: lag)
    monkeypatch.setattr(
        "cpt.adapters.source_registry.capabilities_payload",
        lambda **kwargs: {"sources": []},
    )
    return ri.build_report(object())


def test_stalled_factor_batch_becomes_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report(monkeypatch, ("2026-10-15", 2))
    hit = [p for p in report["problems"] if "因子批次停摆" in p]
    assert len(hit) == 1
    assert "2026-10-15" in hit[0]


def test_one_day_lag_is_not_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    """K 线当天入库、因子 cron 次日凌晨才跑 ⇒ 白天永远是滞后 1 天，不是故障。"""
    report = _report(monkeypatch, ("2026-10-16", 1))
    assert not [p for p in report["problems"] if "因子批次停摆" in p]


def test_unreadable_factor_table_is_not_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report(monkeypatch, None)
    assert not [p for p in report["problems"] if "因子批次停摆" in p]


def test_fingerprint_is_stable_while_stalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这条用例专门钉住 R63 / 陷阱 49**：停摆期间 K 线日与滞后天数都在长，
    但批次覆盖日冻结 —— 状态指纹必须**不变**，否则每天重发一条「还是坏的」。
    """
    first = _report(monkeypatch, ("2026-10-15", 2))
    later = _report(monkeypatch, ("2026-10-15", 5))  # 又过了三个交易日
    assert ri._state_key(first) == ri._state_key(later)


def test_fingerprint_changes_when_factor_batch_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复（或继续前进）必须能被看出来 —— 否则「从坏到好」那条通知发不出去。"""
    stalled = _report(monkeypatch, ("2026-10-15", 3))
    recovered = _report(monkeypatch, ("2026-10-20", 0))
    assert ri._state_key(stalled) != ri._state_key(recovered)


def test_problem_text_omits_the_monotone_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """正文里不能出现 K 线日与滞后天数（都在每天变）。"""
    report = _report(monkeypatch, ("2026-10-15", 4))
    hit = [p for p in report["problems"] if "因子批次停摆" in p][0]
    assert "2026-10-2" not in hit  # 没有 K 线那侧的日期
    assert "4 个交易日" not in hit
    assert "≥2" in hit  # 阈值是常量，可以写
