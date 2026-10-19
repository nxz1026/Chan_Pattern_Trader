"""R68 回归钉子：czsc 的 ``max_bi_num=0`` **不是「不限制」，而是「用默认值 50」**。

## 这个坑有多贵

本仓三处（一条生产路径 + 两处对照工具）都写着 ``max_bi_num=0``，字面读作
「不限笔数」。实测（czsc 1.0.1，``_native.abi3.so``）：

    czsc.CZSC(raw, max_bi_num=0, min_bi_len=6).max_bi_num  ->  50

内核 ``crates/czsc-core/src/analyze/mod.rs:88`` ::

    pub fn resolve_max_bi_num(explicit: usize) -> usize {
        resolve_env_usize(explicit, "CZSC_MAX_BI_NUM", "czsc_max_bi_num", 50)
    }

而 ``resolve_env_usize``（同文件 ``:65``）的第一条就是 ``if explicit > 0 { return explicit; }``
—— **0 落进「未指定」分支**，于是取默认 50。截断在 ``:229`` ::

    if self.bi_list.len() > self.max_bi_num {
        let start_idx = self.bi_list.len() - self.max_bi_num;
        self.bi_list.drain(0..start_idx);
    }

连 ``bars_raw`` 也跟着从头 drain（``:243`` 用 ``bi_list.first().fx_a.elements[0].dt``
反推），所以**分型集合也被截掉**：1000 根 fixture 下 ``bars_raw`` 1000 → 593、
``fx_list`` 322 → 202。

## 为什么必须钉死

``docs/audit-p2-12-endpoint-extreme.md:77-86`` 据此写下「native 的分型集合本身就比
czsc 大 50%~75%（327 vs 202）」，并把它当成「先修 bi.py 是在错的地基上施工」的
**核心论据**。实测那句话是**量测伪影**：把 ``max_bi_num`` 真正放开后，两者在共同
窗口内的分型序列**下标与种类完全相同**（322/322、356/356、348/348，0 差异）。
native 多出来的只是 czsc 因截断而没覆盖的**窗口边缘**那 1~5 个分型。

## 三条断言各自的职责

1. ``test_zero_means_fifty_not_unlimited`` —— 钉住 0 的真实语义（文档级事实）；
2. ``test_truncation_is_reproducible_and_bites`` —— 钉住「0 与放开确实不同」，
   防止将来 czsc 改了语义后本文件静默失效变绿；
3. ``test_fractal_sets_agree_once_truncation_is_lifted`` —— 钉住**推翻了那条
   「大 50%~75%」的说法**，且用的是**两个量纲都对**的比较（merged 空间）。

## 量测要点（踩过坑）

- **必须比 merged 空间，不能比毫秒**。czsc 的 ``fx.dt`` 落在分型窗口的**中心** K 上，
  native 的 ``Fractal.start_time`` 也取中间那根（``fractal.py:142``），看似同量纲；
  但 czsc 分型的 ``elements[0]`` 是窗口**左**端，比 `dt` 早两根，早期用
  ``elements[0].dt`` 去对齐会整体偏移 2 根。
- **czsc 分型 ≠ 笔端点**。``bi.tx_list`` 才是端点；``fx_list`` 在 czsc 里就是
  **独立检测出来的全部 3K 窗口分型**，与 native 同源。
- ``max_bi_num`` 传负数直接在 PyO3 层 ``OverflowError``（``can't convert negative
  int to unsigned``）—— 不能拿 -1 当「不限」。
"""

from __future__ import annotations

import csv
import datetime
import pathlib

import pytest
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import CanonicalBar

czsc = pytest.importorskip("czsc")
from czsc import CZSC, Freq, RawBar  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests" / "fixtures" / "oracle"

MIN_BI_LEN = 6
#: 字面意义上的「不限」。踩过坑：不能写 -1（PyO3 抛 OverflowError）。
UNLIMITED = 10**9

_TIME_FIELDS = {"open_time", "close_time"}
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

#: 三个 oracle fixture 在 n=1000 下的实测基线（czsc 1.0.1）。
#: ``capped_*`` 是**现行口径**（``max_bi_num=0``，实际 50），``open_*`` 是放开后，
#: ``native_fx`` 是本仓 ``detect_fractals`` 的全量。三列都必须钉住：只钉一列的话，
#: 「0 的语义变了」与「fixture 换了」无法区分。
BASELINE: dict[str, dict[str, int]] = {
    "btcusdt_5m_2024-02-01": {
        "native_fx": 327,
        "capped_fx": 202,
        "capped_bi": 50,
        "open_fx": 322,
        "open_bi": 88,
    },
    "btcusdt_5m_2024-09-01": {
        "native_fx": 357,
        "capped_fx": 203,
        "capped_bi": 49,
        "open_fx": 356,
        "open_bi": 90,
    },
    "btcusdt_5m_2025-04-01": {
        "native_fx": 353,
        "capped_fx": 245,
        "capped_bi": 49,
        "open_fx": 348,
        "open_bi": 74,
    },
}


