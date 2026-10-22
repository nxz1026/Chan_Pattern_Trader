"""回测执行器与信号层单测。**不连库** —— 用假面板和假后端。

重点钉三件事：
1. **前视不变式**：第 ``t`` 根的信号只许看到 ``bars[:t+1]``。
2. **配对纪律**：同一结构、两端都能成交，否则整对丢掉。
3. **不依赖未来**：``collect_status_samples`` 不许用「后来有没有 confirmed」筛样本。
"""

from __future__ import annotations

import datetime as dt

from cpt.backtest import runner as rn
from cpt.backtest import signals as sg
from cpt.backtest.panel import BarPanel
from cpt.backtest.signals import SignalAt, SignalTrace

# ───────────────────────── 假面板 ─────────────────────────


def _panel(
    closes: list[float],
    *,
    limit_up_at: set[int] | None = None,
    start: str = "2026-01-05",
) -> BarPanel:
    """造一个只有 close 有意义的假面板。``returns`` 按 ``close[t+h]/close[t]-1`` 算。"""
    ups = limit_up_at or set()
    d0 = dt.date.fromisoformat(start)
    n = len(closes)
    dates = tuple(
        int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=dt.UTC).timestamp() * 1000)
        + i * 86_400_000
        for i in range(n)
    )
    up = tuple(i in ups for i in range(n))
    returns: dict[tuple[int, int], float] = {}
    for h in (1, 5, 10, 20):
        for i in range(n - h):
            if up[i]:
                continue
            if closes[i] > 0:
                returns[(h, i)] = closes[i + h] / closes[i] - 1.0
    return BarPanel(
        code="600000",
        dates_ms=dates,
        open=tuple(closes),
        high=tuple(closes),
        low=tuple(closes),
        close=tuple(closes),
        pre_close=tuple(closes),
        volume=tuple(1e6 for _ in closes),
        is_limit_up=up,
        is_limit_down=tuple(False for _ in closes),
        is_bomb=tuple(False for _ in closes),
        is_one_word=tuple(False for _ in closes),
        touched_limit=tuple(False for _ in closes),
        returns=returns,
        adjusted=tuple(True for _ in closes),
    )


def _trace(code: str, signals: list[tuple[int, str, str]], *, structure: str = "S1") -> SignalTrace:
    """``signals`` 是 ``(index, status, divergence_status)``。"""
    objs = tuple(
        SignalAt(
            index=i,
            date_ms=1_700_000_000_000 + i * 86_400_000,
            status=status,
            structure_id=structure,
            divergence_status=ds,
            prev_status=None,
        )
        for i, status, ds in signals
    )
    counts: dict[str, int] = {}
    for _i, status, _ds in signals:
        counts[status] = counts.get(status, 0) + 1
    return SignalTrace(code=code, bars_count=60, signals=objs, status_counts=counts, skipped=0)


# ───────────────────────── 前视不变式（最重要） ─────────────────────────


class _SpyBackend:
    """记录每次被调时看到的最后一根 bar，用来证明没偷看未来。"""

    def __init__(self) -> None:
        self.seen_last: list[float] = []
        self.seen_len: list[int] = []

    def compute_structures(self, bars, config):  # noqa: ANN001, ARG002
        self.seen_last.append(bars[-1].close)
        self.seen_len.append(len(bars))
        return _EmptyResult()


class _EmptyResult:
    bi_list: tuple = ()
    zs_list: tuple = ()


class _Bar:
    __slots__ = ("close", "close_time")

    def __init__(self, close: float, close_time: int) -> None:
        self.close = close
        self.close_time = close_time


def test_signals_never_see_future_bars() -> None:
    """**第 t 根只能看到 bars[:t+1]** —— 回测唯一真正容易作弊的地方。

    假后端的 ``compute_structures`` 每次收到的最后一个 bar 的 close，
    必须正好等于 ``bars[t].close``，且长度正好是 ``t+1``。
    """
    bars = [_Bar(10.0 + i, 1_700_000_000_000 + i * 86_400_000) for i in range(30)]
    spy = _SpyBackend()
    sg.structure_signals(bars, code="600000", backend=spy, config=object(), warmup=20)
    assert spy.seen_len == list(range(21, 31)), spy.seen_len
    assert spy.seen_last == [10.0 + i for i in range(20, 30)]


def test_signals_skip_before_warmup() -> None:
    """预热期内的 bar 不产生任何调用。"""
    bars = [_Bar(10.0, 1_700_000_000_000 + i * 86_400_000) for i in range(10)]
    spy = _SpyBackend()
    trace = sg.structure_signals(bars, code="600000", backend=spy, config=object(), warmup=50)
    assert spy.seen_len == []
    assert trace.signals == ()
    assert trace.bars_count == 10


def test_signals_counts_skipped_bars() -> None:
    """笔为空的日子记 ``skipped``，不是「没有信号」。

    「没算」和「算了没信号」混在一起会让样本量看起来比实际大。
    """
    bars = [_Bar(10.0, 1_700_000_000_000 + i * 86_400_000) for i in range(10)]
    trace = sg.structure_signals(
        bars, code="600000", backend=_SpyBackend(), config=object(), warmup=5
    )
    assert trace.skipped == 5
    assert trace.signals == ()


def test_date_ms_handles_millisecond_epoch() -> None:
    """``close_time`` 是毫秒。按秒处理会抛 ValueError（R70 踩过）。"""
    bar = _Bar(10.0, 1_700_000_000_000)
    assert sg._date_ms(bar) == 1_700_000_000_000


