from __future__ import annotations

from typing import Any

from cpt.application.dashboard_export import slice_snapshot


def test_slice_snapshot_is_non_mutating_and_updates_bar_count() -> None:
    source = {
        "candles": [{"open_time": 1}, {"open_time": 2}, {"open_time": 3}],
        "market": {"bar_count": 3},
    }
    result = slice_snapshot(source, 2, 3)
    assert [item["open_time"] for item in result["candles"]] == [2, 3]
    assert result["market"]["bar_count"] == 2
    assert source["market"]["bar_count"] == 3


# --------------------------------------------------------------------------- #
# R32：切片的其余部分 —— 三个「本来就该有人问」的问题
# --------------------------------------------------------------------------- #


def _snapshot(n: int = 10) -> dict[str, Any]:
    """一个带结构块与下标的完整快照（模拟 600 根缓冲的缩小版）。"""
    return {
        "candles": [{"open_time": i, "close": i} for i in range(n)],
        "market": {
            "bar_count": n,
            "first_open_time": 0,
            "last_open_time": n - 1,
            "interval_ms": 3_600_000,
        },
        # 结构块：bar_index 按**完整窗口**计数
        "level_tree": {"levels": [{"elements": [{"bar_index": i} for i in range(0, n, 2)]}]},
        "overlays": {"pens": [{"start_bar": i, "end_bar": i + 1} for i in range(0, n, 2)]},
        "indicators": {"macd": [{"t": i} for i in range(n)]},
        "data_quality": {"closed_bar_count": n - 1},
    }


def test_market_time_bounds_follow_the_slice_not_the_window() -> None:
    """``bar_count`` 被改成切片后的根数了，时间跨度就必须跟着走。

    R32 真机实测过这个矛盾：同一个 ``market`` dict 里 ``bar_count=1``，
    ``first/last_open_time`` 却跨 600 小时。切片要自洽。
    """
    result = slice_snapshot(_snapshot(), 7, 9)

    assert result["market"]["bar_count"] == 3
    assert result["market"]["first_open_time"] == 7
    assert result["market"]["last_open_time"] == 9


def test_empty_slice_yields_none_bounds_not_stale_ones() -> None:
    """切不出根时给 ``None``（沿用 ``dashboard._market`` 空序列的约定），
    **不能**留着完整窗口的时间戳 —— 那正是 R32 之前那个自相矛盾。"""
    result = slice_snapshot(_snapshot(), 100, 200)

    assert result["candles"] == []
    assert result["market"]["bar_count"] == 0
    assert result["market"]["first_open_time"] is None
    assert result["market"]["last_open_time"] is None


def test_structure_blocks_are_left_untouched_and_say_so() -> None:
    """**钉住 R32 测到的那个事实**：结构块不被切，所以下标会越界。

    这条不是「期望它这样」，而是「**它现在就是这样，而且必须如实声明**」：
    消费方拿 ``bar_index`` 去索引本导出的 ``candles`` 会 IndexError。
    哪天真的去切结构了，这条测试会失败，那时记得把 docstring 与
    ``unsliced_blocks_note`` 一起改掉。
    """
    source = _snapshot()
    result = slice_snapshot(source, 7, 9)

    assert result["level_tree"] == source["level_tree"]
    assert result["overlays"] == source["overlays"]
    assert result["indicators"] == source["indicators"]
    # 越界是真实存在的：导出的 candles 只有 3 根，结构下标却到 8
    assert len(result["candles"]) == 3
    assert max(e["bar_index"] for e in result["level_tree"]["levels"][0]["elements"]) >= 8

    note = result["slice"]["unsliced_blocks_note"]
    assert "bar_index" in note
    assert result["slice"]["sliced_blocks"] == ["candles", "market"]
    # 切片丢掉了多少根也要看得见
    assert result["slice"]["source_bar_count"] == 10
    assert result["slice"]["candle_count"] == 3
