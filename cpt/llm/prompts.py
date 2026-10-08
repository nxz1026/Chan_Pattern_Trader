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

from cpt.domain.levels import level_label, level_table_for

__all__ = ["PURPOSE_EXPLAIN", "explain_request", "level_label", "render_structure_payload"]

#: 用例标识，会落进审计表的 ``purpose`` 列
PURPOSE_EXPLAIN = "explain_structure"

_SYSTEM_EXPLAIN = """你是一个缠中说禅（缠论）结构分析助手，服务于一个量化研究看板。

规则：
1. **只用中文回答**，不要切换成英文。
2. 你的输出会被直接显示在看板上，**不要**复述输入的 JSON，不要写「好的，以下是…」
   这类开场白，直接给结论。
3. 你**只解释**，不做判断：不要给买卖建议、不要预测涨跌、不要说「建议买入/卖出」。
4. 不确定的地方明说「这一点从给出的结构无法判断」，不要编。
5. 全文控制在 5 段以内。
6. **级别单位按市场而异，务必照抄输入里的「本级标签」，不要自己把 level 数字
   换算成时间。** 具体地：输入市场为 `a_share` 时，数据源是**日线**，
   level=5 的含义是「日线级别」，**不是**「5 分钟级别」；输入市场为 `crypto`
   时 level 的单位才是分钟。写错级别单位会产出一条听起来专业、实则完全错误的
   解释，比说「不知道」有害得多。"""


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

    ## 级别标签必须一起给（R28-9）

    ``structure`` 里的 ``level`` 是**裸数字**，而它的单位**按市场而异**：
    加密侧是分钟数，A 股侧是相对层级编号（数据源是日线）。模型看到裸 ``5``
    只能按「分钟」猜 —— 实测它确实猜了，正文写「5 分钟级别的一笔」。

    所以这里把 ``级别说明`` 整张表附上，并在结构里补一个 ``本级标签``，
    让模型**照抄标签而不是自己换算单位**。
    """
    table = level_table_for(market)
    enriched = dict(structure)
    if "level" in structure:
        enriched["本级标签"] = level_label(market, structure.get("level"))

    payload: dict[str, Any] = {
        "标的": f"{code} {name}".strip(),
        "市场": market,
        "级别说明": {
            "重要": "level 的单位按市场而异，**不要**一律当成分钟数",
            "级别表": {str(key): spec.as_dict() for key, spec in table.items()},
        },
        "结构": enriched,
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


# ── R45 新增：结构判断摘要 ──────────────────────────────────────
PURPOSE_SUMMARIZE = "summarize_recommendation"

#: 摘要用例的 system 提示词。**关键：它只被允许复述，不被允许推理。**
_SUMMARY_SYSTEM = (
    "你是缠论结构看板的旁白。用**不超过 3 句话**把给定的结构判断说成人话："
    "它在讲什么、依据是什么、接下来该看什么。"
    "\n\n"
    "硬性约束：\n"
    "1. **只能使用给定事实**，不得引入任何未给出的价格、数值、指标或结论；\n"
    "2. 不得给出买卖建议、不得预测涨跌、不得使用「建议」「必涨」「稳赚」等措辞；\n"
    "3. 若给定事实里写着「非投资建议」，你也必须在结尾带上这句；\n"
    "4. 事实与常识冲突时，以给定事实为准，不要「纠正」它。"
)


def summarize_request(
    *,
    code: str,
    name: str,
    action_label: str,
    headline: str,
    reason: str,
    price: float | None = None,
    disclaimer: str = "",
    subject_id: str = "",
    cache_bucket: str = "",
    max_tokens: int = 220,
) -> Any:
    """构造「给推荐配人话」的 :class:`~cpt.llm.base.LLMRequest`。

    :param action_label: 确定性算出的动作词（``买入结构`` / ``观望`` …）。
    :param headline: 确定性算出的结论。
    :param reason: 确定性算出的依据。
    :param price: **不复权**参考价（可挂单的那个），不是后复权价。
    :param cache_bucket: 时间桶标识（如 ``2026-10-08T06:00Z``）。非空时在提示词
        末尾追加一行**仅供去重分区**的系统标记，用来实现「每个时间窗重新生成一次」。

    ## 为什么提示词里**只有这三行**

    买卖与价格由 :mod:`cpt.application.recommendation` **纯确定性**算出。
    这里的输入**只有那个结果** —— 不含分型/笔/中枢/背驰明细。

    ⇒ 模型**没有机会编造**：它看不到原始结构数据，也就无从算出别的结论。
    如果把结构明细一起给它「让它结合数据说得更准」，
    那等于让它有机会产出与确定性结果**矛盾**的判断，
    而看板上两个数字并排显示时，没人知道该信哪个。

    这条约束是本用例存在的全部意义，**不要为了「更聪明」而放宽**。

    ## 为什么时间桶要写进提示词

    幂等键是 ``sha256(purpose + system + user)``（见
    :func:`cpt.storage.llm_call_store.request_hash`），
    唯一索引落在 ``(purpose, request_hash)`` 上。也就是说「同一份提示词只调用一次模型」
    是**永久**的，不由任何 TTL 决定。

    要做到「6h 之后点按钮真能重新生成」，就只能让提示词本身随窗口变化 ——
    :param:`cache_bucket` 就是那个变量。代价是模型会看到这一行，所以文案里
    明确要求它**不得复述**；收益是 ``request_hash`` 仍是「提示词的纯函数」这一
    不变量不被破坏（不需要改 ``request_hash`` 语义或唯一索引）。
    """
    from cpt.llm.base import LLMRequest

    lines = [
        f"标的：{name or code}（{code}）",
        f"结构判断：{action_label}",
        f"结论：{headline}",
    ]
    if price is not None:
        lines.append(f"参考价（不复权）：{price:.2f}")
    if reason:
        lines.append(f"依据：{reason}")
    if disclaimer:
        lines.append(f"附注：{disclaimer}")
    if cache_bucket:
        # 只在**有窗口语义**的调用方（追踪页「再讲一次人话」）才出现。
        # 主看板 submit_llm_summarize 不传，提示词与 R45 完全一致（向后兼容）。
        lines.append(f"（系统标记 cache-bucket={cache_bucket}：仅用于去重分区，禁止复述）")
    user = "请把下面这个结构判断用不超过 3 句话说成人话。\n\n" + "\n".join(lines)
    return LLMRequest(
        purpose=PURPOSE_SUMMARIZE,
        system=_SUMMARY_SYSTEM,
        user=user,
        max_tokens=max_tokens,
        subject_id=subject_id,
    )
