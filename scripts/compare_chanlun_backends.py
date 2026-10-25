#!/usr/bin/env python3
"""chanlun 三后端对比（R45）。

## 怎么跑

    python scripts/compare_chanlun_backends.py [--code 600519] [--bars 120]

本机（2026-10-18 实测）**czsc 1.0.1 已装**，所以能真跑 native↔czsc 两方对比。
``reference`` 侧只有仓内的 ``InMemoryChanlunBackend`` **测试占位**，脚本如实报出，
并把它**排除在「计数一致」的判定之外**（占位按 bar 高低点做极简判定，本来就不
等价于真后端）。

装 czsc **不会**改变生产用的后端 —— ``DEFAULT_BACKEND`` 硬编码为 ``native``，
不依赖 czsc 是否安装（``auto`` 模式的静默切换隐患早被 R16-4 堵住）。

## 为什么需要它（以及 R67 修掉的口径失真）

`architecture.md` §2.1 记着一个真陷阱：``backend_factory`` 的 ``auto`` 模式
一旦发现装了 czsc 就会**静默切后端**，而

    native 笔端点中位跨度 2 根、短跨度占 66.9%；czsc 是另一个量级

⇒ 看板上**每一根笔都会变**。这个风险值得用「两个真后端对同一输入给出一致计数」
来兜底。

但 2026-10-18（R67 / 审计 P1-6）发现本脚本**量错了东西**：三个后端都是
**无参构造**的，而笔门槛 ``min_bi_len`` 是**构造参数**（``native_chanlun.py``
的 ``__init__(*, min_bi_len: int | None = None)``），生产在
``cpt/application/a_share_snapshot.py`` 显式传 ``min_bi_len_for("1d") == 6``。
于是脚本量到的是「没门槛的 native」（600519 / 300 根 = **99 笔**），而生产是
**28 笔** —— 差 3.5 倍，拿它做对比等于拿两个不同口径互证。

现在门槛统一从 ``RulesConfig`` 取（``--min-bi-len`` 可覆盖；``0`` = 关闭门槛，
只用于复现 R45 的历史数字），并且**计数不一致会进退出码**：此前它只打印
``❌ 存在差异`` 却仍 ``return 0``，在脚本/门禁调用链里等于永远"通过"。

## 契约（三个后端都必须满足）

``canvas_registry.js`` 要求「四个画布必须一致：结构元素数量两两相等」。
同理，多个后端喂同一批 bar，必须给出**同量级**的结构计数：

1. 笔按时间**严格递增**，无零长笔；
2. 笔方向只能是 ±1；
3. 中枢 ``high > low``；
4. ``bi_ids`` 不越界。

## 用法

    python scripts/compare_chanlun_backends.py [--code 600519] [--bars 120]
    python scripts/compare_chanlun_backends.py --min-bi-len 0   # 复现历史数字

退出码：0 = 契约全过且（真后端之间的）计数一致；1 = 有契约违规、后端抛异常、
或真后端计数不一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.domain.models import CanonicalBar  # noqa: E402

#: 仓内测试占位后端（按 bar 高低点做极简判定）——**不计入**「计数一致」的判定。
PLACEHOLDER_LABEL = "reference(InMemory 占位)"


def _load(name: str) -> ModuleType:
    p = ROOT / "cpt" / "adapters" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_cmp_{name}", p)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"_cmp_{name}"] = m
    spec.loader.exec_module(m)
    return m


def resolve_min_bi_len(raw: int | None) -> int | None:
    """``--min-bi-len`` → 传给后端构造函数的笔门槛。

    ``None``（没传）取生产配置 ``RulesConfig().min_bi_len_for("1d")``；传 ``0``
    表示**关闭门槛**（native 用 ``None``），只用于复现 R45 那批历史数字 ——
    那时的数字就是"没门槛的 native"，与生产不是同一个口径（见模块 docstring）。
    """
    if raw is None:
        from cpt.domain.config import RulesConfig

        return RulesConfig().min_bi_len_for("1d")
    return raw or None


def available_backends(min_bi_len: int | None) -> list[tuple[str, Any]]:
    """探测可用后端；``min_bi_len`` 必须与生产同口径地传进去（否则量的不是生产）。

    2026-10-18（R67 / P1-6）之前这里全是无参构造 ⇒ native 走 ``min_bi_len=None``
    （无门槛），600519/300 根量到 99 笔，而生产是 28 笔。
    """
    out: list[tuple[str, Any]] = []
    try:
        nat = _load("native_chanlun").NativeChanlunBackend
        out.append(("native", nat(min_bi_len=min_bi_len)))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  native 不可用: {type(exc).__name__}: {exc}")
    try:
        inm = _load("reference_chanlun").InMemoryChanlunBackend
        out.append((PLACEHOLDER_LABEL, inm()))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  reference 不可用: {type(exc).__name__}: {exc}")
    try:
        import czsc  # noqa: F401

        cz = _load("czsc_chanlun").CzscChanlunBackend
        # czsc 的 min_bi_len 是 int（不接 None）；关闭门槛时用 1。
        out.append(("czsc", cz(min_bi_len=min_bi_len or 1)))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  czsc 不可用: {type(exc).__name__}: {str(exc)[:70]}")
    return out


def fetch_bars(code: str, n: int) -> list[CanonicalBar]:
    import datetime as dt

    from cpt.adapters.a_share_local import open_conn

    conn = open_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT date, open, high, low, close, volume FROM public.daily_bar
           WHERE code=%s ORDER BY date DESC LIMIT %s""",
        (code, n),
    )
    rows = cur.fetchall()[::-1]
    out = []
    for d, o, h, lo, c, v in rows:
        ms = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000)
        out.append(
            CanonicalBar(
                open_time=ms,
                open=float(o),
                high=float(h),
                low=float(lo),
                close=float(c),
                volume=float(v or 0),
                close_time=ms + 86400000 - 1,
                quote_volume=0.0,
                trade_count=0,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
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

    return {
        "fractals": n("fx_list", "fractals", "fx"),
        "bis": n("bi_list", "bis", "bi"),
        "zhongshus": n("zs_list", "zhongshus", "zs"),
    }


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
    ap.add_argument(
        "--min-bi-len",
        type=int,
        default=None,
        help=(
            "笔门槛（去包含后 K 线根数）；默认取生产配置"
            " RulesConfig().min_bi_len_for('1d')。传 0 = 关闭门槛，只用于"
            "复现 R45 的历史数字（那时脚本量的就是没门槛的 native）"
        ),
    )
    args = ap.parse_args(argv)
    min_bi_len = resolve_min_bi_len(args.min_bi_len)

    print("=" * 88)
    print("R45 chanlun 多后端对比")
    print("=" * 88)
    print(f"笔门槛 min_bi_len = {min_bi_len if min_bi_len is not None else '关闭'}")
    print("探测后端可用性：")
    backends = available_backends(min_bi_len)
    for label, _ in backends:
        print(f"  ✅ {label}")
    if not any(lb == "czsc" for lb, _ in backends):
        print("  ❌ czsc —— 未装，做不了 native↔czsc 对比（`pip install czsc==1.0.1`）")
    if not any("InMemory" in lb for lb, _ in backends):
        print("  ℹ️  reference 只有 InMemory 测试占位，本脚本不把它算进一致性判定")

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
        # 占位后端不参与一致性判定（见模块 docstring）：它按 bar 高低点做极简判定，
        # 与真后端本来就不等价，把它算进来只会让脚本**永远**报 ❌ —— 于是没人再看它。
        real = {lb: c for lb, c in results.items() if lb != PLACEHOLDER_LABEL}
        if len(real) >= 2:
            same = all(len({c[k] for c in real.values()}) == 1 for k in keys)
            print(f"\n真后端计数一致: {'✅' if same else '❌ 存在差异'}")
            if not same:
                for k in keys:
                    vals = {lb: c[k] for lb, c in real.items()}
                    if len(set(vals.values())) > 1:
                        print(f"    ⚠️ {k}: {vals}")
                # R67（P1-6）：不一致必须进退出码。此前只打印 ❌ 却 return 0，
                # 在脚本/门禁调用链里等于永远"通过"。
                detail = "；".join(f"{k}={ {lb: c[k] for lb, c in real.items()} }" for k in keys)
                problems.append(f"真后端计数不一致：{detail}")
        else:
            print(
                f"\n⚠️ 真后端只有 {len(real)} 个（占位 {PLACEHOLDER_LABEL} 不计入）"
                " —— **不能声称做了对比**。"
            )
        if PLACEHOLDER_LABEL in results:
            print(
                "\n    ℹ️ 上面若看到占位后端的数字：``InMemoryChanlunBackend`` 是仓内\n"
                "       **测试占位**（按 bar 高低点做极简判定，见其 docstring），\n"
                "       本来就不等价于真后端，**已排除在一致性判定之外**。"
            )
    else:
        print(f"\n⚠️ 只有 {len(results)} 个后端可用 —— **不能声称做了对比**。")

    print(f"\n契约检查: {'✅ 全部通过' if not problems else '❌'}")
    for p in problems[:10]:
        print(f"    {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
