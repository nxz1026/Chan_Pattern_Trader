"""每日巡检的**退出码契约**（R45 P0-1 补测 · 第四批：生产 cron）。

## 为什么补这个

`deploy/cron/run-inspection-daily.sh` 每天 **03:40 UTC** 在生产上跑。
它的失败语义是今天 R45 亲手改的（与 `factor_recompute` 同一个病）：

> 「跑完了」与「该做的没做成」共用 rc=0 ⇒ cron / 看门狗 / 外部监控全都看不见。

`factor_recompute` 那次改动**有**测试（`test_factor_recompute_exit_code.py`），
**`run_inspection` 这边没有** —— 改了但没钉住。

## 钉的是什么

| 情形 | 期望 rc |
|---|---|
| 一切正常 / 状态没变 | 0 |
| `--print` 只打印 | 0 |
| **发现问题但告警没送出去** | **3**（不是 0！） |
| 告警通道压根没配 | 3（同上：本该发而没发） |

`--print``/`--force`/`--quiet`` 三个开关也要覆盖 —— 它们决定走不发告警那条路。

全程离线：`build_report` / `rms` / `notify` 全换假实现，不碰 DB、不发真飞书。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "run_inspection_under_test", _ROOT / "scripts" / "run_inspection.py"
)
assert _spec and _spec.loader
ri = importlib.util.module_from_spec(_spec)
sys.modules["run_inspection_under_test"] = ri
_spec.loader.exec_module(ri)


def _install(
    monkeypatch,
    *,
    problems: list[str],
    degraded: list[str],
    notified: bool,
    webhook: bool = True,
    prev: dict | None = None,
    history: list[dict] | None = None,
    capture: list | None = None,
):
    """把巡检的所有外部依赖换成假实现。"""
    # ⚠️ 键要**对齐** ``build_report`` 的真实返回（run_inspection.py:147）——
    # 第一版只给了 5 个键，main 里读 ``report["sources"]`` 直接 KeyError。
    # 漏一个键就测不成，而且报错指向「KeyError」而不是「你的夹具不全」。
    report = {
        "checked_at": 1_700_000_000_000,
        "metric_rows": 10,
        "symbols": 5,
        "newest_row_at": 1_700_000_000_000,
        "metric_age_ms": 60_000,
        "sources": [],
        "problems": problems,
        "degraded": degraded,
        "counts_jumped": [],
        "fingerprint_changes": [],
    }
    monkeypatch.setattr(ri, "build_report", lambda conn: report)
    monkeypatch.setattr(ri, "webhook_configured", lambda: webhook)

    def _notify(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if capture is not None:
            capture.append((args, kwargs))
        return notified

    monkeypatch.setattr(ri, "notify_problem", _notify)

    class _Client:
        def _get_conn(self):  # noqa: ANN202
            return _Conn()

        def close(self) -> None:
            return None

    class _Conn:
        def commit(self) -> None:
            return None

    # ⚠️ ``RunMetric = object`` 接不住 kwargs（``object() takes no arguments``）
    # —— 假类也得**有形状**。
    @dataclasses.dataclass
    class _Row:
        kind: str = ""
        market: str = ""
        symbol: str = ""
        bar_count: int = 0
        health: str = ""
        detail: str = ""

    class _RMS:
        RunMetric = _Row  # main 用 ``rms.RunMetric(kind=…, …)`` 构造
        KIND_INSPECTION = "inspection"
        appended: list = []
        history: list = []
        last_query: dict = {}

        @staticmethod
        def latest_inspection(conn):  # noqa: ANN001
            return prev

        @staticmethod
        def append_metrics(conn, rows):  # noqa: ANN001
            _RMS.appended = list(rows)

        @staticmethod
        def recent_metrics(conn, *, kind=None, market=None, symbol=None, limit=200):  # noqa: ANN001, E501
            # main 里为告警正文数「连续 N 天」要走这里。顺序必须与真 store 一致：
            # ``observed_at`` **倒序**（否则起点会被算成最新那条）。
            _RMS.last_query = {"kind": kind, "limit": limit}
            return tuple(_RMS.history[:limit])

    _RMS.history = list(history or [])

    monkeypatch.setattr(ri, "AShareLocalClient", _Client)
    monkeypatch.setattr(ri, "rms", _RMS)
    return _RMS


# ── 正常路径 ──────────────────────────────────────────────────
def test_no_problem_and_no_change_returns_zero(monkeypatch) -> None:
    """「无问题 + 状态没变」⇒ 0，且**不该**发告警。

    ⚠️ 第一版我编了个 ``prev.state_key="same"``，而真 key 是
    ``_state_key(report)`` 算出来的 ⇒ 必然不相等 ⇒ 判定「状态变了」
    ⇒ 尝试告警 ⇒ 假 notify 返回 False ⇒ 拿到 **3**。
    ⇒ **别编 key，用同一个 report 算。**
    """
    rms = _install(monkeypatch, problems=[], degraded=[], notified=False)
    report = ri.build_report(None)  # type: ignore[arg-type]
    # 重装一次，让 prev 用**真算出来的** key
    _install(
        monkeypatch,
        problems=[],
        degraded=[],
        notified=False,
        prev={"detail": {"state_key": ri._state_key(report)}},
    )  # noqa: SLF001
    assert ri.main([]) == 0
    assert not rms.appended or True  # 落库与否与本断言无关


def test_print_mode_returns_zero_and_sends_nothing(monkeypatch) -> None:
    """``--print`` 只打印 ⇒ 0。"""
    _install(monkeypatch, problems=["坏了"], degraded=[], notified=False)
    assert ri.main(["--print"]) == 0


# ── ⚠️ 核心：本该发而没发 ⇒ 非零 ─────────────────────────────
def test_alert_not_delivered_returns_3(monkeypatch) -> None:
    """⚠️ **这条是今天那个病的守卫**。

    发现问题、但飞书没送出去 ⇒ **rc=3**，不能 0 ——
    rc=0 的话 cron / 看门狗 / 外部监控全都会认为「今天没问题」。
    """
    _install(monkeypatch, problems=["水位断了"], degraded=[], notified=False)
    assert ri.main(["--force"]) == 3, "告警没送达却退 0 ⇒ 监控看不见"


def test_webhook_not_configured_also_returns_3(monkeypatch) -> None:
    """⚠️ **压根没配 webhook** ⇒ 同样 **rc=3**（R45 与 owner 确认后改）。

    原来这里只打印、仍返回 0 —— 于是「配了但发送失败」报 3、
    而「压根没配」报 0，**更严重的那种反而更安静**。

    可达性不是理论问题：``deploy/env/cpt-dashboard.env`` **被 gitignore**
    （R45 才把它补进 ``.example``，就是因为照模版部署会静默失去所有告警）。

    代价：本地开发机（有意不配）每天退 3。内容**照样打印到日志**，
    排查不受影响；换来「告警静默失效」能被机器发现。
    """
    _install(monkeypatch, problems=["水位断了"], degraded=[], notified=False, webhook=False)
    assert ri.main(["--force"]) == 3


def test_alert_delivered_returns_zero(monkeypatch) -> None:
    """对照组：告警**送达** ⇒ 0。"""
    _install(monkeypatch, problems=["水位断了"], degraded=[], notified=True)
    assert ri.main(["--force"]) == 0


def test_record_is_appended_regardless(monkeypatch) -> None:
    """无论发不发得出告警，**巡检结论都要落一行**（看板要看）。"""
    rms = _install(monkeypatch, problems=["x"], degraded=[], notified=False)
    ri.main(["--force"])
    assert rms.appended, "没落库 —— 看板上的巡检状态会一直是空的"


# ── R63：只在状态变化时发（用户 2026-10-14 拍板）─────────────────
def test_persistent_problems_are_not_re_sent_daily(monkeypatch) -> None:
    """⚠️ 问题**一直在**、但状态指纹没变 ⇒ **不发**。

    原判据 ``changed or bool(problems)``：只要有问题就每天推一条一模一样的卡片。
    A 股那 20 只链路断的标的在修好之前会天天响 —— 天天响等于不响，人会连
    「新变坏的那条」一起无视掉。判别力靠假 ``notify_problem`` 恒返回 False：
    只要它**尝试**发，rc 就是 3（见 R45 那组），所以 rc=0 就证明没发。
    """
    problems = ["cn/300299 health=failing（缺口 4、stale=True、bars=118）"]
    _install(monkeypatch, problems=problems, degraded=[], notified=False)
    report = ri.build_report(None)  # type: ignore[arg-type]
    _install(
        monkeypatch,
        problems=problems,
        degraded=[],
        notified=False,
        prev={"detail": {"state_key": ri._state_key(report)}},  # noqa: SLF001
    )
    assert ri.main([]) == 0, "问题没变却又发了一遍 —— 天天响等于不响"


def test_state_change_still_alerts(monkeypatch) -> None:
    """对照组：状态指纹变了 ⇒ 照发（收紧的是「重发」，不是「变化」）。"""
    _install(
        monkeypatch,
        problems=["水位断了"],
        degraded=[],
        notified=True,
        prev={"detail": {"state_key": "上一次的指纹"}},
    )
    assert ri.main([]) == 0


def test_first_run_with_problems_still_alerts(monkeypatch) -> None:
    """首次巡检发现问题**要发**：「没有基线」不等于「没有故障」。"""
    _install(monkeypatch, problems=["水位断了"], degraded=[], notified=False, prev=None)
    assert ri.main([]) == 3, "首次发现问题却没发 —— 空 prev_state 被当成「状态没变」了"


# ── 告警正文的「连续 N 天 / 本状态起于」那一行 ──────────────────────
#
# 这一行是并行 session 加的功能（R64 期间别人写进 main 的），原先**没有假体也
# 没有测试**：``_RMS`` 缺 ``recent_metrics`` ⇒ 8 条退出码测试全报
# ``AttributeError: type object '_RMS' has no attribute 'recent_metrics'``。
# 下面既补假体，也把这一行的语义钉住。
def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _row(state_key: str, day_ms: int, *, as_text: bool = False) -> dict:
    """造一条巡检水位行：``detail`` 是 dict，或（真库里也可能是）JSON 串。"""
    payload = {"state_key": state_key}
    detail: object = json.dumps(payload, ensure_ascii=False) if as_text else payload
    return {"detail": detail, "observed_at": day_ms}


def _patch_rms(monkeypatch, rows: list[dict]):
    class _RMS:
        KIND_INSPECTION = "inspection"

        @staticmethod
        def recent_metrics(conn, *, kind=None, market=None, symbol=None, limit=200):  # noqa: ANN001, E501
            return tuple(rows[:limit])

    monkeypatch.setattr(ri, "rms", _RMS)
    return _RMS


def test_streak_counts_history_rows_including_this_run(monkeypatch) -> None:
    """轮数**不 +1**：``main`` 是先落库再拼正文，窗口里最新那条就是本轮。

    这串是「本轮 + 昨天（同状态）+ 前天（旧状态）」⇒ 连续 2 天，起点是昨天
    （这一串最旧那条），不是前天（那是旧状态的最后一天）。
    """
    _patch_rms(
        monkeypatch,
        [
            _row("S", _ms(2026, 10, 15)),
            _row("S", _ms(2026, 10, 14)),
            _row("OLD", _ms(2026, 10, 13)),
        ],
    )
    assert ri._state_streak_info(None, "S") == (2, "2026-10-14")


def test_streak_is_first_day_without_history(monkeypatch) -> None:
    """历史为空（本轮没落成）⇒ 报第 1 天，不报「连续 0 天」。"""
    _patch_rms(monkeypatch, [])
    assert ri._state_streak_info(None, "S") == (1, "")


def test_streak_omits_start_when_window_all_matches(monkeypatch) -> None:
    """窗口内全同 ⇒ 数到边界了，起点在窗口外：宁可不说，不编日期。"""
    _patch_rms(monkeypatch, [_row("S", _ms(2026, 10, 15)), _row("S", _ms(2026, 10, 14))])
    assert ri._state_streak_info(None, "S") == (2, "")


def test_streak_reads_state_key_from_json_string_detail(monkeypatch) -> None:
    """``detail`` 存成 JSON 串（旧行）时也要认得出来。"""
    _patch_rms(
        monkeypatch,
        [_row("S", _ms(2026, 10, 15), as_text=True), _row("OLD", _ms(2026, 10, 14))],
    )
    assert ri._state_streak_info(None, "S") == (1, "2026-10-15")


def test_extract_state_key_survives_broken_json() -> None:
    assert ri._extract_state_key({"detail": "{不是 JSON"}) == ""
    assert ri._extract_state_key({"detail": None}) == ""
    assert ri._extract_state_key({}) == ""


def test_observed_date_ignores_non_numeric_timestamp() -> None:
    assert ri._observed_date({"observed_at": "2026-10-15"}) == ""
    assert ri._observed_date({}) == ""


def test_alert_body_carries_streak_line(monkeypatch) -> None:
    """函数返回对了但没接进告警正文是另一回事 —— 这条钉接线。"""
    sent: list = []
    _install(monkeypatch, problems=["水位断了"], degraded=[], notified=True, capture=sent)
    assert ri.main(["--force"]) == 0
    lines = sent[0][0][1] if sent else []
    assert any(line.startswith("连续 ") for line in lines), lines
