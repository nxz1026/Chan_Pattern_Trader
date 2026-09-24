"""R16-5 四画布前端契约：离线可用、脚本加载顺序、vendor 资产完整性。

这些是**源码级**断言，用真实浏览器跑的是 ``/home/ubuntu/work/cpt-audit/audit_R16.js``。
放在 pytest 里的理由是：CI 不装浏览器，但"页面不许出现任何 CDN 外链"这条验收
必须在每次提交时都有人守。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"
VENDOR = DASHBOARD / "vendor"

#: index.html 里 ``src=`` / ``href=`` 指向的**绝对 URL**（CDN）—— 必须为 0。
ABSOLUTE_ASSET_RE = re.compile(r"""\b(?:src|href)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE)

#: vendor 资产的 sha256 白名单（改版本必须同步改这里 + vendor/README.md）。
VENDOR_SHA256 = {
    "lightweight-charts.standalone.production.js": (
        "46fc69534ec098f095bbcd1d9a26d693d39a8b9eeff7343536765b3dd28c2bdf"
    ),
    "plotly-finance.min.js": ("55655d938260f1d0ffcc92e1aa5347a2e7fead4559eac23e76e08fcdbe495a51"),
    "bootstrap.min.css": ("7f1d37f0d90b6385354c2ac10e2bb91563c46bd7a266ed351222ebcac8496c2a"),
    "bootstrap.bundle.min.js": ("aa53d582f97eb594c2a5cc5824574707f9ba9837bce3046bfa5f3556860f4e04"),
    "bootstrap-icons.css": ("b9e2ee3ee86f447aebb15c14fe952200ce9afcde0e6b8b693bdc0907ea444b42"),
    "fonts/bootstrap-icons.woff2": (
        "ae167342f8ad5aad834e774ddc99528b72ac9171a684f23ed79d83ea176ca04e"
    ),
}

#: 画布模块的加载顺序（defer 按文档顺序执行，顺序错了注册表就是空的）。
EXPECTED_SCRIPT_ORDER = [
    "./canvas_registry.js",
    "./vendor/lightweight-charts.standalone.production.js",
    "./vendor/plotly-finance.min.js",
    "./canvas_b.js",
    "./canvas_c.js",
    "./canvas_d.js",
    "./dashboard.js",
]


def _index_html() -> str:
    return (DASHBOARD / "index.html").read_text(encoding="utf-8")


