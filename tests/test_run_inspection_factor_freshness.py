"""巡检里的「因子批次停摆」这一条（R67，P0-1 的**复发探测**）。**离线**。

## 为什么单开这条检查

P0-1（2026-10-17）：``factor_recompute.py`` 的账本按「股票」而不是「交易日」记账，
日跑从此空转，因子表最新日**冻结**在 10-15，全市场静默退到不复权价。而当时
**所有既有检查都是绿的** —— 它们看的全是「单只标的的水位 / stale」，没有一条回答
「因子表整体有没有跟着日历往前走」。

## 判据为什么不是 ``max(trade_date)``

按需补因子路径已删除（R96）
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
from datetime import UTC, date, datetime
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


# ── P1-5：水位流停摆的两条正文也不能带单调量 ─────────────────────────────
#
# 同一个病、另两个入口（R67 / 审计 P1-5）：`水位流已断 {age // 60000} 分钟` 与
# `A 股水位流已停 {cn_age_h:.0f} 小时` 把**单调增长**的时长写进了 problems，而
# `_state_key` 用全量 problems 做指纹 ⇒ R63「只在状态变化时发」当场失效：一条停摆
# 变成每天一条「还是坏的」。上面那条因子检查立的规矩（正文只放冻结值）必须对
# 同文件里所有告警一视同仁，否则下次照样从别的入口复发（陷阱 #30 的判据）。

_FROZEN_MS = int(datetime(2026, 10, 18, 0, 0, tzinfo=UTC).timestamp() * 1000)


def _stall_report(monkeypatch: pytest.MonkeyPatch, *, age_hours: float) -> dict[str, Any]:
    """一条 A 股健康但**很旧**的水位行 ⇒ 只该出「水位流已断」「A 股水位流已停」两条。"""
    monkeypatch.setattr(
        ri,
        "_recent_rows",
        lambda conn: [
            {
                "market": "cn",
                "symbol": "600519",
                "observed_at": _FROZEN_MS,
                "health": "ok",
                "bar_count": 119,
                "gap_count": 0,
                "stale": False,
            }
        ],
    )
    monkeypatch.setattr(ri, "_now_ms", lambda: _FROZEN_MS + int(age_hours * 3_600_000))
    monkeypatch.setattr(ri, "_factor_lag", lambda conn: None)
    monkeypatch.setattr(
        "cpt.adapters.source_registry.capabilities_payload",
        lambda **kwargs: {"sources": []},
    )
    return ri.build_report(object())


def test_watermark_stall_body_does_not_grow_with_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """**R67 / P1-5 回归**：停摆 40 小时与 50 小时的告警正文必须**逐字相同**。

    停多久只说明"还坏着"，不构成新状态 —— 正文一变，指纹就变，每天重发。
    """
    early = _stall_report(monkeypatch, age_hours=40)
    late = _stall_report(monkeypatch, age_hours=50)

    assert [p for p in early["problems"] if "水位流已断" in p], "全局那条必须报出来"
    assert [p for p in early["problems"] if "A 股水位流已停" in p], "A 股那条必须报出来"
    assert early["problems"] == late["problems"], "告警正文随停摆时长变化 = 每天重发"
    assert ri._state_key(early) == ri._state_key(late)


def test_watermark_stall_text_keeps_the_frozen_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    """去掉时长不等于丢诊断：冻结的「最后一行」时间戳必须在正文里。"""
    report = _stall_report(monkeypatch, age_hours=40)
    text = " ".join(report["problems"])
    assert "2026-10-18 00:00:00Z" in text
    assert f"阈值 {ri.STALE_METRIC_MS // 60000} 分钟" in text
    assert f"阈值 {ri.ASHARE_STREAM_STALL_HOURS} 小时" in text
    assert "40 小时" not in text and "2400 分钟" not in text


def test_watermark_recovery_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复必须能被看出来 —— 否则「从坏到好」那条通知会被指纹撞掉。"""
    stalled = _stall_report(monkeypatch, age_hours=40)
    recovered = _stall_report(monkeypatch, age_hours=0)
    assert not [p for p in recovered["problems"] if "水位流" in p]
    assert ri._state_key(stalled) != ri._state_key(recovered)
