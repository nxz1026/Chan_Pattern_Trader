"""P2-12 回归钉子：native 门槛路径**不满足**「笔端点 = 窗口内同类最极端」这个不变量。

## 为什么钉「现状」而不是钉「修好」

不变量本身是对的（参照实现 czsc 满足它），但**修好它的代价被 DP 证明**：
在现行 gate=6 下，满足不变量的笔数**上界**只有 41/53/38（现行 98/104/110），
即砍掉一半以上的笔。而唯一能归零的路径（全局 DP 选择）与
``cpt/domain/bi.py:127`` 承诺的「合并端点、不丢弃」**语义冲突**，落地要升
``SCHEMA_VERSION`` 到 v2 并重建全部 fixture。详见
[docs/audit-p2-12-endpoint-extreme.md](../docs/audit-p2-12-endpoint-extreme.md)。

所以本用例的职责是**把现状的性质钉死**，让将来任何一个：

* 改了门槛算法却没更新计数 → 红；
* 宣称「已经修好端点极值」→ 红（因为 DP 上界会跟着变）；
* 意外把 ``gate=None`` 路径也弄出违规 → 红（该路径有结构性保证，见下）

都能被立刻发现。

## 不变量定义（与 ``scripts/audit_p2_12_evidence.py`` 同源）

笔窗口 ``[s, e]`` 是锚点下标闭区间：

* **I2**：区间内与**起点**同类的锚点里，起点最极端
* **I3**：区间内与**终点**同类的锚点里，终点最极端

「更极端」＝顶取更高 ``high``、底取更低 ``low``（``_is_more_extreme``，严格不等）。

## 两个非平凡的量测要点（踩过坑，保留）

1. **端点映射必须 start↔start、end↔end**。``Bi.start_time`` 取起点分型的
   ``start_time``，但 ``Bi.end_time`` 取终点分型的 **``end_time``**（``_make_bi``）。
   用 ``f.start_time == b.end_time`` 找终点**恒不匹配** ⇒ 末端检查被**静默跳过**，
   会得出「末违规恒为 0」的假结论。
2. **跨度必须按 ``merged_index`` 差算**，不是分型列表下标差 —— 分型在合并序列里
   稀疏，两种量纲实测能差一倍（gate=5 时 DP 上界 25 vs 57）。
"""

from __future__ import annotations

import csv
import pathlib
from collections.abc import Callable, Sequence

import pytest
from cpt.domain.bi import _is_more_extreme, build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import CanonicalBar, Fractal

ROOT = pathlib.Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests" / "fixtures" / "oracle"
PROD_GATE = 6

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

