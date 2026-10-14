"""``dashboard.bundle.js`` 的构建不变量。

R61 的教训：线上只加载 bundle，源文件（``dash-chrome.js`` 等）**不是**运行时输入。
于是存在两类只有 bundle 才看得见的失效：

1. 改了源文件**忘了重建** bundle —— 测试读源文件全绿，线上还是旧代码；
2. 把共享声明只加进非权威模块的 ``<<<HEAD`` —— bundle 只拼 ``ORDER[0]``
   (``dash-core.js``) 的头，那份声明被**静默丢弃**，运行时 ``ReferenceError``
   被 `catch` 吞掉，页面无报错但后续 DOM 写入全部不执行。

这里把两条都钉成断言；渲染层的端到端断言在
``tests/test_dashboard_ashare_r61_render.py``。
"""

from __future__ import annotations

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
    """bundle 的权威头只能来自 ``ORDER[0]``；``check_head_declarations`` 依赖它。"""
    assert bdb.ORDER[0] == "dash-core.js"


def test_head_declaration_missing_from_authoritative_head_fails() -> None:
    """非权威头独有声明 → 直接失败，不许静默丢弃。"""
    with pytest.raises(SystemExit) as excinfo:
        bdb.check_head_declarations(
            "dash-chrome.js",
            "const q = 1;\nconst SOURCE_LABELS = { sina: '新浪' };",
            "const q = 1;\n",
        )
    message = str(excinfo.value)
    assert "SOURCE_LABELS" in message
    assert "dash-chrome.js" in message
    assert bdb.ORDER[0] in message


def test_head_declaration_subset_of_authoritative_head_passes() -> None:
    """副本落后（少声明）是允许的 —— 只在**多出**声明时才失败。"""
    bdb.check_head_declarations(
        "dash-chrome.js",
        "const q = 1;",
        "const q = 1;\nconst num = (v) => v;\nfunction setText() {}",
    )


def test_identical_heads_pass() -> None:
    bdb.check_head_declarations("dash-structure.js", "const q = 1;\n", "const q = 1;\n")
