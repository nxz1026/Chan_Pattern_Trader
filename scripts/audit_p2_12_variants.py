"""P2-12 变体对照（在**未修改**的 cpt/domain/bi.py 上运行，变体以纯函数模拟）。

判据与 `scripts/audit_p2_12_evidence.py` 一致（索引空间、start↔start/end↔end 映射）。
跨度一律用 **``merged_index`` 差**（门槛的量纲）。

变体：
  cur     现行：贪心选端点（`_span < min_bi_len` 时 `end_idx += 2`）
  rep1    P2-12 报告建议①：`end_idx += 2` 时若更极端则把 `start_idx` 前移到 `end_idx-1`
  rep2    P2-12 报告建议②：端点不动，`Bi.high/low` 扩到窗口 K 线极值
  dp      DP 最优（在「端点=窗口同类最极端 + 跨度≥门槛」下笔数最大）
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from typing import Any  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_p212_ev", ROOT / "scripts" / "audit_p2_12_evidence.py"
)
if _spec is None or _spec.loader is None:  # pragma: no cover - 环境损坏时立即暴露
    raise RuntimeError("无法加载 scripts/audit_p2_12_evidence.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

from cpt.domain.bi import (  # noqa: E402
    _is_more_extreme,
    _make_bi,
    build_bis,
)
from cpt.domain.models import Bi, CanonicalBar, Fractal  # noqa: E402
from cpt.domain.zhongshu import build_zhongshus  # noqa: E402


def anchors_of(fxs: Sequence[Fractal]) -> list[Fractal]:
    a: list[Fractal] = []
    for f in fxs:
        if not a:
            a.append(f)
            continue
        if f.kind == a[-1].kind:
            if _is_more_extreme(f, a[-1]):
                a[-1] = f
        else:
            a.append(f)
    return a


def span_mi(anc: Sequence[Fractal], i: int, j: int) -> int:
    """门槛的量纲是**去包含后 K 线根数**（``Fractal.merged_index`` 差）。

    不能用 ``bar_index``、也不能用分型在列表里的下标差 —— 分型在合并序列里稀疏，
    两种量纲实测能差一倍（gate=5 时 DP 上界 25 vs 57），用错会把约束代价低估一半以上。
    """
    lo, hi = anc[i].merged_index, anc[j].merged_index
    assert lo is not None and hi is not None, "分型缺 merged_index，跨度无从计算"
    return hi - lo + 1


def is_ext(anc: Sequence[Fractal], i: int, j: int, kind: str) -> int:
    """[i, j] 内与 kind 同类的最极端者下标。"""
    best = i
    for k in range(i + 1, j + 1):
        if anc[k].kind == kind and _is_more_extreme(anc[k], anc[best]):
            best = k
    return best


def cur(anc: Sequence[Fractal], gate: int) -> list[int]:
    idxs = [0]
    e = 1
    while e < len(anc):
        if span_mi(anc, idxs[-1], e) < gate:
            e += 2
            continue
        idxs.append(e)
        e += 1
    return idxs


def rep1(anc: Sequence[Fractal], gate: int) -> list[int]:
    """报告建议①的字面读法：顺延前先看 `end_idx - 1` 是否更极端，是则起点前移。"""
    idxs = [0]
    e = 1
    while e < len(anc):
        if span_mi(anc, idxs[-1], e) < gate:
            cand = anc[idxs[-1]].kind
            prev = e - 1
            if (
                prev > idxs[-1]
                and anc[prev].kind == cand
                and _is_more_extreme(anc[prev], anc[idxs[-1]])
            ):
                idxs[-1] = prev
            e += 2
            continue
        idxs.append(e)
        e += 1
    return idxs


def dp(anc: Sequence[Fractal], gate: int) -> list[int]:
    n = len(anc)
    ok: list[list[bool]] = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if anc[i].kind == anc[j].kind or span_mi(anc, i, j) < gate:
                continue
            if is_ext(anc, i, j, anc[i].kind) != i:
                continue
            if is_ext(anc, i, j, anc[j].kind) != j:
                continue
            ok[i][j] = True
    best: list[int] = [0] * n
    prev: list[int] = [-1] * n
    for j in range(n):
        for i in range(j):
            if ok[i][j] and best[i] + 1 > best[j]:
                best[j] = best[i] + 1
                prev[j] = i
    if n == 0:
        return []
    end = max(range(n), key=lambda k: best[k])
    chain: list[int] = []
    k = end
    while k != -1:
        chain.append(k)
        k = prev[k]
    return sorted(chain)


def measure(
    anc: Sequence[Fractal],
    idxs: Sequence[int],
    wide: bool = False,
    bars: Sequence[CanonicalBar] | None = None,
) -> dict[str, Any]:
    bis: list[Bi] = []
    for a, b in zip(idxs, idxs[1:], strict=False):
        bi = _make_bi(anc[a], anc[b], 0)
        if wide and bars is not None:
            from dataclasses import replace

            win = [x for x in bars if bi.start_time <= x.open_time <= bi.end_time]
            if win:
                bi = replace(
                    bi,
                    high=max(x.high for x in win),
                    low=min(x.low for x in win),
                )
        bis.append(bi)
    pairs = list(zip(idxs, idxs[1:], strict=False))
    sv = sum(1 for i, j in pairs if is_ext(anc, i, j, anc[i].kind) != i)
    ev_ = sum(1 for i, j in pairs if is_ext(anc, i, j, anc[j].kind) != j)
    cont = all(pairs[k][0] == pairs[k - 1][1] for k in range(1, len(pairs)))
    het = all(anc[i].kind != anc[j].kind for i, j in pairs)
    zs = build_zhongshus(tuple(bis), 0)
    ws = sorted(z.high - z.low for z in zs)
    return {
        "bi": len(bis),
        "zs": len(zs),
        "sv": sv,
        "ev": ev_,
        "cont": cont,
        "het": het,
        "minw": round(min(ws), 2) if ws else 0,
    }


VARIANT_NAMES = {
    "cur": "现行",
    "rep1": "报告建议①",
    "rep2": "报告建议②(端点不动+扩极值)",
    "dp": "DP 最优",
}
FUNCS: dict[str, Any] = {"cur": cur, "rep1": rep1, "dp": dp}


def main() -> None:
    gate = 6
    print("P2-12 变体对照（gate=6，跨度按 merged_index 差）")
    print("注：报告建议②不动端点，故其 s/viol 与现行相同；它只改 Bi.high/low。\n")
    for csvf in sorted(ev.ORACLE.glob("*.csv")):
        bars = ev.load(csvf)
        fxs = list(ev.detect_fractals(ev.merge_contained_bars(list(bars)), level=0))
        anc = anchors_of(fxs)
        print(f"=== {csvf.stem} ===")
        base: dict[str, Any] | None = None
        for key in ("cur", "rep1", "rep2", "dp"):
            if key == "rep2":
                r = measure(anc, cur(anc, gate), wide=True, bars=bars)
            else:
                r = measure(anc, FUNCS[key](anc, gate))
            if base is None:
                base = r
            print(
                f"  {VARIANT_NAMES[key]:<24} 笔={r['bi']:>4} 中枢={r['zs']:>3} "
                f"起违规={r['sv']:>4} 末违规={r['ev']:>4} 连续={str(r['cont']):<5} "
                f"异类={str(r['het']):<5} 中枢最小宽={r['minw']:>8.2f}"
            )
        print()
        # 交叉核对：真 build_bis 的现行结果应与 cur() 一致
        real = build_bis(fxs, level=0, min_bi_len=gate)
        print(
            f"  [交叉核对] 真 build_bis 笔数={len(real)}；cur() 复现笔数={len(cur(anc, gate)) - 1}"
            f" ⇒ {'一致' if len(real) == len(cur(anc, gate)) - 1 else '⚠️ 不一致'}"
        )
        print()


if __name__ == "__main__":
    main()
