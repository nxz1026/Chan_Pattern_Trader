"""回测层单测（``cpt/backtest``）。**不连库** —— 用假行喂给装配层。

真实形态的样本值来自 2026-10-21 实测（``daily_bar`` 3,445,867 行 vs
``derived_bar`` 3,445,646 行，221 行只有前者）。
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from cpt.backtest import panel as pnl
from cpt.backtest import stats as st

# ───────────────────────── 面板装配 ─────────────────────────


def _row(
    code: str,
    date: dt.date,
    *,
    close: float = 10.0,
    open_: float = 10.0,
    high: float = 10.0,
    low: float = 10.0,
    pre_close: float = 10.0,
    volume: float = 1e6,
    is_limit_up: bool | None = False,
    is_limit_down: bool | None = False,
    is_bomb: bool | None = False,
    is_one_word: bool | None = False,
    touched_limit: bool | None = False,
    hfq_factor: float | None = 1.0,
) -> tuple[object, ...]:
    """第 14 列是复权因子。默认 1.0（不改变价格），``None`` 表示缺因子。"""
    return (
        code,
        date,
        open_,
        high,
        low,
        close,
        pre_close,
        volume,
        is_limit_up,
        is_limit_down,
        is_bomb,
        is_one_word,
        touched_limit,
        hfq_factor,
    )


def _series(
    n: int,
    *,
    code: str = "600000",
    start: dt.date = dt.date(2026, 1, 5),
    step: float = 0.01,
    limit_up_at: set[int] | None = None,
) -> list[tuple[object, ...]]:
    """造 n 根连续 bar，每天涨 ``step`` 比例。``limit_up_at`` 指定哪天涨停。"""
    rows: list[tuple[object, ...]] = []
    close = 10.0
    d = start
    ups = limit_up_at or set()
    for i in range(n):
        rows.append(
            _row(
                code,
                d,
                close=close,
                pre_close=close / (1 + step) if i else close,
                is_limit_up=(i in ups),
            )
        )
        close = round(close * (1 + step), 4)
        d += dt.timedelta(days=1)
    return rows


def test_panel_computes_returns_as_fraction_not_percent() -> None:
    """``returns`` 存**小数**。存百分数会让下游少乘 100（R70 探针踩过）。"""
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), close=10.0 + i) for i in range(6)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    r = panel.return_at(1, 0)
    assert r == pytest.approx(11.0 / 10.0 - 1.0)


def test_panel_has_no_return_for_incomplete_window() -> None:
    """窗口不足 ⇒ ``None``。不许拿未来的 0 填充。"""
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), close=10.0) for i in range(3)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert panel.return_at(5, 0) is None
    assert panel.return_at(1, 2) is None


def test_panel_tradable_only_drops_limit_up_signal_days() -> None:
    """**C 的核心**：涨停日的买入机会不可执行，默认剔掉。

    这条钉住涨跌停标记真的在起作用 —— 没有它，回测基线会被涨停次日的
    高收益系统性抬高（实测涨停次日 +2.00%、胜率 58%，非涨停 +0.02%、48%）。
    """
    rows = _series(40, step=0.01, limit_up_at={20})
    with_flag = pnl._build_panel("600000", rows, end=dt.date(2026, 3, 1), tradable_only=True)
    without = pnl._build_panel("600000", rows, end=dt.date(2026, 3, 1), tradable_only=False)
    assert with_flag is not None and without is not None
    # 第 20 根在所有窗口上都不该有收益
    for h in pnl.HORIZONS:
        assert with_flag.return_at(h, 20) is None, f"h{h} 在涨停日仍有收益"
    # 关掉剔除后同一位置有值
    assert without.return_at(1, 20) is not None
    # 可交易样本数严格更少
    assert len(with_flag.returns) < len(without.returns)


def test_panel_tradable_only_keeps_future_limit_up_days() -> None:
    """**剔的是信号日涨停，不是未来某天涨停**。

    买进去之后它涨停了 —— 那是真实收益，不许剔。第一版如果把
    「窗口内出现过涨停」也剔掉，会把所有赚钱样本删干净。
    """
    rows = _series(60, step=0.01, limit_up_at={30})
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 3, 1), tradable_only=True)
    assert panel is not None
    # 第 20 天买入、第 30 天涨停、第 40 天卖出 → 这个样本必须还在
    assert panel.return_at(20, 20) is not None


def test_panel_applies_adjust_factor_to_prices() -> None:
    """复权因子必须乘到价格上 —— 否则除权日的假跌会污染收益。

    实测（300 只票、76,798 个 h20 样本）：后复权 +0.133% vs 原始价 +0.031%，
    29 个样本差异超过 15 个百分点。
    """
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), close=10.0, hfq_factor=1.5)
        for i in range(5)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert panel.close[0] == pytest.approx(15.0)
    assert panel.adjusted == (True,) * 5


def test_panel_skips_samples_without_adjust_factor() -> None:
    """缺因子 ⇒ 那根 bar 的收益**不出现**，而不是拿原始价顶替。

    「没有复权」与「复权后是原价」是两件事：后者只在因子恰好为 1 时成立。
    """
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), close=10.0 + i, hfq_factor=None)
        for i in range(5)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert panel.adjusted == (False,) * 5
    assert panel.returns == {}


def test_panel_mixed_factor_only_uses_adjusted_positions() -> None:
    """有的有因子、有的没有 ⇒ 只用有因子的位置算收益。"""
    rows = []
    for i in range(5):
        rows.append(
            _row(
                "600000",
                dt.date(2026, 1, 5) + dt.timedelta(days=i),
                close=10.0 + i,
                hfq_factor=(1.0 if i < 3 else None),
            )
        )
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert panel.adjusted == (True, True, True, False, False)
    # 只有 i=0,1,2 可作为起点，且终点也必须在窗内
    assert (1, 0) in panel.returns
    assert (1, 3) not in panel.returns


def test_panel_rejects_nonpositive_factor() -> None:
    """因子 <= 0 是脏数据，按缺失处理（乘上去会把价格变成 0 或负数）。"""
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), close=10.0, hfq_factor=0.0)
        for i in range(3)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert panel.adjusted == (False, False, False)
    assert panel.close[0] == pytest.approx(10.0)


def test_panel_treats_missing_tags_as_not_limit_up() -> None:
    """``LEFT JOIN`` 给出 ``NULL`` 标签（daily_bar 有、derived_bar 缺）。

    按「未标记涨停」处理是保守方向：宁可多算一个样本，也不悄悄少算。
    实测这类行有 221 个。
    """
    rows = [
        _row("600000", dt.date(2026, 1, 5) + dt.timedelta(days=i), is_limit_up=None)
        for i in range(5)
    ]
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=True)
    assert panel is not None
    assert panel.is_limit_up == (False,) * 5
    assert panel.return_at(1, 0) is not None


def test_panel_min_bars_filters_short_series() -> None:
    """太短的票整个丢掉 —— 结构层冷启动要 250 根，短序列算不出结构。"""
    rows = _series(5)
    panel = pnl._build_panel("600000", rows, end=dt.date(2026, 2, 1), tradable_only=False)
    assert panel is not None
    assert len(panel) == 5


def test_panel_is_empty_for_no_rows() -> None:
    assert pnl._build_panel("600000", [], end=dt.date(2026, 2, 1), tradable_only=False) is None


def test_load_panel_queries_storage_not_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_panel`` 必须走 :mod:`cpt.storage.backtest_panel_store` 取数。

    R86 实测过：直接写 SQL 时 ``check_sql_layering`` 因为 SQL 在变量里**漏报**，
    于是回测层成了一块不受门禁管的新层。这条钉住下沉的结果。
    """
    called: dict[str, object] = {}

    def fake_fetch(
        conn: object, *, start: dt.date, end: dt.date, codes: object = None
    ) -> list[tuple[object, ...]]:
        called["start"] = start
        called["end"] = end
        called["codes"] = codes
        return _series(30, code="600519")

    monkeypatch.setattr(pnl, "fetch_panel_rows", fake_fetch)
    out = pnl.load_panel(object(), start=dt.date(2026, 1, 1), end=dt.date(2026, 6, 1), min_bars=10)
    assert "600519" in out
    # end 必须往前多取，否则末尾窗口算不出
    assert called["end"] == dt.date(2026, 7, 1)


