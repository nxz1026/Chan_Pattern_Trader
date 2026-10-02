"""结构化输出的**防御式解析**（R28-2）。

## 为什么需要它，以及为什么不能照着「模型不守规矩」来设计

R25 台账记的是「`agnes-3.0-flash` **无视**『只输出 JSON』指令」。2026-10-02 真机
复测**推翻了这个结论**，而且推翻的方式很关键：三个用例它都吐了**合法 JSON**，
一句散文都没有。

真实失败形态是六种（全部来自真机抓包，样本见
``tests/fixtures/llm_structured_samples.py``）：

===== ==================================================================
用例   形状                                    整段 ``json.loads``
===== ==================================================================
A/C    干净 JSON                                成功
B      **合法 JSON、但完全不合 schema**          成功 → 但字段是错的
D      markdown 围栏 ```` ```json ````           失败（pos 0）
E      **截断的 JSON**（Unterminated string）    失败（pos 171）
F      散文 + 围栏                              失败（pos 0）
===== ==================================================================

## 最危险的是 B，不是 D

D/E/F 都很显眼：``json.loads`` 直接抛错，调用方立刻知道出事了。

**B 是静默的**：``json.loads`` 成功，``data.get("summary")`` 返回 ``None``，
一路传下去 —— 直到前端某个字段显示空白，才发现「结构化摘要」一直是空的。
模型在 B 里做的事是：完全无视要求的 schema，把**输入**原样包一层
``{"code": 200, "data": <输入>}`` 吐回来。

所以本模块的核心纪律：**schema 校验是每一次解析尝试的一部分**，不是解析之后的
独立步骤。整段能解析但 schema 不符时，判定为**这次尝试失败**，继续往下试别的
候选；全都不行才报 ``schema_mismatch``。

## 绝不抛

LLM 层全程 best-effort（与 recorder / 结构事件同一纪律）：解析失败返回
``data=None`` + 机器可读的 ``reason``，调用方据此降级到原文展示。解析器抛异常
会让「模型输出不合预期」变成「看板 500」，方向完全反了。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PARSE_REASONS",
    "ParsedStructured",
    "parse_structured",
]


@dataclass(frozen=True, slots=True)
class ParsedStructured:
    """一次防御式解析的结果。

    :param data: 解析成功且通过 schema 校验的数据；失败为 ``None``。
    :param method: 命中的策略（见 :func:`parse_structured`），失败为 ``""``。
    :param reason: 机器可读的失败原因，落审计用；成功为 ``"ok"``。
    :param raw: 模型原始输出，**永远保留** —— 降级展示要靠它。
    """

    data: dict[str, Any] | list[Any] | None
    method: str
    reason: str
    raw: str


#: 全部合法的 ``reason`` 取值。新增要同步更新测试。
#:
#: - ``ok`` —— 解析成功且 schema 通过；
#: - ``empty`` —— 模型没输出任何东西（空串 / 纯空白）；
#: - ``not_json`` —— 找不到任何能解析的 JSON 候选；
#: - ``truncated`` —— 找到了 JSON 容器但**没闭合**（多半是 max_tokens 截断）；
#: - ``schema_mismatch`` —— 解析成功但必需字段缺失/类型不对（真机用例 B）。
PARSE_REASONS = frozenset({"ok", "empty", "not_json", "truncated", "schema_mismatch"})

_FENCE = "```"


# --------------------------------------------------------------------------- #
# 候选抽取
# --------------------------------------------------------------------------- #


def _iter_fences(text: str) -> Iterator[str]:
    """产出 `````lang ... ``` ``` 围栏里的内容。"""
    cursor = 0
    while True:
        start = text.find(_FENCE, cursor)
        if start < 0:
            return
        body = start + len(_FENCE)
        # 跳过紧跟的 language 标签（json / JSON / python ...），直到换行
        newline = text.find("\n", body)
        if newline < 0:
            return
        close = text.find(_FENCE, newline)
        if close < 0:
            return
        yield text[newline + 1 : close]
        cursor = close + len(_FENCE)


def _scan_balanced(text: str, start: int) -> tuple[str | None, bool]:
    """从 ``start``（必须是 ``{`` 或 ``[``）扫一个配平的 JSON 容器。

    :returns: ``(片段, 是否闭合)``。片段在未闭合时仍返回，便于上层判「截断」。

    **必须手工扫，不能用 ``text.find('}')``** —— 字符串字面量里可以出现 ``}``、
    ``{``、``\\}``。真机用例里 note 字段就带 ``sources: fractal-0`` 这类内容，
    朴素的 index-of 会切错位置，产出一个语法非法的片段，然后被当成
    ``not_json`` 丢掉 —— 明明原文有可解析的 JSON。
    """
    opener = text[start]
    closer = {"{": "}", "[": "]"}[opener]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1], True
    # 没闭合 —— 多半是被 max_tokens 截断了
    return text[start:], False


def _iter_braces(text: str) -> Iterator[tuple[str, bool]]:
    """产出所有「顶层」配平容器的片段。

    顶层 = 不被另一个容器包住的。嵌套的 ``{"a": {"b": 1}}`` 只产出外层那个 ——
    内层单独拿去解析当然也能过，但它几乎不会是调用方要的 shape。
    """
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        # 往前看：若落在另一个未闭合的容器内部，就跳过
        if _inside_open_container(text, index):
            index += 1
            continue
        fragment, closed = _scan_balanced(text, index)
        if not fragment:
            return
        yield fragment, closed
        index += 1 if not closed else index + len(fragment)


def _inside_open_container(text: str, position: int) -> bool:
    """``position`` 处是否在某个**尚未闭合**的容器内部。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(position):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth > 0


# --------------------------------------------------------------------------- #
# schema 校验
# --------------------------------------------------------------------------- #


def _validate(
    data: Any,
    required: Sequence[str],
    *,
    expect_object: bool,
) -> str:
    """校验解析结果。返回 ``""`` 表示通过，否则返回失败原因片段。"""
    if expect_object and not isinstance(data, dict):
        return f"期望对象，实际是 {type(data).__name__}"
    if not expect_object and not isinstance(data, list):
        return f"期望数组，实际是 {type(data).__name__}"
    if expect_object:
        assert isinstance(data, dict)
        missing = [key for key in required if key not in data]
        if missing:
            return f"缺字段 {missing}"
        nulls = [key for key in required if data.get(key) is None]
        if nulls:
            return f"字段为 null {nulls}"
    return ""


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def parse_structured(
    text: str,
    *,
    required: Sequence[str] = (),
    expect_object: bool = True,
) -> ParsedStructured:
    """把模型的自由文本解析成结构化数据，**永不抛**。

    按优先级依次尝试，每个候选都要**同时**满足「能解析」和「schema 通过」才算命中：

    1. ``whole`` —— 整段就是 JSON（最常见，也最快）；
    2. ``fence`` —— ```json 围栏 / 裸 ``` 围栏的内容（真机用例 D、F）；
    3. ``embedded`` —— 文本里配平的 ``{...}`` / ``[...]``（夹在散文中间的情况）。

    真机用例 B（合法 JSON 但 schema 全错）会在第 1 步解析成功、schema 失败，
    然后继续试 2/3；都不行才返回 ``schema_mismatch``。**这正是本模块存在的理由** ——
    只做 ``json.loads`` 的话，B 会静默通过并把 ``None`` 传下去。

    :param required: 必需字段名（仅 ``expect_object=True`` 时检查）。
    :returns: :class:`ParsedStructured`；``data`` 为 ``None`` 时 ``reason`` 说明原因。
    """
    raw = text or ""
    if not raw.strip():
        return ParsedStructured(None, "", "empty", raw)

    seen_unclosed = False

    # 1) 整段
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        pass
    else:
        bad = _validate(parsed, required, expect_object=expect_object)
        if not bad:
            return ParsedStructured(parsed, "whole", "ok", raw)
        # 整段能解析但 schema 不符：记下来，继续试别的候选（真机用例 B）

    # 2) 围栏
    for fragment in _iter_fences(raw):
        try:
            parsed = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        bad = _validate(parsed, required, expect_object=expect_object)
        if not bad:
            return ParsedStructured(parsed, "fence", "ok", raw)

    # 3) 嵌入式配平容器
    schema_failed = False
    for fragment, closed in _iter_braces(raw):
        if not closed:
            seen_unclosed = True
            continue
        try:
            parsed = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        bad = _validate(parsed, required, expect_object=expect_object)
        if not bad:
            return ParsedStructured(parsed, "embedded", "ok", raw)
        schema_failed = True

    if schema_failed:
        return ParsedStructured(None, "", "schema_mismatch", raw)
    if seen_unclosed:
        # 找到了容器但没闭合 —— 多半是 max_tokens 把输出截断了。
        # 刻意**不**做「就地补括号硬救」：截断的 JSON 语义是残缺的，
        # 硬救出来的结构化数据是编的，比降级到原文危险得多。
        return ParsedStructured(None, "", "truncated", raw)
    return ParsedStructured(None, "", "not_json", raw)
