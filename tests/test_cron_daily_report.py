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
def _truth_source_crontab() -> pathlib.Path:
    """crontab 真相源（Emotion-CPT，随契约包分发）。"""
    from emotion_cpt.deploy.cron_spec import CRONTAB_PATH

    return CRONTAB_PATH


CRONTAB = _truth_source_crontab()  # ★ 真相源在 Emotion-CPT


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


def test_fail_marker_behind_a_later_success_is_not_reported(tmp_path: pathlib.Path) -> None:
    """⚠️ 历史 ``!!!!!`` 不能当成当前故障（2026-10-16 的真实故障）。

    日志没有 logrotate，标记写进去就一直躺着。那天 6 条作业全部 ``rc=0``，
    日报却报了 5 条早就修好的失败，把真问题（日报自己送不出去）一起埋掉。
    后来成功过 ⇒ 历史残留，不算。
    """
    _seed_all_logs(tmp_path, ok=True)
    name = pathlib.Path(cdr.JOBS[0][1]).name
    _write(
        tmp_path,
        name,
        f"===== {_ts(30)} 开始（保留 7 天 env=yes）=====\n"
        f"===== {_ts(30)} 结束 rc=1 =====\n"
        "!!!!! 清理未完成（rc=1）—— dashboard 运行行会继续堆积 !!!!!\n"
        f"===== {_ts(2)} 开始（保留 7 天 env=yes）=====\n"
        f"===== {_ts(2)} 结束 rc=0 =====\n",
    )
    report = cdr.build_report(24, str(tmp_path))
    assert report["problems"] == [], f"已修好的失败仍被上报：{report['problems']}"


def test_fail_marker_after_the_last_success_is_still_reported(tmp_path: pathlib.Path) -> None:
    """反向控制：**确实还没修掉**的失败不能被上面那条规则顺手静默。"""
    _seed_all_logs(tmp_path, ok=True)
    name = pathlib.Path(cdr.JOBS[0][1]).name
    _write(
        tmp_path,
        name,
        f"===== {_ts(3)} 结束 rc=0 =====\n"
        f"===== {_ts(2)} 开始 =====\n"
        f"===== {_ts(2)} 结束 rc=4 =====\n"
        "!!!!! 关注列表收盘汇总未完成（rc=4）—— 当天没有汇总推送 !!!!!\n",
    )
    report = cdr.build_report(24, str(tmp_path))
    assert len(report["problems"]) == 1, report["problems"]
    assert "失败标记" in report["problems"][0]


def test_quoted_failures_from_other_jobs_are_not_the_reports_own(tmp_path: pathlib.Path) -> None:
    """⚠️ 日报扫的日志里包含它自己，而它会把别的作业的失败行抄进自己日志。

    2026-10-16 实测：它的日志里有 4 条 ``  · !!!!!`` 引用行（还一天多套一层 ``·``），
    全被当成日报自己的失败 —— 于是那条真问题（日报 rc=4 送不出去）被噪音埋掉。
    引用行属于**那条作业**，不属于日报。
    """
    _seed_all_logs(tmp_path, ok=True)
    # ★ 显式写文件名，不再用 `cdr.JOBS[-1]` 当代理 ——
    #   JOBS 现在是从 crontab 真相源推导的，顺序由 crontab 决定
    #   （2026-10-24 加了 carry-forward 等作业后，最后一条已经不是日报自己）
    name = "cron-daily-report.log"
    _write(
        tmp_path,
        name,
        f"[{_ts(1)}] 日报开始（回看 24 小时）\n"
        "扫描窗口：最近 24 小时\n"
        f"  run-inspection：正常 · [{_ts(1)}] 巡检结束 rc=0\n"
        "**run-metric-prune** 失败 1 条：\n"
        "  · !!!!! 清理未完成（rc=1）—— run 类行会继续堆积 !!!!!\n"
        "  · · !!!!! 上一轮抄写留下的嵌套引用 !!!!!\n"
        f"===== {_ts(1)} 日报结束 rc=4 =====\n"
        "!!!!! 日报未完成（rc=4）—— 作业失败可能仍然没人知道 !!!!!\n",
    )
    report = cdr.build_report(24, str(tmp_path))
    assert len(report["problems"]) == 1, report["problems"]
    assert report["problems"][0].startswith("cron-daily-report: 1 条"), report["problems"]


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


