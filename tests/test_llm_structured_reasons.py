"""结构化解析的 ``reason`` 归类必须**说真话**（R45）。

**全程离线**：纯字符串解析，不联网、不调模型。

## 这条 bug 的性质：不是解析错，是**归类撒谎**

模块 docstring 自己写着「**最危险的是 B，不是 D**」——
B 是「合法 JSON、但完全不合 schema」，``json.loads`` 成功、字段是错的，
一路传下去直到前端某处空白才被发现。

而 R45 之前，**另一族 B（整段就是合法 JSON 标量）被归成了 ``not_json``**：

    "just a bare string"   →  not_json      （真相：解析成功了，是类型不对）
    123 / null / true       →  not_json      （同上）
    {"code":200,"data":{}}  →  schema_mismatch  （用例 B，本来的对）

``not_json`` 的定义是「找不到任何能解析的 JSON 候选」。它**明明解析成功了**。
这个 reason 会落进审计表与 UI，把排查方向指向「模型没输出 JSON」——
而真相是「输出的是 JSON，只是形状不对」。**归类撒谎比归类粗糙更有害。**

## 为什么原来的用例 B 是对的、标量却不对

``schema_failed`` 标志原先初始化在**第 3 步之前**，所以第 1 步（整段）校验失败
时无处置位。对象形态的 B 之所以正确，是因为第 3 步的 ``_iter_braces``
会把同一个容器**再扫一遍**，那次才置上位。标量形态压根没有 ``{`` / ``[``，
第 3 步扫不到任何东西 ⇒ 标志永远是 False ⇒ 落到末尾的 ``not_json``。
"""

from __future__ import annotations

import pytest
from cpt.llm.structured import PARSE_REASONS, parse_structured

_REQUIRED = ("summary",)


def _reason(text: str) -> str:
    return parse_structured(text, required=_REQUIRED).reason


# ---------------------------------------------------------------------------
# 核心回归：合法 JSON 标量 ⇒ schema_mismatch，**不是** not_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('"just a bare string"', id="bare-string"),
        pytest.param("123", id="bare-number"),
        pytest.param("null", id="bare-null"),
        pytest.param("true", id="bare-bool"),
        pytest.param("[1, 2, 3]", id="array-when-object-expected"),
    ],
)
def test_valid_json_scalar_is_schema_mismatch_not_not_json(text: str) -> None:
    """整段**能解析**就不许说 ``not_json``。"""
    reason = _reason(text)
    assert reason == "schema_mismatch", (
        f"{text!r} 是合法 JSON，只是形状不对，却报成了 {reason!r} —— "
        f"这会把排查引向「模型没输出 JSON」"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('```json\n"bare in fence"\n```', id="fence-scalar"),
        pytest.param('```json\n{"code":1}\n```', id="fence-wrong-schema"),
    ],
)
def test_fenced_wrong_shape_is_schema_mismatch(text: str) -> None:
    """围栏里的候选同样要置位（原实现第 2 步也没置）。"""
    assert _reason(text) == "schema_mismatch"


def test_object_case_b_still_schema_mismatch() -> None:
    """真机用例 B（模块 docstring 里的「最危险」那个）不能回归。"""
    assert _reason('{"code": 200, "data": {}}') == "schema_mismatch"


# ---------------------------------------------------------------------------
# 真正的 not_json / truncated / empty —— 别被误扩张
# ---------------------------------------------------------------------------


def test_prose_is_still_not_json() -> None:
    assert _reason("完全不是 JSON 的一段散文") == "not_json"


def test_truncated_json_is_still_truncated() -> None:
    """刻意**不**就地补括号硬救 —— 截断的 JSON 硬救出来是编的。"""
    assert _reason('{"summary": "截断') == "truncated"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_is_empty(text: str) -> None:
    assert _reason(text) == "empty"


def test_happy_path_still_ok() -> None:
    r = parse_structured('{"summary": "结构完好"}', required=_REQUIRED)
    assert r.reason == "ok"
    assert r.data == {"summary": "结构完好"}
    assert r.method == "whole"


# ---------------------------------------------------------------------------
# reason 词汇表本身
# ---------------------------------------------------------------------------


def test_every_reason_is_declared() -> None:
    """``PARSE_REASONS`` 是给审计/UI 看的契约，不得有未声明取值。"""
    seen = {
        _reason(t)
        for t in ('"s"', "123", "null", "true", "[1]", "散文", '{"a":1', "", '{"summary":"x"}')
    }
    assert seen <= PARSE_REASONS, f"出现了未声明的 reason：{seen - PARSE_REASONS}"


def test_raw_is_always_preserved() -> None:
    """降级展示靠 ``raw``，任何路径都不能丢。"""
    for text in ('"s"', "散文", '{"a":1', ""):
        assert parse_structured(text, required=_REQUIRED).raw == text
