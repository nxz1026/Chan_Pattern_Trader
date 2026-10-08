"""CPT 追踪页「讲人话」前端契约（R52）。

钉住的是**接入点**，不是渲染细节 —— 这三个点任意一个写错都会让功能整体失效，
而页面上只表现为一句「等待超时（60s）」，从 UI 上根本看不出是哪一层错的：

1. 轮询 ``subject_id`` 必须是 ``track:{user}:{code}``。后端
   :func:`cpt.storage.llm_call_store.recent_calls` 是 ``WHERE subject_id = %s``
   **精确匹配**；只传前缀 ``track:{user}:`` 永远查出 ``count=0``（R52 实测）。
2. 成功终态是 ``ok``（``cpt/storage/llm_call_store.py`` 的 ``STATUS_OK``），
   不是 ``succeeded``。写错就永远判失败。
3. 先查再睡：``duplicate``（同时间窗已生成）的 ``call_id`` 已经是终态，
   第一次查询就该命中，不该白等一个轮询周期。
"""

from __future__ import annotations

from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
TRACK_JS = DASHBOARD / "dash-track.js"
TRACK_HTML = DASHBOARD / "track.html"


@pytest.fixture(scope="module")
def track_js() -> str:
    return TRACK_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def track_html() -> str:
    return TRACK_HTML.read_text(encoding="utf-8")


def _fn_body(source: str, name: str) -> str:
    """粗切一个 ``function <name>(`` 的函数体到下一个顶层 ``\n  }``。

    够用即可：这里断的是「谁在谁前面」，不需要真解析 JS。
    """
    start = source.index(f"function {name}(")
    end = source.index("\n  }\n", start)
    return source[start:end]


def test_poll_subject_id_is_exact_not_prefix(track_js: str) -> None:
    """⚠️ R52 的核心硬伤：#182 传的是前缀，后端精确匹配 → 永远 count=0。"""
    body = _fn_body(track_js, "apiLlmStatus")
    assert "track:${user}:${code}" in body
    # 前缀写法（模板串后面直接跟反引号 / 冒号收尾）必须已经不存在
    assert "track:${user}:`" not in body


def _code_only(source: str) -> str:
    """去掉整行注释。

    断言「旧写法已不存在」时必须先剥注释 —— 修复说明里会**引用**那句旧代码，
    否则测试会被自己的注释绊倒（R52 首次运行就踩了）。
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


def test_success_status_is_ok(track_js: str) -> None:
    """DB 终态是 ok；只认 succeeded 会导致「等待超时」。"""
    assert 'const OK_STATUSES = new Set(["ok", "succeeded"]);' in track_js
    # 直接与 "succeeded" 比大小的旧写法必须消失
    assert '=== "succeeded"' not in _code_only(track_js)


def test_poll_checks_before_sleeping(track_js: str) -> None:
    """duplicate 路径要靠「先查」才能在第一个周期拿到终态。"""
    body = _fn_body(track_js, "pollSpeak")
    assert body.index("apiLlmStatus") < body.index("setTimeout")


def test_poll_timeout_is_60s(track_js: str) -> None:
    assert "POLL_TIMEOUT_MS = 60000" in track_js
    # 界面文案要与常量一致，别留 30s 的旧提示
    assert "等待超时（30s）" not in track_js


def test_speak_again_polls_duplicate_instead_of_blind_refresh(track_js: str) -> None:
    """duplicate 分支必须走 poll（拿到结果再刷新），不能盲刷 advice。"""
    body = _fn_body(track_js, "handleAction")
    assert "pollSpeak(callId, code)" in body


def test_track_html_cache_busts_the_script(track_html: str) -> None:
    """静态根是**独立部署副本**，改了 js 不同步版本号浏览器会一直吃旧缓存。"""
    tags = [line for line in track_html.splitlines() if "dash-track.js" in line]
    assert len(tags) == 1
    assert "?v=" in tags[0]
    assert "llm-ok-fix" not in tags[0]
