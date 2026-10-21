"""``dashboard.bundle.js`` 的构建不变量。

R61 的教训：线上只加载 bundle，源文件（``dash-chrome.js`` 等）**不是**运行时输入。
于是存在两类只有 bundle 才看得见的失效：

1. 改了源文件**忘了重建** bundle —— 测试读源文件全绿，线上还是旧代码；
2. 把共享声明加进非权威模块的 ``<<<HEAD`` —— bundle 只拼 ``ORDER[0]``
   (``dash-core.js``) 的头，那份声明会被**静默丢弃**。R77 起这条从「能抓到
   才失败」升级为「非空即失败」：其余 6 个模块的头必须是空的。

这里把两条都钉成断言；渲染层的端到端断言在
``tests/test_dashboard_ashare_r61_render.py``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts import build_dashboard_bundle as bdb

from tests.conftest import DASHBOARD_JS_ORDER

ROOT = Path(__file__).parents[1]


def test_committed_bundle_is_in_sync_with_sources() -> None:
    """提交的 bundle 必须与从源文件重建的结果**逐字节**一致。

    这条挂掉时的正确动作是 ``python scripts/build_dashboard_bundle.py``，
    而不是改这个断言。
    """
    committed = (ROOT / "dashboard" / "dashboard.bundle.js").read_text(encoding="utf-8")
    assert committed == bdb.build(), (
        "dashboard.bundle.js 与源文件不一致 —— 改了 dash-*.js 之后忘了重建 bundle。"
        "跑 `python scripts/build_dashboard_bundle.py` 重新生成。"
    )


def test_builder_sources_match_test_harness_order() -> None:
    """``ORDER``（进 bundle 的 7 个模块）必须是 ``DASHBOARD_JS_ORDER`` 的**保序子序列**。

    两者漂移时，``dashboard_js()`` 系列的契约测试读的就是**另一套顺序/文件**，
    绿灯不再代表线上代码没问题。（``ORDER`` 只含 dash-* 七个模块；``index.html``
    另外单独加载 url_safety / cpt_job / canvas_* / market_a_share / inspection_panel。）
    """
    harness = list(DASHBOARD_JS_ORDER)
    indices = []
    for name in bdb.ORDER:
        assert name in harness, f"{name} 进了 bundle，却不在 DASHBOARD_JS_ORDER 里"
        indices.append(harness.index(name))
    assert indices == sorted(indices), (
        f"ORDER 与 DASHBOARD_JS_ORDER 相对顺序不一致：{bdb.ORDER} vs "
        f"{[harness[i] for i in sorted(indices)]}"
    )


def test_authoritative_head_is_dash_core() -> None:
    """bundle 的权威头只能来自 ``ORDER[0]``；``check_quarantined_head`` 依赖它。"""
    assert bdb.ORDER[0] == "dash-core.js"


def test_non_authoritative_head_with_a_declaration_fails() -> None:
    """非权威头里有**代码** → 直接失败，不许静默丢弃。"""
    with pytest.raises(SystemExit) as excinfo:
        bdb.check_quarantined_head("dash-chrome.js", "  const SOURCE_LABELS = { sina: '新浪' };")
    message = str(excinfo.value)
    assert "SOURCE_LABELS" in message
    assert "dash-chrome.js" in message
    assert bdb.ORDER[0] in message


def test_non_authoritative_head_with_only_comments_passes() -> None:
    """空哨兵段只放指路注释是合法的 —— 判空要剥注释，不能看字符串非空。"""
    bdb.check_quarantined_head(
        "dash-chrome.js",
        "// 共享头在 dash-core.js\n// 别加在这里\n/* 块注释也不算 */\n",
    )
    bdb.check_quarantined_head("dash-structure.js", "")


def test_non_authoritative_head_with_a_function_fails() -> None:
    """函数声明同样被丢弃 —— 守卫不能只认 ``const``。"""
    with pytest.raises(SystemExit):
        bdb.check_quarantined_head("dash-ops.js", "function helper() {}")


def test_every_non_authoritative_head_is_empty_in_the_real_repo() -> None:
    """真实仓库里 6 个模块的 HEAD 段必须只剩注释（R77 收敛后的稳态）。

    这条比单元断言更难绕过：它读的是**磁盘上的源文件**。
    """
    for name in bdb.ORDER[1:]:
        head, _, _ = bdb._parts(name)
        bare = bdb._COMMENT_RE.sub("", head).strip()
        assert not bare, f"{name} 的 <<<HEAD 又有代码了：{bare[:100]!r} —— 加进 dash-core.js"


def test_no_shared_table_is_duplicated_across_modules() -> None:
    """R77 的核心不变量：共享文案表在全仓只允许出现**一次**。

    R71 改 ``SIGNAL_TYPE_LABELS`` 只改了 dash-core，6 份副本没跟 ⇒ 页面一直
    显示「一买信号」。根因就是「同一个 ``const`` 有好几份绑定」。
    """
    tables = (
        "STATUS_LABELS",
        "STATUS_EDUCATION",
        "DIVERGENCE_LABELS",
        "SIGNAL_TYPE_LABELS",
        "RANGE_REASON_LABELS",
        "TREND_KIND_LABELS",
        "PROBE_STATUS_LABELS",
        "SOURCE_ROLE_LABELS",
        "SOURCE_LABELS",
    )
    owners: dict[str, list[str]] = {t: [] for t in tables}
    for name in bdb.ORDER:
        src = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        for table in tables:
            if re.search(rf"^\s*const {table} = ", src, re.M):
                owners[table].append(name)
    dupes = {t: v for t, v in owners.items() if len(v) != 1}
    assert not dupes, f"这些共享表不是恰好一份：{dupes}"
    assert all(v == ["dash-core.js"] for v in owners.values()), owners
