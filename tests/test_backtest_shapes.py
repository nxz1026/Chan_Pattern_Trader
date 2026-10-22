"""形状扫描单测。**不连库** —— 用假面板。

要钉住的东西都是 R88 吃过的亏：

1. **「新低」的定义不许漂**：``cl[i] <= min(cl[i-20:i])`` 与 ``[i-19:i]`` 是两个
   形状（差 5 倍样本）。测试把下标算死。
2. **基线必须同单元同预热**：形状的 n 与基线的 n 都从 ``warmup`` 起算，
   不许拿全量 K 线当基线（R70 的 4 千事件 vs 130 万 K 线）。
3. **看未来即失败**：任何 ``match`` 只许读 ``panel.close[i-lookback:i+1]``，
   测试用「把 i 之后的数据改成极端值，判定结果不变」来钉。
"""

from __future__ import annotations

import datetime as dt

import pytest
from cpt.backtest import shapes as sp
from cpt.backtest.panel import BarPanel


def _panel(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    adjusted: list[bool] | None = None,
    code: str = "600000",
) -> BarPanel:
    d0 = dt.date(2026, 1, 5)
    n = len(closes)
    dates = tuple(
        int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=dt.UTC).timestamp() * 1000)
        + i * 86_400_000
        for i in range(n)
    )
    op = opens if opens is not None else list(closes)
    adj = adjusted if adjusted is not None else [True] * n
    returns: dict[tuple[int, int], float] = {}
    for h in (1, 5, 10, 20):
        for i in range(n - h):
            if adj[i] and adj[i + h] and closes[i] > 0:
                returns[(h, i)] = closes[i + h] / closes[i] - 1.0
    return BarPanel(
        code=code,
        dates_ms=dates,
        open=tuple(op),
        high=tuple(closes),
        low=tuple(closes),
        close=tuple(closes),
        pre_close=tuple(closes),
        volume=tuple(1e6 for _ in closes),
        is_limit_up=tuple(False for _ in closes),
        is_limit_down=tuple(False for _ in closes),
        is_bomb=tuple(False for _ in closes),
        is_one_word=tuple(False for _ in closes),
        touched_limit=tuple(False for _ in closes),
        returns=returns,
        adjusted=tuple(adj),
    )


# ───────────────────────── 形状定义 ─────────────────────────


def test_shape_names_are_unique() -> None:
    """撞名会让 JSON 键互相覆盖 —— 实跑踩过（两个 low_20_up）。"""
    names = [s.name for s in sp.SHAPES]
    assert len(names) == len(set(names))


def test_every_shape_says_how_it_is_measured() -> None:
    for s in sp.SHAPES:
        assert s.note, f"{s.name} 没有量法说明"
        assert s.lookback >= 1


def test_low_20_window_is_the_20_bars_before_today() -> None:
    """``cl[i] <= min(cl[i-20:i])``：窗口是前 20 根，**不含当天**。

    造一段 19 根 100 + 第 20 根（下标 19）跌到 95，然后一路回到 100 并收阳。

    - 下标 19：窗口 ``cl[0:19]`` 全 100，``95 <= 100`` ⇒ 命中（昨天之前无更低点）。
    - 下标 20：窗口 ``cl[0:20]`` 里含 ``cl[19]=95``，``100 <= 95`` 不成立 ⇒ 不命中。
    - 下标 21：窗口 ``cl[1:21]`` 里含 ``95``，``cl[21]=100`` ⇒ 不命中。

    这条钉死「窗口是 20 根且不含当天」—— R88 踩过 ``[i-19:i]`` 差 5 倍样本。
    """
    # 前 21 根 100 → 第 21 根（下标 21）跌到 95，第 22 根回到 100 并收阳。
    closes = [100.0] * 21 + [95.0, 100.0]
    opens = [100.0] * 21 + [94.0, 99.0]  # 后两根都收阳（close > open）
    panel = _panel(closes, opens=opens)
    shape = sp.low_after_n_day_low(days=20)
    # 下标 21：窗口 cl[1:21] 全 100，95 <= 100 ⇒ 命中
    assert shape.match(panel, 21) is True
    # 下标 22：窗口 cl[2:22] 里含 cl[21]=95，100 <= 95 不成立 ⇒ 不命中
    assert shape.match(panel, 22) is False
    # 下标 19：只有 19 根历史，不够 20 根窗口 ⇒ 不命中
    assert shape.match(panel, 19) is False


def test_low_20_rejects_a_day_that_is_not_up() -> None:
    closes = [100.0] * 20 + [99.0]
    opens = [100.0] * 20 + [100.0]  # 收阴
    panel = _panel(closes, opens=opens)
    assert sp.low_after_n_day_low(days=20).match(panel, 20) is False


