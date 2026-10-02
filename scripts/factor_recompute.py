"""按 Wind 公司行动**重算**后复权因子（R37）。

## 为什么要有这个脚本

现在的 ``asel.ref_adjust_factor`` 存的是 **``腾讯后复权收盘 ÷ 库里不复权收盘``**
这个**逐日比值**。而后复权因子按定义应在一次除权事件内**恒定**（分段常数）。

R36 实测的后果（全部真机量过）：

- 该比值本身不是常数 —— 600519 在 125 个交易日里从 6.749131 漂到 7.170869
  （相对极差 **6.25%**）；000001 在 20 天内 195.54~200.57（**2.57%**）；
- 绕开解析器直接读腾讯原始返回，**腾讯自己的 hfq 与它自己的不复权就不是等比的**；
- 于是本地 open/high/low 带 0.1%~0.9% 的畸变，而分型/笔端点对这些值敏感
  ⇒ parity 的结构匹配率只有 **22%~55%**。

**离线修不了**（R37 干跑）：比值序列是「漂移 + 偶发真台阶」，阈值法把漂移当台阶
（600825 在最近 4 天里被检出 4 个"台阶"，其实是每天约 −0.1% 的漂移）。

**唯一真值是公司行动本身**。于是按定义算（公式与实现见
:func:`cpt.adapters.a_share_factor.factor_from_actions`）：

    f = Π_{除权日 e}  (1 + 送转比例_e) / (1 − 每股派息_e / 除权前收盘_e)

Wind 的 ``get_stock_events`` 给的就是这三样（除权除息日 / 每股派息 / 送转比例）。

## 安全边界（这个脚本刻意做不到的那几件事）

- **绝不写** ``asel.ref_adjust_factor``（生产表）—— 只写暂存表
  ``asel.ref_adjust_factor_v2``；
- **绝不自动切换** —— 切换是人工决定（看报告 → 决定）；
- **每天有调用上限**（``--max-calls``），撞到 ``RATE_LIMIT_ERROR`` / 通道不可用
  **立即停**并落盘进度 —— 额度按天切，所以这是分几天跑完的机制；
- **可断点续跑**（``~/.cache/cpt/factor_recompute_state.json``）；
- **优先跑正在被看的票**（自选 → 热门池/策略源 → 其余）：额度只够跑一部分时，
  先修用户眼前会看的那批；
- 单只票失败只记账，不中断整轮。

## 用法

    # 看看进度
    .venv/bin/python scripts/factor_recompute.py --report

    # 今天跑一批（默认 80 次调用上限；额度不够会自动提前停）
    .venv/bin/python scripts/factor_recompute.py

    # 调试单只（不写暂存表）
    .venv/bin/python scripts/factor_recompute.py --only 000001 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters.a_share_factor import (  # noqa: E402
    codes_with_factors,
    ensure_recompute_stage,
    factor_from_actions,
    hot_pool_codes,
    load_recent_closes,
    save_recompute_factors,
)
from cpt.adapters.a_share_local import AShareLocalClient  # noqa: E402
from cpt.adapters.wind_source import (  # noqa: E402
    WindQuotaError,
    WindSourceClient,
    WindSourceError,
    WindUnavailableError,
)

_LOG = logging.getLogger("factor_recompute")

#: 进度文件（断点续跑）
STATE_PATH: Final[Path] = Path(
    os.getenv("CPT_FACTOR_RECOMPUTE_STATE", "~/.cache/cpt/factor_recompute_state.json")
).expanduser()

#: 默认每日调用上限。**真正的硬边界是供应商的 RATE_LIMIT_ERROR**，这个只是保险。
DEFAULT_MAX_CALLS: Final[int] = 80

#: 自选文件（``WatchlistStore`` 用的同一个路径）
WATCHLIST_PATH: Final[Path] = Path(
    os.getenv("CPT_WATCHLIST", "~/.cache/cpt/watchlist.json")
).expanduser()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"done": [], "failed": {}, "days": {}}
    try:
        raw: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("进度文件损坏，重开一轮：%s", STATE_PATH)
        return {"done": [], "failed": {}, "days": {}}
    return raw


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def watchlist_codes() -> list[str]:
    """自选代码（**JSON 文件**，不是表 —— R24 起自选走 ``WatchlistStore``）。"""
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items", data) if isinstance(data, dict) else data
    out: list[str] = []
    for it in items or []:
        code = str(it.get("code", "")) if isinstance(it, dict) else str(it)
        code = code.split(".")[0]
        if code and code not in out:
            out.append(code)
    return out


def priority_codes(conn: Any) -> list[str]:
    """候选代码，按「是否正在被看」排序。"""
    order: list[str] = []
    seen: set[str] = set()
    for group in (watchlist_codes(), list(hot_pool_codes(conn)), list(codes_with_factors(conn))):
        for c in group:
            c = str(c).split(".")[0]
            if c and c not in seen:
                seen.add(c)
                order.append(c)
    return order


@dataclass
class CodeResult:
    code: str
    ok: bool
    rows: int = 0
    steps: int = 0
    note: str = ""


def current_latest_factor(conn: Any, code: str) -> float | None:
    """库里**最新一根**的因子 —— 重标定的目标锚点。取不到就返回 ``None``（不重标定）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hfq_factor FROM asel.ref_adjust_factor WHERE code=%s "
            "ORDER BY trade_date DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] else None


