#!/usr/bin/env python
"""盘中采集守护的单测（R89）。**先把「跑得通不通」证明给 emotion-core 看。**

覆盖的都是「错了会让人误判」的点：

1. ``outcome`` 三态不许混：``error`` 不能被说成 ``ok``，``empty`` 不能算失败；
2. 不在盘中时段 / 休市日要 ``skipped`` 并 rc=0（**不是失败**，日历说了不算失败）；
3. 读不到交易日历时**偏向跑**（漏采一整天的代价 > 多采一轮无用数据）；
4. 连续失败到阈值才报警（单轮抖动不许叫醒人），且 rc=1；
5. 日志是 JSON 行，字段固定（emotion-core 侧可能要按字段做外部核对）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _unit(name: str) -> Path:
    """unit 真相源在 Emotion-CPT（随契约包分发）—— 跨仓对账，不是各读各的副本。"""
    import emotion_cpt.deploy

    return Path(emotion_cpt.deploy.__file__).resolve().parent / "systemd" / name


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "intraday_collectd", ROOT / "scripts" / "intraday_collectd.py"
)
assert _SPEC and _SPEC.loader
collectd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(collectd)


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份独立日志，不碰 ``~/logs/``。"""
    log = tmp_path / "intraday.log"
    monkeypatch.setattr(collectd, "LOG_PATH", log)
    return log


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **测试不许写真库。**

    ``run_round()`` 末尾会调 ``_persist()`` 把这一轮的结论写进
    ``public.intraday_seal_snapshot`` —— 而上面那些用例喂的是假 ``scan()``
    （``600519 / bid1_lots=50000 / limit_price=1100``）。只要机器上连得到库、
    且 ``CPT_INTRADAY_DB`` 没关，**假数据就真的落进生产表**。

    R91 生产实测到的后果：表里 117 行有 18 行是这个夹具漏出来的假数据，而且它们的
    时间戳落在真实轮次之后，盘中面板于是显示「1 只封板、封单 5 万手、强度正常」——
    看着像真的，实际是测试数据。

    修法用 ``DB_WRITE_ENABLED``（生产开关）而不是打桩 psycopg：它就在被测函数的
    第一行，关掉之后连 import 都不会发生，比打桩更难被骗过。
    """
    monkeypatch.setattr(collectd, "DB_WRITE_ENABLED", False)


def _read(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


class TestNoRealDatabaseWrites:
    """R91：把「单测不许写真库」这条钉住，防止夹具被删后又悄悄开始写。"""

    def test_db_write_is_disabled_inside_tests(self) -> None:
        assert collectd.DB_WRITE_ENABLED is False, (
            "单测里 DB_WRITE_ENABLED 必须是 False —— 否则 run_round() 会把用例里的"
            "假行情写进真实生产表 intraday_seal_snapshot"
        )

    def test_ok_round_reports_disabled_and_writes_nothing(
        self, _isolated_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Some:
            def scan(self, **_: Any) -> dict[str, Any]:
                return {
                    "rank_size": 100,
                    "candidates": [{"code": "600519", "name": "贵州茅台"}],
                    "quotes": {},
                    "sealed": [
                        {
                            "code": "600519",
                            "name": "贵州茅台",
                            "bid1_lots": 50_000.0,
                            "distance_pct": 0.0,
                            "limit_price": 1100.0,
                        }
                    ],
                    "errors": [],
                }

        monkeypatch.setattr(collectd, "IntradayCollector", lambda: _Some())
        rec = collectd.run_round(limit=40, main_board_only=False)
        assert rec["outcome"] == "ok"
        # 开关关着时 _persist 在 import 之前就返回，连 psycopg 都不会被 import
        assert rec["db"] == "disabled"


class TestSessionGate:
    def test_before_open_is_outside_session(self) -> None:
        assert collectd.in_session(collectd.datetime(2026, 10, 22, 9, 29)) is False

    def test_at_open_is_inside(self) -> None:
        assert collectd.in_session(collectd.datetime(2026, 10, 22, 9, 30)) is True

    def test_at_close_is_still_inside(self) -> None:
        """15:00 收盘集合竞价结束那一刻，最后一笔成交价才算数。"""
        assert collectd.in_session(collectd.datetime(2026, 10, 22, 15, 0)) is True

    def test_after_close_is_outside(self) -> None:
        assert collectd.in_session(collectd.datetime(2026, 10, 22, 15, 1)) is False


class TestTradingDay:
    def test_db_unreachable_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读不到日历 ⇒ **跑**，不是跳过。

        故障方向不对称：漏采一天会让观察期计数中断，多采一轮只是多行日志。
        """
        import builtins

        real_import = builtins.__import__

        def _boom(name: str, *a: Any, **k: Any) -> Any:
            if name == "psycopg":
                raise RuntimeError("库挂了")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        now = collectd.datetime(2026, 10, 22, 10, 0)
        assert collectd.trading_day(now) == "2026-10-22"


