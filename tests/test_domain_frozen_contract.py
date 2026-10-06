"""冻结口径的**变更门禁**（R45，owner 选定的方案 b）。

## 为什么需要它

`cpt/domain/config.py` 声明「``v0`` 配置冻结，改动必须走 ``v0.x`` 升级流程」，
但 R45 核实发现**这个纪律没有任何机制强制**：

- ``SCHEMA_VERSION`` 硬编码为 ``"v0"``，且 :meth:`RulesConfig.from_dict` 会
  ``pop`` 掉传入的同名字段 ⇒ **这个版本号永远不会变**；
- 于是改了参数、版本号还是 ``v0``，旧 fixture 照样通过校验被回放，
  **且不会有任何提示** —— 回放会静默使用旧口径。

真正检测漂移的是 ``reproducibility.config_hash``（``run_metric`` 里确实在比对），
但它管不到**回放 fixture** 这条路。

## 这道门禁怎么做（以及为什么不依赖 git history）

方案 (b)：CI 断言「改了口径参数就必须同时升版本 + 同步 ``docs/rules.md`` §9」。

⚠️ **不能**写成「和上次提交比 diff」—— CI 是全新 checkout，没有 history。
所以做成**自包含**的钉子：把字段与默认值钉在下面，改了就红。

另外额外要求：新增/改名的参数**必须出现在** ``docs/rules.md`` 里 ——
这样「同步文档」也不靠自觉，而是被机械检查。

## 改口径时怎么走

1. 改 ``cpt/domain/config.py`` 的字段或默认值
2. 同步 ``docs/rules.md`` §9
3. 升 ``SCHEMA_VERSION``（``"v0"`` → ``"v0.1"``）
4. 重新生成本文件里的 :data:`FROZEN`，否则 CI 会红并告诉你差在哪
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from cpt.domain.config import SCHEMA_VERSION, RulesConfig

ROOT = Path(__file__).resolve().parents[1]
RULES_DOC = ROOT / "docs" / "rules.md"

#: v0 冻结口径的**钉子**。改动 RulesConfig 的字段或默认值会让本门禁红 ——
#: 那是故意的：它要求你同时升 ``SCHEMA_VERSION`` 并同步 ``docs/rules.md`` §9。
#:
#: 改的时候请**连同本字典一起更新**，并在 commit message 里写明为什么改。
FROZEN: dict[str, object] = {
    "contain_direction": "forward",
    "fx_qy_middle": True,
    "fx_qj_ck": True,
    "bi_type_new": True,
    "zs_wzgx": "zgd",
    "zs_level_count": 1,
    "min_elements_for_higher_bi": 5,
    "min_bi_len": 6,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "divergence_compare": "area",
    "levels": (5, 30),
    "config_version": "v1",
}


def _current() -> dict[str, object]:
    cfg = RulesConfig()
    return {f.name: getattr(cfg, f.name) for f in dataclasses.fields(RulesConfig)}


# --------------------------------------------------------------------------- #
# 已经走过一次升级流程：v0 -> v1（2026-10-06）
# --------------------------------------------------------------------------- #
#
# 起因：``min_bi_len`` 的跨度门槛真正在 native 后端生效了。此前它只对 czsc
# 生效，而生产用的就是 native（``DEFAULT_BACKEND`` 就是它），参数被静默丢弃
# —— 文档声称已接线、代码里没有。
#
# 门槛一生效，全部结构（笔 / 中枢 / 走势类型 / 信号 / 水位指纹 / 缓存 run）
# 与 v0 **不可比**，所以走了本文件自己写明的三步流程：
#
#   1) 同步 ``docs/rules.md`` §9.8 —— 已重写为「门槛现在对 native 也生效」
#   2) 升 ``cpt/domain/config.py`` 的 ``SCHEMA_VERSION`` —— ``"v0"`` -> ``"v1"``
#   3) 更新本文件的钉子 —— ``config_version`` 同步到 ``"v1"``
#
# **注意**：字段默认值一个都没变（``min_bi_len`` 仍是 6），变的只是「门槛是否
# 真的被施加」。但这仍然必须升版 —— 升版挡的是「拿 v0 的回放 fixture 跑 v1
# 的代码，界面显示一切正常而每个数字都是错的」。v0 的回放 fixture 已作废，
# 需按 v0.x 流程重建。


def test_frozen_config_matches_pinned_snapshot() -> None:
    """口径参数与 :data:`FROZEN` 一致；不一致即「改了却没走升级流程」。"""
    cur = _current()
    changed = {
        k: (FROZEN.get(k, "<新增>"), cur.get(k))
        for k in set(cur) | set(FROZEN)
        if cur.get(k) != FROZEN.get(k)
    }
    assert not changed, (
        "RulesConfig 的字段/默认值变了，但没走「v0.x 升级流程」。\n"
        f"  差异: {changed}\n"
        "  必须同时做三件事：\n"
        "    1) 同步 docs/rules.md §9（该参数必须出现在文档里，见下一条用例）\n"
        "    2) 升 cpt/domain/config.py 的 SCHEMA_VERSION\n"
        "    3) 更新本文件的 FROZEN 钉子\n"
        "  理由：不升版本的话，旧的回放 fixture 会通过 from_dict 校验、"
        "静默使用旧口径（config_version 恒为 v0，守卫永远不会触发）。"
    )


def test_every_frozen_param_is_documented_in_rules_md() -> None:
    """每个冻结参数都必须在 ``docs/rules.md`` 里出现。

    这条让「同步文档」也不靠自觉 —— 加了新参数却忘了写进规则文档，门禁会红。
    """
    doc = RULES_DOC.read_text(encoding="utf-8")
    missing = [name for name in FROZEN if name != "config_version" and name not in doc]
    assert not missing, (
        f"这些冻结参数在 docs/rules.md 里查不到：{missing}。"
        f"  §9 是口径的权威文档，参数没写进去 = 口径没定义。"
    )


def test_schema_version_is_pinned_and_matches_snapshot() -> None:
    """``SCHEMA_VERSION`` 与钉子里的 ``config_version`` 必须一致。

    R45 核实过的坑：``from_dict`` 会 ``pop`` 掉传入的 ``config_version``，
    所以这个值**只跟随代码**。若二者漂移，回放守卫会拒收**当前代码自己
    序列化出来的**配置 —— 一道永远误伤（或永远失效）的门禁。
    """
    assert SCHEMA_VERSION == FROZEN["config_version"], (
        f"config.SCHEMA_VERSION={SCHEMA_VERSION!r} 与本文件的钉子 "
        f"{FROZEN['config_version']!r} 不一致 —— 改一个就要改另一个"
    )


def test_config_version_cannot_be_overridden_by_data() -> None:
    """``from_dict`` 必须忽略数据里传入的 ``config_version``。

    这是「版本号只跟随代码」的实现点。没它的话，fixture 可以自称任意版本、
    绕过 v0 冻结契约（``config.py`` docstring 里点名的那个坑）。
    """
    cfg = RulesConfig.from_dict({"config_version": "v99"})
    assert cfg.config_version == SCHEMA_VERSION, "数据覆盖了版本号"