def reanchor(new_factors: dict[str, float], anchor_factor: float) -> dict[str, float]:
    """把按定义算出的因子**重标定**到与库里一致的锚点。

    ## 为什么这一步不做就会毁掉所有图

    实测 000001：库里最新一根的因子是 **200.57**，而按定义算出来（锚在最新）只有
    **1.0219** —— 差 **196 倍**。两者**形状相同**（都是把分红加回去的后复权：
    腾讯 hfq 首末比 1.1616 vs 不复权 0.9966，多出的 1.1656 ≈ 三年分红累积），
    **只是归一化锚点不同**：库里的锚在最早那根，我算的锚在最新那根。

    因子是**乘在价格上**的，所以直接换表 ⇒ 每张 A 股图的每一个价格都会**缩放
    ~200 倍**。这不是精度问题，是量纲问题。

    :param anchor_factor: 库里**最新一根**的因子值（换算后的目标锚点）。
    """
    if not new_factors:
        return {}
    latest_key = max(new_factors)
    base = new_factors[latest_key]
    if not base:
        return dict(new_factors)
    k = anchor_factor / base
    return {d: v * k for d, v in new_factors.items()}


def prev_closes_for(bars: tuple[tuple[int, float], ...], ex_dates: list[str]) -> dict[str, float]:
    """每个除权日对应的**除权前收盘**（往回找最近一个更早的交易日）。"""
    timeline = sorted(
        (dt.datetime.fromtimestamp(t / 1000, tz=dt.UTC).date().isoformat(), c) for t, c in bars
    )
    out: dict[str, float] = {}
    for ex in ex_dates:
        prior = [c for d, c in timeline if d < ex]
        if prior:
            out[ex] = prior[-1]
    return out


def process_code(
    client: AShareLocalClient,
    wind: WindSourceClient,
    code: str,
    *,
    write: bool,
    retries: int = 2,
) -> tuple[CodeResult, int]:
    """取一次公司行动、算因子、写暂存表。返回 ``(结果, 实际调用次数)``。

    ## 为什么要有界重试

    实测 Wind 这条通道**不稳**：本轮 5 次调用里 1 次在 90s 上限真超时
    （``subprocess.TimeoutExpired`` → ``WindSourceError``）。分几天无人值守跑时，
    一次超时就等于**浪费一格额度**还拿不到数据，所以瞬时失败要重试。

    但**额度/通道类错误绝不重试** —— 那必须立刻停下等下一天（见 :func:`main`）。
    """
    conn = client._get_conn()  # noqa: SLF001
    windcode = AShareLocalClient._to_wind_code(code)  # noqa: SLF001

    calls = 0
    actions: tuple[Any, ...] = ()
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            actions = wind.fetch_corporate_actions(windcode)
            calls += 1
            last_err = None
            break
        except (WindQuotaError, WindUnavailableError):
            raise  # 额度/通道问题：交给 main 立刻停，不重试
        except WindSourceError as exc:  # 超时 / 后端错
            calls += 1
            last_err = exc
            if attempt < retries:
                _LOG.warning("%s 第 %d 次取数失败（%s），重试", code, attempt + 1, exc)
                time.sleep(3.0 * (attempt + 1))
    if last_err is not None:
        raise last_err

    bars = load_recent_closes(conn, code)
    if len(bars) < 30:
        return CodeResult(code, False, note="本地 K 线不足 30 根"), calls
    if not actions:
        return CodeResult(code, False, note="Wind 无公司行动记录"), calls

    fmap = factor_from_actions(
        actions, prev_closes=prev_closes_for(bars, [a.ex_date for a in actions])
    )
    steps = sorted(fmap)
    if not steps:
        return CodeResult(code, False, note="无任何可用除权台阶（缺派息或除权前收盘）"), calls

    # ⚠️ 必须重标定：按定义算出的因子锚在**最新**一根，库里锚在**最早**一根
    # （实测 000001 相差 ~196 倍）。不重标定就直接换表 ⇒ 全部价格缩放两个数量级。
    anchor = current_latest_factor(conn, code)
    if anchor:
        fmap = reanchor(fmap, anchor)

    rows: list[tuple[str, str, float, str, str]] = []
    for open_ms, _close in bars:
        d = dt.datetime.fromtimestamp(open_ms / 1000, tz=dt.UTC).date().isoformat()
        # ⚠️ ``steps`` 已升序 ⇒ 要取**最近的那一次**除权（``later[0]``）的累计乘子。
        # 写成 ``later[-1]``（最晚那次）会让每一根 bar 都拿到同一个因子 ——
        # 整个分段结构塌成 2 个取值。是 R37 的形状对账（相对因子中位差 9%）逮到它的。
        later = [s for s in steps if s > d]
        factor = fmap[later[0]] if later else 1.0
        rows.append((code, d, factor, "wind_events", f"wind:get_stock_events steps={len(steps)}"))
    if write:
        save_recompute_factors(conn, rows)
    return CodeResult(code, True, rows=len(rows), steps=len(steps)), calls