class TestRunRound:
    def test_exception_becomes_error_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def scan(self, **_: Any) -> dict[str, Any]:
                raise collectd.IntradayError("涨幅榜取不到：超时")

        monkeypatch.setattr(collectd, "IntradayCollector", lambda: _Boom())
        rec = collectd.run_round(limit=40, main_board_only=False)
        assert rec["outcome"] == "error"
        assert "超时" in rec["error"]

    def test_unexpected_exception_is_still_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def scan(self, **_: Any) -> dict[str, Any]:
                raise KeyError("代码变了")

        monkeypatch.setattr(collectd, "IntradayCollector", lambda: _Boom())
        rec = collectd.run_round(limit=40, main_board_only=False)
        assert rec["outcome"] == "error"
        assert "KeyError" in rec["error"]

    def test_no_candidates_is_empty_not_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Nil:
            def scan(self, **_: Any) -> dict[str, Any]:
                return {
                    "rank_size": 100,
                    "candidates": [],
                    "quotes": {},
                    # 采集层（IntradayCollector.scan）已经把「谁封板了」判好，
                    # seal_board **不重算** —— 所以这里要模拟采集层的输出。
                    "sealed": [
                        {
                            "code": "600519",
                            "name": "贵州茅台",
                            "bid1_lots": 50_000.0,
                            "distance_pct": 0.0,
                            "limit_price": 1100.0,
                        }
                    ],
                    "errors": [],
                }

        monkeypatch.setattr(collectd, "IntradayCollector", lambda: _Nil())
        rec = collectd.run_round(limit=40, main_board_only=False)
        assert rec["outcome"] == "empty"
        assert rec["rank_size"] == 100

    def test_ok_round_carries_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Some:
            def scan(self, **_: Any) -> dict[str, Any]:
                return {
                    "rank_size": 100,
                    "candidates": [{"code": "600519", "name": "贵州茅台"}],
                    "quotes": {
                        "600519": {
                            "code": "600519",
                            "name": "贵州茅台",
                            "last": 1100.0,
                            "prev_close": 1000.0,
                            "bid1_vol": 5_000_000.0,
                            "bid1_price": 1100.0,
                            "asks": [],
                        }
                    },
                    # 采集层（IntradayCollector.scan）已经把「谁封板了」判好，
                    # seal_board **不重算** —— 所以这里要模拟采集层的输出。
                    "sealed": [
                        {
                            "code": "600519",
                            "name": "贵州茅台",
                            "bid1_lots": 50_000.0,
                            "distance_pct": 0.0,
                            "limit_price": 1100.0,
                        }
                    ],
                    "errors": [],
                }

        monkeypatch.setattr(collectd, "IntradayCollector", lambda: _Some())
        rec = collectd.run_round(limit=40, main_board_only=False)
        assert rec["outcome"] == "ok"
        assert rec["candidates"] == 1
        assert rec["sealed"] == 1


