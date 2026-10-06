#!/usr/bin/env python3
"""每日作业日报：扫各 cron 作业日志，发现失败/异常就发飞书。

## 为什么需要它

R57 上线前审计的结论：**四条 cron 作业失败时「非零退出 + 写一行日志」，然后就没有然后了。**

- crontab 里没有 ``MAILTO``
- 全仓没有 logrotate
- **没有任何 watchdog / 消费者去 grep 这些日志**

唯一有出口的是 ``run-inspection-daily.sh`` 的飞书告警，而它只覆盖
**业务巡检自己的结论**（水位、数据源状态），完全不看另外三条作业的死活。

⇒ 因子重算或清理失败 = 表继续膨胀 / 数据继续陈旧，而**外部毫无异常**。
这与 R23 那条「保留期只写在注释里、从没自动化」是同一个病：
**做了记录 ≠ 有人会看。**

## 它扫什么

时间窗取**最近 24 小时**（按行首的 ISO 时间戳解析，不是按行数 —— 按行数会在
日志暴涨或某作业长时间没跑时给出错误结论）。两类命中：

1. ``!!!!!`` 标记行 —— 四条脚本在 rc≠0 时都会打，这是**跨脚本统一的失败标记**，
   不依赖任何单个脚本的措辞。
2. 「上一轮还在跑，跳过」—— 该作业当天**根本没执行**。它 exit 0，所以
   ``!!!!!`` 扫不到；但从运维视角「没跑」和「跑了成功」必须能区分开。

## 静默日不打扰

一切正常时**只打印到日志、不发飞书**（与 ``run_inspection.py`` 的静默日语义一致）。
有异常才发。

⚠️ **持久性失败会每天报一次**，这是刻意的：一个还没被修掉的失败，天天提醒
比「报一次然后没人记得」强。真嫌吵再加去重（存上次状态做差分），
但那时应该先问「这个失败为什么还没人处理」。
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters.feishu import ENV_WEBHOOK, notify_problem, webhook_configured  # noqa: E402

_LOG = logging.getLogger("cron_daily_report")

#: 被扫的作业 → 日志文件。**新增 cron 作业时必须同时加到这里** ——
#: 否则新作业的失败永远不会出现在日报里，而日报看起来一切正常。
#:
#: ⚠️ 刻意**包含日报自己**：它失败时（rc=3 webhook 没配 / rc=4 飞书没送达）
#: 恰恰是最需要人知道的时候，而下一轮日报会扫到它昨天写的 ``!!!!!``。
#: 「谁来看日报的日报」这个死循环由**下一轮自己**解开。
#:
#: 判据测试在 ``tests/test_cron_daily_report.py`` 的
#: ``test_every_cron_script_is_scanned_by_the_daily_report``：
#: 本表与 ``deploy/cron/crontab`` 的调度行按**日志文件名**双向比对。
JOBS: Final[tuple[tuple[str, str], ...]] = (
    ("factor-recompute", "/home/ubuntu/logs/factor-recompute.log"),
    ("run-inspection", "/home/ubuntu/logs/run-inspection.log"),
    ("run-metric-prune", "/home/ubuntu/logs/run-metric-prune.log"),
    ("dashboard-run-prune", "/home/ubuntu/logs/dashboard-run-prune.log"),
    ("cron-daily-report", "/home/ubuntu/logs/cron-daily-report.log"),
)

#: 四条脚本统一使用的失败标记。改这个标记要同时改这四处。
FAIL_MARKER: Final[str] = "!!!!!"

#: 「上一轮还在跑，跳过」—— exit 0，所以 FAIL_MARKER 扫不到，得单独认。
SKIP_MARKER: Final[str] = "还在跑，跳过"

_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


def _line_time(line: str) -> dt.datetime | None:
    m = _TS.search(line)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def scan(path: str, since: dt.datetime) -> tuple[list[str], list[str], str]:
    """返回 (失败行, 跳过行, 最后一次「结束 rc=」的原始行)。"""
    p = Path(path)
    if not p.exists():
        return [], [], f"（日志不存在：{path}）"
    failures: list[str] = []
    skips: list[str] = []
    last_rc = "（最近 24 小时没有结束标记）"
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - 读日志失败本身就是异常信号
        return [f"读日志失败 {path}: {exc}"], [], last_rc
    for line in raw.splitlines():
        stamp = _line_time(line)
        if stamp is not None and stamp < since:
            continue
        if "结束 rc=" in line:
            last_rc = line.strip()
        if FAIL_MARKER in line:
            failures.append(line.strip())
        elif SKIP_MARKER in line:
            skips.append(line.strip())
    return failures, skips, last_rc


def build_report(hours: int, logs_dir: str | None = None) -> dict[str, Any]:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    problems: list[str] = []
    lines: list[str] = []
    for name, default_path in JOBS:
        path = os.path.join(logs_dir, Path(default_path).name) if logs_dir else default_path
        failures, skips, last_rc = scan(path, since)
        if failures:
            problems.append(f"{name}: {len(failures)} 条失败标记")
            lines.append(f"**{name}** 失败 {len(failures)} 条：")
            lines += [f"  · {f}" for f in failures[-5:]]
        elif skips:
            problems.append(f"{name}: {len(skips)} 次未执行（上一轮还在跑）")
            lines.append(f"**{name}** 未执行 {len(skips)} 次：")
            lines += [f"  · {s}" for s in skips[-3:]]
        else:
            lines.append(f"{name}：正常 · {last_rc}")
    return {
        "problems": problems,
        "lines": lines,
        "window_hours": hours,
        "webhook": webhook_configured(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24, help="回看窗口（小时），默认 24")
    ap.add_argument("--logs-dir", default=None, help="日志目录（测试用，默认用 JOBS 里的绝对路径）")
    a = ap.parse_args(argv)

    # 与另两个 cron 入口（run_inspection.py / factor_recompute.py）同款：
    # 没有它就只有 lastResort（level=WARNING），INFO 会被静默丢弃、
    # 且日志行没有时间戳与 logger 名 —— 而日报要靠时间戳切窗口。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    report = build_report(a.hours, a.logs_dir)

    print(f"扫描窗口：最近 {report['window_hours']} 小时")
    for line in report["lines"]:
        print(f"  {line}")

    if not report["problems"]:
        print("\n全部正常 —— 不发飞书（静默日）。")
        return 0

    print(f"\n发现 {len(report['problems'])} 类问题：")
    for p in report["problems"]:
        print(f"  - {p}")

    if not report["webhook"]:
        # ⚠️ 与 run_inspection.py 同款处理：「压根没配」比「配了但发失败」更彻底，
        # 所以**更安静**的那种反而要报错，不能返回 0。
        print(f"\n[未配置 {ENV_WEBHOOK}] 本应发送的告警：", file=sys.stderr)
        for line in report["lines"]:
            print(f"  {line}", file=sys.stderr)
        return 3

    title = f"cron 日报：{len(report['problems'])} 类作业异常（最近 {report['window_hours']} 小时）"
    # ⚠️ notify_problem 返回**是否真的送达**。日报的全部价值就在「出了事能让人知道」，
    # 而它自己就是那个「知不知道」的最后一环 —— 送不出去还返回 0，
    # 就是把「告警通道自己坏了」变成又一次静默。
    delivered = notify_problem(title, report["lines"])
    if not delivered:
        print(f"\n❌ 飞书**没有送达**：{title}", file=sys.stderr)
        for line in report["lines"]:
            print(f"  {line}", file=sys.stderr)
        return 4
    print(f"\n已发送飞书告警：{title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