def _load(path: pathlib.Path) -> list[CanonicalBar]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            CanonicalBar(
                **{
                    name: (int(row[name]) if name in _TIME_FIELDS else float(row[name]))
                    for name in _BAR_FIELDS
                },
                is_closed=True,
            )
            for row in csv.DictReader(fh)
        ]


def _raw(bars: list[CanonicalBar]) -> list[RawBar]:
    return [
        RawBar(
            symbol="B",
            id=i,
            dt=datetime.datetime.fromtimestamp(b.open_time / 1000),
            freq=Freq.F5,
            open=b.open,
            close=b.close,
            high=b.high,
            low=b.low,
            vol=b.volume,
            amount=b.quote_volume,
        )
        for i, b in enumerate(bars)
    ]


def _analyze(bars: list[CanonicalBar], max_bi_num: int) -> CZSC:
    return CZSC(_raw(bars), max_bi_num=max_bi_num, min_bi_len=MIN_BI_LEN)


def _open_time_to_merged_index(bars: list[CanonicalBar]) -> dict[int, int]:
    """原始 K 线 ``open_time`` -> 去包含后序列下标。

    这是**唯一**能把 czsc 的时间锚点翻译到 native 量纲的桥：两边都只保证
    「某个分型的中心 K 落在哪一根原始 bar 上」，而 native 的门槛量纲
    （``bi.py:183`` 的 ``merged_index`` 差 + 1）只在去包含后序列里有定义。
    """
    out: dict[int, int] = {}
    for mi, mb in enumerate(merge_contained_bars(bars)):
        for i in mb.source_indices:
            out[bars[i].open_time] = mi
    return out


def _load_fixture(name: str) -> list[CanonicalBar]:
    return _load(ORACLE / f"{name}.csv")


# --------------------------------------------------------------------------- #
# 1. 0 的真实语义
# --------------------------------------------------------------------------- #


def test_zero_means_fifty_not_unlimited() -> None:
    """``max_bi_num=0`` 被内核解析成默认值 50，**不是**「不限制」。

    这条是整个文件的立足点：如果哪天 czsc 把 0 改成真的「不限」，
    这里会立刻红，而那正是本文件其余断言的前提。
    """
    bars = _load_fixture("btcusdt_5m_2024-02-01")
    assert _analyze(bars, 0).max_bi_num == 50
    # 传 50 与传 0 必须是同一条路径（同值、同结果），否则 50 是巧合而非默认值
    assert _analyze(bars, 50).max_bi_num == 50
    assert len(_analyze(bars, 0).bi_list) == len(_analyze(bars, 50).bi_list)
    # 放开后必须**大于** 50，否则本文件测的不是截断
    assert _analyze(bars, UNLIMITED).max_bi_num == UNLIMITED


def test_negative_max_bi_num_is_rejected_at_the_pyo3_layer() -> None:
    """负数是 ``OverflowError``，不能拿 -1 当「不限」。"""
    bars = _load_fixture("btcusdt_5m_2024-02-01")
    with pytest.raises(OverflowError):
        _analyze(bars, -1)


# --------------------------------------------------------------------------- #
# 2. 截断真实存在
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_truncation_is_reproducible_and_bites(name: str) -> None:
    """现行口径与放开后的计数都钉住 —— 两列同时钉才能区分「语义变了」与「数据换了」。"""
    n = BASELINE[name]
    bars = _load_fixture(name)
    capped = _analyze(bars, 0)
    opened = _analyze(bars, UNLIMITED)

    assert len(capped.fx_list) == n["capped_fx"], name
    assert len(capped.bi_list) == n["capped_bi"], name
    assert len(opened.fx_list) == n["open_fx"], name
    assert len(opened.bi_list) == n["open_bi"], name

    # 截断是**真丢历史**，不是仅仅少算几个：笔数与分型数都必须严格变少。
    assert len(capped.bi_list) < len(opened.bi_list), name
    assert len(capped.fx_list) < len(opened.fx_list), name

    # 而且丢的是**最早**那一段：``bars_raw`` 被从头 drain。
    assert len(capped.bars_raw) < len(opened.bars_raw), name
    assert capped.bars_raw[0].dt > opened.bars_raw[0].dt, name
    assert capped.bars_raw[-1].dt == opened.bars_raw[-1].dt, name