class TestMain:
    def test_outside_session_skips_with_rc0(
        self, _isolated_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(collectd, "now_market", lambda: collectd.datetime(2026, 10, 22, 20, 0))
        assert collectd.main(["--once"]) == 0
        rec = _read(_isolated_log)[-1]
        assert rec["outcome"] == "skipped"
        assert "盘中时段" in rec["reason"]

    def test_non_trading_day_skips_with_rc0(
        self, _isolated_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(collectd, "now_market", lambda: collectd.datetime(2026, 10, 22, 10, 0))
        monkeypatch.setattr(collectd, "trading_day", lambda now: None)
        assert collectd.main(["--once"]) == 0
        assert _read(_isolated_log)[-1]["outcome"] == "skipped"

    def test_single_error_under_threshold_does_not_alert(
        self, _isolated_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(collectd, "now_market", lambda: collectd.datetime(2026, 10, 22, 10, 0))
        monkeypatch.setattr(collectd, "trading_day", lambda now: "2026-10-22")
        monkeypatch.setattr(
            collectd,
            "run_round",
            lambda **_: {"at": "x", "outcome": "error", "error": "抖了一下"},
        )
        assert collectd.main(["--rounds", "2", "--max-failures", "3", "--interval", "0"]) == 0

    def test_consecutive_errors_reach_threshold_and_alert(
        self,
        _isolated_log: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(collectd, "now_market", lambda: collectd.datetime(2026, 10, 22, 10, 0))
        monkeypatch.setattr(collectd, "trading_day", lambda now: "2026-10-22")
        monkeypatch.setattr(
            collectd,
            "run_round",
            lambda **_: {"at": "x", "outcome": "error", "error": "取不到"},
        )
        rc = collectd.main(["--rounds", "5", "--max-failures", "3", "--interval", "0"])
        assert rc == 1
        assert "连续 3 轮失败" in capsys.readouterr().err

    def test_ok_resets_the_failure_streak(
        self, _isolated_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(collectd, "now_market", lambda: collectd.datetime(2026, 10, 22, 10, 0))
        monkeypatch.setattr(collectd, "trading_day", lambda now: "2026-10-22")
        seq = iter(["error", "error", "ok", "error", "error", "error"])
        monkeypatch.setattr(
            collectd,
            "run_round",
            lambda **_: {"at": "x", "outcome": next(seq), "error": "e"},
        )
        # 第 6 轮时 streak 才是 3（第 3 轮 ok 清零），所以这里必须报
        assert collectd.main(["--rounds", "6", "--max-failures", "3", "--interval", "0"]) == 1


class TestLogShape:
    def test_summary_has_fixed_fields(self) -> None:
        s = collectd.summarise(
            [
                {"outcome": "ok"},
                {"outcome": "empty"},
                {"outcome": "error", "error": "boom"},
            ]
        )
        assert s == {"rounds": 3, "ok": 1, "empty": 1, "errors": 1, "first_error": "boom"}

    def test_nothing_imports_the_collectd_in_production_paths(self) -> None:
        """采集守护是**独立进程**，不许被 cpt 包里的模块 import。

        这条与 ``test_nothing_imports_the_probe_in_production_paths`` 同源：
        ADR 0003 的分工是「CPT 只采集」，一旦 cpt 包里有人 import 它，
        判定逻辑就会顺理成章地被塞进服务进程 —— 那是分工被推翻的信号。
        """
        import ast

        offenders: list[str] = []
        for path in sorted((ROOT / "cpt").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if "intraday_collectd" in m:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert offenders == [], f"这些地方 import 了采集守护：{offenders}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


class TestDeployment:
    """部署形态也是契约：漏了 systemd 单元，观察期就没有可观测对象。"""

    def test_systemd_unit_runs_one_round_and_exits(self) -> None:
        svc = (_unit("cpt-intraday-collect.service")).read_text(encoding="utf-8")
        assert "Type=oneshot" in svc
        assert "--once" in svc
        # 「不带 PYTHONPATH」是门禁⑰的日常回归路径，写进单元注释是纪律，写死断言是防止被删
        assert "PYTHONPATH" in svc
        assert "NoNewPrivileges=true" in svc

    def test_timer_does_not_use_the_hour_step_trap(self) -> None:
        """``HH:MM/STEP`` 的 STEP 是**小时**步长（R89 实测翻车）。

        第一版写 ``09:30/2:00``，本意「每 2 分钟」，systemd 解成「每 2 小时的
        第 00 分」⇒ 一天只跑 3 次，而日志显示 ``outcome: skipped`` 看着一切正常。
        正确写法是把**小时**写成范围：``09..11,13..14:00/2:00``。
        """
        timer = (_unit("cpt-intraday-collect.timer")).read_text(encoding="utf-8")
        lines = [ln for ln in timer.splitlines() if ln.startswith("OnCalendar=")]
        assert len(lines) == 1, lines
        expr = lines[0].split("=", 1)[1]
        # 小时必须是范围（含 ``..``），否则 /2:00 会被当成小时步长
        after_date = expr.split(" *-*-* ", 1)[1]
        hour_part = after_date.split(":", 1)[0]
        assert ".." in hour_part, f"小时字段必须是范围：{hour_part!r}"
        # 分钟步长必须是 2
        assert ":00/2:00" in expr, expr
        assert "Asia/Shanghai" in expr, "必须显式带时区（本机是 UTC）"
        assert "Mon..Fri" in expr, "周末不该跑"

    def test_trading_day_uses_the_calendar_not_daily_bar(self) -> None:
        """盘中没有当天的日线，拿 ``daily_bar`` 判交易日会**永远 skipped**。

        R89 实测：第一版拿 ``max(date) FROM daily_bar`` 判「今天开市没有」，
        盘中它恒为昨天 ⇒ 每 2 分钟都报 skipped，一次没真正采过，而日志看起来
        完全正常。判据必须是事先排好表的 ``trade_calendar``。
        """
        import ast

        src = (ROOT / "scripts" / "intraday_collectd.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "trading_day"
        )
        # 只看真正会执行的东西：字符串常量与 import。**注释和 docstring 不算** ——
        # 判据要是搜全文，docstring 里写一句「不能拿 daily_bar 判」就自己把自己
        # 判死（这个坑 R89 一轮踩了三次）。
        # docstring 节点的**身份**要排除，不能靠字符串比较 ——
        # ``ast.get_docstring`` 返回的是去缩进后的文本，与 ``Constant.value``
        # 原文并不相等，按值排除会漏掉它（R89 实测踩过）。
        doc_node = (
            fn.body[0]
            if fn.body
            and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            else None
        )
        strings = [
            n.value
            for n in ast.walk(fn)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n is not (doc_node.value if doc_node is not None else None)
        ]
        # 函数体内的惰性 import 是 ``from X import a, b`` 形式，被导入的名字在
        # ``ImportFrom.names`` 里，不在 ``module`` 里（R89 实测踩过）。
        imported: list[str] = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Import):
                imported.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imported.extend(a.name for a in n.names)
                imported.append(n.module or "")
        assert any("a_share_local" in m for m in imported), imported
        assert "is_trade_day" in imported, f"必须查交易日历：{imported}"
        # 光 import 不够 —— 必须**真的调用**。只断 import 的话，把返回值换成
        # ``None`` 照样绿（R89 反向注入实测：注入后 20 条全绿没抓到）。
        called = [
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "is_trade_day" in called, f"必须调用 is_trade_day：{called}"
        assert not any("daily_bar" in st for st in strings), (
            f"trading_day 不许拿 daily_bar 判交易日：{strings}"
        )

    def test_manual_entry_script_has_no_pythonpath(self) -> None:
        body = (ROOT / "deploy" / "cron" / "intraday-collect.sh").read_text(encoding="utf-8")
        import re

        assert re.search(r"(?m)^\s*(export\s+)?PYTHONPATH=", body) is None
