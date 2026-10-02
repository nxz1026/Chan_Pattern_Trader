"""``cpt.llm.structured`` 防御式解析测试。

夹具全部是**真机抓包**（``tests/fixtures/llm_structured_samples.py``），不是手写
的假形状。理由见该固件文件的 docstring：一句话 —— 凭想象写的样本只会验证
「解析器符合解析器的作者」。
"""

from __future__ import annotations

import pytest
from cpt.llm.structured import PARSE_REASONS, parse_structured

from tests.fixtures.llm_structured_samples import (
    SAMPLE_A_CLEAN,
    SAMPLE_B_VALID_BUT_WRONG_SCHEMA,
    SAMPLE_C_COMPACT,
    SAMPLE_D_FENCE,
    SAMPLE_E_TRUNCATED,
    SAMPLE_F_PROSE_THEN_FENCE,
)

REQUIRED = ("summary", "changed")


# --------------------------------------------------------------------------- #
# 1. 干净 JSON（真机 A / C）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sample", [SAMPLE_A_CLEAN, SAMPLE_C_COMPACT], ids=["A", "C"])
def test_clean_json_parsed_as_whole(sample: str) -> None:
    out = parse_structured(sample, required=REQUIRED)
    assert out.reason == "ok"
    assert out.method == "whole"
    assert out.data is not None
    assert out.data["summary"]
    assert len(out.data["changed"]) == 2


# --------------------------------------------------------------------------- #
# 2. 静默失败：合法 JSON 但 schema 不符（真机 B）—— 本模块存在的核心理由
# --------------------------------------------------------------------------- #


def test_valid_json_with_wrong_schema_is_not_accepted() -> None:
    """真机用例 B：``json.loads`` 会成功，但模型吐的是 ``{code, data}`` 而非 schema。

    只做 ``json.loads`` 的解析器在这里会**静默通过**，把 ``None`` 一路传下去，
    直到前端某个字段空白才被发现。所以 schema 校验必须是解析尝试的一部分。
    """
    import json

    # 先证明「它确实是合法 JSON」—— 这正是它危险的原因
    assert isinstance(json.loads(SAMPLE_B_VALID_BUT_WRONG_SCHEMA), dict)

    out = parse_structured(SAMPLE_B_VALID_BUT_WRONG_SCHEMA, required=REQUIRED)
    assert out.data is None, "schema 不符时不能返回数据"
    assert out.reason == "schema_mismatch", out.reason
    # 原始输出必须留着，调用方要靠它降级展示
    assert out.raw == SAMPLE_B_VALID_BUT_WRONG_SCHEMA


def test_wrong_schema_without_required_accepts_anything_valid() -> None:
    """不声明 required 时，B 那种形状就该被接受 —— 校验是**按需**的，不是默认全查。"""
    out = parse_structured(SAMPLE_B_VALID_BUT_WRONG_SCHEMA)
    assert out.reason == "ok"
    assert out.method == "whole"


def test_null_valued_required_field_is_rejected() -> None:
    """``{"summary": null}`` 过了「键存在」，但对调用方等于没有。"""
    out = parse_structured('{"summary": null, "changed": []}', required=REQUIRED)
    assert out.data is None
    assert out.reason == "schema_mismatch"


# --------------------------------------------------------------------------- #
# 3. markdown 围栏（真机 D / F）
# --------------------------------------------------------------------------- #


def test_json_fence_is_extracted() -> None:
    out = parse_structured(SAMPLE_D_FENCE, required=("summary",))
    assert out.reason == "ok", out.reason
    assert out.method == "fence"
    assert out.data is not None and "level 5" in out.data["summary"]


def test_prose_then_fence_is_extracted() -> None:
    """真机 F：先说一大段「信息不足」，再给围栏。"""
    out = parse_structured(SAMPLE_F_PROSE_THEN_FENCE, required=("summary",))
    assert out.reason == "ok", out.reason
    assert out.method == "fence"
    assert out.data == {"summary": "000001 平安银行"}


def test_bare_fence_without_language_tag() -> None:
    out = parse_structured('```\n{"summary": "x"}\n```', required=("summary",))
    assert out.reason == "ok", out.reason
    assert out.data == {"summary": "x"}


def test_fence_containing_prose_only_is_not_json() -> None:
    out = parse_structured("```\n这不是 JSON，只是一段说明。\n```", required=("summary",))
    assert out.data is None
    assert out.reason in {"not_json", "schema_mismatch"}


# --------------------------------------------------------------------------- #
# 4. 截断（真机 E）
# --------------------------------------------------------------------------- #


