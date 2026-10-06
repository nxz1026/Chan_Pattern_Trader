"""cron 日报（``scripts/cron_daily_report.py``）的行为契约。

## 为什么要有这个测试

R57 上线前审计的发现：四条 cron 作业失败时「非零退出 + 写一行日志」，然后
**没有任何消费者**（无 MAILTO、无 logrotate、无 watchdog）。日报是补这个洞的，
所以它自己必须可靠 —— 一个「永远报一切正常」的日报比没有更坏。

## 本文件钉住什么

1. **静默日不打扰**：没有问题时**不发飞书**（避免每天一条噪音，三周后被无视）。
2. **失败必须被发现**：``!!!!!`` 是跨脚本统一的失败标记，扫到就要报。
3. **「没跑」与「跑成功」必须能区分**：锁跳过时 ``exit 0``，扫不到 ``!!!!!`` ——
   但从运维视角「今天这条作业压根没执行」和「跑了并且成功」是两件事。
4. **时间窗按时间戳切**，不按行数（按行数会在日志暴涨时给出错误结论）。
5. **告警送不出去要报错**：``0`` 只给「扫完了且一切正常」。
6. ⚠️ **``JOBS`` 与 ``deploy/cron/crontab`` 的调度行必须一一对应** ——
   少一条 = 那条作业的失败**永远不会**出现在日报里，而日报看起来一切正常。
   这正是 R57 加第 4 条 cron 时在 ``deploy/README.md`` 上踩过的坑，
   现在把它钉成测试，免得再踩第二次。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest
from scripts import cron_daily_report as cdr

ROOT = pathlib.Path(__file__).resolve().parents[1]
CRONTAB = ROOT / "deploy" / "cron" / "crontab"


def _ts(offset_hours: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=offset_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _write(tmp_path: pathlib.Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def _seed_all_logs(tmp_path: pathlib.Path, ok: bool = True) -> None:
    """给 JOBS 里每条作业写一份「正常」日志。"""
    for _name, path in cdr.JOBS:
        body = f"===== {_ts(2)} 开始 =====\n干活中\n===== {_ts(2)} 结束 rc=0 =====\n"
        if not ok:
            body += f"[{_ts(1)}] !!!!! 作业未完成（rc=1）—— 数据会继续堆积 !!!!!\n"
        _write(tmp_path, pathlib.Path(path).name, body)


# ── ① 静默日 ────────────────────────────────────────────────────────────────


def test_clean_logs_report_nothing_and_do_not_notify(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_all_logs(tmp_path, ok=True)
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: sent.append((s, d)) or True)

    report = cdr.build_report(24, str(tmp_path))

    assert report["problems"] == [], f"干净日志不该报问题：{report['problems']}"
    assert cdr.main(["--logs-dir", str(tmp_path)]) == 0
    assert sent == [], "静默日发了飞书 —— 三周后这条通道就会被无视"


# ── ② 失败必须被发现 ────────────────────────────────────────────────────────


def test_fail_marker_is_reported(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_all_logs(tmp_path, ok=False)
    monkeypatch.setattr(cdr, "webhook_configured", lambda: True)
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: sent.append((s, d)) or True)

    report = cdr.build_report(24, str(tmp_path))
    assert len(report["problems"]) == len(cdr.JOBS), report["problems"]

    assert cdr.main(["--logs-dir", str(tmp_path)]) == 0
    assert len(sent) == 1, "有失败却没发告警"
    assert any("未完成" in line for line in sent[0][1])


# ── ③ 「没跑」≠「跑成功」 ───────────────────────────────────────────────────


def test_skipped_run_is_reported_even_though_exit_code_is_zero(
    tmp_path: pathlib.Path,
) -> None:
    """锁跳过时脚本 ``exit 0``，日志里只有「还在跑，跳过」，没有 ``!!!!!``。

    「今天这条作业压根没执行」和「跑了并且成功」必须能被区分 ——
    前者需要人管，后者不需要。
    """
    _seed_all_logs(tmp_path, ok=True)
    name = pathlib.Path(cdr.JOBS[0][1]).name
    _write(
        tmp_path,
        name,
        f"[{_ts(1)}] 上一轮巡检还在跑，跳过\n===== {_ts(3)} 结束 rc=0 =====\n",
    )
    report = cdr.build_report(24, str(tmp_path))
    assert report["problems"], "「上一轮还在跑，跳过」被当成了成功"
    assert "未执行" in report["problems"][0]


# ── ④ 时间窗按时间戳，不按行数 ──────────────────────────────────────────────


def test_failures_outside_the_window_are_ignored(tmp_path: pathlib.Path) -> None:
    _seed_all_logs(tmp_path, ok=True)
    name = pathlib.Path(cdr.JOBS[0][1]).name
    _write(tmp_path, name, f"[{_ts(48)}] !!!!! 三天前的失败 !!!!!\n")
    report = cdr.build_report(24, str(tmp_path))
    assert report["problems"] == [], "48 小时前的失败不该进 24 小时窗口"


def test_window_boundary_is_time_based_not_line_based(tmp_path: pathlib.Path) -> None:
    """失败行**没有时间戳**时无法判定年龄 —— 必须当命中，不能静默丢弃。

    静默丢弃是最坏的一种：日志格式一改，日报就变成「一切正常」。
    """
    _seed_all_logs(tmp_path, ok=True)
    name = pathlib.Path(cdr.JOBS[0][1]).name
    _write(tmp_path, name, "!!!!! 无时间戳的失败行 !!!!!\n")
    report = cdr.build_report(24, str(tmp_path))
    assert report["problems"], "无法判定年龄的失败行被静默丢弃了"


# ── ⑤ 告警送不出去要报错 ────────────────────────────────────────────────────


def test_missing_webhook_exits_nonzero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「压根没配」比「配了但发失败」更彻底 ⇒ 更要报错，不能返回 0。"""
    _seed_all_logs(tmp_path, ok=False)
    monkeypatch.setattr(cdr, "webhook_configured", lambda: False)
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: pytest.fail("不该尝试发送"))
    assert cdr.main(["--logs-dir", str(tmp_path)]) == 3


