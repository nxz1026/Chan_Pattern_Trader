"""R75 · 前端「说人话」：术语表讲计算机、正文不露英文键名、死面板不占位。

## 为什么需要这组测试

用户原话（m02814）：*「这个缠论我看不懂，我学计算机的，你要把他做成"讲人话"
我才看得懂」*。这不是审美问题，是**可用性缺陷**：页面上有

- ``<dt>kind</dt>`` ``<dt>level</dt>`` ``<dt>revision</dt>`` —— 英文键名直接当标签；
- ``alert 未收盘`` ``confirmed 已确认`` —— 英文状态混中文；
- 术语表用缠论解释缠论（「三段重叠笔构成的价格密集区」）—— 对不懂缠论的人等于没解释；
- 「走势类型」面板在生产上**永远是空的**（``a_share_snapshot.py`` 传
  ``trend_types=()``），占着位置还挂着 raw ``kind``。

这些都不是逻辑 bug，所以任何单元测试都不会红 —— 必须直接断言页面文案本身。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DASH = _ROOT / "dashboard"
_HTML = (_DASH / "index.html").read_text(encoding="utf-8")


def _dash_scripts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(_DASH.glob("dash-*.js"))}


# ── 1. 英文键名不许当标签 ──────────────────────────────────────────
def test_no_english_key_is_used_as_a_label() -> None:
    """``<dt>kind</dt>`` 这类直接把字段名当标签的写法一律不许出现。

    ``id`` 这种在中文语境里也是通用缩写的除外 —— 但本项目当前一处都没有。
    """
    offenders = re.findall(r"<dt>([a-zA-Z_][\w]*)</dt>", _HTML)
    assert offenders == [], f"这些标签还是英文键名，用户看不懂：{offenders}"


# ── 2. 术语表要用计算机语言解释 ────────────────────────────────────
def test_glossary_explains_terms_in_programming_language() -> None:
    """每个术语的解释里都要出现一个**程序员的锚点**。

    这不是为了好看：没有锚点就退化成「用缠论解释缠论」，用户读三遍还是不懂。
    """
    glossary = _HTML[
        _HTML.index('data-testid="term-glossary"') : _HTML.index(
            "</section>", _HTML.index('data-testid="term-glossary"')
        )
    ]
    terms = re.findall(r'<dt data-term="([^"]+)">', glossary)
    assert terms, "术语表里一个术语都没找到"
    # 每条术语的说明里必须至少有一个编程概念的词
    anchors = (
        "时间序列",
        "极值",
        "local extremum",
        "折线图",
        "抽稀",
        "降采样",
        "求交集",
        "区间",
        "枚举",
        "tag",
        "状态机",
        "比较",
        "数据结构",
    )
    for term in terms:
        block = glossary[glossary.index(f'data-term="{term}"') :]
        block = block[: block.index("</div>")]
        assert any(a in block for a in anchors), (
            f"术语「{term}」的解释里没有任何编程锚点，仍是缠论黑话：{block[:160]}"
        )


def test_glossary_dd_removed_the_pure_chanlun_phrasing() -> None:
    """旧的纯缠论文案要彻底删掉，不能只是「在旁边加一句白话」。"""
    for banned in ("三段重叠笔构成的价格密集区", "缠论基本单位：顶分型"):
        assert banned not in _HTML, f"旧的黑话解释还在：{banned}"


# ── 3. 图例不留英文状态 ────────────────────────────────────────────
def test_legend_has_no_english_state_names() -> None:
    legend = re.search(r'data-testid="chart-legend".*?</ul>', _HTML, re.S)
    assert legend, "找不到图例"
    body = legend.group(0)
    for banned in (">alert ", ">confirmed ", ">candidate "):
        assert banned not in body, f"图例里还有 raw 状态名：{banned}"
    assert "未收盘" in body and "已确认" in body


# ── 4. 走势类型死面板：隐藏 + 说明原因 ─────────────────────────────
def test_trend_type_panel_is_hidden_with_a_reason() -> None:
    """生产上 ``trend_types`` 恒为空，所以这块要么隐藏并解释，要么就别显示。

    只隐藏不解释的话，用户会以为页面坏了 —— 所以要连「为什么没有」一起钉住。
    """
    m = re.search(r'<section data-testid="structure-trend-type"[^>]*>', _HTML)
    assert m, "找不到走势类型区块"
    assert "hidden" in m.group(0), "走势类型面板没有隐藏"

    note = re.search(r'data-testid="trend-type-unavailable"[^>]*>(.*?)</p>', _HTML, re.S)
    assert note, "隐藏了却没说明为什么，用户会以为页面坏了"
    text = note.group(1)
    assert "未接入" in text or "没有接入" in text or "还没有接入" in text
    assert "a_share_snapshot" in text, "要指回具体是哪条路径没接数据"


def test_trend_kind_labels_cover_every_domain_constant() -> None:
    """前端映射必须覆盖 ``cpt/domain/trend_type.py`` 里的全部 ``_KIND_*``。

    少一个的后果：将来接上数据时，界面上会突然冒出一个英文 raw 值，
    而且没人会想起来是这里漏了。
    """
    # 判据源码已迁至 emotion_cpt.structure ⇒ 跟随已安装包定位，别硬编码仓库路径
    import emotion_cpt.structure.trend_type as _trend_type_mod

    src = Path(_trend_type_mod.__file__).read_text(encoding="utf-8")
    kinds = set(re.findall(r'_KIND_[A-Z_]+:\s*Final\[str\]\s*=\s*"([^"]+)"', src))
    assert kinds, "从 trend_type.py 里没解析出 _KIND_* 常量，解析规则可能失效了"

    core = (_DASH / "dash-core.js").read_text(encoding="utf-8")
    labelled = set(re.findall(r"^\s{4}(\w+):\s*\"", core, re.M))
    missing = kinds - labelled
    assert not missing, f"TREND_KIND_LABELS 漏了这些形态：{sorted(missing)}"


# ── 5. 共享表只有一份（R77 收敛后） ────────────────────────────────
def test_trend_kind_labels_live_in_exactly_one_module() -> None:
    """``TREND_KIND_LABELS`` 只许有**一份**定义，在 ``dash-core.js`` 里。

    R77 之前这里钉的是「6 份副本逐字一致」—— 那是在**管理一个错误结构**：
    同一张表有 7 个绑定，渲染走哪个取决于谁先声明。R71 就是改了权威表、
    副本没跟，页面一直显示旧文案。

    R77 把副本删干净，判据从「6 份一致」翻成「只有 1 份」—— 结构上不可能
    再漂移，也就不需要维护同步纪律。
    """
    owners = [name for name, src in _dash_scripts().items() if "const TREND_KIND_LABELS = {" in src]
    assert owners == ["dash-core.js"], f"TREND_KIND_LABELS 不是恰好一份：{owners}"


# ── 6. 面板标题不再出现缠论专有名词 ────────────────────────────────
def test_panel_titles_do_not_use_chanlun_jargon() -> None:
    titles = re.findall(r'class="panel-title">([^<]+)<', _HTML)
    assert titles, "一个面板标题都没找到"
    for t in titles:
        assert "缠论" not in t, f"面板标题还在用缠论这个词：{t}"