# ── ⑥ ⚠️ 日报扫的日志必须与真相源一致，且仓内作业必须写日志 ──────────────────


def test_daily_report_jobs_are_derived_from_the_truth_source() -> None:
    """日报的 JOBS 必须是**推导**出来的，不是手抄的。

    手抄清单曾与 crontab 各漂各的（真实故障：因子重算的日志重定向被弄丢
    ⇒ 日报扫着一个不存在的日志，而真正的作业失败没人知道）。
    这条断言钉住「不许再手抄」。
    """
    from emotion_cpt.deploy.cron_spec import jobs_from_crontab

    assert cdr.JOBS == jobs_from_crontab(), (
        "日报的 JOBS 与 crontab 真相源推导结果不一致 —— "
        "JOBS 必须是推导出来的（scripts/cron_daily_report.py 的 _derive_jobs）"
    )


def test_every_repo_cron_job_writes_a_log() -> None:
    """★ 仓内 cron 作业**必须**有日志（两种合法形态都算）。

    背景：2026-10-24 把因子重算的调度从 02:20 改到 09:50 时，顺手把
    ``>> /home/ubuntu/logs/factor-recompute.log 2>&1`` 弄丢了
    ⇒ **因子重算失败会彻底静默**（因子表继续陈旧），而日报因为
    「扫的日志名对不上 crontab」报的是**配置漂移**，真正的故障没人提。

    这类缺陷的特征：**配置看起来是对的**（作业在、脚本在、调度对），
    只是「结果没人看」。所以必须有一条能失败的断言钉住。

    合法形态有两种，**都算数**：
    1. crontab 行上的 ``>> /home/ubuntu/logs/xxx.log 2>&1``
    2. **脚本内部自己写日志**（``LOG=...`` + ``>>"$LOG"``）
       —— 很多老脚本是这种，强行改成 (1) 反而会与脚本的锁文件逻辑打架。

    豁免：脚本不在任何仓的 ``deploy/cron/`` 下（外部脚本，如 ``jc-*``、
    ``backup-db.sh``）—— 它们自己管日志，不在本条断言范围内。
    """
    from emotion_cpt.deploy.cron_spec import load_crontab

    offenders = []
    for job in load_crontab():
        if "/deploy/cron/" not in job.script:
            continue  # 外部脚本
        if job.log is not None:
            continue  # 形态 1：crontab 级重定向
        sp = pathlib.Path(job.script)
        if not sp.is_file():
            offenders.append(f"{job.schedule}  {job.script}（脚本不存在）")
            continue
        body = sp.read_text(encoding="utf-8", errors="replace")
        # 形态 2：脚本内部自己重定向到某个日志
        if re.search(r'>>\s*"?\$\{?\w*LOG', body) or re.search(r'>>\s*\S+\.log', body):
            continue
        offenders.append(f"{job.schedule}  {job.script}（没有任何日志重定向）")
    assert not offenders, (
        "这些仓内 cron 作业没有日志 ⇒ 失败会彻底静默：\n  "
        + "\n  ".join(offenders)
        + "\n请在 crontab 真相源（Emotion-CPT）里给它加 >> /home/ubuntu/logs/xxx.log 2>&1，"
        "或让脚本自己写日志"
    )


def test_skip_exempt_jobs_do_not_count_as_failures(tmp_path: pathlib.Path) -> None:
    """★ Q6=A（owner 裁决）：日报**按作业跳过 skip 检测**。

    豁免作业的「还在跑,跳过」**不算故障**（不进 problems），
    但**要留痕**（日报里照常写一行）—— **不静默**。

    为什么需要：有些作业跑得比调度还密，「上一轮还在跑」是**正常**的；
    把它当「未执行 ⇒ 故障」会天天误报 ⇒ **狼来了** ⇒ 真问题被埋掉。
    """
    _seed_all_logs(tmp_path)
    exempt = sorted(cdr.SKIP_EXEMPT_JOBS)
    assert exempt, "豁免清单不能为空（否则这条机制没被用上，等于没做）"
    name = exempt[0]
    path = next(p for n, p in cdr.JOBS if n == name)
    _write(tmp_path, pathlib.Path(path).name, f"[{_ts(1)}] {cdr.SKIP_MARKER}\n")

    report = cdr.build_report(24, str(tmp_path), check_shared=False)
    assert not report["problems"], report["problems"]
    body = "\n".join(report["lines"])
    assert name in body, body
    assert "已知可忽略" in body, body