def test_panel_module_contains_no_sql() -> None:
    """回测层不许有 SQL 字面量（分层门禁的单元版）。"""
    import pathlib
    import re

    root = pathlib.Path(pnl.__file__).resolve().parent
    pattern = re.compile(r"\b(?:SELECT\s|INSERT\s+INTO\s|DELETE\s+FROM\s)", re.IGNORECASE)
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.name} 里有 SQL 字面量，回测层不该直接写 SQL"


# ───────────────────────── 统计 ─────────────────────────


def _s(code: str, day: int, ret: float) -> st.Sample:
    return st.Sample(code=code, date_ms=day, ret=ret)


def test_summarise_empty() -> None:
    s = st.summarise([])
    assert s.n == 0
    assert s.mean is None
    assert s.win_rate is None


def test_summarise_win_rate_excludes_zero() -> None:
    """``ret > 0`` 才算赢。等号不算 —— 平盘不是赢。"""
    s = st.summarise([_s("a", 1, 0.01), _s("a", 2, 0.0), _s("a", 3, -0.01)])
    assert s.win_rate == pytest.approx(1 / 3)


def test_summarise_high_mean_low_win_rate() -> None:
    """均值正、胜率 <50% 是真实场景（R70 的 confirmed 就是）。

    少数大涨拉高均值，一笔典型交易多半是亏的。
    """
    # 9 笔 -1%，1 笔 +20%
    rets = [-0.01] * 9 + [0.20]
    s = st.summarise([_s("a", i, r) for i, r in enumerate(rets)])
    assert s.mean is not None and s.mean > 0
    assert s.win_rate == pytest.approx(0.1)
    assert s.median is not None and s.median < 0


