"""对账报告的台阶配对（R44）。**离线**：注入假 cursor，不碰 DB。

## 背景：报告曾经给出一个**看起来很严重**的结论

2026-10-03 跑全量 5017 只，报告说：

    除权日台阶总体匹配率：10723/13108 = 81.8%
    台阶对不齐  1223 只

差点因此判定「重算不靠谱、不能切表」。

而用独立判据（拿 ``daily_bar`` 的前收与实际收盘算理论除权价）逐日核对，
最差的几只票 **18/18 全部精确相等**。数据是对的，错的是**报告**。

## 根因

``analyse()`` 拿**生产表的下标** ``i`` 去索引**暂存表**：``new[i - 1]``。
两张表起点/行数不同：

    生产 800 行  2023-06-15 ~ 2026-09-30
    暂存 666 行  2024-01-02 ~ 2026-09-30

于是配到了完全不同的日子（实测 603259 的 2024-06-25 被配上 2025-01-09），
算出的「台阶」没有物理意义。

修法：按**日期**查，而不是按下标。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "factor_report_mod", ROOT / "scripts" / "factor_report.py"
)
fr = importlib.util.module_from_spec(_spec)
sys.modules["factor_report_mod"] = fr
_spec.loader.exec_module(fr)


class _FakeCursor:
    """按表名返回预设行；只支持报告用到的两种查询。"""

    def __init__(self, prod: list[tuple[str, float]], new: list[tuple[str, float]]) -> None:
        self._data = {
            "asel.ref_adjust_factor": prod,
            "asel.ref_adjust_factor_v2": new,
        }
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        # ⚠️ 必须**先匹配长的**：``asel.ref_adjust_factor`` 是
        # ``asel.ref_adjust_factor_v2`` 的**子串**，先查短的那个会让暂存表查询
        # 拿到生产表的数据 —— 那样测试会**假通过**（两边数据相同，台阶当然对得上）。
        # 踩过，别改成朴素的 `if tbl in sql`。
        if "ref_adjust_factor_v2" in sql:
            self._rows = self._data["asel.ref_adjust_factor_v2"]
        elif "ref_adjust_factor" in sql:
            self._rows = self._data["asel.ref_adjust_factor"]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


def _seq(start: str, n: int) -> list[str]:
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def test_misaligned_indices_are_paired_by_date() -> None:
    """核心回归：生产表起点**早于**暂存表时，台阶仍要按同一天配对。

    构造：生产 800 行从 2023-06-15 起，暂存 666 行从 2024-01-02 起 ——
    正是 603259 那只票的形状。
    """
    prod_dates = _seq("2023-06-15", 800)
    new_dates = _seq("2024-01-02", 666)

    # 生产与暂存在共同区间里**完全一致**的因子（1.0 → 1.5 台阶）
    def factors(dates: list[str]) -> list[tuple[str, float]]:
        out = []
        for d in dates:
            out.append((d, 1.0 if d < "2025-03-10" else 1.5))
        return out

    prod = factors(prod_dates)
    new = factors(new_dates)
    # ⚠️ 先确认测试桩真的把两张表区分开了 —— 否则下面那个断言会因为
    # 「两边数据相同、台阶自然对得上」而**假通过**，测不到错位。
    cur = _FakeCursor(prod, new)
    assert len(fr._series(cur, "asel.ref_adjust_factor", "X")) == 800
    assert len(fr._series(cur, "asel.ref_adjust_factor_v2", "X")) == 666

    # 挑一个**下标必然错位**的除权日
    ex_date = "2025-03-10"
    rep = fr.analyse(cur, "TEST", [ex_date])

    assert rep is not None
    assert rep.ex_dates == 1, f"除权日没被计入（ex_dates={rep.ex_dates}）"
    assert rep.step_matched == 1, (
        f"台阶没配上（matched={rep.step_matched}）—— 说明仍在按下标配对。"
        f"最大台阶差 {rep.max_step_dev:.4f}"
    )
    assert rep.max_step_dev == 0.0, f"台阶差应为 0，实得 {rep.max_step_dev}"


def test_aligned_case_still_works() -> None:
    """两表起点相同（占位票的形状）时行为不变 —— 防止修过头。"""
    dates = _seq("2024-01-02", 300)
    factors = [(d, 1.0 if d < "2024-06-03" else 1.2) for d in dates]
    cur = _FakeCursor(factors, factors)
    rep = fr.analyse(cur, "TEST2", ["2024-06-03"])
    assert rep is not None
    assert rep.ex_dates == 1
    assert rep.step_matched == 1
    assert rep.max_step_dev == 0.0


def test_staging_missing_the_ex_date_is_skipped() -> None:
    """除权日在暂存表里没有 → 该日不计入分母（而不是记成失配）。"""
    dates = _seq("2024-01-02", 300)
    prod = [(d, 1.0 if d < "2024-06-03" else 1.2) for d in dates]
    # 暂存表刻意删掉除权日那天
    new = [(d, 1.0 if d < "2024-06-03" else 1.2) for d in dates if d != "2024-06-03"]
    cur = _FakeCursor(prod, new)
    rep = fr.analyse(cur, "TEST3", ["2024-06-03"])
    assert rep is not None
    assert rep.ex_dates == 0, "缺当天数据时不该计入除权日分母"
