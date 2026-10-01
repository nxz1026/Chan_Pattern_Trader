"""提示词模板。

集中管理、便于版本化（`architecture.md` §4.2）。**每条模板都必须写死输出语言与
格式约束** —— 实测 `agnes-3.0-flash` 是聊天模型，**不严格遵守「只输出 JSON」这类
指令**（给它 JSON 指令，它会写一段解释）。所以：

- 首版只做**自由文本**用例（规则解释），格式约束天然满足；
- 将来做结构化用例（差异摘要 / 标注辅助）时，**不要**假设模型会守规矩，
  必须防御式解析（取代码块 / 正则提取 / 解析失败降级为原文）。这一点已记进
  `architecture.md` §4.3。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["PURPOSE_EXPLAIN", "explain_request", "render_structure_payload"]

#: 用例标识，会落进审计表的 ``purpose`` 列
PURPOSE_EXPLAIN = "explain_structure"

_SYSTEM_EXPLAIN = """你是一个缠中说禅（缠论）结构分析助手，服务于一个量化研究看板。

规则：
1. **只用中文回答**，不要切换成英文。
2. 你的输出会被直接显示在看板上，**不要**复述输入的 JSON，不要写「好的，以下是…」
   这类开场白，直接给结论。
3. 你**只解释**，不做判断：不要给买卖建议、不要预测涨跌、不要说「建议买入/卖出」。
4. 不确定的地方明说「这一点从给出的结构无法判断」，不要编。
5. 全文控制在 5 段以内。"""


def render_structure_payload(
    *,
    code: str,
    name: str,
    market: str,
    structure: dict[str, Any],
    rules: dict[str, Any] | None = None,
) -> str:
    """把一条结构渲染成给模型看的紧凑文本。

    直接喂整个 snapshot 太大（几十万 token 的 candles），所以只挑**结构对象**。
    """
    payload: dict[str, Any] = {
        "标的": f"{code} {name}".strip(),
        "市场": market,
        "结构": structure,
    }
    if rules:
        payload["规则口径"] = rules
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def explain_request(
    *,
    code: str,
    name: str,
    market: str,
    structure: dict[str, Any],
    rules: dict[str, Any] | None = None,
    subject_id: str = "",
    max_tokens: int = 600,
) -> Any:
    """构造「规则解释」用例的 :class:`~cpt.llm.base.LLMRequest`。

    :param structure: 目标结构对象（Bi / ZhongShu / TrendType 的 asdict 形状）。
    :param rules: 相关规则片段，用于让解释有据可依而不是泛泛而谈。
    """
    from cpt.llm.base import LLMRequest

    rendered = render_structure_payload(
        code=code, name=name, market=market, structure=structure, rules=rules
    )
    user = (
        "请解释下面这个缠论结构：它是什么、怎么形成的、当前处于什么状态、"
        "以及后续需要观察什么来确认或证伪。\n\n"
        f"```json\n{rendered}\n```"
    )
    return LLMRequest(
        purpose=PURPOSE_EXPLAIN,
        system=_SYSTEM_EXPLAIN,
        user=user,
        max_tokens=max_tokens,
        temperature=0.2,
        subject_id=subject_id,
    )
