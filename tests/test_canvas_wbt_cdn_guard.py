"""画布 D 的 CDN 外链护栏（R45）。

**离线**：只测正则与纯函数，不装 wbt、不联网。

## 为什么这道护栏要有测试

`canvas_wbt.render` 里那道检查的注释写着：

    防御性：模板升级引入外链时响亮失败

也就是说它的**存在意义**就是「模板升级时拦住外链」。而 R45 实测：
这道护栏**自己有一个后门**（只认显式协议）、且**零测试覆盖**
（`grep cdn_reference_leaked tests/` 无命中）—— 一道自称防御性的护栏
既没被测过，本身还有洞。

## 后门在哪

原正则要求 **显式协议**（`http` / `https`），于是**协议相对 URL**
（`src="//cdn.example.com/x.js"`）直接漏过去 —— 它同样是外链，断网时同样拉不到。

⚠️ 同时**不能**误伤 ``src="/vendor/x.js"``：那正是页面要注入的本地资源。

## 为什么这条还重要

画布 D 的 iframe 是 ``srcdoc`` 渲染的 wbt 报告。外链漏出去意味着
**看板在断网环境下失效**（R16-5 的验收项之一就是「断网可用」）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "canvas_wbt_mod", ROOT / "cpt" / "application" / "canvas_wbt.py"
)
cw = importlib.util.module_from_spec(_spec)
sys.modules["canvas_wbt_mod"] = cw
_spec.loader.exec_module(cw)


# ---------------------------------------------------------------------------
# 必须判为「外链」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param('<link rel="stylesheet" href="https://cdn.jsdelivr.net/x.css">', id="https"),
        pytest.param("<script src='http://cdn.example.com/x.js'></script>", id="http-single-quote"),
        pytest.param('<img src="HTTPS://UPPER.CASE/x.png">', id="uppercase"),
        # ⚠️ R45 修的主目标：协议相对 URL
        pytest.param('<script src="//cdn.example.com/x.js"></script>', id="protocol-relative-js"),
        pytest.param('<link href="//fonts.googleapis.com/css">', id="protocol-relative-css"),
    ],
)
def test_external_references_are_caught(snippet: str) -> None:
    assert cw._CDN_RE.search(snippet), f"漏掉了外链（会破坏断网可用）：{snippet}"


# ---------------------------------------------------------------------------
# 必须**不**判为外链（误伤会打掉本地 vendor 注入）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param('<script src="/vendor/plotly.min.js"></script>', id="同源绝对路径"),
        pytest.param('<link href="./vendor/bootstrap.css">', id="相对路径"),
        pytest.param("<script>var x = 'https://example.com';</script>", id="文本里的 URL"),
        pytest.param('<div data-url="https://example.com/x"></div>', id="非 src/href 属性"),
        pytest.param('<script src="data:text/javascript,void 0"></script>', id="data URI"),
    ],
)
def test_local_references_are_not_flagged(snippet: str) -> None:
    assert not cw._CDN_RE.search(snippet), f"误伤了本地引用：{snippet}"