def test_delivery_failure_exits_nonzero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日报的全部价值就是「出了事有人知道」，它自己送不出去必须可见。"""
    _seed_all_logs(tmp_path, ok=False)
    monkeypatch.setattr(cdr, "webhook_configured", lambda: True)
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: False)
    assert cdr.main(["--logs-dir", str(tmp_path)]) == 4


# ── ⑥ ⚠️ JOBS 与 crontab 必须一一对应 ───────────────────────────────────────


def _crontab_log_names() -> set[str]:
    """从 crontab 的调度行里抽出**重定向目标**的日志文件名。

    按日志名比对而不是脚本名 —— 因为 ``JOBS`` 存的是日志路径，两边唯一的
    契约锚点是 crontab 里那行 ``>> /home/ubuntu/logs/xxx.log``。
    """
    raw = CRONTAB.read_text(encoding="utf-8")
    found = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.search(r">>\s*\S*/([A-Za-z0-9_.-]+\.log)\b", stripped)
        if m:
            found.add(m.group(1))
    return found


def test_every_cron_script_is_scanned_by_the_daily_report() -> None:
    """⚠️ **本文件最重要的一条**。

    R57 加第 4 条 cron 时，``deploy/README.md`` 的合并安装清单漏了它 ⇒ 照文档
    部署时那条作业**永远装不进 crontab**，而看板毫无异常。同类陷阱在日报这里
    会再发生一次：加了作业却忘了登记 ``JOBS``，那条作业的失败就永远不会出现
    在日报里，**而日报看起来一切正常**。
    """
    in_crontab = _crontab_log_names()
    scanned = {pathlib.Path(p).name for _n, p in cdr.JOBS}

    missing = in_crontab - scanned
    assert not missing, (
        f"crontab 里有这些作业的日志，日报却不扫：{sorted(missing)} —— "
        f"它们的失败永远不会出现在日报里。加作业时必须同步 "
        f"scripts/cron_daily_report.py 的 JOBS。"
    )
    # 反向也查一遍：日报扫了个 crontab 里没有的作业同样是配置漂移
    extra = scanned - in_crontab
    assert not extra, f"日报扫了 crontab 里没有的作业日志：{sorted(extra)}"


def test_report_scans_its_own_log() -> None:
    """日报必须扫**自己的**日志。

    它失败时（webhook 没配 rc=3 / 飞书没送达 rc=4）恰恰是最该被知道的时候，
    而下一轮日报会看到它昨天写的 ``!!!!!`` —— 这是解开「谁看日报的日报」
    死循环的唯一办法（不能指望自己报告自己）。
    """
    names = {pathlib.Path(p).name for _n, p in cdr.JOBS}
    assert "cron-daily-report.log" in names, "日报不扫自己的日志 —— 它自己失败将无人知晓"


def test_fail_marker_is_the_same_one_all_scripts_write() -> None:
    """日报认的失败标记必须与四条脚本实际写进日志的**同一串**。

    改标记而不同步日报 = 全部失败静默。逐个核对而不是写死。
    """
    marker = cdr.FAIL_MARKER
    writers = [
        "factor-recompute-daily.sh",
        "run-inspection-daily.sh",
        "run-metric-prune-daily.sh",
        "dashboard-run-prune-daily.sh",
        "cron-daily-report.sh",
    ]
    for name in writers:
        src = (ROOT / "deploy" / "cron" / name).read_text(encoding="utf-8")
        assert marker in src, f"{name} 没有写 {marker} —— 它的失败日报看不见"


def test_crontab_actually_schedules_the_report() -> None:
    """日报必须真的被 crontab 排上，而不是只躺在仓里。

    与「合并安装清单漏一条」同源：文件存在 ≠ 会被执行。
    """
    raw = CRONTAB.read_text(encoding="utf-8")
    assert "cron-daily-report.sh" in raw, "日报作业没有登记进 deploy/cron/crontab"
    line = next(
        ln
        for ln in raw.splitlines()
        if "cron-daily-report.sh" in ln and not ln.strip().startswith("#")
    )
    fields = line.split()
    assert len(fields) >= 5 and fields[4] == "*", f"日报 crontab 行格式异常：{line!r}"
