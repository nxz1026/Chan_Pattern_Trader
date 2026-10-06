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


def _lengths(bis, fractals) -> list[int]:
    """每笔的跨度，单位是**去包含后**的 K 线根数 —— **与门槛同一量纲**。

    ⚠️ 2026-10-06 更正：原来这里返回的是**原始** bar 根数
    （``(end_time - start_time) / BAR_MS``）。包含关系会合并 K 线，
    去包含后根数 <= 原始根数，所以同一个门槛值在两个量纲里严格程度不同。
    用原始根数定档会**低估门槛的真实严格度**：实测 BTCUSDT 5m 90 天样本上，
    原始 p80 = 6，而门槛真正作用的 merged p80 = 4、p95 才是 6 ——
    也就是说「门槛 6 ≈ p80 / 砍掉最短两成」是错的，实际是 p95 / 砍掉约 65%。

    这份数字会被 :func:`test_calibration_table_is_written` 写进
    ``docs/calibration-r56-min-bi-len.json``，所以**量纲错了会被自动
    写进产物**，让文档重新漂回错误结论 —— 必须在这里改对，而不是改文档。
    """
    return [_span_of(b, fractals) for b in bis]


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


def _merged_index_maps(fractals) -> tuple[dict[int, int], dict[int, int]]:
    """分型的 ``merged_index`` 两张查找表：**起端点用 start_time，末端点用 end_time**。

    ⚠️ 两者取自分型的**不同字段**（2026-10-06 实测更正）：``Bi.start_time`` 是
    **起点分型的 start_time**（``middle.open_time``，结尾 ``000000``），
    而 ``Bi.end_time`` 是**终点分型的 end_time**（``middle.close_time``，
    结尾 ``999999``）—— 见 ``cpt/domain/bi.py::_make_bi`` 的
    ``start_time=start.start_time, end_time=end.end_time``。

    只建一张 ``{f.start_time: ...}`` 再拿 ``bi.end_time`` 去查，**永远查不到**
    （结尾 000000 vs 999999）。这正是下面 ``_span_of`` 一直查不到端点的原因。
    """
    by_start = {f.start_time: f.merged_index for f in fractals if f.merged_index is not None}
    by_end = {f.end_time: f.merged_index for f in fractals if f.merged_index is not None}
    return by_start, by_end


def _span_of(bi, fractals) -> int:
    """按端点的 merged_index 算跨度（与 build_bis 内部同一口径）。

    ⚠️ 原来查不到端点时返回 ``PROD_GATE``，于是调用方的
    ``assert span >= PROD_GATE`` **恒真** —— 这条「门槛定义」测试从上线起
    就没断言过任何东西（2026-10-06 实测：114/114 笔全部走兜底）。
    静默兜底比报错贵：它让一条空测试看起来像一条通过的测试。
    查不到就抛，让「量纲/映射写错了」当场暴露。
    """
    by_start, by_end = _merged_index_maps(fractals)
    start = by_start.get(bi.start_time)
    end = by_end.get(bi.end_time)
    if start is None or end is None:
        raise AssertionError(
            f"无法把笔映射回分型的 merged_index："
            f"start_time={bi.start_time} end_time={bi.end_time}。"
            f"多半是 _make_bi 的字段取法变了（起端点用 start_time、末端点用 "
            f"end_time），而不是数据有问题 —— 请更新 _merged_index_maps。"
        )
    return int(end) - int(start) + 1


@pytest.mark.parametrize("csv_path", _csv_files(), ids=lambda p: p.stem)
def test_span_measurement_matches_the_gates_own_unit(csv_path: pathlib.Path) -> None:
    """**口径自检**：证明 :func:`_span_of` 量的确实是门槛自己用的那个量。

    ## 为什么需要这条

    本文件两次栽在同一类错误上，且都**测不出来**：

    1. :func:`_lengths` 一度返回毫秒，拿去跟「6 根」比 —— 差三个数量级，
       ``v0_short_lt_gate`` 恒为 0，看着全绿。
    2. :func:`_span_of` 用 ``{f.start_time: ...}`` 去查 ``bi.end_time``
       （后者取自分型的 ``end_time``，结尾 999999），**永远查不到** ⇒
       每次返回兜底的 ``PROD_GATE`` ⇒ 「门槛定义」那条断言**恒真**。
       实测 114/114 笔全部走兜底，那条测试从上线起没断言过任何东西。

    ⇒ **「断言通过」不能证明「量对了」。** 这条测试反过来用门槛的**定义**
    来校验测量工具：门槛 ``G`` 的产出，最小跨度必须**恰好等于 G**、
    且**零违反**。只要测量量纲和门槛不一致（或者映射坏了查不到端点），
    这条立刻红。

    ⚠️ 这不是「多测一遍同样的东西」：上面那条测的是「实现对不对」，
    这条测的是「**我们以为在测的那个量**是不是真的那个量」。
    """
    bars = _load_csv(csv_path)
    fractals = detect_fractals(merge_contained_bars(list(bars)), level=0)
    gated = build_bis(fractals, level=0, min_bi_len=PROD_GATE)
    assert gated, f"{csv_path.name}: 门槛 {PROD_GATE} 之后一笔都不剩，样本不可用"
    spans = [_span_of(b, fractals) for b in gated]
    below = [s for s in spans if s < PROD_GATE]
    assert not below, f"{csv_path.name}: 有 {len(below)} 笔跨度 < 门槛 {PROD_GATE} —— {_span_of} 与门槛不同量纲？"
    assert min(spans) == PROD_GATE, (
        f"{csv_path.name}: 最小跨度 {min(spans)} != 门槛 {PROD_GATE} —— "
        f"门槛合并的是「不足即并」，因此必有一笔恰好等于门槛。"
        f"不相等说明测量量纲与门槛用的不是同一个量（merged_index vs 原始下标）"
    )


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


