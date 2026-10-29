"""P2-12 证据包生成（在**未修改**的 cpt/domain/bi.py 上运行）。

## 判据（索引空间，已用手算合成样例自审）

笔端点 → 分型下标的映射必须 start↔start、end↔end：
``Bi.start_time`` 取起点分型的 ``start_time``，而 ``Bi.end_time`` 取终点分型的
``end_time``（``_make_bi``）。用 ``f.start_time == b.end_time`` 找终点**恒失败**，
末端检查会被静默跳过 —— 早期两套互相矛盾的数字（53/57/59 与 47/52/57）就是这么来的。

不变量：
  I2 起端：笔窗口 ``[s, e]`` 内与起点**同类**的分型里，起点最极端
  I3 末端：笔窗口 ``[s, e]`` 内与终点**同类**的分型里，终点最极端
  I4 相邻笔共享端点（连续）；两端异类
「更极端」= 顶取更高 ``high``、底取更低 ``low``。

## czsc 侧的 kind

``FX.mark`` 是 ``Mark`` 枚举，``str()`` 为中文「顶分型」/「底分型」，
``repr()`` 为 ``Mark.G``/``Mark.D``。⚠️ ``FX.fx`` **不是**种类，是价格字符串
（59 种取值）—— 早期用 ``endswith("G")`` 判 ``str(mark)`` 导致全部被判成底，
产出「连续 4 个同类分型」的假象，据此得出的结论全部作废。
"""

from __future__ import annotations

import csv
import datetime
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ⚠️ 必须先于 `import cpt`：以 `python scripts/xxx.py` 方式运行时 ``sys.path[0]``
# 是 ``scripts/``，而本仓 ``cpt`` 的 editable 安装**指向另一个 checkout**
# （`/home/ubuntu/work/.morning/Chan_Pattern_Trader/cpt/__init__.py`），
# 于是脚本会静默地对着**另一份代码**运算。``scripts/check_doc_drift.py:108``
# 早有这个防护；这里照做。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import czsc  # noqa: E402
from cpt.adapters.czsc_chanlun import MAX_BI_NUM_UNLIMITED  # noqa: E402
from cpt.domain.bi import _is_more_extreme, build_bis  # noqa: E402
from cpt.domain.contain import merge_contained_bars  # noqa: E402
from cpt.domain.fractal import detect_fractals  # noqa: E402
from cpt.domain.models import CanonicalBar, Fractal  # noqa: E402
from cpt.domain.zhongshu import build_zhongshus  # noqa: E402
from czsc import Freq, RawBar  # noqa: E402

ORACLE = ROOT / "tests" / "fixtures" / "oracle"