def test_low_20_rejects_close_above_the_window_minimum() -> None:
    closes = [100.0] * 19 + [90.0, 95.0]
    opens = [100.0] * 19 + [90.0, 90.0]  # 最后一根收阳
    panel = _panel(closes, opens=opens)
    # 下标 20 的窗口 cl[0:20] 里含 cl[19]=90，cl[20]=95 > 90 ⇒ 不算新低
    assert sp.low_after_n_day_low(days=20).match(panel, 20) is False


def test_near_low_2pct_takes_strictly_more_samples() -> None:
    """``tol=1.02`` 必须比 ``tol=1.0`` 宽 —— 这是两个不同形状，不是同一个。"""
    closes = [100.0] * 19 + [90.0, 91.5]
    opens = [100.0] * 19 + [90.0, 90.0]
    panel = _panel(closes, opens=opens)
    assert sp.low_after_n_day_low(days=20, tol=1.0).match(panel, 20) is False
    assert sp.low_after_n_day_low(days=20, tol=1.02).match(panel, 20) is True


def test_drop_pct_shape_requires_the_prior_drop() -> None:
    # 前 20 根从 100 跌到 95（-5%），当天收阳
    closes = [100.0 - i * 0.25 for i in range(19)] + [95.0, 96.0]
    opens = list(closes)
    opens[20] = 94.0  # 收阳
    panel = _panel(closes, opens=opens)
    shape = sp.up_after_down_streak(days=20, pct=0.04)
    assert shape.match(panel, 20) is True

    # 同一序列，要求跌超 10% ⇒ 不成立
    assert sp.up_after_down_streak(days=20, pct=0.10).match(panel, 20) is False


def test_below_ma_shape_needs_close_below_the_average() -> None:
    closes = [100.0] * 19 + [99.0]
    opens = [100.0] * 19 + [98.0]
    panel = _panel(closes, opens=opens)
    assert sp.dip_below_ma(span=20).match(panel, 19) is True

    closes2 = [100.0] * 19 + [101.0]
    opens2 = [100.0] * 19 + [100.0]
    panel2 = _panel(closes2, opens=opens2)
    assert sp.dip_below_ma(span=20).match(panel2, 19) is False


def test_below_ma_rejects_span_below_one() -> None:
    with pytest.raises(ValueError):
        sp.dip_below_ma(span=0)


# ───────────────────────── 前视不变式 ─────────────────────────


@pytest.mark.parametrize("shape", sp.SHAPES, ids=[s.name for s in sp.SHAPES])
def test_shapes_never_look_at_the_future(shape: sp.Shape) -> None:
    """把第 i 根之后的收盘价改成极端值，判定结果不许变。

    这是「不许用未来信息筛样本」（R70/R87 的根因）的机器化检查。
    """
    closes = [100.0 - ((i * 37) % 11) * 0.5 for i in range(60)]
    opens = [c - 0.3 for c in closes]
    base = _panel(closes, opens=opens)
    tampered_closes = list(closes)
    tampered_closes[41:] = [c * 100 for c in closes[41:]]  # i=40 之后全部篡改
    tampered = _panel(tampered_closes, opens=opens)
    for i in range(shape.lookback, 41):
        assert shape.match(base, i) == shape.match(tampered, i), (
            f"{shape.name} 在第 {i} 根的判定受未来数据影响了"
        )


# ───────────────────────── 基线口径 ─────────────────────────


def test_baseline_and_shape_share_warmup_and_unit() -> None:
    """基线不许拿「预热前的 K 线」凑数 —— 两者都从 warmup 起算。"""
    closes = [100.0] * 30 + [95.0] * 10 + [100.0] * 30
    panel = _panel(closes)
    panels = {panel.code: panel}
    h = 5
    base = sp.baseline(panels, horizon=h, warmup=20)
    shape = sp.shape_samples(panels, sp.SHAPES[0], horizon=h, warmup=20)
    # 基线覆盖所有 warmup 之后有窗口的位置
    assert len(base) == len(panel) - h - 20
    # 形状是基线的子集
    base_keys = {(s.code, s.date_ms) for s in base}
    assert all((s.code, s.date_ms) in base_keys for s in shape)


def test_warmup_changes_the_answer() -> None:
    """R88 实测：不卡预热 h20 是 +2.52%，卡 250 根之后只剩 +0.25%。

    这里用一段「前 20 根涨、后 20 根平」的假数据验证方向：
    warmup=0 时样本含涨段，均值更高。
    """
    rises = [100.0 + i for i in range(20)]
    flat = [119.0] * 40
    panel = _panel(rises + flat)
    panels = {panel.code: panel}
    h = 5
    loose = sp.baseline(panels, horizon=h, warmup=0)
    strict = sp.baseline(panels, horizon=h, warmup=25)
    from cpt.backtest.stats import summarise

    assert summarise(loose).mean is not None
    assert summarise(strict).mean is not None
    assert summarise(loose).mean > summarise(strict).mean


