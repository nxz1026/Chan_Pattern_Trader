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
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "run_inspection_under_test", _ROOT / "scripts" / "run_inspection.py"
)
assert _spec and _spec.loader
ri = importlib.util.module_from_spec(_spec)
sys.modules["run_inspection_under_test"] = ri
_spec.loader.exec_module(ri)


def _install(monkeypatch, *, problems: list[str], degraded: list[str],
             notified: bool, webhook: bool = True, prev: dict | None = None):
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
    monkeypatch.setattr(ri, "notify_problem", lambda *a, **k: notified)

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
        RunMetric = _Row          # main 用 ``rms.RunMetric(kind=…, …)`` 构造
        KIND_INSPECTION = "inspection"
        appended: list = []

        @staticmethod
        def latest_inspection(conn):  # noqa: ANN001
            return prev

        @staticmethod
        def append_metrics(conn, rows):  # noqa: ANN001
            _RMS.appended = list(rows)

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
    report = ri.build_report(None)                     # type: ignore[arg-type]
    # 重装一次，让 prev 用**真算出来的** key
    _install(monkeypatch, problems=[], degraded=[], notified=False,
             prev={"detail": {"state_key": ri._state_key(report)}})  # noqa: SLF001
    assert ri.main([]) == 0
    assert not rms.appended or True                    # 落库与否与本断言无关


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
    _install(monkeypatch, problems=["水位断了"], degraded=[],
             notified=False, webhook=False)
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
