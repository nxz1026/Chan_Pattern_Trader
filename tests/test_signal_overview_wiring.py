"""R73 方案 A1 的跨层接线契约。

三层各写各的，接口一旦漂移就会「后端有数、前端不显示」而没人发现。
这里钉死四件事：

1. 路由存在且只认整数 ``days``（``_MAX_DAYS`` 封顶）；
2. 后端 ``signal_overview`` 返回的键，前端 ``renderSignalOverview`` 真的读了；
3. 前端**不**把 ``age_days`` 说成「卡了 N 天」（口径红线）；
4. 「信号统计」（数事件）与「信号全景」（数票）的标签各写各的，不串。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "cpt/web/app.py"
MODULE = ROOT / "cpt/application/signal_overview.py"
SIGNAL_JS = ROOT / "dashboard/dash-signal.js"
OPS_JS = ROOT / "dashboard/dash-ops.js"
STRUCTURE_JS = ROOT / "dashboard/dash-structure.js"
BUNDLE = ROOT / "dashboard/dashboard.bundle.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_object(source: str, name: str) -> str:
    """从 JS 源码里抠出 ``const NAME = { ... };`` 的块（够用即可，不做真解析）。"""
    match = re.search(rf"const {name} = \{{(.*?)\n  \}};", source, re.S)
    assert match, f"找不到 {name} 定义"
    return match.group(1)


def test_route_is_registered() -> None:
    """``/api/dashboard/signal-overview`` 必须在路由表里。"""
    assert '"/api/dashboard/signal-overview"' in _read(APP)


def test_route_reuses_the_days_validation() -> None:
    """新路由必须复用 ``_MAX_DAYS`` 封顶 —— 否则 ``?days=99999999`` 会扫全表。"""
    source = _read(APP)
    start = source.index('"/api/dashboard/signal-overview"')
    block = source[start : start + 900]
    assert "_MAX_DAYS" in block
    assert "invalid_days" in block


def test_payload_self_reports_basis() -> None:
    """口径自报：不写 basis 前端只能猜是事件数还是票数。"""
    source = _read(APP)
    assert '"basis": "latest_signal_event_per_code"' in source


def test_payload_degrades_instead_of_raising() -> None:
    """DB 抖动降级成 available=false，不 500。"""
    source = _read(APP)
    start = source.index("def _signal_overview_payload")
    block = source[start : start + 1800]
    assert "except Exception" in block
    assert '"available"] = False' in block
    assert "signal_history_unavailable" in block


def test_frontend_reads_every_key_the_backend_ships() -> None:
    """后端给的展示用键，前端都得真的读。

    只校验**展示必需**的几个：漏读任何一个都会让面板出现空白或「—」。
    """
    js = _read(SIGNAL_JS)
    for key in ("status_counts", "blind_spots", "stale_count", "stale_after_days", "rows"):
        # 三种合法读法：``obj.key`` / ``obj["key"]`` / 解构 ``const { key } = obj``
        read_forms = (
            rf"\.{key}\b",
            rf'\["{key}"\]',
            rf"\{{\s*{key}\s*\}}",
        )
        assert any(re.search(form, js) for form in read_forms), f"前端没读 {key}"


def test_frontend_consumes_age_is_lower_bound_caveat() -> None:
    """口径行必须说明「距最后跃迁只是下界」，否则用户会把 0 天读成「今天才开始」。"""
    js = _read(SIGNAL_JS)
    assert "下界" in js
    assert "距最后跃迁" in js


def test_frontend_never_claims_stuck_days() -> None:
    """口径红线：不许把 ``age_days`` **断言**成「卡了 N 天」。

    允许两种情况出现这四个字：源码注释（解释为什么不这么写）、
    以及明否定的文案（「它不等于卡了这么久」）。禁的是当结论用：
    形如「卡了 N 天」直接拼在展示串里。
    """
    js = _read(SIGNAL_JS)
    # 断言式用法：紧跟数字或模板变量的「卡了 N 天 / 卡了${x}天」
    assert not re.search(r"卡了\s*(\$\{|\d)", js), "把 age_days 断言成了「卡了 N 天」"
    # 展示串里不许以「卡了」起头（那是结论口吻）
    assert not re.search(r"[`\"']卡了", js), "展示串以「卡了」起头"


def test_standings_and_events_are_labelled_differently() -> None:
    """全景区块标题不能和「信号统计」撞名，两者统计单元不同。"""
    js = _read(SIGNAL_JS)
    assert "信号全景" in js
    assert "只取最新一条" in js
    assert "事件条数" in js


def test_loader_is_wired_into_refresh() -> None:
    """``loadSignalOverview`` 必须被调用，否则区块永远停在「尚未拉取」。"""
    ops = _read(OPS_JS)
    assert "async function loadSignalOverview()" in ops
    assert "loadSignalOverview();" in ops


def test_remote_ops_holds_overview_state() -> None:
    """状态槽位得先在 ``remoteOps`` 里声明，否则首次拉取前读的是 undefined。"""
    assert "signalOverview: null" in _read(STRUCTURE_JS)


def test_signal_stats_and_overview_hit_different_endpoints() -> None:
    """两个 loader 打各自的路由，别拼错成同一个。"""
    ops = _read(OPS_JS)
    assert "/signal-stats?" in ops
    assert "/signal-overview?" in ops


def test_bundle_carries_the_overview_renderer() -> None:
    """bundle 与源文件同步（构建脚本的 --check 之外再钉一次）。"""
    bundle = _read(BUNDLE)
    assert "renderSignalOverview" in bundle
    assert "signal-overview-basis" in bundle


def test_backend_module_declares_lower_bound() -> None:
    """后端模块自身也把 ``age_is_lower_bound`` 写死在返回值里，不靠前端自觉。"""
    module = _read(MODULE)
    assert '"age_is_lower_bound": True' in module


def test_testid_names_are_present_in_source() -> None:
    """前端 testid 命名空间统一以 ``signal-overview-`` 开头，便于将来整体搬迁。"""
    js = _read(SIGNAL_JS)
    for testid in (
        "signal-overview",
        "signal-overview-basis",
        "signal-overview-total",
        "signal-overview-stale",
        "signal-overview-rows",
        "signal-overview-row",
    ):
        # 两种写法都合法：
        #   ① 直接赋值 ``el.dataset.testid = "..."``
        #   ② 作为 ``row(key, value, testid)`` 的第三参传入，由行构造器统一赋
        direct = re.search(rf'dataset\.testid\s*=\s*"{testid}"', js)
        via_arg = re.search(rf'"{testid}"', js)
        assert direct or via_arg, f"缺 {testid}"


# --- R78：全市场分布（走完几步 / 卡在哪步为什么）的接线 ---------------------


def test_bundle_carries_the_step_distribution_renderer() -> None:
    """分布渲染器与步号标签都进了 bundle（构建脚本 --check 之外再钉一次）。"""
    bundle = _read(BUNDLE)
    assert "buildStepDistribution" in bundle
    assert "STEP_INDEX_LABELS" in bundle


def test_step_index_labels_cover_every_step_the_backend_can_emit() -> None:
    """后端能吐的每个步号键，前端都得有人话标签，否则页面会显示裸数字。

    键从后端源码里读，不在这里抄一份 —— 后端加一档、前端忘了加，测试就红。
    """
    module = _read(MODULE)
    keys = {"0", "1", "2", "3", "unknown"}
    # 后端源码里 ``_STEP_INDEX_FULL = 3`` 决定了真步号上界。
    match = re.search(r"_STEP_INDEX_FULL\s*=\s*(\d+)", module)
    assert match, "找不到 _STEP_INDEX_FULL 定义"
    assert int(match.group(1)) == 3

    js = _read(SIGNAL_JS)
    block = _extract_object(js, "STEP_INDEX_LABELS")
    for key in keys:
        assert key in block, f"STEP_INDEX_LABELS 缺键 {key}"


def test_frontend_never_treats_unknown_as_a_step_number() -> None:
    """``unknown`` 是「不知道走到哪」，不是第 4 步 —— 标签里必须说清这点。"""
    js = _read(SIGNAL_JS)
    block = _extract_object(js, "STEP_INDEX_LABELS")
    unknown_line = next(line for line in block.splitlines() if "unknown:" in line)
    assert "不确定" in unknown_line


def test_no_reason_hint_is_rendered_and_never_rounded_into_a_step() -> None:
    """没记原因的票必须单独成句说明，不能被并进某个步号凑成一个好看的分布。"""
    js = _read(SIGNAL_JS)
    assert 'p.dataset.testid = "signal-overview-no-reason"' in js
    assert "no_reason_but_blocked" in js
    # 不允许出现「把 unknown 加到 2 上」这类凑数写法
    assert "counts.unknown +" not in js


def test_distribution_block_is_appended_to_the_overview_section() -> None:
    """分布块确实被挂进全景面板，不是定义了没人调用。"""
    js = _read(SIGNAL_JS)
    assert "section.appendChild(buildStepDistribution(ov));" in js


def test_backend_ships_step_distribution_and_titles() -> None:
    """分布与步号标题都由后端下发，前端不硬编码中文步名。"""
    module = _read(MODULE)
    assert '"step_distribution": _step_distribution(rows)' in module
    assert '"step_titles": dict(STEP_TITLES)' in module