# --------------------------------------------------------------------------- #
# 3. 推翻「native 分型集大 50%~75%」
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_fractal_sets_agree_once_truncation_is_lifted(name: str) -> None:
    """放开截断后，共同窗口内两套分型的**下标序列与种类序列完全相同**。

    ``docs/audit-p2-12-endpoint-extreme.md:77-86`` 据 ``max_bi_num=0`` 的数字
    写下「native 的分型集合本身就比 czsc 大 50%~75%」，并把它当作
    「先修 ``bi.py`` 是在错的地基上施工」的核心论据。实测那句话是量测伪影 ——
    native 多出来的只是 czsc 因截断而**没覆盖到的窗口边缘**那 1~5 个分型。

    本用例钉住修正后的口径；将来若有人重新主张「分型集大小差异」，
    必须先解释为什么这条断言会绿。
    """
    bars = _load_fixture(name)
    ot2mi = _open_time_to_merged_index(bars)

    native_fx = list(detect_fractals(merge_contained_bars(bars), level=0))
    opened = _analyze(bars, UNLIMITED)

    # ``detect_fractals`` 的返回类型把 ``merged_index`` 标成 ``int | None``
    # （退化输入没有 ``source_indices`` 时确实会是 None）。这里用真 fixture，
    # 断言它必须全部有值 —— 与 ``_open_time_to_merged_index`` 同源的前提。
    native_mi = [fx.merged_index for fx in native_fx]
    assert all(mi is not None for mi in native_mi), name
    native_mi_int = [int(mi) for mi in native_mi if mi is not None]

    # czsc 分型的 dt 落在窗口**中心** K 上（elements[1]），可直接映回 merged 下标。
    czsc_mi = [ot2mi[int(x.dt.timestamp() * 1000)] for x in opened.fx_list]
    # 严格递增且无重复 —— 否则「序列相同」这个比较本身没有意义。
    assert czsc_mi == sorted(czsc_mi)
    assert len(set(czsc_mi)) == len(czsc_mi)

    lo, hi = min(czsc_mi), max(czsc_mi)
    pos = [i for i, mi in enumerate(native_mi_int) if lo <= mi <= hi]
    native_in_window = [native_fx[i] for i in pos]

    assert len(native_in_window) == len(czsc_mi), name
    assert [native_mi_int[i] for i in pos] == czsc_mi, name

    czsc_kind = ["top" if "顶" in str(x.mark) else "bottom" for x in opened.fx_list]
    assert [fx.kind for fx in native_in_window] == czsc_kind, name

    # native 在窗口外多出的分型只有零星几个，**不是** 50%~75%。
    # 这条把「差异是边缘效应」这个性质钉住：一旦有人改成分型过滤/包含处理，
    # 窗口外的数量级会跳，而窗口内的「完全相同」会先破 —— 两条互为交叉验证。
    outside = len(native_fx) - len(native_in_window)
    assert outside <= 8, (name, outside)


def test_the_documented_ratio_was_an_artifact() -> None:
    """把文档那句「大 50%~75%」重算一遍，证明它来自截断而非算法差异。

    这条不是重复上一条 —— 上一条证「放开后相同」，这条证**放开前的比值**
    恰好落在文档写下的区间里，从而把「文档量的是截断后的数」这件事闭环。

    分子用 native 全量（``native_fx``），分母用**文档实际用的那个数**
    （``capped_fx``，即 ``max_bi_num=0`` 的 202/203/245），不加任何修正项。
    """
    ratios = [n["native_fx"] / n["capped_fx"] - 1 for n in BASELINE.values()]
    # 文档原文「大 50%~75%」（docs/audit-p2-12-endpoint-extreme.md:81）。
    # 实测 62% / 76% / 44% —— 第一、二个正落在文档区间内，第三个偏小（245 那个
    # fixture 截断得轻），这正是文档写「50%~75%」这个**区间**而非单值的原因。
    assert all(0.40 <= r <= 0.80 for r in ratios), ratios
    # 放开截断后同一比值塌到个位数百分比 —— 与上面形成对照，证明大比值来自截断。
    lifted = [n["native_fx"] / n["open_fx"] - 1 for n in BASELINE.values()]
    assert all(r < 0.02 for r in lifted), lifted
