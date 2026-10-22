"""观察期巡检（R83）：把「我没看见出事」变成「出事会通知我」。

## 为什么还要一个脚本（不是已经有 run_inspection.py 了吗）

`run_inspection.py` 回答的是「K 线/因子/结构**算得对不对**」，它有一条自己的
硬结论：**A 股水位行龄 ≤3 天是正常轮换**（批次一天只测热池 50 只），所以它
**故意**对单标的的陈旧沉默。这对「算法正确性」是对的，对「**系统还在转吗**」
是错的 —— 后者要的是另一组问题：

| 问题 | run_inspection.py | 本脚本 |
|---|---|---|
| 结构算得对不对 | ✅ | ✗ |
| 因子表跟没跟上日历 | ✅ | ✗ |
| **服务进程还活着吗** | ✗ | ✅ |
| **序列有没有落后** | ✗ | ✅ |
| **昨天该跑的批次跑了没有** | ✗ | ✅ |
| **采集器的盘口通不通** | ✗ | ✅ |

## 判据来源（每条都钉在一个可查的对象上）

1. **服务**：`cpt-dashboard.service` 是否 `active` + `/api/dashboard/health`
   返回 `ok:true`。**两个都要**：进程活着但 health 返回 `ok:false` 是 R25 那种
   「静默死亡」；health 通但 unit 死了是「僵尸进程」，重启一次就好。
   ⚠️ 这里**不用** `ss` 判端口 —— R83 实测容器里 `ss -ltn` 输出全空（无权限），
   会把人骗成「端口没监听」。判端口一律走 `curl`。
2. **序列**：`last_value >= max(id)`。**落后才是病，超前不是** —— `nextval`
   跳号（测试、回滚、多进程并发）都会让 `last_value` 跑在前面，那是正常的。
   这条以前是 R72 加的自愈**悄悄**修掉的，只在日志里留一行 warning、没人看；
   自愈是保险带，不应该兼任监控。
3. **批次**：`cpt_run_metric` 里最近一轮 A 股批次的**行龄**。
   `run_inspection.py` 只在 36h 无新行时才报（它对，因为它管的是「整体停摆」）；
   本脚本按**日历**判：正常日应该 < 30h，跨周末放宽到 78h（周五跑完到周一）。
4. **盘口**：`SinaQuoteClient.fetch_quotes` 取一只活跃票，要求拿到 ≥1 档买或卖。
   ADR 0003 的整条路线都吊在「新浪五档可用」这一条上，它挂了得有人知道。

## 告警策略：**只在状态变了时发**（与 run_inspection.py 同源）

天天发「一切正常」等于不响。首次只建基线不发；**首次就有问题要发**；
之后状态指纹变了才发；恢复也发一条。`--force` 强制发。

## 用法

    .venv/bin/python scripts/watch_period_check.py            # 检查 + 按需告警
    .venv/bin/python scripts/watch_period_check.py --print    # 只打印
    .venv/bin/python scripts/watch_period_check.py --force    # 强制发
    .venv/bin/python scripts/watch_period_check.py --json     # 机器可读

  退出码：0 = 全绿（或仅静默）；1 = 有问题；2 = 参数错；78 = 告警通道不可用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters.feishu import ENV_WEBHOOK, notify_problem, webhook_configured  # noqa: E402

__all__ = [
    "BATCH_LAG_ALERT_HOURS",
    "BATCH_LAG_ALERT_HOURS_WEEKEND",
    "STATE_PATH",
    "check_batch",
    "check_service",
    "check_sequence",
    "check_quote",
    "collect",
    "main",
]

#: 状态指纹落在哪。**与 cpt 的数据无关**，只是「上次看到了什么」。
STATE_PATH: Final[Path] = Path(
    os.environ.get("CPT_WATCH_STATE") or Path.home() / ".cache" / "cpt" / "watch_period_state.json"
)

#: 默认探测地址。systemd 里 CPT_PORT=8010，nginx 反代在 /cpt/。
HEALTH_URL: Final[str] = (
    os.environ.get("CPT_HEALTH_URL") or "http://127.0.0.1:8010/api/dashboard/health"
)
SERVICE_NAME: Final[str] = os.environ.get("CPT_SERVICE_NAME") or "cpt-dashboard.service"

#: 批次行龄告警线（小时）。正常日 24h 一轮 ⇒ 30h 给了 6h 余量。
#: 跨周末：周五 08:00 UTC 跑完，周一 08:00 才下一轮 ⇒ 72h + 余量 = 78h。
#:
#: ⚠️ 为什么不能只用一个数：用 30h 会在**每个周末**误报两次；用 78h 则工作日
#: 停摆两天才响。日历是判据的一部分，不是可调参数。
BATCH_LAG_ALERT_HOURS: Final[float] = 30.0
BATCH_LAG_ALERT_HOURS_WEEKEND: Final[float] = 78.0

#: ``cpt_run_metric.market`` 里 A 股的取值。**实测是 ``'cn'`` 不是 ``'ashare'``**
#: （R83：第一版按 ``'ashare'`` 查，一行都没有，脚本报「批次从没跑成」——
#: 一个错的口径就造出了一条假的停产告警）。crypto 侧是 ``'crypto'``。
ASHARE_MARKET: Final[str] = "cn"

#: 探盘口用哪只票。用最活跃的那只（茅台），它停牌的概率最低。
QUOTE_PROBE_CODE: Final[str] = os.environ.get("CPT_QUOTE_PROBE_CODE") or "600519"

#: health 接口的超时（秒）。本地环回，2 秒足够；挂了就能立刻判死。
HEALTH_TIMEOUT: Final[float] = 2.0

#: 盘中采集的新鲜度上限（分钟）。timer 每 2 分钟一轮，10 分钟 = 允许连挂 5 轮。
#: 只在**盘中时段**判，收盘后当然不会再有新记录 —— 全天候判会每天误报。
INTRADAY_STALE_MINUTES: Final[float] = float(
    os.environ.get("CPT_INTRADAY_STALE_MINUTES") or 10.0
)

#: 采集日志路径。与 ``scripts/intraday_collectd.py`` 共用同一条环境变量。
INTRADAY_LOG: Final[Path] = Path(
    os.environ.get("CPT_INTRADAY_LOG") or (Path.home() / "logs" / "intraday-collect.log")
)


def _now() -> datetime:
    return datetime.now(UTC)


def check_service() -> dict[str, Any]:
    """服务活着吗：unit active **且** health ok。两个都要，见模块 docstring。"""
    result: dict[str, Any] = {"name": "service", "ok": True, "problems": [], "detail": {}}
    # unit 状态：不 import docker，直接用 systemctl（本脚本跑在宿主机上）。
    # 注意：脚本本身不 sudo，is-active 只读不需要。
    try:
        proc = os.popen(f"systemctl is-active {SERVICE_NAME} 2>/dev/null")
        state = proc.read().strip()
        proc.close()
    except OSError as exc:  # pragma: no cover - systemctl 不在 PATH 上才会到这里
        state = f"error:{exc}"
    result["detail"]["unit_state"] = state
    if state != "active":
        result["ok"] = False
        result["problems"].append(f"{SERVICE_NAME} 状态 {state}（不是 active）")
        # unit 挂了就不用再探 health —— 那是同一个故障的两面
        return result

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        result["ok"] = False
        result["problems"].append(f"health 探不通：{HEALTH_URL}（{exc}）")
        return result
    result["detail"]["health"] = {
        k: body.get(k) for k in ("ok", "degraded", "last_error", "stale", "consecutive_failures")
    }
    if body.get("ok") is not True:
        result["ok"] = False
        detail = body.get("last_error") or "未给原因"
        result["problems"].append(f"health 返回 ok={body.get('ok')!r}（{detail}）")
    elif body.get("stale") is True:
        # stale 不等于坏：刚开机还没轮询过。但连续失败要报。
        if int(body.get("consecutive_failures") or 0) >= 3:
            result["ok"] = False
            result["problems"].append(
                f"health 连续失败 {body.get('consecutive_failures')} 次（{body.get('last_error')}）"
            )
    return result


def check_sequence(conn: Any) -> dict[str, Any]:
    """bigserial 序列有没有落后于 ``max(id)``。超前不是病，见模块 docstring。"""
    result: dict[str, Any] = {"name": "sequence", "ok": True, "problems": [], "detail": {}}
    for table in ("cpt_structure_event", "cpt_signal_event"):
        row = conn.execute(
            f"SELECT max(id), (SELECT last_value FROM pg_sequences "
            f" WHERE schemaname = 'public' AND sequencename = %s) FROM public.{table}",
            (f"{table}_id_seq",),
        ).fetchone()
        max_id, seq = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
        result["detail"][table] = {"max_id": max_id, "seq": seq}
        if max_id > seq:
            result["ok"] = False
            result["problems"].append(
                f"{table} 序列落后 {max_id - seq}：seq={seq} < max(id)={max_id}"
                "（自愈会在下次写入时修，但说明有别的写入路径在抢号）"
            )
    return result


def check_batch(conn: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """最近一轮 A 股批次有多旧。跨周末用更宽的线，见 ``BATCH_LAG_ALERT_HOURS*``。"""
    now = now or _now()
    result: dict[str, Any] = {"name": "batch", "ok": True, "problems": [], "detail": {}}
    row = conn.execute(
        "SELECT max(observed_at), count(*) FROM public.cpt_run_metric WHERE market = %s",
        (ASHARE_MARKET,),
    ).fetchone()
    if row is None or row[0] is None:
        # 一条水位都没有 ≠ 一切正常：要么表被清了，要么批次从没跑成过。
        result["ok"] = False
        result["problems"].append("cpt_run_metric 里没有任何 A 股水位行（批次从没跑成？）")
        return result
    latest = row[0]
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    lag_hours = (now - latest).total_seconds() / 3600.0
    threshold = BATCH_LAG_ALERT_HOURS_WEEKEND if now.weekday() >= 5 else BATCH_LAG_ALERT_HOURS
    result["detail"] = {
        "latest": latest.isoformat(),
        "lag_hours": round(lag_hours, 1),
        "threshold_hours": threshold,
        "rows": int(row[1]),
    }
    if lag_hours > threshold:
        result["ok"] = False
        result["problems"].append(
            f"最近一轮 A 股批次在 {latest:%Y-%m-%d %H:%M}Z，已 {lag_hours:.1f}h"
            f"（阈值 {threshold:.0f}h）—— 批次可能没跑成"
        )
    return result


def check_intraday(*, now: datetime | None = None) -> dict[str, Any]:
    """盘中采集活没活（R89 B4）。

    **只在盘中时段判**：收盘后当然没有新记录，全天候判就是每天 15:00 之后
    必红一次，然后人就会习惯性忽略告警 —— 那比不告警更糟。

    判据只有一条：最新一条日志距今不超过 :data:`INTRADAY_STALE_MINUTES`。
    「最新一条是 error」不算问题 —— 单轮失败是常态，连续失败由
    ``intraday_collectd`` 自己按 ``max_failures`` 判（那边才会 rc=1）。
    """
    result: dict[str, Any] = {"name": "intraday", "ok": True, "problems": [], "detail": {}}
    moment = now or _now()
    from zoneinfo import ZoneInfo

    market_now = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    in_session = dtime(9, 30) <= market_now.time() <= dtime(15, 0)
    detail: dict[str, Any] = {
        "log": str(INTRADAY_LOG),
        "in_session": in_session,
        "market_time": market_now.isoformat(timespec="seconds"),
    }
    result["detail"] = detail
    if not INTRADAY_LOG.exists():
        if in_session:
            result["ok"] = False
            result["problems"].append(f"盘中采集日志不存在：{INTRADAY_LOG}")
        return result

    try:
        from cpt.application.intraday_status import parse_log_tail
    except ImportError as exc:  # pragma: no cover
        result["ok"] = False
        result["problems"].append(f"import 失败：{exc}")
        return result

    records = parse_log_tail(INTRADAY_LOG)
    if not records:
        if in_session:
            result["ok"] = False
            result["problems"].append("盘中有采集时段，但日志里一条记录都没有")
        return result

    from datetime import datetime as _dt

    try:
        last_at = _dt.fromisoformat(str(records[-1].get("at")))
    except (TypeError, ValueError):
        result["ok"] = False
        result["problems"].append("采集日志最后一条的 at 无法解析")
        return result
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=market_now.tzinfo)
    age_min = (market_now - last_at).total_seconds() / 60.0
    detail.update(
        {
            "last_at": last_at.isoformat(timespec="seconds"),
            "age_minutes": round(age_min, 1),
            "last_outcome": records[-1].get("outcome"),
        }
    )
    if in_session and age_min > INTRADAY_STALE_MINUTES:
        result["ok"] = False
        result["problems"].append(
            f"盘中采集 {age_min:.0f} 分钟没有新记录（上限 {INTRADAY_STALE_MINUTES:.0f} 分钟）"
            f" —— 最后一轮 {last_at.isoformat(timespec='seconds')}"
        )
    return result


def check_quote(code: str = QUOTE_PROBE_CODE) -> dict[str, Any]:
    """盘口通道通不通：新浪批量五档能不能拿到至少一档。"""
    result: dict[str, Any] = {"name": "quote", "ok": True, "problems": [], "detail": {}}
    try:
        from cpt.adapters.a_share_public import ASharePublicError, SinaQuoteClient
    except ImportError as exc:  # pragma: no cover
        result["ok"] = False
        result["problems"].append(f"import 失败：{exc}")
        return result
    try:
        quotes = SinaQuoteClient(timeout=8.0).fetch_quotes([code])
    except ASharePublicError as exc:
        result["ok"] = False
        result["problems"].append(f"新浪五档取不到：{exc}")
        return result
    quote = quotes.get(code)
    if quote is None:
        result["ok"] = False
        result["problems"].append(f"新浪没有返回 {code}（响应里没有这一行）")
        return result
    levels = sum(1 for x in list(quote.get("bids") or []) + list(quote.get("asks") or []) if x)
    result["detail"] = {
        "code": code,
        "last": quote.get("last"),
        "levels": levels,
        "time": f"{quote.get('date')} {quote.get('time')}",
    }
    if levels == 0:
        # 全空只可能是停牌 —— 探针票选了最活跃的一只，全空说明通道有问题
        result["ok"] = False
        result["problems"].append(f"{code} 十档全空（探针票不该停牌，疑似通道问题）")
    return result


def collect(*, skip_network: bool = False) -> dict[str, Any]:
    """跑全部检查，返回汇总。单项失败不影响其它项（否则一个挂了全看不见）。"""
    checks: list[dict[str, Any]] = [check_service()]
    try:
        import psycopg
        from cpt.adapters.a_share_local import connection_kwargs

        conn = psycopg.connect(**connection_kwargs())
        try:
            checks.append(check_sequence(conn))
            checks.append(check_batch(conn))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 连不上库本身就是要报的结论
        checks.append(
            {"name": "database", "ok": False, "problems": [f"连不上库：{exc}"], "detail": {}}
        )
    # 盘中采集只看本地日志，不出网、不查库 —— 放在网络段之外，
    # ``--skip-network`` 时也要跑（它就是用来在弱网下判断采集链路的）。
    try:
        checks.append(check_intraday())
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"name": "intraday", "ok": False, "problems": [f"采集巡检异常：{exc}"], "detail": {}}
        )
    if not skip_network:
        # 盘口探活要出网，最不可靠 —— 它抛异常时**必须**降级成一条问题，
        # 不能让整个巡检崩掉。否则「新浪抖了一下」与「服务挂了」在告警上
        # 长得一模一样（都收不到消息），而后者才是真该知道的。
        try:
            checks.append(check_quote())
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {"name": "quote", "ok": False, "problems": [f"盘口探活异常：{exc}"], "detail": {}}
            )
    problems = [p for c in checks for p in c["problems"]]
    return {
        "checked_at": _now().isoformat(timespec="seconds"),
        "ok": not problems,
        "problems": problems,
        "checks": checks,
    }


def _state_key(report: dict[str, Any]) -> str:
    """状态指纹。**只看问题集合**，不看时间戳/延迟数值 —— 否则每小时都在「变」。"""
    return json.dumps(sorted(report["problems"]), ensure_ascii=False)


def _load_state() -> dict[str, Any] | None:
    try:
        payload: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # 文件在但不是 dict（被手工写坏/写成了列表）同样当没有基线：
    # 宁可多发一条，也不要在一个半坏的状态文件上比对指纹。
    return payload if isinstance(payload, dict) else None


def _save_state(key: str) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"state_key": key, "at": _now().isoformat(timespec="seconds")}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        tmp.replace(STATE_PATH)
    except OSError as exc:  # pragma: no cover - 只是缓存，坏了大不了多发一条
        print(f"[warn] 状态文件写不了：{exc}", file=sys.stderr)


def _render(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for check in report["checks"]:
        mark = "✅" if check["ok"] else "❌"
        names = {
            "service": "服务",
            "sequence": "序列",
            "batch": "批次",
            "quote": "盘口",
            "database": "数据库",
        }
        label = names.get(check["name"], check["name"])
        extra = ""
        if check["name"] == "batch" and check["detail"].get("lag_hours") is not None:
            d = check["detail"]
            extra = f"（最近一轮 {d['lag_hours']}h 前，阈值 {d['threshold_hours']:.0f}h）"
        elif check["name"] == "quote" and "levels" in check["detail"]:
            extra = f"（{check['detail']['code']} {check['detail']['levels']}/10 档）"
        elif check["name"] == "sequence":
            parts = [f"{k} {v['seq']}/{v['max_id']}" for k, v in check["detail"].items()]
            extra = f"（seq/max(id)：{'，'.join(parts)}）" if parts else ""
        lines.append(f"{mark} {label}{extra}")
        for problem in check["problems"]:
            lines.append(f"    · {problem}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="观察期巡检：服务/序列/批次/盘口")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", help="只打印，不写状态不发告警"
    )
    parser.add_argument("--force", action="store_true", help="强制发飞书（即使状态没变）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--skip-network", action="store_true", help="跳过盘口探活（离线调试用）")
    args = parser.parse_args(argv)

    report = collect(skip_network=args.skip_network)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"观察期巡检 {report['checked_at']}")
        for line in _render(report):
            print(line)
        print("全绿" if report["ok"] else f"❌ {len(report['problems'])} 个问题")

    if args.print_only:
        return 0 if report["ok"] else 1

    key = _state_key(report)
    previous = _load_state()
    first_time = previous is None
    changed = first_time or (previous is not None and previous.get("state_key") != key)
    # 首次只建基线不发；**但首次就有问题要发** —— 「没有基线」不等于「没有故障」
    should_send = args.force or (changed and (not first_time or not report["ok"]))

    if not should_send:
        _save_state(key)
        if not report["ok"]:
            print("（状态未变，不重复告警）")
        return 0 if report["ok"] else 1

    if not webhook_configured():
        # 与 run_inspection.py 同款：告警通道自己坏了却报成功，是最坏的失效
        print(f"[warn] {ENV_WEBHOOK} 未配置，发不出告警", file=sys.stderr)
        _save_state(key)
        return 78

    if report["ok"]:
        summary = "观察期巡检：恢复全绿"
    else:
        summary = f"观察期巡检：{len(report['problems'])} 个问题"
    sent = notify_problem(summary, _render(report))
    _save_state(key)
    if not sent:
        print("[warn] 飞书发送失败（详见 stderr）", file=sys.stderr)
        return 78
    print("已推送飞书" if changed else "已推送飞书（强制）")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