def test_summarise_indistinguishable_when_t_small() -> None:
    """噪声样本必须被判为「与 0 分不开」。"""
    rets = [0.001, -0.001] * 50
    s = st.summarise([_s("a", i, r) for i, r in enumerate(rets)])
    assert s.indistinguishable_from_zero


def test_summarise_zero_variance_is_certainty_not_noise() -> None:
    """方差为零 = 每个样本一模一样 = **确定性**，不是「与 0 分不开」。

    第一版把 ``t is None`` 当成噪声，于是「50 个样本全是 +5%」被判成
    与 0 分不开 —— 逻辑反得很彻底。
    """
    s = st.summarise([_s("a", i, 0.05) for i in range(50)])
    assert s.stdev == 0.0
    assert s.t_stat == math.inf
    assert s.indistinguishable_from_zero is False


def test_summarise_zero_variance_zero_mean_is_flat() -> None:
    """全 0 的样本：确定性，且确实与 0 不可区分（它就是 0）。"""
    s = st.summarise([_s("a", i, 0.0) for i in range(10)])
    assert s.indistinguishable_from_zero is True


def test_summarise_single_sample_has_no_t() -> None:
    s = st.summarise([_s("a", 1, 0.05)])
    assert s.n == 1
    assert s.t_stat is None


def test_paired_only_keeps_shared_days() -> None:
    """配对只认 ``(code, date)`` 两边都有。R70 的坑：不同天的差值不能配对。"""
    a = [_s("600000", 1, 0.05), _s("600000", 2, 0.01)]
    b = [_s("600000", 2, 0.00), _s("600000", 3, 0.02)]
    out = st.paired(a, b)
    assert out.n == 1
    assert out.mean == pytest.approx(0.01)


def test_paired_zero_when_no_overlap() -> None:
    a = [_s("600000", 1, 0.05)]
    b = [_s("600000", 9, 0.00)]
    out = st.paired(a, b)
    assert out.n == 0


def test_compare_refuses_to_call_non_overlapping_a_difference() -> None:
    """**R70 最贵的一条教训**：配对 n=0 时不许说「A 比 B 高」。

    非配对差值在任何样本单元下都能算出来，但那不是差异。
    """
    a = [_s("600000", i, 0.05) for i in range(1, 10)]
    b = [_s("600000", i, 0.00) for i in range(100, 110)]
    cmp = st.compare(a, b, label_a="提前买", label_b="等确认")
    assert cmp.paired.n == 0
    assert "配对比较不成立" in cmp.verdict
    assert "不能当差异读" in cmp.verdict


def test_compare_says_indistinguishable_for_noise() -> None:
    """**有方差**的噪声：均值 +0.1% 但每天正负抖动 —— 必须判成与 0 分不开。

    注意样本要有方差：全同值的序列是「确定性」不是噪声（见上面那条零方差测试）。
    """
    import random

    rng = random.Random(7)
    a = [_s("600000", i, 0.001 + rng.uniform(-0.06, 0.06)) for i in range(200)]
    b = [_s("600000", i, rng.uniform(-0.06, 0.06)) for i in range(200)]
    cmp = st.compare(a, b)
    assert "与 0 分不开" in cmp.verdict


def test_compare_reports_direction_when_real() -> None:
    a = [_s("600000", i, 0.05) for i in range(50)]
    b = [_s("600000", i, 0.00) for i in range(50)]
    cmp = st.compare(a, b, label_a="A", label_b="B")
    assert "A 比 B 高" in cmp.verdict
    assert cmp.paired.n == 50


def test_compare_handles_empty_groups() -> None:
    cmp = st.compare([], [_s("a", 1, 0.1)], label_a="A", label_b="B")
    assert "样本不足" in cmp.verdict


def test_compare_flags_mean_vs_win_rate_divergence() -> None:
    """均值正、胜率低要在结论里点出来。"""
    a = [_s("600000", i, -0.01) for i in range(9)] + [_s("600000", 9, 0.30)]
    b = [_s("600000", i, 0.0) for i in range(10)]
    cmp = st.compare(a, b, label_a="A", label_b="B")
    assert any("胜率" in note for note in cmp.notes)


def test_excess_over_market_drops_unknown_dates() -> None:
    """某天不在 ``market`` 里就丢样本 —— **不许补 0**（补 0 等于假设大盘没动）。"""
    samples = [_s("600000", 1, 0.05), _s("600000", 2, 0.03)]
    out = st.excess_over_market(samples, {1: 0.02})
    assert out.n == 1
    assert out.mean == pytest.approx(0.03)


def test_excess_over_market_zero_when_matching_market() -> None:
    samples = [_s("600000", i, 0.02) for i in range(10)]
    out = st.excess_over_market(samples, {i: 0.02 for i in range(10)})
    assert out.mean == pytest.approx(0.0)


def test_summary_dict_round_trips() -> None:
    d = st.summarise([_s("a", 1, 0.01), _s("a", 2, 0.02)]).as_dict()
    for key in (
        "n",
        "mean",
        "median",
        "win_rate",
        "stdev",
        "t_stat",
        "indistinguishable_from_zero",
    ):
        assert key in d
    assert math.isfinite(float(d["mean"]))