def test_truncated_json_reported_as_truncated_not_silently_repaired() -> None:
    """真机用例 E：停在字符串中间。

    **刻意不补括号硬救** —— 截断的 JSON 语义是残缺的，硬救出来的结构化数据
    是**编的**，比降级到原文展示危险得多。前端拿到半截数组却以为它完整，
    比明说「输出被截断」糟糕得多。
    """
    out = parse_structured(SAMPLE_E_TRUNCATED, expect_object=False)
    assert out.data is None
    assert out.reason == "truncated", out.reason
    assert out.raw == SAMPLE_E_TRUNCATED


def test_truncated_object_also_detected() -> None:
    out = parse_structured('{"summary": "写到一半就断', required=("summary",))
    assert out.data is None
    assert out.reason == "truncated"


# --------------------------------------------------------------------------- #
# 5. 扫描器：字符串字面量里的括号不能当结构
# --------------------------------------------------------------------------- #


def test_braces_inside_string_literals_do_not_break_scanning() -> None:
    """朴素的 ``text.find('}')`` 会在这里切错位置。

    note 里带 ``}``、``{``、``\\}`` 是模型输出里极常见的形态（JSON 片段、
    正则、模板占位）。切错 → 片段语法非法 → 被当成 ``not_json`` 丢掉，
    而原文其实完全可解析。
    """
    # raw string：让 \" 以字面两个字符落到待解析文本里（JSON 里是「字符串内的
    # 转义引号」）。写成普通字符串的 "\\\\"" 实际是「转义反斜杠 + 闭合引号」，
    # JSON 会认为字符串提前结束 —— 那样测的就不是我想测的东西了。
    payload = r'{"summary": "包含 } 和 { 的文本，还有转义 \" 引号", "changed": []}'
    out = parse_structured(payload, required=REQUIRED)
    assert out.reason == "ok", out.reason
    assert out.data is not None
    assert "}" in out.data["summary"]
    assert '"' in out.data["summary"]


def test_embedded_object_among_prose_without_fence() -> None:
    out = parse_structured(
        '分析如下：\n{"summary": "上移", "changed": ["a"]}\n以上。',
        required=REQUIRED,
    )
    assert out.reason == "ok", out.reason
    assert out.method == "embedded"
    assert out.data is not None and out.data["summary"] == "上移"


# --------------------------------------------------------------------------- #
# 6. 失败路径一律不抛
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sample",
    ["", "   ", "\n\n", "完全不是 JSON 的一段中文。", "{", "[", '{"a":', "```json\n{"],
    ids=[
        "empty",
        "spaces",
        "newlines",
        "prose",
        "open-brace",
        "open-bracket",
        "partial",
        "open-fence",
    ],
)
def test_never_raises_on_garbage(sample: str) -> None:
    """解析器抛异常会把「模型输出不合预期」变成「看板 500」，方向完全反了。"""
    out = parse_structured(sample, required=REQUIRED)
    assert out.data is None
    assert out.reason in PARSE_REASONS
    assert out.raw == sample


def test_none_input_is_handled() -> None:
    out = parse_structured(None)  # type: ignore[arg-type]
    assert out.data is None
    assert out.reason == "empty"


# --------------------------------------------------------------------------- #
# 7. 数组模式
# --------------------------------------------------------------------------- #


def test_array_mode() -> None:
    out = parse_structured('[{"id": "a"}, {"id": "b"}]', expect_object=False)
    assert out.reason == "ok", out.reason
    assert isinstance(out.data, list)
    assert len(out.data) == 2


def test_object_returned_when_array_expected_is_rejected() -> None:
    out = parse_structured('{"a": 1}', expect_object=False)
    assert out.data is None
    assert out.reason == "schema_mismatch"


def test_array_returned_when_object_expected_is_rejected() -> None:
    out = parse_structured("[1, 2]", expect_object=True, required=())
    assert out.data is None
    assert out.reason == "schema_mismatch"


# --------------------------------------------------------------------------- #
# 8. 解析器管不了的部分（写下来免得下一个人以为它管了）
# --------------------------------------------------------------------------- #


def test_semantically_empty_but_syntactically_valid_is_still_accepted() -> None:
    """真机 F 围栏里那句 ``{"summary": "000001 平安银行"}`` —— schema 过了，内容是废的。

    **解析器不应该、也确实没有**在这里拦它：它语法完全正确、字段齐备。
    「内容有没有用」是质量判据，得在用例层另外做（长度下限 / 与输入的相关性）。
    写这条测试是为了让这个边界**显式**，而不是留个「以为解析器管了」的坑。
    """
    out = parse_structured('{"summary": "000001 平安银行"}', required=("summary",))
    assert out.reason == "ok"
    assert out.data == {"summary": "000001 平安银行"}
