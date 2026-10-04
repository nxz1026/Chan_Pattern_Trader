#!/usr/bin/env python3
"""chanlun 三后端对比（R45）。

## 为什么需要它

`architecture.md` §2.1 记着一个真陷阱：``backend_factory`` 的 ``auto`` 模式
一旦发现装了 czsc 就会**静默切后端**，而

    native 笔端点中位跨度 2 根、短跨度占 66.9%；czsc 是另一个量级

⇒ 看板上**每一根笔都会变**。这个风险值得用「三方对同一输入给出一致计数」
来兜底，但**本环境装不上 czsc**（PyPI 对 pip 重置、crates.io 403、
czsc 是 Rust 扩展还需 cargo）。

所以本脚本刻意**不假装能做三方对比**：它探测哪些后端真的可用，
对可用的跑**契约不变量**，并明确报出缺哪一个。

## 契约（三个后端都必须满足）

``canvas_registry.js`` 要求「四个画布必须一致：结构元素数量两两相等」。
同理，三个后端喂同一批 bar，必须给出**同量级**的结构计数：

1. 笔按时间**严格递增**，无零长笔；
2. 笔方向只能是 ±1；
3. 中枢 ``high > low``；
4. ``bi_ids`` 不越界。

## 用法

    python scripts/compare_chanlun_backends.py [--code 600519] [--bars 120]

装了 ``.[chan]`` 之后本脚本会真正做三方对比；没装则如实报「只跑了 N 个」。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.domain.models import CanonicalBar  # noqa: E402


def _load(name: str):
    p = ROOT / "cpt" / "adapters" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_cmp_{name}", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"_cmp_{name}"] = m
    spec.loader.exec_module(m)
    return m


def available_backends() -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    try:
        nat = _load("native_chanlun").NativeChanlunBackend
        out.append(("native", nat()))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  native 不可用: {type(exc).__name__}: {exc}")
    try:
        inm = _load("reference_chanlun").InMemoryChanlunBackend
        out.append(("reference(InMemory 占位)", inm()))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  reference 不可用: {type(exc).__name__}: {exc}")
    try:
        import czsc  # noqa: F401

        cz = _load("czsc_chanlun").CzscChanlunBackend
        out.append(("czsc", cz()))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  czsc 不可用: {type(exc).__name__}: {str(exc)[:70]}")
    return out


def fetch_bars(code: str, n: int) -> list[CanonicalBar]:
    import datetime as dt

    from cpt.adapters.a_share_local import AShareLocalClient

    conn = AShareLocalClient()._get_conn()  # noqa: SLF001
    cur = conn.cursor()
    cur.execute(
        """SELECT date, open, high, low, close, volume FROM public.daily_bar
           WHERE code=%s ORDER BY date DESC LIMIT %s""",
        (code, n),
    )
    rows = cur.fetchall()[::-1]
    out = []
    for d, o, h, l, c, v in rows:
        ms = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000)
        out.append(CanonicalBar(
            open_time=ms, open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v or 0), close_time=ms + 86400000 - 1,
            quote_volume=0.0, trade_count=0, taker_buy_base_volume=0.0,
            taker_buy_quote_volume=0.0, is_closed=True,
        ))
    return out


def count_of(res: object) -> dict[str, int]:
    def n(*names: str) -> int:
        for nm in names:
            v = getattr(res, nm, None)
            if v is not None:
                try:
                    return len(v)
                except TypeError:
                    continue
        return 0

    return {"fractals": n("fx_list", "fractals", "fx"),
            "bis": n("bi_list", "bis", "bi"),
            "zhongshus": n("zs_list", "zhongshus", "zs")}


def check_contract(label: str, res: object) -> list[str]:
    """返回违反的契约条目（空 = 通过）。

    ⚠️ 两种后端产出的笔**形状不同**：``native`` 直接产 domain ``Bi``
    （``start_time`` / ``end_time`` 是**毫秒时间戳**），而 ``reference`` /
    ``czsc`` 产 ``BiRaw``（``start_bar`` / ``end_bar`` 是**bar 下标**）。
    契约检查必须同时认这两套字段名，否则会把「字段名不同」误报成「契约违规」。
    """
    bad: list[str] = []
    bis = list(getattr(res, "bi_list", None) or getattr(res, "bis", None) or [])
    zs = list(getattr(res, "zs_list", None) or getattr(res, "zhongshus", None) or [])

    def span(b: object) -> tuple[int | None, int | None]:
        for a, z in (("start_time", "end_time"), ("start_bar", "end_bar")):
            va, vz = getattr(b, a, None), getattr(b, z, None)
            if va is not None and vz is not None:
                return int(va), int(vz)
        return None, None

    prev_end = None
    for i, b in enumerate(bis):
        st, en = span(b)
        if st is None or en is None:
            bad.append(f"bi[{i}] 缺时间/下标字段")
            continue
        if en <= st:
            bad.append(f"bi[{i}] 零长/反向 [{st},{en}]")
        if prev_end is not None and st < prev_end:
            bad.append(f"bi[{i}] 与上一个真重叠（上止 {prev_end}）")
        d = getattr(b, "direction", None)
        if d not in (1, -1):
            bad.append(f"bi[{i}] direction={d!r} 非法（须 ±1）")
        prev_end = en
    for i, z in enumerate(zs):
        hi, lo = getattr(z, "high", None), getattr(z, "low", None)
        if hi is None or lo is None:
            continue
        if hi <= lo:
            bad.append(f"zs[{i}] high<=low（{hi}<={lo}）")
        ids = list(getattr(z, "bi_ids", None) or ())
        if ids and max(ids) >= len(bis):
            bad.append(f"zs[{i}] bi_ids 越界（max={max(ids)}, 共 {len(bis)} 笔）")
    return [f"{label}: {x}" for x in bad]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", default="600519")
    ap.add_argument("--bars", type=int, default=120)
    args = ap.parse_args(argv)

    print("=" * 88)
    print("R45 chanlun 三后端对比")
    print("=" * 88)
    print("探测后端可用性：")
    backends = available_backends()
    for label, _ in backends:
        print(f"  ✅ {label}")
    for missing in ("czsc", "reference(真实现)"):
        if not any(missing.split("(")[0] in lb for lb, _ in backends):
            print(f"  ❌ {missing} —— 本环境装不上，**无法做真三方对比**")

    bars = fetch_bars(args.code, args.bars)
    print(f"\n输入：{args.code} 的 {len(bars)} 根日线")

    from cpt.domain.config import RulesConfig

    cfg = RulesConfig()
    results: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    for label, backend in backends:
        try:
            res = backend.compute_structures(bars, cfg)
            results[label] = count_of(res)
            problems += check_contract(label, res)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {label} 执行失败: {type(exc).__name__}: {str(exc)[:80]}")
            problems.append(f"{label}: 执行抛 {type(exc).__name__}")

    print(f"\n{'后端':28s} {'分型':>6s} {'笔':>6s} {'中枢':>6s}")
    print("-" * 50)
    for label, c in results.items():
        print(f"  {label:26s} {c['fractals']:>6d} {c['bis']:>6d} {c['zhongshus']:>6d}")

    if len(results) >= 2:
        keys = ["fractals", "bis", "zhongshus"]
        same = all(len({c[k] for c in results.values()}) == 1 for k in keys)
        print(f"\n各后端计数一致: {'✅' if same else '❌ 存在差异'}")
        if not same:
            for k in keys:
                vals = {lb: c[k] for lb, c in results.items()}
                if len(set(vals.values())) > 1:
                    print(f"    ⚠️ {k}: {vals}")
            if any("InMemory" in lb for lb in results):
                print(
                    "\n    ℹ️ 差异**不代表**后端不可互换：``InMemoryChanlunBackend``\n"
                    "       是仓内**测试占位**（按 bar 高低点做极简判定，见其 docstring），\n"
                    "       本来就不等价于真后端。真正的对比需要 czsc 与\n"
                    "       ``reference`` 的真实现 —— 两者都不在本环境。\n"
                    "       本脚本刻意不把占位后端算进『三方对比』。"
                )
    else:
        print(f"\n⚠️ 只有 {len(results)} 个后端可用 —— **不能声称做了对比**。")

    print(f"\n契约检查: {'✅ 全部通过' if not problems else '❌'}")
    for p in problems[:10]:
        print(f"    {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