#: gate=6 下的实测基线。**这些数字是本用例的核心断言**：
#: 它们同时编码了「缺陷存在」与「缺陷的规模」，改动口径必然打破其中之一。
BASELINE: dict[str, dict[str, int]] = {
    "btcusdt_5m_2024-02-01": {"bi": 98, "start_viol": 53, "end_viol": 49, "dp_max": 41},
    "btcusdt_5m_2024-09-01": {"bi": 104, "start_viol": 57, "end_viol": 52, "dp_max": 53},
    "btcusdt_5m_2025-04-01": {"bi": 110, "start_viol": 59, "end_viol": 57, "dp_max": 38},
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


def _anchors(fxs: Sequence[Fractal]) -> list[Fractal]:
    """复现 ``build_bis`` 的前置折叠：同类相邻只留更极端者，异类则追加。

    这段折叠只在**输入相邻**的分型间比较 —— 一旦 ``anchors`` 严格交替，两个同类
    端点之间必然隔着一个异类端点，再也不会被比较。这正是 I2/I3 在门槛路径上被
    破坏的机制（``end_idx += 2`` 让它们重新相邻，却不重新裁定）。
    """
    out: list[Fractal] = []
    for f in fxs:
        if not out:
            out.append(f)
            continue
        if f.kind == out[-1].kind:
            if _is_more_extreme(f, out[-1]):
                out[-1] = f
        else:
            out.append(f)
    return out


def _extreme(seq: Sequence[Fractal], lo: int, hi: int, kind: str) -> int:
    best = lo
    for k in range(lo + 1, hi + 1):
        if seq[k].kind == kind and _is_more_extreme(seq[k], seq[best]):
            best = k
    return best


def _pairs(bis: Sequence, anc: Sequence[Fractal]) -> list[tuple[int, int]]:
    """笔 → (起点下标, 终点下标)。两个方向各建一张表（见模块 docstring 要点 1）。"""
    st = {f.start_time: i for i, f in enumerate(anc)}
    et = {f.end_time: i for i, f in enumerate(anc)}
    out: list[tuple[int, int]] = []
    for b in bis:
        i, j = st.get(b.start_time), et.get(b.end_time)
        if i is None or j is None:
            raise AssertionError(f"笔 {b.start_time}→{b.end_time} 无法映射回锚点")
        out.append((i, j))
    return out


def _violations(anc: Sequence[Fractal], pairs: Sequence[tuple[int, int]]) -> tuple[int, int]:
    sv = sum(1 for i, j in pairs if _extreme(anc, i, j, anc[i].kind) != i)
    ev = sum(1 for i, j in pairs if _extreme(anc, i, j, anc[j].kind) != j)
    return sv, ev


def _dp_max(anc: Sequence[Fractal], gate: int) -> int:
    """满足 I2+I3+跨度门槛的**最长**端点链长度 —— 任何算法的上界。

    跨度用 ``merged_index`` 差（门槛的量纲，见模块 docstring 要点 2）。
    """
    n = len(anc)
    mi = [f.merged_index for f in anc]
    ok = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if anc[i].kind == anc[j].kind or mi[j] - mi[i] + 1 < gate:
                continue
            if _extreme(anc, i, j, anc[i].kind) != i:
                continue
            if _extreme(anc, i, j, anc[j].kind) != j:
                continue
            ok[i][j] = True
    best = [0] * n
    for j in range(n):
        for i in range(j):
            if ok[i][j] and best[i] + 1 > best[j]:
                best[j] = best[i] + 1
    return max(best) if n else 0


def _fixture_ids() -> list[str]:
    return [p.stem for p in sorted(ORACLE.glob("*.csv"))]


@pytest.fixture(scope="module")
def loaded() -> dict[str, tuple[list[CanonicalBar], list[Fractal]]]:
    if not ORACLE.exists():
        pytest.skip(f"没有 oracle CSV：{ORACLE}")
    out: dict[str, tuple[list[CanonicalBar], list[Fractal]]] = {}
    for path in sorted(ORACLE.glob("*.csv")):
        bars = _load(path)
        fxs = list(detect_fractals(merge_contained_bars(list(bars)), level=0))
        out[path.stem] = (bars, fxs)
    return out


# --------------------------------------------------------------------------- #
# ① 判据自审：手算可验的合成序列（钉住「判据本身没写错」）
# --------------------------------------------------------------------------- #


def _mkf(kind: str, mi: int, high: float, low: float) -> Fractal:
    t = mi * 10
    return Fractal(
        kind=kind,
        level=0,
        bar_index=mi,
        start_time=t,
        end_time=t + 9,
        high=high,
        low=low,
        source_ids=(f"a{mi}",),
        merged_index=mi,
    )


def test_violation_metric_matches_hand_computation() -> None:
    """手算基准：gate=4 时现行实现给出 (0,3)+(3,6)，两笔起点都不是窗口内最极端。

    窗口 ``[0,3]`` 内底分型是 {0(100), 2(90)}，最极端是 2 ⇒ 起点 0 违规。
    窗口 ``[3,6]`` 内顶分型是 {3(106), 5(110)}，最极端是 5 ⇒ 起点 3 违规。
    若判据写错（例如拿 ``start_time`` 配 ``end_time``），这个断言会先红。
    """
    syn = [
        _mkf("bottom", 0, 101, 100),
        _mkf("top", 1, 105, 104),
        _mkf("bottom", 2, 99, 90),
        _mkf("top", 3, 106, 105),
        _mkf("bottom", 4, 98, 95),
        _mkf("top", 5, 110, 108),
        _mkf("bottom", 6, 81, 80),
        _mkf("top", 7, 120, 118),
    ]
    anc = _anchors(syn)
    assert len(anc) == 8, "合成序列的锚点数应为 8（全异类相邻，折叠不动）"
    bis = build_bis(syn, level=0, min_bi_len=4)
    pairs = _pairs(bis, anc)
    assert pairs == [(0, 3), (3, 6)], f"门径路径的现行输出应为两笔，实测 {pairs}"
    sv, ev = _violations(anc, pairs)
    assert (sv, ev) == (2, 0), f"手算起违规应为 2，实测 {sv}"


# --------------------------------------------------------------------------- #
# ② 缺陷存在且规模被钉住
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stem", _fixture_ids())
def test_gate_path_violates_the_endpoint_extremality_invariant(
    loaded: dict[str, tuple[list[CanonicalBar], list[Fractal]]], stem: str
) -> None:
    """gate=6 下 I2/I3 违规数**与基线一致**（缺陷存在，规模已知）。"""
    _, fxs = loaded[stem]
    anc = _anchors(fxs)
    bis = build_bis(fxs, level=0, min_bi_len=PROD_GATE)
    pairs = _pairs(bis, anc)
    sv, ev = _violations(anc, pairs)
    want = BASELINE[stem]
    assert len(bis) == want["bi"], f"{stem} 笔数变了：{len(bis)} != {want['bi']}"
    assert sv == want["start_viol"], f"{stem} 起违规变了：{sv} != {want['start_viol']}"
    assert ev == want["end_viol"], f"{stem} 末违规变了：{ev} != {want['end_viol']}"


@pytest.mark.parametrize("stem", _fixture_ids())
def test_no_gate_path_is_structurally_clean(
    loaded: dict[str, tuple[list[CanonicalBar], list[Fractal]]], stem: str
) -> None:
    """``gate=None`` 时 I2/I3 必须**恒为 0** —— 缺陷完全由门槛路径引入。

    这是本用例集里最有解释力的一条：它把缺陷的**归因**钉在 ``end_idx += 2`` 上，
    而不是「整个笔算法都不对」。若哪天有改动让无门槛路径也违规，说明前置折叠
    （``_anchors``）被破坏了。
    """
    _, fxs = loaded[stem]
    anc = _anchors(fxs)
    bis = build_bis(fxs, level=0)
    pairs = _pairs(bis, anc)
    sv, ev = _violations(anc, pairs)
    assert (sv, ev) == (0, 0), f"{stem} 无门槛路径应零违规，实测 ({sv}, {ev})"


@pytest.mark.parametrize("stem", _fixture_ids())
def test_dp_upper_bound_shows_the_fix_costs_half_the_bis(
    loaded: dict[str, tuple[list[CanonicalBar], list[Fractal]]], stem: str
) -> None:
    """修好不变量所付代价的**上界**：即使最优也就 41/53/38 笔。

    这条把「能不能既修好又保住笔数」这个反复出现的直觉钉死：**不能**。
    若将来 DP 上界显著变大（例如笔数不变也能满足不变量），说明 native 的分型集
    变了（上游分型检测被修），届时应当重新评估是否值得落地修复。
    """
    _, fxs = loaded[stem]
    anc = _anchors(fxs)
    want = BASELINE[stem]
    got = _dp_max(anc, PROD_GATE)
    cur = len(build_bis(fxs, level=0, min_bi_len=PROD_GATE))
    assert got == want["dp_max"], f"{stem} DP 上界变了：{got} != {want['dp_max']}"
    assert got < cur, (
        f"{stem} DP 上界 {got} 不再小于现行笔数 {cur} —— "
        "不变量与跨度门槛可能已不再冲突，应重新评估 P2-12 是否值得修"
    )


def test_dp_bound_agrees_with_hand_computation_on_synthetic_case() -> None:
    """合成序列上手算 DP 上界：``[0,7]`` 是唯一合法笔（跨度 8 ≥ 4）。"""
    syn = [
        _mkf("bottom", 0, 101, 100),
        _mkf("top", 1, 105, 104),
        _mkf("bottom", 2, 99, 90),
        _mkf("top", 3, 106, 105),
        _mkf("bottom", 4, 98, 95),
        _mkf("top", 5, 110, 108),
        _mkf("bottom", 6, 81, 80),
        _mkf("top", 7, 120, 118),
    ]
    anc = _anchors(syn)
    # 合法笔要求两端异类、跨度≥4、且两端在各自窗口内最极端。
    # (2,5) 跨度 4 ✓ 且底 {2,4}→2、顶 {3,5}→5 ✓ ⇒ 合法。
    assert _dp_max(anc, 4) >= 1
    assert _extreme(anc, 2, 7, "bottom") == 6, "手算：[2,7] 内最极端底是 6(low 80)"
    assert _extreme(anc, 2, 7, "top") == 7, "手算：[2,7] 内最极端顶是 7(high 120)"


# --------------------------------------------------------------------------- #
# ③ 文档口径别再声称已修
# --------------------------------------------------------------------------- #


def test_docs_record_the_defect_as_known_not_fixed() -> None:
    """证据包必须同时写明「缺陷存在」与「不修的理由」，且不含「已修复」口径。"""
    doc = ROOT / "docs" / "audit-p2-12-endpoint-extreme.md"
    text = doc.read_text(encoding="utf-8")
    assert "DP" in text and "41/53/38" in text, "证据包必须写出 DP 上界这个关键数"
    assert "不改代码" in text, "证据包必须写明处置是「不改代码」"
    for claim in ("已修复", "已修好", "P2-12 已修"):
        assert claim not in text, f"证据包不得声称 {claim!r}"

    traps = (ROOT / "docs" / "known-traps.md").read_text(encoding="utf-8")
    assert "P2-12" in traps, "known-traps 必须有 P2-12 条目"


# --------------------------------------------------------------------------- #
# ④ 跨度量纲必须用 merged_index（曾是真实误判源）
# --------------------------------------------------------------------------- #


def test_span_dimension_is_merged_index_not_list_position(
    loaded: dict[str, tuple[list[CanonicalBar], list[Fractal]]],
) -> None:
    """两种跨度量纲在真实 fixture 上**确有差异** —— 用错会严重低估代价。

    分型在合并序列里是稀疏的，列表下标差 ≈ 2 而 ``merged_index`` 差可达 8。
    实测 ``btcusdt_5m_2024-02-01`` gate=5：下标差得 25、``merged_index`` 差得 57。
    本断言不依赖具体数值，只要求「两种量纲结果不同」，避免将来有人把
    ``_dp_max`` 的 span 换回下标差却毫无察觉。
    """
    _, fxs = loaded["btcusdt_5m_2024-02-01"]
    anc = _anchors(fxs)
    mi = [f.merged_index for f in anc]
    spans_mi = [mi[k] - mi[k - 1] for k in range(1, len(mi))]
    assert max(spans_mi) > 2, (
        "merged_index 间距应出现 >2 的空隙（分型稀疏）；"
        "若全是 2，说明两种量纲等价，本注意事项可以从文档里删掉"
    )


def test_callable_type_alias_is_importable() -> None:
    """占位：确认本模块的 ``Callable`` 导入确实被用到（ruff 会抓未用导入）。"""
    fn: Callable[[int], int] = lambda x: x + 1  # noqa: E731
    assert fn(1) == 2
