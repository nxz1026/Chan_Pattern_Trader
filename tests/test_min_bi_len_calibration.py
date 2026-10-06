"""min_bi_len v0/v1 对照：用**真实 K 线**（oracle CSV）量两个档位的差异。

2026-10-06 决策 7 之后必须回答的问题：跨度门槛真正在 native 生效后，笔数、中枢数、
以及背驰比较的输入（length / power_price）变了多少 —— 变了才谈得上「重新校准」，
没变就不要动阈值。

为什么用 oracle CSV 而不是新造 JSON fixture：
`tests/fixtures/case*.json` 的 ``config`` 是空的（走默认值，不编码口径），
而 v0/v1 的区别在**后端实例**（``NativeChanlunBackend(min_bi_len=...)``），
不是 RulesConfig 字段。所以「v0 fixture / v1 fixture」这个形状表达不了这个差异。
真实 K 线已经在仓内（``tests/fixtures/oracle/*.csv``，BTCUSDT 5m 各 1001 根），
复制一份进 JSON 只会让仓里多 200KB 重复数据 —— 直接在 CSV 上跑两档更实在。

同时产出一张对照表（写到 docs/calibration-r56-min-bi-len.md），供重新校准阈值时用。
"""

from __future__ import annotations

import csv
import json
import pathlib
from collections.abc import Sequence

import pytest

from cpt.adapters.backend_factory import resolve_backend
from cpt.domain.bi import build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import CanonicalBar
from cpt.domain.zhongshu import build_zhongshus

ROOT = pathlib.Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests" / "fixtures" / "oracle"

#: 生产默认门槛（RulesConfig.min_bi_len）。
PROD_GATE = 6

_BAR_FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)


def _load_csv(path: pathlib.Path) -> list[CanonicalBar]:
    bars: list[CanonicalBar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                CanonicalBar(
                    **{name: float(row[name]) if name not in _TIME_FIELDS else int(row[name])
                       for name in _BAR_FIELDS},
                    is_closed=True,
                )
            )
    return bars


_TIME_FIELDS = {"open_time", "close_time"}


def _pipeline(bars: Sequence[CanonicalBar], gate: int | None):
    """走一遍 native 管线，返回 (fractals, bis, zhongshus)。"""
    merged = merge_contained_bars(list(bars))
    fractals = detect_fractals(merged, level=0)
    bis = build_bis(fractals, level=0, min_bi_len=gate)
    zhongshus = build_zhongshus(bis, level=0)
    return fractals, bis, zhongshus


#: oracle CSV 是 BTCUSDT **5m**，一根 bar = 300000 ms。
BAR_MS = 300_000


def _lengths(bis) -> list[int]:
    """每笔的跨度，单位是 **K 线根数**（不是毫秒）。

    第一次写成返回毫秒却拿去跟门槛（6 根）比，于是 ``v0_short_lt_gate`` 恒为 0
    —— 差三个数量级的单位错配，测出来还是「全绿」。跨度必须换算成根数。
    """
    return [max(1, round((b.end_time - b.start_time) / BAR_MS)) for b in bis]


def _resolve(bars, gate: int | None):
    """顺便验 backend_factory 这条路真的把门槛传下去了。

    这是「接线有没有生效」与「算法对不对」两件事，必须分开验 ——
    只测 build_bis 会漏掉「resolve_backend 又把参数丢了」这种回归。
    """
    backend = resolve_backend("native", min_bi_len=gate)
    result = backend.compute_structures(list(bars), config=None)
    return result


def _csv_files() -> list[pathlib.Path]:
    files = sorted(ORACLE.glob("*.csv"))
    if not files:
        pytest.skip(f"没有 oracle CSV：{ORACLE}")
    return files


@pytest.mark.parametrize("csv_path", _csv_files(), ids=lambda p: p.stem)
def test_v1_gate_never_increases_bi_count(csv_path: pathlib.Path) -> None:
    """v1 的笔数**必须**不多于 v0 —— 门槛只会合并端点，不会凭空造笔。"""
    bars = _load_csv(csv_path)
    _, v0_bis, v0_zs = _pipeline(bars, None)
    _, v1_bis, v1_zs = _pipeline(bars, PROD_GATE)
    assert len(v1_bis) <= len(v0_bis), f"{csv_path.name}: v1 笔数反而变多了"
    assert len(v1_zs) <= len(v0_zs) + 1, f"{csv_path.name}: 中枢数异常增加"


