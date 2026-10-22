"""门禁⑱ ``check_derived_gap.py`` 的判据（R90）。

这道门禁连库（CI 不跑，由日报跑），所以这里不连真库，只钉三件事：

1. SQL 里「首日」的定义是 ``min(date)`` per code —— 与实测口径一致；
2. 只有**非首日**缺口才判红，首日缺口不判红；
3. 一句话能把事说清（报红文案里有「非上市首日」与「derive」）。

样例查询只用来定位，拿不到也不许影响判红 —— 有测试钉住。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_derived_gap", ROOT / "scripts" / "check_derived_gap.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append(sql)
        if self._conn.raise_on_samples and "LIMIT %s" in sql:
            raise RuntimeError("样例查询炸了")

    def fetchone(self) -> tuple:
        return (self._conn.first_day, self._conn.other_day)

    def fetchall(self) -> list[tuple]:
        return list(self._conn.samples)


class _Conn:
    def __init__(
        self,
        *,
        first_day: int = 0,
        other_day: int = 0,
        samples: list[tuple] | None = None,
        raise_on_samples: bool = False,
    ) -> None:
        self.first_day = first_day
        self.other_day = other_day
        self.samples = samples or []
        self.raise_on_samples = raise_on_samples
        self.executed: list[str] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_first_day_is_defined_as_min_date_per_code() -> None:
    """判据里的「首日」必须是 ``min(date)``，不是「第一行数据」之类。"""
    src = (ROOT / "scripts" / "check_derived_gap.py").read_text(encoding="utf-8")
    assert "min(date) AS first_date" in src
    assert "GROUP BY code" in src


def test_first_day_only_gaps_are_green() -> None:
    mod = _load()
    problems, lines, counts = mod.check(_Conn(first_day=221, other_day=0))
    assert problems == []
    assert counts == {"first_day": 221, "other_day": 0}
    assert any("221" in line for line in lines)


def test_any_non_first_day_gap_is_red() -> None:
    mod = _load()
    problems, _lines, counts = mod.check(_Conn(first_day=221, other_day=3))
    assert counts == {"first_day": 221, "other_day": 3}
    assert len(problems) == 1
    assert "非上市首日" in problems[0]
    assert "derive" in problems[0]


def test_no_gap_at_all_is_green() -> None:
    mod = _load()
    problems, _lines, counts = mod.check(_Conn())
    assert problems == []
    assert counts == {"first_day": 0, "other_day": 0}


def test_samples_are_listed_when_red() -> None:
    mod = _load()
    _problems, lines, _counts = mod.check(
        _Conn(other_day=2, samples=[("600000", "2026-10-22"), ("000001", "2026-10-22")])
    )
    joined = "\n".join(lines)
    assert "600000" in joined
    assert "样例" in joined


def test_sample_query_failure_does_not_hide_the_red() -> None:
    """样例只是定位用的 —— 它炸了也必须照样判红。"""
    mod = _load()
    problems, _lines, _counts = mod.check(_Conn(other_day=2, raise_on_samples=True))
    assert len(problems) == 1


def test_report_wiring_calls_the_gate() -> None:
    """日报必须真的接上这道门禁 —— 否则等于没做。"""
    src = (ROOT / "scripts" / "cron_daily_report.py").read_text(encoding="utf-8")
    assert "check_derived_gap.py" in src
    assert "_derived_gap_problems()" in src
    assert "derived_bar 缺口检查失败" in src