def test_non_exempt_job_skip_still_counts_as_problem(tmp_path: pathlib.Path) -> None:
    """反向：没被豁免的作业，skip **仍然**算问题。

    ⚠️ 这条是防止「豁免机制被用滥」——
    加一个作业进豁免清单是**容易**的，加错了就少了一个真问题的出口。
    """
    _seed_all_logs(tmp_path)
    non_exempt = [n for n, _ in cdr.JOBS if n not in cdr.SKIP_EXEMPT_JOBS]
    assert non_exempt, "不该所有作业都被豁免"
    name = non_exempt[0]
    path = next(p for n, p in cdr.JOBS if n == name)
    _write(tmp_path, pathlib.Path(path).name, f"[{_ts(1)}] {cdr.SKIP_MARKER}\n")

    report = cdr.build_report(24, str(tmp_path), check_shared=False)
    assert report["problems"], "非豁免作业的 skip 必须仍然算问题"


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


# ── ⑦ 共享表检查接进日报（治「上游断更没人知道」）─────────────────────────


def _fake_run(returncode: int, stdout: str = ""):
    class _P:
        pass

    # ⚠️ 类体里访问不到外层函数参数，必须在类外赋值
    _P.returncode = returncode
    _P.stdout = stdout
    _P.stderr = ""

    def _run(_argv, **_kw):  # noqa: ANN001, ANN003
        return _P()

    return _run


def test_stale_shared_tables_become_an_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """共享表检查返回 1（不新鲜）⇒ 日报必须**把它当问题**。

    2026-10-07 实测 Oracle 上没有任何东西会写那 7 张表，而 CPT 自己的 A 股
    快照虽能算出 ``data_quality.severity``，但那是**给人看**的 —— 没人盯面板
    就等于没有。这条是那件事的报警出口，所以必须验它真的会响。
    """
    _seed_all_logs(tmp_path, ok=True)
    monkeypatch.setattr(cdr, "webhook_configured", lambda: True)
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: sent.append((s, d)) or True)
    monkeypatch.setattr(
        cdr.subprocess,
        "run",
        _fake_run(1, "daily_bar: 最新 2026-09-30（期望 ≥ 2026-10-10）"),
    )

    report = cdr.build_report(24, str(tmp_path), check_shared=True)
    assert any("共享表" in p for p in report["problems"]), report["problems"]
    assert cdr.main(["--logs-dir", str(tmp_path), "--check-shared"]) == 0
    assert len(sent) == 1, "共享表断更却没有发告警 —— 这正是要治的那个洞"


def test_unverifiable_shared_tables_are_also_a_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """退出码 2（连不上库/查询出错）**也要算问题**。

    「无法验证」与「一切正常」在日报里长得一模一样的话，等于没接这个检查 ——
    上游断更 + 检查自己也断，两个故障会互相掩盖。
    """
    _seed_all_logs(tmp_path, ok=True)
    monkeypatch.setattr(cdr, "webhook_configured", lambda: True)
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cdr, "notify_problem", lambda s, d: sent.append((s, d)) or True)
    monkeypatch.setattr(cdr.subprocess, "run", _fake_run(2, "连不上库: 模拟失败"))

    report = cdr.build_report(24, str(tmp_path), check_shared=True)
    joined = " ".join(report["problems"])
    assert "共享表" in joined and "无法验证" in joined, report["problems"]
    assert cdr.main(["--logs-dir", str(tmp_path), "--check-shared"]) == 0
    assert len(sent) == 1, "「无法验证」没有变成告警 —— 那等于没接这个检查"


def test_fresh_shared_tables_add_no_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """退出码 0 ⇒ 不产生问题，且明细仍进报告（能看到「查过了、结论是什么」）。"""
    _seed_all_logs(tmp_path, ok=True)
    monkeypatch.setattr(cdr.subprocess, "run", _fake_run(0, "共享表新鲜度：全部新鲜。"))
    report = cdr.build_report(24, str(tmp_path), check_shared=True)
    assert not any("共享表" in p for p in report["problems"]), report["problems"]
    assert any("共享表新鲜度" in ln for ln in report["lines"])
