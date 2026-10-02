"""parity 视图模型：同一批 K 线下，**生产侧 vs 参照侧**的逐项结构对照。

## 这是一段被删掉又接回来的链路（R20 删 → R35 接）

- **2026-09-25**：本模块以「待接线」状态写好（投影函数 + 契约）。
- **R20（``a5365bd``）删掉它**，理由写在那次提交消息里：

      oracle 参照实现 R13 已整体删除，available:false 是永久的，投影函数属死代码

  同时**保留**了 ``dashboard_snapshot_v2`` 的 unavailable 键位、
  ``/api/dashboard/parity`` 路由与前端 ``renderParityCharts`` ——
  理由是「前端 28 处消费点依赖该形状，摘面板代价大于收益」。
- **R35**：那条「参照实现已删除 ⇒ 永久 unavailable」的前提**不再成立**。原设计里的
  "oracle" 指另一个项目提供的参考结构，那个项目确实没交付；但仓里还有另一种同样
  成立的参照 —— **第二个缠论后端**。所以本模块复活，并拿到**真正的生产者**。

## 参照侧现在是什么（owner 2026-10-02 拍板：czsc 优先，回落腾讯）

| 优先 | 参照 | 语义 | 成本 |
|---|---|---|---|
| 1 | **czsc 后端** | 同一批 K 线、两个后端的逐项对照（纯实现对照） | 零外部成本 |
| 2 | **腾讯 hfq 序列** | 本地库结构 vs 公开源结构（**数据链路**对照） | 每快照 TTL 一次 HTTP |

真机现状（2026-10-02，oracle）：czsc **没装**（``.[chan]`` extra 未安装），所以
``auto`` 回落 native ⇒ 实际走的是回落分支，payload 里 ``reference.source`` 会如实
写明。**装 czsc 会同时把生产结构从 native 切成 czsc**（每张图的笔都会变），那是
另一个决定，本模块不碰。

## 形状契约（前端 28 处消费点依赖，别乱改）

::

    {
      "available": bool,            # false 时前端显示「Oracle 对比未启用：{reason}」
      "reason": str,                # 仅 available=false 时有意义
      "reference": {"source", "detail"},   # 参照侧是谁/为什么降级
      "fractals": {"summary": {...}, "items": [...]},
      "bis": {...},
      "zhongshus": {...},
    }

``items`` 的每一行前端要用到 ``kind`` / ``status``（→ CSS ``parity-<status>``）/
``cpt`` / ``oracle``（哪一侧存在）以及 ``ref.start_time ?? ref.bar_index``（点选高亮
的时间锚）。``status ∈ matched | missing | extra | mismatched``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

__all__ = [
    "PARITY_KINDS",
    "REFERENCE_SOURCES",
    "build_parity_snapshot",
    "build_parity_view",
]

#: 三个对照种类。**前端把这份清单写死**（``renderParityCharts`` 的
#: ``["fractals","bis","zhongshus"]``），所以这里也用它，而不是遍历 payload 的键 ——
#: payload 顶层还有 ``available``/``reason``/``reference`` 这些元数据。
PARITY_KINDS: Final[tuple[str, ...]] = ("fractals", "bis", "zhongshus")

#: 参照侧的合法取值（写进 payload，前端可据此显示"跟谁比的"）。
REFERENCE_SOURCES: Final[tuple[str, ...]] = ("czsc", "tencent_hfq", "none")

#: 数值字段的相对容差。**为什么需要**（R35 真机实测）：腾讯 hfq 序列只给 3 位小数，
#: 本地库是满精度，同一根 bar 的 high/low 实测相对差 0.002% ~ 0.54%（中位 0.175%）。
#: 用"全字段全等"判 matched 会让**每一条**都变成 mismatched，面板显示
#: 「matched 0 · 100% 不一致」—— 那是个**误导性的结论**：结构其实是对上的。
#: 所以价格类差异按容差放过，但仍记进 ``value_diffs``（信息不丢）。
#:
#: 1% 是量出来的：实测最大 0.54%，取整到 1% 留一倍余量；而真正该报的结构差异
#: （``length`` 2 vs 3 这种整数差）不在容差范围内，仍会报 mismatched。
VALUE_TOLERANCE: Final[float] = 0.01

_PLACEHOLDER_TIME: Final[int] = -1


def _drift(cpt_value: Any, ref_value: Any) -> float | None:
    """相对差（百分比）；不可比时返回 ``None``。"""
    if not isinstance(cpt_value, (int, float)) or not isinstance(ref_value, (int, float)):
        return None
    if isinstance(cpt_value, bool) or isinstance(ref_value, bool):
        return None
    base = float(cpt_value)
    if base == 0.0:
        return 0.0 if float(ref_value) == 0.0 else None
    return (float(ref_value) - base) / abs(base) * 100.0


def _key(item: dict[str, Any], kind: str) -> tuple[Any, ...]:
    """配对键 = **种类 + 层级 + 时间区间 + 方向**。

    必须带 ``level``：多级别结构里同区间可能有多层（``min_bi_len`` 之外的层级），
    不带 level 会把不同层级的同区间结构配成一对，对照结果失去意义。
    """
    return (
        kind,
        item.get("level"),
        item.get("start_time", item.get("bar_index")),
        item.get("end_time"),
        item.get("direction"),
    )


def usable(item: dict[str, Any]) -> bool:
    """占位结构**不参与**对照。

    ``PLACEHOLDER_TIME``（-1）是"没算出来"的哨兵（见 ``export`` 里为什么拒绝导出它）。
    两侧都带占位的话，对照会把它们判成 matched —— 一个绿色的假象。所以**滤掉**。
    """
    for field in ("start_time", "end_time"):
        if item.get(field) == _PLACEHOLDER_TIME:
            return False
    return True


def build_parity_view(
    cpt_items: Sequence[dict[str, Any]],
    ref_items: Sequence[dict[str, Any]],
    *,
    kind: str,
) -> dict[str, Any]:
    """把两侧归一化结构序列配成确定性的对照视图。"""
    cpt_by_key = {_key(i, kind): i for i in cpt_items if usable(i)}
    ref_by_key = {_key(i, kind): i for i in ref_items if usable(i)}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(cpt_by_key) | set(ref_by_key), key=str):
        cpt = cpt_by_key.get(key)
        ref = ref_by_key.get(key)
        # status 描述的是 **CPT 侧的偏差**：CPT 没有 → missing（参照有），
        # CPT 多出来 → extra（参照没有）。这与 R20 删掉前的实现逐字一致，
        # 而且被当时那条测试钉死（cpt 1 条 / 参照 2 条 ⇒ missing:1）。
        differences: list[str] = []
        value_diffs: list[dict[str, Any]] = []
        if cpt is None:
            status = "missing"
        elif ref is None:
            status = "extra"
        else:
            for field in sorted(set(cpt) | set(ref)):
                cpt_value, ref_value = cpt.get(field), ref.get(field)
                if cpt_value == ref_value:
                    continue
                drift = _drift(cpt_value, ref_value)
                if drift is not None and abs(drift) <= VALUE_TOLERANCE * 100:
                    # 舍入级差异：记下来，但不把状态翻成 mismatched
                    value_diffs.append(
                        {
                            "field": field,
                            "cpt": cpt_value,
                            "ref": ref_value,
                            "drift_pct": round(drift, 6),
                        }
                    )
                else:
                    differences.append(field)
            status = "mismatched" if differences else "matched"
        rows.append(
            {
                "kind": kind,
                "status": status,
                "key": list(key),
                # 前端按 ``item.cpt`` / ``item.oracle`` 取"这一侧有没有这个东西"，
                # 键名是契约的一部分，别改成 ref。
                "cpt": cpt,
                "oracle": ref,
                "difference_fields": [] if status not in {"matched", "mismatched"} else differences,
                # 容差内但确有差异的字段（信息不丢，只是不算"不一致"）
                "value_diffs": value_diffs,
            }
        )
    matched = sum(r["status"] == "matched" for r in rows)
    total = len(rows)
    drifts = [abs(v["drift_pct"]) for r in rows for v in r["value_diffs"]]
    return {
        "summary": {
            "matched": matched,
            "missing": sum(r["status"] == "missing" for r in rows),
            "extra": sum(r["status"] == "extra" for r in rows),
            "mismatched": sum(r["status"] == "mismatched" for r in rows),
            "total": total,
            "match_rate": round(matched / total, 4) if total else 1.0,
            # R35：容差内差异的规模（实测 600519 最大 0.54%）—— 面板据此说明
            # 「结构对上了，数值只差这么多」，而不是笼统说"不一致"。
            "drifted": sum(1 for r in rows if r["value_diffs"]),
            "max_drift_pct": round(max(drifts), 4) if drifts else 0.0,
            "value_tolerance_pct": VALUE_TOLERANCE * 100,
        },
        "items": tuple(rows),
    }


def build_parity_snapshot(
    *,
    fractals: Sequence[dict[str, Any]] = (),
    bis: Sequence[dict[str, Any]] = (),
    zhongshus: Sequence[dict[str, Any]] = (),
    ref_fractals: Sequence[dict[str, Any]] = (),
    ref_bis: Sequence[dict[str, Any]] = (),
    ref_zhongshus: Sequence[dict[str, Any]] = (),
    reference: str = "none",
    reference_detail: str = "",
    available: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    """组装 parity 对象。``available=False`` 时只带原因，不带三个 kind。

    参数名用 ``ref_*``（而 payload 键仍是 ``oracle``）—— 前端键名是契约，参数名不是。
    """
    payload: dict[str, Any] = {
        "available": available,
        "reason": reason,
        "reference": {"source": reference, "detail": reference_detail},
    }
    if not available:
        return payload
    payload["fractals"] = build_parity_view(fractals, ref_fractals, kind="fractal")
    payload["bis"] = build_parity_view(bis, ref_bis, kind="bi")
    payload["zhongshus"] = build_parity_view(zhongshus, ref_zhongshus, kind="zhongshu")
    return payload