def load(path: pathlib.Path) -> list[CanonicalBar]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            CanonicalBar(
                open_time=int(r["open_time"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                close_time=int(r["close_time"]),
                quote_volume=float(r["quote_volume"]),
                trade_count=int(r["trade_count"]),
                taker_buy_base_volume=float(r["taker_buy_base_volume"]),
                taker_buy_quote_volume=float(r["taker_buy_quote_volume"]),
                is_closed=True,
            )
            for r in csv.DictReader(fh)
        ]


def extreme(seq: Sequence[Fractal], lo: int, hi: int, kind: str) -> int:
    best = lo
    for idx in range(lo + 1, hi + 1):
        if seq[idx].kind != kind:
            continue
        if _is_more_extreme(seq[idx], seq[best]):
            best = idx
    return best


def pairs_of(bis: Sequence[Any], seq: Sequence[Fractal]) -> tuple[list[tuple[int, int]], int]:
    """笔 → (起点分型下标, 终点分型下标)。**两个方向各建一张表**。"""
    st = {f.start_time: i for i, f in enumerate(seq)}
    et = {f.end_time: i for i, f in enumerate(seq)}
    out: list[tuple[int, int]] = []
    miss = 0
    for b in bis:
        i, j = st.get(b.start_time), et.get(b.end_time)
        if i is None or j is None:
            miss += 1
            continue
        out.append((i, j))
    return out, miss


def violations(
    seq: Sequence[Fractal], pairs: Sequence[tuple[int, int]]
) -> tuple[int, int, bool, bool]:
    sv = sum(1 for i, j in pairs if extreme(seq, i, j, seq[i].kind) != i)
    ev = sum(1 for i, j in pairs if extreme(seq, i, j, seq[j].kind) != j)
    cont = all(pairs[k][0] == pairs[k - 1][1] for k in range(1, len(pairs)))
    het = all(seq[i].kind != seq[j].kind for i, j in pairs)
    return sv, ev, cont, het


SpanFn = Callable[[Sequence[Fractal], int, int], int]


def merged_span(mi: Sequence[int]) -> SpanFn:
    """返回「按 ``merged_index`` 量跨度」的 span 回调。

    ⚠️ 门槛的量纲是**去包含后** K 线根数（``Fractal.merged_index``），不是分型
    在列表里的下标差 —— 分型在合并序列里稀疏，两种量纲实测能差一倍
    （gate=5 时 21 vs 61），用错会得出「约束几乎无代价」的错误结论。
    """

    def span(_seq: Sequence[Fractal], lo: int, hi: int) -> int:
        return mi[hi] - mi[lo] + 1

    return span


def dp_max(seq: Sequence[Fractal], gate: int, span: SpanFn) -> int:
    """满足 I2+I3+跨度门槛的**最长**端点序列长度（笔数）—— 任何算法的上界。"""
    n = len(seq)
    ok = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if seq[i].kind == seq[j].kind or span(seq, i, j) < gate:
                continue
            if extreme(seq, i, j, seq[i].kind) != i:
                continue
            if extreme(seq, i, j, seq[j].kind) != j:
                continue
            ok[i][j] = True
    best = [0] * n
    for j in range(n):
        for i in range(j):
            if ok[i][j] and best[i] + 1 > best[j]:
                best[j] = best[i] + 1
    return max(best) if n else 0


def selfcheck() -> None:
    """手算可验的合成样例：钉住「判据本身」没写错。"""

    def mk(kind: str, mi: int, high: float, low: float) -> Fractal:
        return Fractal(
            kind=kind,
            level=0,
            bar_index=mi,
            start_time=mi * 10,
            end_time=mi * 10 + 9,
            high=high,
            low=low,
            source_ids=(f"a{mi}",),
            merged_index=mi,
        )

    # gate=4：旧实现得 (0,3)+(3,6)，两笔起点都不是窗口内同类最极端
    syn = [
        mk("bottom", 0, 101, 100),
        mk("top", 1, 105, 104),
        mk("bottom", 2, 99, 90),
        mk("top", 3, 106, 105),
        mk("bottom", 4, 98, 95),
        mk("top", 5, 110, 108),
        mk("bottom", 6, 81, 80),
        mk("top", 7, 120, 118),
    ]
    bis = build_bis(syn, level=0, min_bi_len=4)
    pairs, miss = pairs_of(bis, syn)
    sv, ev, _, _ = violations(syn, pairs)
    print(f"[自审] 合成 gate=4 笔={len(bis)} 对={pairs} 未映射={miss} 起违规={sv} 末违规={ev}")
    print("[自审] 手算：窗口 [0,3] 内最极端底=2(90)、[3,6] 内最极端顶=5(110)")
    print("[自审] ⇒ (0,3) 与 (3,6) 两笔的起点都该被判违规；实测如上")
    assert miss == 0 and sv == 2, "判据自审失败"


def main() -> None:
    selfcheck()
    print()
    rows_out: list[dict[str, Any]] = []
    for csvf in sorted(ORACLE.glob("*.csv")):
        bars = load(csvf)
        fxs = list(detect_fractals(merge_contained_bars(list(bars)), level=0))
        raw = [
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
        # ⚠️ R68：**不能**写 0 —— 内核把它当「未指定」回落默认 50，会截断
        # ``bi_list`` 并连带砍掉 ``bars_raw``（分型集合跟着被截，1000 根下
        # ``fx_list`` 322 → 202，本脚本早先的证据就是这样被量歪的）。
        eng = czsc.CZSC(raw, max_bi_num=MAX_BI_NUM_UNLIMITED, min_bi_len=6)
        fc = list(eng.fx_list)
        cb = list(eng.bi_list)
        ci = {f.dt: k for k, f in enumerate(fc)}

        def ckind(f: Any) -> str:
            return "top" if "顶" in str(f.mark) else "bottom"

        cfx = [
            Fractal(
                kind=ckind(f),
                level=0,
                bar_index=k,
                start_time=int(f.dt.timestamp() * 1000),
                end_time=int(f.dt.timestamp() * 1000),
                high=float(f.high),
                low=float(f.low),
                source_ids=(f"c{k}",),
                merged_index=k,
            )
            for k, f in enumerate(fc)
        ]
        cpairs = [(ci[b.fx_a.dt], ci[b.fx_b.dt]) for b in cb if b.fx_a.dt in ci and b.fx_b.dt in ci]
        csv_, cev, ccont, chet = violations(cfx, cpairs)

        rec: dict[str, Any] = {
            "fixture": csvf.stem,
            "native_fx": len(fxs),
            "czsc_fx": len(fc),
            "czsc_bi": len(cb),
            "czsc_start_viol": csv_,
            "czsc_end_viol": cev,
            "czsc_cont": ccont,
            "czsc_hetero": chet,
            "native": [],
        }
        print(f"=== {csvf.stem} ===")
        print(
            f"  分型数：native={len(fxs)}  czsc={len(fc)}（差 {len(fxs) - len(fc)}）"
            f" | czsc 笔={len(cb)} 起违规={csv_} 末违规={cev} 连续={ccont} 异类={chet}"
        )
        for gate in (None, 3, 4, 5, 6, 7, 8, 10):
            bis = build_bis(fxs, level=0, min_bi_len=gate) if gate else build_bis(fxs, level=0)
            p, miss = pairs_of(bis, fxs)
            sv, ev, cont, het = violations(fxs, p)
            zs = build_zhongshus(bis, 0)
            # ⚠️ 跨度必须用 ``merged_index`` 差（门槛的量纲，见 Fractal 文档
            # 「别拿 bar_index 当笔的跨度」），**不是**分型列表下标差 ——
            # 分型在合并序列里稀疏，两种量纲能差出一倍（实测 gate=5 得 21 vs 61）。
            # 下标必须与 `fxs` 严格对齐（span 回调按分型下标取值），
            # 所以这里**不能**过滤 None —— 缺值应直接暴露而不是静默错位。
            _mi: list[int] = [int(f.merged_index) for f in fxs if f.merged_index is not None]
            assert len(_mi) == len(fxs), "有分型缺 merged_index，跨度会错位"
            up = "-" if gate is None else dp_max(fxs, gate, merged_span(_mi))
            native_rows: list[dict[str, Any]] = rec["native"]
            native_rows.append(
                {
                    "gate": gate,
                    "bi": len(bis),
                    "zs": len(zs),
                    "start_viol": sv,
                    "end_viol": ev,
                    "cont": cont,
                    "hetero": het,
                    "unmapped": miss,
                    "dp_max": up,
                }
            )
            print(
                f"  gate={str(gate):>4} 笔={len(bis):>4} 中枢={len(zs):>3} "
                f"起违规={sv:>4} 末违规={ev:>4} 连续={cont} 异类={het} "
                f"未映射={miss} DP上界={up}"
            )
        print()
        rows_out.append(rec)
    (ROOT / "docs" / "audit-p2-12-evidence.json").write_text(
        json.dumps(rows_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("⇒ 原始数据已写入 docs/audit-p2-12-evidence.json")


if __name__ == "__main__":
    main()