def test_unadjusted_positions_are_not_samples() -> None:
    """缺复权因子的位置不许算进收益（原始价在除权日会造假跌）。"""
    closes = [100.0 + i for i in range(40)]
    adjusted = [True] * 40
    adjusted[10] = False
    panel = _panel(closes, adjusted=adjusted)
    samples = sp.baseline({panel.code: panel}, horizon=5, warmup=0)
    dates = {s.date_ms for s in samples}
    assert panel.dates_ms[10] not in dates


# ───────────────────────── 报告 ─────────────────────────


def test_scan_reports_every_shape_for_every_horizon() -> None:
    panel = _panel([100.0 + (i % 7) for i in range(60)])
    rows = sp.scan({panel.code: panel}, horizons=(1, 5), warmup=20)
    assert len(rows) == len(sp.SHAPES) * 2
    for row in rows:
        assert row["shape"] in {s.name for s in sp.SHAPES}
        assert row["warmup"] == 20
        assert "base_n" in row


def test_describe_handles_an_empty_shape() -> None:
    text = sp.describe(
        {"title": "某形状", "horizon": 20, "n": 0, "mean": None, "win_rate": None, "base_mean": 0.1}
    )
    assert "样本不足" in text


def test_describe_prints_the_baseline_gap() -> None:
    text = sp.describe(
        {
            "title": "前 20 日跌超 4% 后收阳",
            "horizon": 20,
            "n": 100,
            "mean": 0.0405,
            "win_rate": 0.584,
            "base_mean": 0.0022,
        }
    )
    assert "+4.05%" in text
    assert "58.4%" in text
    assert "基线" in text


def test_today_ms_is_utc_midnight() -> None:
    got = sp.today_ms(dt.date(2026, 10, 22))
    expect = int(dt.datetime(2026, 10, 22, tzinfo=dt.UTC).timestamp() * 1000)
    assert got == expect


# ───────────────────────── CLI ─────────────────────────


def test_cli_rejects_unknown_shape(capsys: pytest.CaptureFixture[str]) -> None:
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("shape_scan_cli", root / "scripts/shape_scan.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shape_scan_cli"] = mod
    spec.loader.exec_module(mod)
    rc = mod.main(["--shape", "no_such_shape"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "未知形状" in err


def test_cli_lists_real_shapes_in_help() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("shape_scan_cli2", root / "scripts/shape_scan.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shape_scan_cli2"] = mod
    spec.loader.exec_module(mod)
    text = mod.build_parser().format_help()
    for s in sp.SHAPES:
        assert s.name in text, f"--shape 帮助里没有 {s.name}"


# ───────────────────── R89：三个近义定义不许混用 ─────────────────────


def test_drop_4pct_does_not_require_a_green_candle() -> None:
    """``drop_4pct`` 与探针第 33 行逐字对齐：**不看当天阴阳**。

    实跑踩过：把这条并进 ``if 收阳`` 里，样本从 20,189 涨到 228,660，
    均值从 +4.37% 塌到 +1.04% —— 三个近义定义差 3 个百分点。
    """
    # 前 20 根全 100，第 20 根（下标 20）跌到 95 并**收阴**
    closes = [100.0] * 20 + [95.0]
    opens = [100.0] * 20 + [99.0]  # 收阴
    panel = _panel(closes, opens=opens)
    assert sp.drop_in_window(days=20, pct=0.04).match(panel, 20) is True
    # 同一个位置，「且收阳」那个变体必须不成立
    assert sp.up_after_down_streak(days=20, pct=0.04).match(panel, 20) is False


def test_drop_4pct_up_differs_from_drop_4pct_by_one_condition() -> None:
    """两个形状只差「收阳」一个条件，名字也必须分开。"""
    a = sp.drop_in_window(days=20, pct=0.04)
    b = sp.up_after_down_streak(days=20, pct=0.04)
    assert a.name != b.name
    assert "阴阳" in a.title or "不看" in a.title
    assert "收阳" in b.title


def test_drop_4pct_uses_the_minimum_of_the_prior_window() -> None:
    """比较基准是**前 20 根的最低收盘 × 0.96**，不是「前 20 日累计跌幅」。"""
    # 前 20 根最低 100，累计几乎没跌；今天 95 —— 累计只跌 5%，但比最低价低 5%
    closes = [100.0] * 20 + [95.0]
    panel = _panel(closes, opens=[100.0] * 20 + [100.0])
    assert sp.drop_in_window(days=20, pct=0.04).match(panel, 20) is True

    # 缓跌 5%：前 20 根从 100 匀速跌到 95.1，今天 95 —— 累计跌 5% 但比窗口最低只低 0.1%
    closes2 = [100.0 - i * (100.0 - 95.1) / 19 for i in range(20)] + [95.0]
    panel2 = _panel(closes2, opens=[100.0] + closes2[:20])
    assert sp.drop_in_window(days=20, pct=0.04).match(panel2, 20) is False