@pytest.mark.parametrize("csv_path", _csv_files(), ids=lambda p: p.stem)
def test_lengths_are_merged_spans_not_raw_bar_counts(csv_path: pathlib.Path) -> None:
    """:func:`_lengths` 必须返回**去包含后**根数，而不是原始 bar 根数。

    ## 为什么这条不能省

    上一条自检（:func:`test_span_measurement_matches_the_gates_own_unit`）用的是
    :func:`_span_of`，**绕过了** :func:`_lengths`。于是把 ``_lengths`` 单独改回
    原始根数时，那条自检照样绿 —— 实测变异确认过：13 个用例**全绿**，
    而 ``calibration-r56-min-bi-len.json`` 会被重新写回错误的量纲。
    这正是本文件栽过的第三种「看起来测了、其实没测到」。

    ⇒ 这条直接对着 ``_lengths`` 的**物理含义**断言：
    包含关系会合并 K 线 ⇒ 去包含后根数 **<=** 原始根数；并且在这批样本上
    **严格小于**（merged 中位 3 vs raw 中位 4）。把 ``_lengths`` 换回原始
    根数，两者会相等 ⇒ 立刻红。
    """
    bars = _load_csv(csv_path)
    fractals, bis0, _ = _pipeline(bars, None)
    merged_spans = _lengths(bis0, fractals)
    raw_spans = [int((b.end_time - b.start_time) // BAR_MS) + 1 for b in bis0]
    assert len(merged_spans) == len(raw_spans) == len(bis0)

    for bi, m, r in zip(bis0, merged_spans, raw_spans):
        assert m <= r, (
            f"{csv_path.name}: 去包含后跨度 {m} 大于原始跨度 {r} —— 物理上不可能，"
            f"说明量错了量纲"
        )
    merged_median = sorted(merged_spans)[len(merged_spans) // 2]
    raw_median = sorted(raw_spans)[len(raw_spans) // 2]
    assert merged_median < raw_median, (
        f"{csv_path.name}: merged 中位 {merged_median} == raw 中位 {raw_median} —— "
        f"两者本应不同（包含关系会合并 K 线）。相等说明 _lengths 退回成了原始根数"
    )


def test_calibration_table_is_written() -> None:
    """产出对照表并落盘 —— 这是「重新校准」要用的那份数据，不是测试的副产品。

    ⚠️ 这里的 ``*_len`` 全部是**去包含后**根数（门槛量纲，见 :func:`_lengths`）。
    每次跑测试都会**重写** ``docs/calibration-r56-min-bi-len.json`` ——
    所以量纲一旦在这里写错，错误数字会被自动写进产物、并让
    ``docs/calibration-r56-min-bi-len.md`` 重新漂回错误结论。
    """
    rows = []
    for path in _csv_files():
        bars = _load_csv(path)
        v0_fx, v0_bis, v0_zs = _pipeline(bars, None)
        v1_fx, v1_bis, v1_zs = _pipeline(bars, PROD_GATE)
        v0_len = sorted(_lengths(v0_bis, v0_fx))
        v1_len = sorted(_lengths(v1_bis, v1_fx))
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
    # ⚠️ 必须走 ``write_bytes``，不能用 ``write_text``（2026-10-06 修）。
    #
    # 文本模式在 Windows 上会把 ``\n`` 翻成 CRLF，而本仓 ``core.autocrlf=input``
    # ⇒ git 检出/提交一律用 LF。于是**每跑一次测试就永久弄脏这个被跟踪的文件**：
    # ``git status`` 一直报 M，内容却一字不差（``git diff --ignore-all-space``
    # 为空）。没人发现，是因为 11 道门禁里没有一道查「跑完测试后工作区是否干净」。
    #
    # 这条是新增的门禁⑭（变异抽查）按设计拒绝在脏工作区上运行时暴露出来的 ——
    # 它拒绝执行，理由是「无法区分是你改的还是我改的」。
    #
    # 用字节写就没有任何换行翻译：``json.dumps`` 产出的换行本就是 ``\n``。
    out.write_bytes(
        json.dumps(
            {"gate": PROD_GATE, "rows": rows}, ensure_ascii=False, indent=2
        ).encode("utf-8")
    )
    assert rows, "对照表为空"
    # 至少要有一行真的发生了变化，否则「需要重新校准」这个前提就不成立
    assert any(r["v0_bi"] != r["v1_bi"] for r in rows), (
        "所有样本 v0/v1 笔数相同 —— 门槛没起作用，先别谈校准"
    )