def test_date_ms_upscales_seconds_defensively() -> None:
    """万一上游换成秒级，按量级放大，不静默算错。"""
    bar = _Bar(10.0, 1_700_000_000)
    assert sg._date_ms(bar) == 1_700_000_000_000


def test_trace_summary_counts_transitions_and_days() -> None:
    t1 = _trace(
        "600000", [(5, "structure_ready", "not_detected"), (8, "confirmed", "not_detected")]
    )
    t2 = _trace("600519", [(7, "structure_ready", "detected")])
    out = sg.trace_summary([t1, t2])
    assert out["codes"] == 2
    assert out["transitions"]["structure_ready"] == 2
    assert out["transitions"]["confirmed"] == 1


# ───────────────────────── 配对纪律 ─────────────────────────


def test_status_samples_take_every_ready_regardless_of_future() -> None:
    """**纪律三**：``structure_ready`` 是不是被采纳，不许看它后来有没有 confirmed。

    R70 的 +13.37% 假结论全部来自「只保留最终会 confirmed 的结构」。
    这里三个 ready，其中一个后来 confirmed，两个从没 confirmed ——
    三个都要进样本。
    """
    panel = _panel([10.0] * 60)
    trace = SignalTrace(
        code="600000",
        bars_count=60,
        signals=(
            SignalAt(10, 0, "structure_ready", "S1", "not_detected", None),
            SignalAt(20, 0, "structure_ready", "S2", "not_detected", None),
            SignalAt(30, 0, "structure_ready", "S3", "not_detected", None),
            SignalAt(15, 0, "confirmed", "S1", "not_detected", "structure_ready"),
        ),
        status_counts={"structure_ready": 3, "confirmed": 1},
        skipped=0,
    )
    got = rn.collect_status_samples(trace, panel, statuses=["structure_ready"])
    assert len(got["structure_ready"].by_horizon[5]) == 3


def test_status_samples_skip_limit_up_days() -> None:
    """涨停日不可成交，计数但不进样本。"""
    panel = _panel([10.0] * 60, limit_up_at={10})
    trace = _trace("600000", [(10, "structure_ready", "not_detected")])
    got = rn.collect_status_samples(trace, panel, statuses=["structure_ready"])
    assert len(got["structure_ready"].by_horizon[5]) == 0
    # 按 horizon 各计一次（4 个窗口 × 1 个涨停日），不是按事件计一次。
    assert got["structure_ready"].not_tradable == 4


def test_status_samples_summary_none_when_empty() -> None:
    """空样本返 None，不返一个 n=0 的假对象。"""
    panel = _panel([10.0] * 60)
    trace = _trace("600000", [])
    got = rn.collect_status_samples(trace, panel, statuses=["structure_ready"])
    assert got["structure_ready"].summary(5) is None


# ───────────────────────── 端到端 ─────────────────────────


def test_run_backtest_merges_across_codes() -> None:
    closes = [10.0] * 60
    p1 = _panel(closes)
    p2 = _panel(closes)
    t1 = _trace(
        "600000", [(10, "structure_ready", "not_detected"), (15, "confirmed", "not_detected")]
    )
    t2 = _trace(
        "600519", [(20, "structure_ready", "not_detected"), (25, "confirmed", "not_detected")]
    )
    res = rn.run_backtest(panels={"600000": p1, "600519": p2}, traces={"600000": t1, "600519": t2})
    assert res.codes == 2
    # 两个 structure_ready 事件都在样本里（confirmed 不参与「当买点」统计）
    assert len(rn.status_samples(res, rn.A_STATUS, 5)) == 2


def test_no_pairing_api_survives_r88() -> None:
    """R88：配对口径已删除，不许悄悄复活。

    删除的理由写在 ``runner`` 模块 docstring 里（样本被未来信息筛过）。想加回来
    必须同时改这份测试与文档 —— 这是故意的阻力。
    """
    assert not hasattr(rn, "ab_pairs")
    assert not hasattr(rn, "PairSet")
    assert not hasattr(rn.BacktestResult(codes=1), "pairs")


def test_run_backtest_ignores_trace_without_panel() -> None:
    """有 trace 没面板的票跳过，不炸。"""
    trace = _trace("600000", [(10, "structure_ready", "not_detected")])
    res = rn.run_backtest(panels={}, traces={"600000": trace})
    assert res.codes == 1
    # 三个状态键都在（statuses 默认值），但每个 horizon 都是空样本
    assert set(res.status) == {rn.A_STATUS, rn.B_STATUS, "invalidated"}
    for got in res.status.values():
        assert all(not v for v in got.by_horizon.values())


def test_runner_has_no_sql_and_no_io_imports() -> None:
    """回测层不写 SQL、不连库 —— 由分层门禁兜底，这里再钉一次单元版。

    判据走 AST 只认 **import 语句与字符串字面量里的 SQL**，不搜整份文件文本。
    第一版搜全文，于是 ``panel.py`` 的 docstring 里那句「每只票单独查
    ``derived_bar``」和一句 ``psycopg`` 的产品说明就被判成「有 DB 调用」。
    **注释里提一句不是调用** —— 这个坑本轮已经踩了第二次。
    """
    import ast
    import pathlib

    root = pathlib.Path(rn.__file__).resolve().parent
    sql = ("SELECT ", "INSERT INTO ", "DELETE FROM ", "UPDATE ")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"psycopg", "psycopg2"}:
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"psycopg", "psycopg2"}:
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for kw in sql:
                    if kw in upper and "FROM" in upper or (kw == "UPDATE " and " SET " in upper):
                        offenders.append(f"{path.name}:{node.lineno} SQL 字面量")
                        break
    assert offenders == [], f"回测层不该有这些：{offenders}"
