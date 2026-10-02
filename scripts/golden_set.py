"""Golden set：钉住一批标的的**结构指纹**，用来发现「结构变了但没人知道为什么」。

## 为什么要有它

R39 之前的现状是：``cpt_structure_event`` 记了 2575 次结构变化，却只有
「变了什么」，没有「变没变」的**基线**。于是每次改算法/换数据源，只能靠人肉
翻图对比。现在有了 ``payload["cause"]``（R39），但那只在**事件流**里；
本脚本管的是**当前形状** —— 拿一批固定标的的结构 id 全集当快照，
前后两次一比，就知道算法/数据有没有动过。

## 集合怎么来

``自选 ∪ 热门池 ∪ {600519}``：

- **自选**：用户明确关注的（服务端 watchlist）
- **热门池**���``public.hot_rank`` + ``public.strategy_signal`` 最近日期
- **600519**：钉死的锚 —— 高价、分红频繁、因子表里算过的，
  它的因子一旦错，每张图都跟着错

去重保序（自选优先），与 ``scripts/snapshot_a_share_batch.py`` 同一口径。

## 用法

    python scripts/golden_set.py --build  golden.json    # 建基线
    python scripts/golden_set.py --check golden.json    # 比对（不写库）
    python scripts/golden_set.py --check golden.json --limit 20

``--check`` 有差异时 exit 1 —— 可以直接挂 CI/巡检。

## 基线里存什么

**不存价格**。价格每天都在动，存了只会天天报差异；这里只存**结构形状**：

    ``<code>``: {bar_count, fractal_ids, bi_ids, zhongshu_ids, dataset_hash}

``dataset_hash`` 放进来是有意的：它让「结构没变但输入变了」也能被发现
（那种情况说明因子或 K 线在动，结构暂时没反应 —— 早发现比晚发现好）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters.a_share_factor import hot_pool_codes
from cpt.adapters.a_share_local import AShareLocalClient
from cpt.application.a_share_snapshot import build_ashare_snapshot

#: 钉死的锚标的。不接受命令行增删 —— 基线的意义就在于「这批不许悄悄变」。
ANCHOR_CODES: tuple[str, ...] = ("600519",)


def golden_codes(conn: Any, limit: int) -> list[str]:
    """``自选 ∪ 热门池 ∪ 锚标的``，去重保序。"""
    out: list[str] = []
    seen: set[str] = set()

    def _add(codes: Any) -> None:
        for raw in codes or ():
            # 自选文件存的是**对象**（``{"code": "600519", "note": ...}``），
            # 直接 ``str()`` 会得到 ``"{'code': '600519', ...}"`` 这种垃圾代码 ——
            # 实测踩过：它会一路混进基线，对比时永远对不上。
            if isinstance(raw, dict):
                raw = raw.get("code") or raw.get("symbol") or ""
            code = str(raw).split(".")[0][:6]
            if code and code not in seen:
                seen.add(code)
                out.append(code)

    _add(ANCHOR_CODES)
    _add(hot_pool_codes(conn, limit=limit))
    try:
        import json as _json  # noqa: PLC0415

        from cpt.web.a_share_routes import DEFAULT_WATCHLIST_PATH  # noqa: PLC0415

        if DEFAULT_WATCHLIST_PATH.exists():
            _add(_json.loads(DEFAULT_WATCHLIST_PATH.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — 自选读不到就少几个，不该让整个检查失败
        print(f"  [warn] 读自选失败，按「热门池 + 锚」继续: {exc}", file=sys.stderr)
    return out


def _ids(items: Any, key: str) -> list[str]:
    out = []
    for it in items or ():
        if isinstance(it, dict):
            out.append(str(it.get(key)))
        else:
            out.append(str(getattr(it, key, it)))
    return out


def fingerprint_one(snapshot: dict[str, Any]) -> dict[str, Any]:
    """一只票的**结构形状**（不含价格）。"""
    overlays = snapshot.get("overlays") or {}
    repro = snapshot.get("reproducibility") or {}
    return {
        "bar_count": len(snapshot.get("candles") or []),
        "fractal_ids": _ids(overlays.get("fractals"), "start_time"),
        "bi_ids": _ids(overlays.get("bis"), "start_time"),
        "zhongshu_ids": _ids(overlays.get("zhongshus"), "start_time"),
        "dataset_hash": str(repro.get("dataset_hash") or ""),
    }


def collect(limit: int) -> dict[str, Any]:
    client = AShareLocalClient()
    conn = client._get_conn()
    codes = golden_codes(conn, limit)
    print(f"golden set 共 {len(codes)} 只: {', '.join(codes)}")
    out: dict[str, Any] = {}
    for code in codes:
        try:
            snap = build_ashare_snapshot(code, client=client)
        except Exception as exc:  # noqa: BLE001 — 单只失败不该中断整批
            out[code] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"  {code}  !! {type(exc).__name__}: {exc}")
            continue
        fp = fingerprint_one(snap)
        out[code] = fp
        print(
            f"  {code}  bars={fp['bar_count']:>4}  frac={len(fp['fractal_ids']):>3} "
            f"bi={len(fp['bi_ids']):>3}  zs={len(fp['zhongshu_ids']):>2}"
        )
    conn.rollback()  # 只读检查：水位行由正式路径自己提交，这里不回滚别人的
    return {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "codes": out}


def compare(base: dict[str, Any], now: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    b, n = base.get("codes") or {}, now.get("codes") or {}
    for code in sorted(set(b) | set(n)):
        if code not in n:
            problems.append(f"{code}: 基线里有，这次没跑到（可能被移出热门池或取数失败）")
            continue
        if code not in b:
            problems.append(f"{code}: 这次新增（基线里没有）")
            continue
        bo, no = b[code], n[code]
        if "error" in no:
            problems.append(f"{code}: 取数失败 {no['error']}")
            continue
        for field in ("fractal_ids", "bi_ids", "zhongshu_ids"):
            if bo.get(field) != no.get(field):
                problems.append(
                    f"{code}: {field} 变了 "
                    f"({len(bo.get(field) or [])} -> {len(no.get(field) or [])} 项)"
                )
        if bo.get("bar_count") != no.get("bar_count"):
            problems.append(f"{code}: bar_count {bo.get('bar_count')} -> {no.get('bar_count')}")
        if bo.get("dataset_hash") != no.get("dataset_hash"):
            problems.append(f"{code}: dataset_hash 变了（输入数据或因子动了）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="golden set 结构指纹")
    ap.add_argument("--build", metavar="PATH", help="建基线并写入 PATH")
    ap.add_argument("--check", metavar="PATH", help="与 PATH 的基线比对")
    ap.add_argument("--limit", type=int, default=15, help="热门池取前 N（默认 15）")
    args = ap.parse_args(argv)
    if not args.build and not args.check:
        ap.error("--build / --check 至少给一个")

    now = collect(args.limit)
    if args.build:
        path = Path(args.build)
        path.write_text(
            json.dumps(now, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"基线已写入 {path}")
        return 0

    base = json.loads(Path(args.check).read_text(encoding="utf-8"))
    problems = compare(base, now)
    if not problems:
        print("golden set 一致：结构形状无变化")
        return 0
    print(f"golden set 有 {len(problems)} 处差异：")
    for p in problems:
        print("  -", p)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
