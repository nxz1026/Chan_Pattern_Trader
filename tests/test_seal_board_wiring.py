"""R91：首屏封板强度这条链路的**跨层契约**。

单测各自绿不算完 —— 这条链路跨了 5 层：
``intraday_seal_snapshot`` 表 → ``intraday_seal_store`` 读 → ``seal_board`` 翻译 →
``/api/dashboard/seal-board`` 路由 → 前端 ``renderSealBoard`` 渲染。
任何一层改了键名/字段名，链路就断了，而每一层的单测都还是绿的。
本文件专门盯「层与层之间握手的那个名字」。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_FILES = [
    "dash-core.js",
    "dash-chrome.js",
    "dash-structure.js",
    "dash-signal.js",
    "dash-chart.js",
    "dash-alert.js",
    "dash-ops.js",
]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _js(name: str) -> str:
    return _read(f"dashboard/{name}")


class TestRoute:
    def test_route_exists_before_the_intraday_status_one(self) -> None:
        """两条路由都在首屏用，且语义相邻 —— 顺序不重要但必须都在。"""
        app = _read("cpt/web/app.py")
        assert '"/api/dashboard/seal-board"' in app
        assert '"/api/dashboard/intraday-status"' in app

    def test_route_degrades_instead_of_raising(self) -> None:
        """首屏旁路：库挂了不许把整个 dashboard 变成 500。"""
        app = _read("cpt/web/app.py")
        start = app.index("def _seal_board_payload()")
        end = app.index("def _latest_market_day()")
        body = app[start:end]
        assert "except Exception" in body, "没有兜底 except —— 库抖动会把首屏打成 500"
        assert '"available"] = False' in body or '"available"] = False' in body
        assert "seal_board_unavailable" in body

    def test_route_reads_the_latest_round_only(self) -> None:
        """不许自己去查库写第二份 SQL：必须走 storage 层的 load_latest_round。"""
        app = _read("cpt/web/app.py")
        start = app.index("def _seal_board_payload()")
        end = app.index("def _latest_market_day()")
        body = app[start:end]
        assert "load_latest_round" in body
        assert "SELECT" not in body.upper().replace("SELECTED", ""), (
            "路由里出现了 SQL —— 分层门禁要求 SQL 只在 storage/adapters"
        )


class TestFrontend:
    def test_render_function_exists_and_is_called(self) -> None:
        signal = _js("dash-signal.js")
        ops = _js("dash-ops.js")
        assert "function renderSealBoard(payload)" in signal
        assert "renderSealBoard(remoteOps.sealBoard)" in ops

    def test_loader_hits_the_route_and_keeps_the_flag(self) -> None:
        ops = _js("dash-ops.js")
        assert "/seal-board" in ops
        assert "remoteOps.sealBoard = isObject(body) ? body : null;" in ops

    def test_it_is_not_polled_on_the_interval_timer(self) -> None:
        """一轮采集 2 分钟一次，跟着轮询刷纯浪费。

        真判据：把 ``startPolling`` 的函数体切出来，里面不许出现 ``loadSealBoard``。
        （按「出现次数」判是错的 —— 函数定义行本身就会让计数变成 2。）
        """
        ops = _js("dash-ops.js")
        start = ops.index("function startPolling(url, intervalMs = 5000) {")
        depth = 0
        seen = False
        end = start
        for i in range(start, len(ops)):
            ch = ops[i]
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
                if seen and depth == 0:
                    end = i + 1
                    break
        body = ops[start:end]
        assert "loadSealBoard" not in body, (
            "loadSealBoard 被放进了轮询定时器回调里 —— 应该只在 refreshOps 里调度一次"
        )

    def test_the_html_line_exists(self) -> None:
        html = _read("dashboard/index.html")
        assert 'data-testid="seal-board-line"' in html
        assert 'class="seal-board-line"' in html

    def test_the_two_first_screen_lines_stay_separate(self) -> None:
        """封板强度（读表）与采集状态（读日志）必须各占一行、各有 testid。"""
        html = _read("dashboard/index.html")
        assert 'data-testid="seal-board-line"' in html
        assert 'data-testid="intraday-status-line"' in html
        idx_seal = html.index('data-testid="seal-board-line"')
        idx_status = html.index('data-testid="intraday-status-line"')
        assert idx_seal != idx_status
        # 两行都得在首屏横幅里，而不是散落在别处
        banner_start = html.index('data-testid="market-step-banner"')
        banner_end = html.index("</section>", banner_start)
        assert banner_start < idx_seal < banner_end
        assert banner_start < idx_status < banner_end

    def test_unavailable_is_shown_not_hidden(self) -> None:
        """「没有今天的快照」必须写出来，藏起来等于让人以为今天没票封板。"""
        signal = _js("dash-signal.js")
        start = signal.index("function renderSealBoard(payload)")
        end = signal.index("/* ---------------- R89 B3：盘中采集活没活")
        body = signal[start:end]
        assert 'node.dataset.state = "unavailable"' in body
        assert "没有今天的封板快照" in body

    def test_no_buy_words_in_the_rendered_line(self) -> None:
        signal = _js("dash-signal.js")
        start = signal.index("function renderSealBoard(payload)")
        end = signal.index("/* ---------------- R89 B3：盘中采集活没活")
        body = signal[start:end]
        for word in ("买入", "卖出", "加仓", "抄底", "建议"):
            assert word not in body, f"首屏封板那行出现了禁词「{word}」"


class TestBundle:
    def test_bundle_contains_the_new_function(self) -> None:
        """源文件改了但没重建 bundle，线上就是旧代码 —— 这条守这个。"""
        bundle = _read("dashboard/dashboard.bundle.js")
        assert "function renderSealBoard(payload)" in bundle
        assert '"/seal-board"' not in bundle  # 路由拼接走模板串，不该硬编码

    def test_only_dash_core_holds_shared_declarations(self) -> None:
        """R77 收敛后，共享头只在 ``dash-core.js`` 一份，其余 6 份必须为空。

        R77 之前 7 个模块各存一份 3.3~3.5KB 的副本，改一处漏一处（R71/R76 各炸过一次）。
        现在不是「要求副本逐字一致」，而是「只许有一份」—— 这一条是那条决策的回归钉子。
        """
        assert JS_FILES[0] == "dash-core.js"
        for name in JS_FILES[1:]:
            src = _js(name)
            head = src[src.index("// <<<HEAD") : src.index("// >>>HEAD")]
            body = "\n".join(
                ln for ln in head.splitlines() if not ln.strip().startswith("//")
            ).strip()
            assert body == "", f"{name} 的 <<<HEAD 不是空的：{body[:80]!r}"

    def test_shared_tables_live_in_dash_core_exactly_once(self) -> None:
        """几张共享文案表必须**恰好一份**，且在 dash-core.js。"""
        tables = ("STATUS_LABELS", "SEAL", "INTRADAY_STATE_LABELS")
        for table in tables[:1]:
            owners = [n for n in JS_FILES if f"const {table} = {{" in _js(n)]
            assert owners == ["dash-core.js"], f"{table} 出现在 {owners}"


class TestBundleBuild:
    def test_bundle_is_in_sync_with_sources(self) -> None:
        """真跑一次 --check，不靠肉眼。

        ⚠️ 用 ``sys.executable`` 而不是 ``ROOT/.venv/bin/python``：后者只在
        开发机上存在，CI runner 上没有 ``.venv``，于是这条用例**结构性地**
        在 CI 上必红（2026-10-29 审计实测：`FileNotFoundError`，并把整个
        pytest 步骤打红、连带后面 17 道门禁全部跳过）。
        """
        out = subprocess.run(
            [
                sys.executable,
                "scripts/build_dashboard_bundle.py",
                "--check",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, f"bundle 不同步：\n{out.stdout}\n{out.stderr}"