def report(state: dict[str, Any]) -> None:
    print("=== 进度 ===")
    print(f"  已完成 {len(state.get('done', []))} 只   失败 {len(state.get('failed', {}))} 只")
    days = state.get("days") or {}
    if days:
        print(f"  按日调用: {json.dumps(dict(sorted(days.items())), ensure_ascii=False)}")
    if state.get("stopped"):
        print(f"  上次停止: {state['stopped']}")
    failed = state.get("failed", {})
    if failed:
        print("  最近失败：")
        for code, why in list(failed.items())[-5:]:
            print(f"    {code}: {why}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 Wind 公司行动重算后复权因子（只写暂存表）")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help="本轮调用上限（真正的硬边界是 RATE_LIMIT_ERROR）",
    )
    parser.add_argument("--only", default="", help="只跑这些代码（逗号分隔，调试用）")
    parser.add_argument("--report", action="store_true", help="只打印进度，不取数")
    parser.add_argument(
        "--retries", type=int, default=2, help="单只票的瞬时失败重试次数（额度类错误不重试）"
    )
    parser.add_argument("--dry-run", action="store_true", help="照常取数与计算，但不写暂存表")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    state = load_state()
    if args.report:
        report(state)
        return 0

    client = AShareLocalClient()
    wind = WindSourceClient()
    conn = client._get_conn()  # noqa: SLF001
    if not args.dry_run:
        ensure_recompute_stage(conn)

    if args.only:
        todo = [c.strip() for c in args.only.split(",") if c.strip()]
    else:
        done = set(state.get("done", []))
        todo = [c for c in priority_codes(conn) if c not in done]
        _LOG.info("待处理 %d 只（已跳过 %d 只）", len(todo), len(done))

    today = dt.date.today().isoformat()
    calls = 0
    for code in todo:
        if calls >= args.max_calls:
            _LOG.info("到达本轮上限 %d 次调用，就此打住（下次续跑即可）", args.max_calls)
            break
        try:
            result, used = process_code(
                client, wind, code, write=not args.dry_run, retries=args.retries
            )
        except (WindQuotaError, WindUnavailableError) as exc:
            _LOG.warning("额度/通道不可用（%s），本轮停止：%s", type(exc).__name__, exc)
            state["stopped"] = {"reason": type(exc).__name__, "detail": str(exc)[:200]}
            save_state(state)
            break
        except Exception as exc:  # noqa: BLE001
            calls += 1
            _LOG.warning("%s 失败：%s: %s", code, type(exc).__name__, str(exc)[:160])
            state.setdefault("failed", {})[code] = f"{type(exc).__name__}: {str(exc)[:120]}"
            state.setdefault("days", {})[today] = calls
            save_state(state)
            continue
        calls += used  # 含重试消耗的额度
        if result.ok:
            state.setdefault("done", []).append(code)
            _LOG.info(
                "%s 完成：%d 行 / %d 个除权台阶%s",
                code,
                result.rows,
                result.steps,
                f"（{result.note}）" if result.note else "",
            )
        else:
            state.setdefault("failed", {})[code] = result.note
        state.setdefault("days", {})[today] = calls
        save_state(state)
        time.sleep(0.4)

    save_state(state)
    report(state)
    if state.get("stopped"):
        print(
            "\n  上次因 {} 提前停止；进度已存盘，下次直接续跑。".format(
                state["stopped"].get("reason", "?")
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
