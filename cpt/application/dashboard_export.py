"""Read-only range slicing for research exports.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/export?start_ms=&end_ms=`
（`cpt/web/app.py`）的唯一实现，对应 `docs/dashboard-product-roadmap.md` Phase 6 P2
→「时间范围切片导出」。

> ⚠️ **R32 更正**：roadmap 那行还写着「前端『范围导出』面板加起止时间输入」。
> 真机核对 `/var/www/cpt-dashboard/*.js`：**没有任何文件引用 `dashboard/export`**，
> 那个面板不存在。本接口是**纯 API**，只能手工/脚本调用。
"""

from __future__ import annotations

from typing import Any

#: 本切片**会**改写的键。其余键原样保留（见 :func:`slice_snapshot` 的说明）。
SLICED_BLOCKS: tuple[str, ...] = ("candles", "market")


def slice_snapshot(snapshot: dict[str, Any], start_time: int, end_time: int) -> dict[str, Any]:
    """按时间范围切 ``candles``，并让 ``market`` 与切片**自洽**。不修改入参。

    ## ⚠️ 这不是一个自洽的切片：只有 candles 真的被切了

    除 ``candles`` / ``market`` 外的所有块（``level_tree`` / ``overlays`` /
    ``indicators`` / ``data_quality`` / ``reproducibility`` / ``engine_state`` …）
    **原样保留完整窗口的内容**。它们内部的下标（``bar_index``）仍然按**完整窗口**
    计数。

    R32 真机实测（600 根 1h 缓冲，请求最末 1 小时）：

        candle_count = 1，响应 263 KB
        level_tree 94,367 B / overlays 94,341 B / indicators 72,168 B
          —— 与完整快照**逐字节相同**
        level_tree+overlays 里扫到的 74 个 bar_index，**全部**越界（最大 121）

    也就是说：消费方**不能**拿结构下标去索引本导出的 ``candles``。要按时间过滤
    结构，请用结构对象自己的 ``start_time`` / ``end_time`` 字段。

    为什么保留而不切掉结构：这是研究用导出，结构数据本身就是要看的东西；悄悄
    删掉比"多给了"更危险。所以选择**如实声明**—— ``slice`` 块里写明切了什么、
    没切什么，让误用变成一眼可见。

    R32 之前这里只改 ``bar_count``，于是同一个 ``market`` dict 里
    ``bar_count=1`` 却配着跨 600 小时的 ``first/last_open_time``（自相矛盾）。
    现在两个时间戳也跟着切片走（空切片为 ``None``，与 ``dashboard._market``
    对空序列的约定一致）。

    :raises ValueError: ``start_time > end_time``。
    """
    if start_time > end_time:
        raise ValueError("start_time must be <= end_time")
    result = dict(snapshot)
    candles = snapshot.get("candles", [])
    kept = [candle for candle in candles if start_time <= candle.get("open_time", -1) <= end_time]
    result["candles"] = kept
    result["slice"] = {
        "start_time": start_time,
        "end_time": end_time,
        "candle_count": len(kept),
        # 源窗口有多少根 —— 切片丢掉了多少，一眼可见
        "source_bar_count": len(candles),
        "sliced_blocks": list(SLICED_BLOCKS),
        "unsliced_blocks_note": (
            "level_tree/overlays/indicators 等块未切片，仍是完整窗口的内容；"
            "其中的 bar_index 指向完整窗口，不能用来索引本导出的 candles"
        ),
    }
    market = dict(snapshot.get("market", {}))
    market["bar_count"] = len(kept)
    market["first_open_time"] = kept[0].get("open_time") if kept else None
    market["last_open_time"] = kept[-1].get("open_time") if kept else None
    result["market"] = market
    return result