def _script_sources(html: str) -> list[str]:
    return re.findall(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", html, re.IGNORECASE)


def test_index_html_has_zero_cdn_references() -> None:
    html = _index_html()
    offenders = ABSOLUTE_ASSET_RE.findall(html)
    assert not offenders, f"index.html 里出现绝对 URL 资产引用（离线验收要求 0 个）：{offenders}"


def test_vendor_assets_are_local_and_hash_pinned() -> None:
    for name, expected in VENDOR_SHA256.items():
        path = VENDOR / name
        assert path.is_file(), f"缺少 vendor 资产：{name}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected, f"{name} 的 sha256 与锁定值不符：{digest}"


def test_vendor_readme_documents_provenance() -> None:
    text = (VENDOR / "README.md").read_text(encoding="utf-8")
    for token in ("lightweight-charts", "plotly", "bootstrap", "sha256", "Apache-2.0", "MIT"):
        assert token in text, f"vendor/README.md 缺少 {token} 说明"


def test_canvas_scripts_load_in_registration_order() -> None:
    sources = _script_sources(_index_html())
    for name in EXPECTED_SCRIPT_ORDER:
        assert name in sources, f"index.html 未加载 {name}"
    positions = [sources.index(name) for name in EXPECTED_SCRIPT_ORDER]
    assert positions == sorted(positions), f"画布脚本顺序错误：{sources}"
    # dashboard.js 必须最后（boot() 要读到完整注册表）
    assert sources[-1] == "./dashboard.js"


def test_canvas_switch_container_present() -> None:
    html = _index_html()
    assert 'data-testid="canvas-switch"' in html


def test_registry_defines_contract() -> None:
    source = (DASHBOARD / "canvas_registry.js").read_text(encoding="utf-8")
    assert "window.CPT_CANVASES" in source
    for method in ("register(", "get(", "has(", "ids(", "list("):
        assert method in source, f"画布注册表缺少 {method}"


def test_canvas_modules_register_their_ids() -> None:
    for canvas_id, filename in (("B", "canvas_b.js"), ("C", "canvas_c.js"), ("D", "canvas_d.js")):
        source = (DASHBOARD / filename).read_text(encoding="utf-8")
        assert f'window.CPT_CANVASES.register("{canvas_id}"' in source, (
            f"{filename} 未注册画布 {canvas_id}"
        )
        assert "function render(view)" in source, f"{filename} 未实现 draw 契约"
        # 每个画布都必须先清空区域（含别的画布可能留下的区域），否则会串台
        assert "clearRegion(" in source, f"{filename} 未清理区域"


def test_canvas_modules_use_vendored_libraries_not_cdn() -> None:
    for filename in ("canvas_b.js", "canvas_c.js", "canvas_d.js"):
        source = (DASHBOARD / filename).read_text(encoding="utf-8")
        assert not ABSOLUTE_ASSET_RE.search(source), f"{filename} 里出现 CDN 外链"
    assert "vendor/lightweight-charts.standalone.production.js" in (
        DASHBOARD / "canvas_b.js"
    ).read_text(encoding="utf-8")
    assert "vendor/plotly-finance.min.js" in (DASHBOARD / "canvas_c.js").read_text(encoding="utf-8")
    # 画布 D 的 iframe 资产由 vendorBase() 拼出来，必须指向 vendor 目录
    assert 'replace(/dashboard\\.css.*$/, "vendor/")' in (DASHBOARD / "canvas_d.js").read_text(
        encoding="utf-8"
    )


def test_dashboard_dispatches_through_registry() -> None:
    source = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
    for token in (
        "function drawChartA(view)",
        "function drawChart()",
        "function buildCanvasView()",
        "registry.get(state.canvas)",
        "window.CPT_CANVASES",
        "node.dataset.canvasCounts",
        'url.searchParams.set("canvas", wanted)',
        'new CustomEvent("cpt:canvas-changed"',
    ):
        assert token in source, f"dashboard.js 缺少画布分发关键片段：{token}"


def test_snapshot_query_param_overrides_attribute() -> None:
    """``?snapshot=`` 必须优先于 ``data-snapshot-url``（R16-5 修）。

    原顺序让 ``?snapshot=`` 完全失效 —— 属性在 index.html 里恒非空，离屏审计
    无法把页面指到一份固定快照上。
    """
    source = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
    # R17-3 起显式分了两步：先取参数（A 股模式判断也要用它），再让参数优先。
    # 断言语义不变，同时钉住"参数必须先于属性被求值"。
    explicit = 'const explicitSnapshot = params.get("snapshot");'
    resolved = "const snapshotUrl = explicitSnapshot || root.dataset.snapshotUrl;"
    assert explicit in source
    assert resolved in source
    assert source.index(explicit) < source.index(resolved)


def test_window_filtering_is_applied_to_all_structure_kinds() -> None:
    """四个画布共用同一份窗口内结构：过滤必须发生在 buildCanvasView 里。"""
    source = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
    assert "const inWindow = (item) => {" in source
    for key in ("fractals", "bis", "zhongshus", "trend_types"):
        assert f"{key}: rawOverlays.{key}.filter(inWindow)" in source, (
            f"buildCanvasView 未过滤 {key}"
        )


def test_canvas_css_defines_host_and_switch() -> None:
    css = (DASHBOARD / "dashboard.css").read_text(encoding="utf-8")
    for selector in (".canvas-switch", ".cpt-canvas-host", ".cpt-canvas-d-frame"):
        assert selector in css, f"dashboard.css 缺少 {selector}"
