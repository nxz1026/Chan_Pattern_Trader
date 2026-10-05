"""每日巡检：水位 + 数据源 + 上次结论对比 → 飞书告警。

## 为什么要有这个脚本（R38 的由来）

R38 实测：journal 里躺了 22 小时一条真 bug（``LLM 状态落库失败 ... 'str' object is
not callable``）没人发现。**记下来 ≠ 有人看**。所以巡检要把"现在健康吗"变成一个
**有人会收到**的结论。

## 判什么（两轨各取一刀）

- **第一轨（运行/数据完整性）**
  - 每个标的的**水位行**（:mod:`cpt_run_metric`）：``health``、``stale``、
    ``gap_count``、``last_bar_time``；
  - **水位流本身的新鲜度** —— 这条最关键：如果最新一行都是 10 分钟前的，
    那不是"数据没变"，而是**轮询可能挂了**。没有这一条，探针和被探针一起死掉时
    你会看到一片"正常"（这与 R25 那次 LLM worker 静默死亡同形）。
  - 数据源探活（``include_quota=False``：不碰 Wind 额度）。
- **第二轨（算法）**
  - **结构计数的突变**：``bi_count`` / ``zhongshu_count`` 相对上一行变了多少
    —— 突变是"算法或数据变了"的信号（本仓 R36 那次装 czsc 静默切后端就是
    突变而没人察觉）。
  - ``backend`` / ``dataset_hash`` 变了但结构没变？那更可疑（指纹变了说明输入或
    实现变了，结果却一样）。

## 告警策略：**只在「坏了」或「状态变了」时发**

R38 有一条硬结论：常态降级占了日志 41% 的量，把真信号淹了。告警同理 ——
每天发一条"czsc 还没装"没有任何价值。所以：

- 首次巡检：只记录基线，**不发**（没有"变化"可言）；
- 之后：**只在**有 ``failing``、或与上次结论**状态不同**时发；
- 恢复时也发一条（从坏到好，值得知道）。

## 用法

    .venv/bin/python scripts/run_inspection.py            # 巡检 + 落一行 + 按需告警
    .venv/bin/python scripts/run_inspection.py --print    # 只打印，不落库不发告警
    .venv/bin/python scripts/run_inspection.py --force    # 强制发（调试用）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters.a_share_local import AShareLocalClient  # noqa: E402
from cpt.adapters.feishu import ENV_WEBHOOK, notify_problem, webhook_configured  # noqa: E402
from cpt.storage import run_metric_store as rms  # noqa: E402

_LOG = logging.getLogger("run_inspection")

__all__ = ["STALE_METRIC_MS", "build_report", "main"]

#: 水位行多久算"流本身断了"。轮询是 30s 一轮，10 分钟足够宽松。
STALE_METRIC_MS: Final[int] = 10 * 60 * 1000

#: 结构计数相对上一行的变化幅度，超过它就当"突变"
COUNT_JUMP_RATIO: Final[float] = 0.15


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_report(conn: Any) -> dict[str, Any]:
    """只读：把水位流 + 数据源状态汇成一份结论。"""
    rows = rms.recent_metrics(conn, kind=rms.KIND_RUN, limit=200)
    problems: list[str] = []
    degraded: list[str] = []
    per_symbol: dict[tuple[str, str], dict[str, Any]] = {}
    for row in reversed(rows):  # 时间正序，后写覆盖先写 = 取最新
        per_symbol[(row["market"], row["symbol"])] = row

    if not rows:
        problems.append("水位表为空 —— 巡检/轮询可能从未成功落过一行")

    newest = max((r["observed_at"] or 0) for r in rows) if rows else 0
    age = _now_ms() - newest if newest else None
    if age is not None and age > STALE_METRIC_MS:
        problems.append(
            f"水位流已断 {age // 60000} 分钟"
            f"（阈值 {STALE_METRIC_MS // 60000}）—— 轮询或落库可能挂了"
        )

    for (market, symbol), row in per_symbol.items():
        tag = f"{market}/{symbol}"
        if row["health"] == "failing":
            problems.append(
                f"{tag} health=failing（缺口 {row['gap_count']}、stale={row['stale']}、"
                f"bars={row['bar_count']}）"
            )
        elif row["health"] == "degraded":
            degraded.append(f"{tag} degraded")
        if not row["bar_count"]:
            problems.append(f"{tag} 没有 K 线（bar_count=0）")

    # --- 第二轨：结构计数突变 + 指纹变化 ---
    jumps: list[str] = []
    fingerprint_changes: list[str] = []
    by_symbol: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in reversed(rows):
        by_symbol.setdefault((row["market"], row["symbol"]), []).append(row)
    for (market, symbol), seq in by_symbol.items():
        tag = f"{market}/{symbol}"
        for prev, cur in zip(seq, seq[1:], strict=False):
            for field, label in (
                ("bi_count", "笔数"),
                ("zhongshu_count", "中枢数"),
                ("fractal_count", "分型数"),
            ):
                a, b = int(prev[field] or 0), int(cur[field] or 0)
                if a and abs(b - a) / a > COUNT_JUMP_RATIO:
                    jumps.append(f"{tag} {label} {a} → {b}")
            if prev["dataset_hash"] != cur["dataset_hash"]:
                fingerprint_changes.append(
                    f"{tag} dataset_hash 变了"
                    f"（{(cur['dataset_hash'] or '')[:12]}… ← "
                    f"{(prev['dataset_hash'] or '')[:12]}…）"
                )
            if prev["backend"] != cur["backend"]:
                fingerprint_changes.append(f"{tag} backend {prev['backend']} → {cur['backend']}")
    if jumps:
        degraded.append(f"结构计数突变 {len(jumps)} 处（首条：{jumps[0]}）")
    if fingerprint_changes:
        degraded.append(f"指纹变化 {len(fingerprint_changes)} 处（首条：{fingerprint_changes[0]}）")

    sources: list[dict[str, Any]] = []
    try:
        from cpt.adapters.source_registry import capabilities_payload  # noqa: PLC0415

        payload = capabilities_payload(include_quota=False, use_cache=True, markets=None)
        for item in payload.get("sources", []):
            status = (item.get("probe") or {}).get("status")
            sources.append({"id": item.get("id"), "status": status})
            if status == "unavailable":
                degraded.append(f"数据源 {item.get('id')} 不可用")
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"数据源探活失败：{type(exc).__name__}")

    return {
        "checked_at": _now_ms(),
        "metric_rows": len(rows),
        "symbols": len(per_symbol),
        "newest_row_at": newest,
        "metric_age_ms": age,
        "sources": sources,
        "problems": problems,
        "degraded": degraded,
        "counts_jumped": jumps,
        "fingerprint_changes": fingerprint_changes,
    }


def _state_key(report: dict[str, Any]) -> str:
    """巡检"状态"的指纹 —— 只有它变了才值得发消息。"""
    payload = {
        "problems": sorted(report["problems"]),
        "degraded": sorted(report["degraded"]),
        "sources": sorted((s["id"], s["status"]) for s in report["sources"]),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CPT 每日巡检（水位 + 源 + 突变）")
    parser.add_argument("--print", action="store_true", help="只打印，不落库不发告警")
    parser.add_argument("--force", action="store_true", help="强制发飞书（即使状态没变）")
    parser.add_argument("--quiet", action="store_true", help="正常时不发（--force 的反面）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    client = AShareLocalClient()
    conn = client._get_conn()  # noqa: SLF001
    try:
        report = build_report(conn)
        state = _state_key(report)

        if args.print:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        prev = rms.latest_inspection(conn)
        prev_state = ""
        if prev:
            # ⚠️ ``detail`` 是 jsonb —— psycopg 读回来是 **dict**，不是 str。
            #   第一版写了 ``isinstance(detail, str)``，于是这个判断**永远为假**、
            #   prev_state 恒为 ""、每次运行都被当成"首次" ⇒ 状态变了也不会告警。
            #   而输出看着是对的（"不打扰"），所以这个 bug 靠读代码看不出来 ——
            #   是"我本该发却没发"这个反例把它逼出来的。
            raw = prev.get("detail")
            if isinstance(raw, dict):
                prev_state = str(raw.get("state_key", ""))
            elif isinstance(raw, str):
                try:
                    prev_state = str(json.loads(raw).get("state_key", ""))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    prev_state = ""

        # 落一行巡检结论（这就是看板上要显示的那份）
        health = "failing" if report["problems"] else ("degraded" if report["degraded"] else "ok")
        row = rms.RunMetric(
            kind=rms.KIND_INSPECTION,
            market="",
            symbol="",
            bar_count=report["metric_rows"],
            health=health,
            detail=json.dumps({**report, "state_key": state}, ensure_ascii=False, default=str),
        )
        rms.append_metrics(conn, [row])
        conn.commit()

        # 决定要不要发
        lines: list[str] = []
        lines += [f"🔴 {p}" for p in report["problems"]]
        lines += [f"🟡 {d}" for d in report["degraded"][:5]]
        if report["degraded"][5:]:
            lines.append(f"🟡 …… 另有 {len(report['degraded']) - 5} 条降级项")
        age_min = (report["metric_age_ms"] or 0) // 60000
        lines.append(
            f"水位行 {report['metric_rows']} 条 / 标的 {report['symbols']} 个"
            f" / 最新一行距今 {age_min} 分钟"
        )
        lines.append(
            "飞书通道：" + ("已配置" if webhook_configured() else f"**未配置 {ENV_WEBHOOK}**")
        )

        changed = prev_state != state
        # 首次巡检（没有上次结论）**只记基线不发**：那时"变化"没有意义 ——
        # 没有旧状态可比。R38 写这条规则时先只写进了 docstring，真跑起来
        # 第一次就发了告警（空 prev_state 必然 != state），这里补上实现。
        first_run = not prev_state
        should = args.force or (
            not args.quiet and not first_run and (changed or bool(report["problems"]))
        )
        if args.force or (report["problems"] and first_run and not args.quiet):
            # 首次就发现真问题：**要发**（"没有基线"不等于"没有故障"）
            should = True
        if should:
            title = (
                f"巡检：{len(report['problems'])} 问题 / {len(report['degraded'])} 降级"
                if report["problems"]
                else f"巡检：{len(report['degraded'])} 降级（状态有变化）"
            )
            if not webhook_configured():
                # ⚠️ R45：这一支原来只打印、**仍返回 0** —— 于是
                # 「配了但发送失败」报 3、而「压根没配」报 0，
                # **更严重的那种反而更安静**。
                #
                # 同一个函数里那条兄弟分支的注释原话是
                # 「告警通道坏掉的时候，恰恰最需要机器来发现」——
                # 而「没配」比「配了但发失败」**更彻底**（压根没有通道）。
                #
                # 为什么这条可达而不是理论情况：``deploy/env/cpt-dashboard.env``
                # **被 gitignore**，R45 才把它补进 ``.example``
                # （就是因为照模版部署会静默失去所有告警）
                # ⇒ 部署出来没配 webhook 是**现实状态**。
                #
                # 代价：本地开发机（有意不配）每天退 3。
                # 权衡：内容**照样打印到日志**，排查不受影响；
                # 换来的是「告警静默失效」被机器发现。owner 已确认取后者。
                print(f"[未配置 {ENV_WEBHOOK}] 本应发送的告警：")
                for line in lines:
                    print("   ", line)
                print(f"[!] 巡检发现了问题，但告警通道未配置（{ENV_WEBHOOK}） —— 告警能力不可用")
                return 3
            else:
                ok = notify_problem(title, lines)
                print(f"告警{'已发' if ok else '发送失败'}")
                if not ok:
                    # ⚠️ R45：本应发而没发出去，**不能**以 0 退出。
                    #
                    # ��与 R45 修的 ``factor_recompute`` 同一个病：「跑完了」与
                    # 「该做的没做成」共用 rc=0，cron / 看门狗 / 外部监控全都看不出来。
                    # 这里虽然已经打印了「发送失败」，但**只有人翻日志才知道** ——
                    # 而告警通道坏掉的时候，恰恰最需要机器来发现。
                    #
                    # 用 3 而不是 1：与 factor_recompute 对齐，含义明确
                    # 「巡检跑了，但告警没送出去」。
                    print("[!] 巡检发现了问题，但告警未送达 —— 告警通道可能已失效", flush=True)
                    return 3
        else:
            print("状态无变化且无问题 → 不打扰")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