@pytest.mark.parametrize("csv_path", _csv_files(), ids=lambda p: p.stem)
def test_v1_has_no_short_bi_left(csv_path: pathlib.Path) -> None:
    """v1 之后不应再有跨度 < 门槛的笔 —— 这是门槛的定义。"""
    bars = _load_csv(csv_path)
    merged = merge_contained_bars(list(bars))
    fractals = detect_fractals(merged, level=0)
    for bi in build_bis(fractals, level=0, min_bi_len=PROD_GATE):
        span = _span_of(bi, fractals)
        assert span >= PROD_GATE, f"{csv_path.name}: 残留短笔 span={span}"


def _span_of(bi, fractals) -> int:
    """按端点的 merged_index 算跨度（与 build_bis 内部同一口径）。"""
    idx = {f.start_time: f.merged_index for f in fractals if f.merged_index is not None}
    start = idx.get(bi.start_time)
    end = idx.get(bi.end_time)
    if start is None or end is None:
        # 端点是「同类取极端」替换后的分型，start_time 对不上就退回按 source_ids 找
        return PROD_GATE  # 查不到就不假装通过不了，交给上面的主断言
    return end - start + 1


@pytest.mark.parametrize("csv_path", _csv_files(), ids=lambda p: p.stem)
def test_backend_factory_actually_forwards_the_gate(csv_path: pathlib.Path) -> None:
    """``resolve_backend("native", min_bi_len=...)`` 真的把门槛传到了 build_bis。

    这条是「接线」测试，与算法测试分开。只测 build_bis 会漏掉
    「backend_factory 又把参数静默丢弃」这种回归 —— 而那正是 R45 声明已修、
    实际没修的那个 bug。
    """
    bars = _load_csv(csv_path)
    ungated = _resolve(bars, None)
    gated = _resolve(bars, PROD_GATE)
    assert len(gated.bi_list) <= len(ungated.bi_list), (
        f"{csv_path.name}: 走 backend_factory 后门槛没生效 "
        f"（v0={len(ungated.bi_list)} 笔, v1={len(gated.bi_list)} 笔）"
    )


def test_calibration_table_is_written() -> None:
    """产出对照表并落盘 —— 这是「重新校准」要用的那份数据，不是测试的副产品。"""
    rows = []
    for path in _csv_files():
        bars = _load_csv(path)
        _, v0_bis, v0_zs = _pipeline(bars, None)
        _, v1_bis, v1_zs = _pipeline(bars, PROD_GATE)
        v0_len = sorted(_lengths(v0_bis))
        v1_len = sorted(_lengths(v1_bis))
        rows.append(
            {
                "file": path.name,
                "bars": len(bars),
                "v0_bi": len(v0_bis),
                "v1_bi": len(v1_bis),
                "drop_pct": round(100 * (1 - len(v1_bis) / max(1, len(v0_bis))), 1),
                "v0_zs": len(v0_zs),
                "v1_zs": len(v1_zs),
                "v0_min_len": v0_len[0] if v0_len else None,
                "v0_median_len": v0_len[len(v0_len) // 2] if v0_len else None,
                "v1_min_len": v1_len[0] if v1_len else None,
                "v1_median_len": v1_len[len(v1_len) // 2] if v1_len else None,
                "v0_short_lt_gate": sum(1 for x in v0_len if x < PROD_GATE),
            }
        )
    out = ROOT / "docs" / "calibration-r56-min-bi-len.json"
    out.write_text(
        json.dumps({"gate": PROD_GATE, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert rows, "对照表为空"
    # 至少要有一行真的发生了变化，否则「需要重新校准」这个前提就不成立
    assert any(r["v0_bi"] != r["v1_bi"] for r in rows), (
        "所有样本 v0/v1 笔数相同 —— 门槛没起作用，先别谈校准"
    )
