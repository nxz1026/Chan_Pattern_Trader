"""R91：首屏「今天的封板情绪」（``cpt/application/seal_board.py``）。

守的重点不是「算得对不对」，而是**两种「没有数据」不许混**：

- 表里没有轮次 → 「还没跑过」，不能读成「今天 0 只封板」；
- 有轮次但一只没封 → 「封板薄 / 没样本」，不能读成「采集器挂了」。

两者在盘中的应对完全相反：前者要去查服务，后者什么都不用做。
"""

from __future__ import annotations

import datetime as dt

from cpt.application.intraday_analysis import SEAL_HEALTHY_LOTS
from cpt.application.seal_board import SEAL_BOARD_BASIS, seal_board_summary

AT = dt.datetime(2026, 10, 22, 6, 30, tzinfo=dt.UTC)  # 北京 14:30


def _row(code: str, *, sealed: bool, lots: float | None) -> dict[str, object]:
    return {"at": AT, "code": code, "sealed": sealed, "bid1_lots": lots, "limit_price": 5.23}


def _latest(rows: list[dict[str, object]]) -> dict[str, object]:
    sealed = sum(1 for r in rows if r["sealed"])
    return {
        "at": AT if rows else None,
        "rows": rows,
        "counts": {"total": len(rows), "sealed": sealed, "open_board": len(rows) - sealed},
    }


class TestNoDataIsNotZeroSealed:
    def test_empty_table_says_never_ran_not_zero_sealed(self) -> None:
        out = seal_board_summary({"at": None, "rows": [], "counts": {}})
        assert out["available"] is False
        assert "还没有盘中封板快照" in out["headline"]
        # 关键：不许把「没跑过」说成「今天没有票封板」。
        # 文案里可以出现这句话，但必须在**否定**它（「这不是『今天没有票封板』」）。
        assert "没有票封板" in out["headline"]  # 出现只是因为它被明确否定
        assert "这不是" in out["headline"]
        assert out["sealed"] == 0
        assert out["label"] == "没样本"

    def test_empty_table_explains_the_two_are_different(self) -> None:
        out = seal_board_summary({"at": None, "rows": [], "counts": {}})
        assert "不是「今天没有票封板」" in out["headline"]

    def test_round_with_no_sealed_is_its_own_case(self) -> None:
        """有轮次、一只没封 —— 这是**有数据**，只是样本为空。"""
        out = seal_board_summary(_latest([_row("000001", sealed=False, lots=None)]))
        assert out["available"] is True
        assert out["sealed"] == 0
        assert "没有一只封住" in out["headline"]
        assert "没样本" in out["headline"]
        # 这一句必须与「还没跑过」的不同
        assert "还没有盘中封板快照" not in out["headline"]


class TestNumbers:
    def test_counts_and_median_come_from_the_latest_round_only(self) -> None:
        rows = [
            _row("600293", sealed=True, lots=300_000.0),
            _row("002249", sealed=True, lots=100_000.0),
            _row("600000", sealed=True, lots=50_000.0),
            _row("000001", sealed=False, lots=None),
        ]
        out = seal_board_summary(_latest(rows))
        assert (out["sealed"], out["open_board"], out["total"]) == (3, 1, 4)
        assert out["median_bid_lots"] == 100_000.0  # 三个封板票的中位，不是四只

    def test_label_follows_the_median_against_the_thick_line(self) -> None:
        thick = seal_board_summary(
            _latest([_row("600293", sealed=True, lots=SEAL_HEALTHY_LOTS * 2.5)])
        )
        thin = seal_board_summary(
            _latest([_row("600293", sealed=True, lots=SEAL_HEALTHY_LOTS * 0.2)])
        )
        assert thick["label"] == "厚"
        assert thin["label"] == "薄"
        assert thick["board_strength"] > thin["board_strength"]

    def test_sealed_row_without_volume_does_not_drag_the_median(self) -> None:
        """封着但取不到量（停牌/缺档位）不许当 0 手参与中位。"""
        out = seal_board_summary(
            _latest(
                [
                    _row("600293", sealed=True, lots=None),
                    _row("002249", sealed=True, lots=120_000.0),
                ]
            )
        )
        assert out["median_bid_lots"] == 120_000.0
        assert out["sealed"] == 2

    def test_as_of_is_beijing_time(self) -> None:
        out = seal_board_summary(_latest([_row("600293", sealed=True, lots=60_000.0)]))
        assert out["as_of_hms"] == "14:30", "面板上的钟点必须是北京时间，不是 UTC"
        assert out["headline"].startswith("14:30")


class TestGuardrails:
    def test_every_branch_carries_the_not_a_buy_signal_caveat(self) -> None:
        """R76 红线：任何分支都不许漏掉「不是买入信号」。"""
        for latest in (
            {"at": None, "rows": [], "counts": {}},
            _latest([_row("600293", sealed=True, lots=60_000.0)]),
            _latest([_row("000001", sealed=False, lots=None)]),
        ):
            out = seal_board_summary(latest)
            assert "不是买入信号" in out["action"]

    def test_headline_never_uses_imperative_buy_words(self) -> None:
        import re

        forbidden = re.compile(r"买入|卖出|加仓|抄底|止损|建议")
        for latest in (
            {"at": None, "rows": [], "counts": {}},
            _latest([_row("600293", sealed=True, lots=60_000.0)]),
            _latest([_row("000001", sealed=False, lots=None)]),
        ):
            out = seal_board_summary(latest)
            assert not forbidden.search(str(out["headline"])), out["headline"]

    def test_every_number_points_back_to_a_column(self) -> None:
        out = seal_board_summary(_latest([_row("600293", sealed=True, lots=60_000.0)]))
        assert out["evidence"]["table"] == "public.intraday_seal_snapshot"
        assert out["evidence"]["columns"] == ("at", "code", "sealed", "bid1_lots", "limit_price")
        assert out["basis"] == SEAL_BOARD_BASIS

    def test_only_one_round_is_considered(self) -> None:
        """跨轮混算会让「上一轮封着、这一轮开板」读成两只票。"""
        out = seal_board_summary(_latest([_row("600293", sealed=True, lots=60_000.0)]))
        assert out["evidence"]["rounds_considered"] == 1
