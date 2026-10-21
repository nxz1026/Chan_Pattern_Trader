"""R76：买点语气必须在**所有出口**同时消失 —— 跨层接线测试。

## 为什么单测不够

R71 已经把 ``dashboard/dash-core.js`` 的 ``STATUS_LABELS`` 改成「位置已到（不是买点）」，
页面上也确实那样显示了。但 **R76 盘点时发现推荐卡、追踪页、飞书推送、信号雷达
仍然是旧的买点语气** —— 因为它们读的根本不是 ``dash-core.js``：

======================  ==========================================
出口                    真正读的地方
======================  ==========================================
看板推荐卡              ``cpt/application/recommendation.py`` 的 ``ACTIONS``
追踪页 + 历史表         同一份 ``ACTIONS``（经 ``track_api``）
飞书跃迁推送            ``cpt/application/signal_notify.py::_points_lines``
信号雷达                ``cpt/web/app.py`` 的 ``source_label``
LLM 旁白                ``cpt/llm/prompts.py`` 的禁用词表
======================  ==========================================

只改前端等于把问题藏起来。这个文件钉的是**每一层各说了一遍**这件事，
所以任何一处回退都会红。

## 这里**不**检查什么

``cpt/web/trade_api.py`` 的 ``BUY``/``SELL`` 与 ``cpt/adapters/strategy_signal.py``
的 ``ELIGIBLE_ACTIONS`` 是**机器间契约**（交易机读的 JSON），不是用户文案，
留在英文是对的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard"

#: 用户可见文案里出现就算犯规的词（与 ``test_recommendation._BUY_WORDS`` 同源）。
FORBIDDEN = (
    "买入结构",
    "卖出结构",
    "买入信号",
    "卖出信号",
    "参考买点",
    "参考卖点",
    "建议动作",
    # 表头「动作」也是祈使语气 —— 那一列讲的是结构位置，不是"该做什么"。
    'text: "动作"',
    "BUY / SELL",
)

#: 合法的**否定式**：明确否认自己是买点，恰恰要保留。
NEGATIONS = (
    "不是买点",
    "不是卖点",
    "不是买指令",
    "不是卖出指令",
    "不是买卖指令",
    "不是买入信号",
    "不是卖出信号",
)


def _strip_negations(text: str) -> str:
    for ok in NEGATIONS:
        text = text.replace(ok, "")
    return text


def _js_sources() -> list[Path]:
    """看板所有脚本（排除构建产物 —— bundle 是拼接出来的，改源文件才有效）。"""
    return sorted(p for p in DASH.glob("dash-*.js"))


# ── 1. 前端源文件里不许残留买点词 ────────────────────────────


@pytest.mark.parametrize("path", _js_sources(), ids=lambda p: p.name)
def test_no_buy_language_in_dashboard_sources(path: Path) -> None:
    """``dashboard/dash-*.js`` 的用户文案里不许出现买点词。

    注释里出现没关系（本文件就是在解释为什么不用它），所以先剥掉注释。
    """
    src = path.read_text(encoding="utf-8")
    # 剥 JS 块注释与行注释 —— 只检查真正会渲染出去的字符串。
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    hits = [w for w in FORBIDDEN if w in _strip_negations(src)]
    assert not hits, f"{path.name} 里还有买点语气：{hits}"


def test_dashboard_html_has_no_buy_language() -> None:
    """``index.html`` 的面板标题 / 术语表 / 页脚同样不许有。

    ⚠️ 术语表里 **允许** 出现「一买 / 一卖」—— 那是术语条目本身，
    用户点开才知道历史上叫什么。这里只禁**祈使动作**类词。
    """
    html = (DASH / "index.html").read_text(encoding="utf-8")
    hits = [w for w in FORBIDDEN if w in _strip_negations(html)]
    assert not hits, f"index.html 里还有买点语气：{hits}"


def test_footer_is_fact_not_disclaimer_boilerplate() -> None:
    """用户明说过「自用项目，不用说仅供参考的废话」。

    页脚要讲**这个页面是什么**，不是「不构成投资建议 / 市场有风险」那套。
    """
    html = (DASH / "index.html").read_text(encoding="utf-8")
    assert "不构成投资建议" not in html
    assert "投资须谨慎" not in html
    assert "不预测涨跌" in html


# ── 2. 前端状态映射表与后端逐条一致 ──────────────────────────


def test_track_page_has_status_labels() -> None:
    """追踪页历史上**没有**状态映射，后端 raw 码值直接渲染出去。

    ``dash-track.js`` 刻意不依赖 ``window.CPTDashboard``（它有自己的 API 命名空间），
    所以只能是副本 —— 副本就必须与权威表语义一致，这条测试就是那个订书钉。
    """
    track = (DASH / "dash-track.js").read_text(encoding="utf-8")
    assert "const STATUS_LABELS" in track
    for code in ("structure_ready", "alert", "candidate", "confirmed", "invalidated"):
        assert f"    {code}:" in track, f"dash-track.js 缺少 {code} 的中文映射"


def test_track_page_status_labels_match_backend() -> None:
    """前端副本与 ``cpt.application.recommendation.STATUS_TEXT`` 不许漂移。"""
    from cpt.application.recommendation import STATUS_TEXT

    track = (DASH / "dash-track.js").read_text(encoding="utf-8")
    for code, text in STATUS_TEXT.items():
        assert f'{code}: "{text}"' in track, (
            f"dash-track.js 的 {code} 与后端 STATUS_TEXT 不一致（应为「{text}」）"
        )


def test_track_api_exposes_both_raw_code_and_human_text() -> None:
    """``explain_algorithm`` 里 ``status`` 是人话、``status_code`` 才是码值。

    两个都要有：前端状态色块 / 轮询去重靠码值，
    用户读的是人话。只留一个都会出事。
    """
    src = (REPO / "cpt/web/track_api.py").read_text(encoding="utf-8")
    assert '"status_code": rec.get("status")' in src
    assert '"status": status_text(' in src


# ── 3. 推送与 LLM 出口 ───────────────────────────────────────


def test_push_body_uses_position_wording() -> None:
    """飞书推送正文：``信号位置`` 而不是 ``动作``，区间上下沿而不是买卖点。"""
    from cpt.application import signal_notify

    lines = signal_notify._points_lines(
        {
            "action": "buy",
            "status": "structure_ready",
            "buy": {"reference": {"price_level": 10.0}},
            "sell": {"reference": {"price_level": 12.0}},
        }
    )
    body = " ".join(lines)
    assert "结构位置:" in body
    assert "结构区间下沿" in body and "结构区间上沿" in body
    for word in ("买点", "卖点", "动作:"):
        assert word not in body


def test_llm_prompt_forbids_imperative_verbs() -> None:
    """LLM 旁白是唯一由模型自由发挥的出口，禁用词表必须显式列出来。"""
    from cpt.llm import prompts

    src = (REPO / "cpt/llm/prompts.py").read_text(encoding="utf-8")
    assert "买入" in src and "卖出" in src, "提示词里没有禁用「买入/卖出」"
    assert prompts._SUMMARY_SYSTEM  # 能被 import 且非空
    for word in ("买入", "卖出", "加仓"):
        assert word in prompts._SUMMARY_SYSTEM, f"旁白提示词没禁 {word!r}"


# ── 4. 反向守卫：码值不许被顺手改掉 ──────────────────────────


def test_action_codes_are_unchanged() -> None:
    """**只换文案，不换码值。**

    ``action`` 进了 ``cpt_track_snapshot`` 的 payload、``/api/trade/*`` 的决策批次、
    ``a_share_routes._RECOMMENDATION_KEYS``，前端着色也读 ``data-action``。
    改码值会动历史数据与下游契约 —— 这条测试把它钉死。
    """
    from cpt.application.recommendation import ACTION_BUY, ACTION_HOLD, ACTION_SELL, ACTION_WATCH

    assert (ACTION_BUY, ACTION_SELL, ACTION_WATCH, ACTION_HOLD) == (
        "buy",
        "sell",
        "watch",
        "hold",
    )


def test_machine_contracts_stay_english() -> None:
    """交易机 / 外部策略接口里的 ``BUY``/``SELL`` 是契约，**不许**被本轮改名波及。"""
    trade = (REPO / "cpt/web/trade_api.py").read_text(encoding="utf-8")
    assert '"first_buy": ("BUY", "OPEN_POS")' in trade
    assert '"first_sell": ("SELL", "CLOSE_ALL")' in trade
