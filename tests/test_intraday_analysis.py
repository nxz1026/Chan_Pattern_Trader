"""盘中分析层单测（``cpt/application/intraday_analysis.py``）。**不连网、不连库**。

用 2026-10-21 收盘实跑的**真实形态**造样本：
40 只候选全封板，买一手数从 105 万到 3 万，卖五档全 ``None``。
"""

from __future__ import annotations

from typing import Any

import pytest
from cpt.application import intraday_analysis as ana


def _lot(n: float) -> float:
    """买一手数（``seal_board`` 输出的单位就是手）。"""
    return float(n)


def _board(
    *,
    sealed: list[tuple[str, str, float | None]],
    approaching: list[tuple[str, str, float | None, float]] | None = None,
    unavailable: list[tuple[str, str]] | None = None,
    candidates: int | None = None,
) -> dict[str, Any]:
    """直接造 :func:`seal_board` 的输出，跳过采集层。"""
    s_rows = [
        {"code": c, "name": n, "bid1_lots": v, "distance_pct": 0.0, "state": "sealed"}
        for c, n, v in sealed
    ]
    s_rows.sort(key=lambda r: -(r["bid1_lots"] or 0.0))
    a_rows = [
        {"code": c, "name": n, "bid1_lots": v, "distance_pct": d, "state": "approaching"}
        for c, n, v, d in (approaching or [])
    ]
    a_rows.sort(key=lambda r: abs(r["distance_pct"] or 0.0))
    u_rows = [
        {"code": c, "name": n, "bid1_lots": None, "distance_pct": None, "state": "unknown"}
        for c, n in (unavailable or [])
    ]
    lots = [r["bid1_lots"] for r in s_rows if r["bid1_lots"] is not None]
    import statistics

    return {
        "sealed": s_rows,
        "approaching": a_rows,
        "unavailable": u_rows,
        "counts": {
            "sealed": len(s_rows),
            "approaching": len(a_rows),
            "unavailable": len(u_rows),
            "candidates": candidates
            if candidates is not None
            else len(s_rows) + len(a_rows) + len(u_rows),
        },
        "median_bid_lots": float(statistics.median(lots)) if lots else None,
        "total_bid_lots": float(sum(lots)),
    }


# ───────────────────────── seal_board ─────────────────────────


def _scan(*, quotes: dict[str, dict[str, Any]], sealed_codes: list[str]) -> dict[str, Any]:
    """造 :meth:`IntradayCollector.scan` 的输出（真实形态）。"""
    candidates = []
    sealed = []
    for code, q in quotes.items():
        last = q.get("last")
        prev = q.get("prev_close")
        limit = round(prev * 1.1, 2) if prev else None
        distance = 0.0 if (limit and last == limit) else 0.5
        candidates.append(
            {
                "code": code,
                "name": q.get("name") or code,
                "last": last,
                "prev_close": prev,
                "limit_price": limit,
                "distance_pct": distance,
                "board": "main",
            }
        )
        if code in sealed_codes:
            bv = q.get("bid1_vol")
            sealed.append(
                {
                    "code": code,
                    "name": q.get("name"),
                    "bid1_lots": None if bv is None else bv / 100.0,
                    "distance_pct": distance,
                }
            )
    sealed.sort(key=lambda r: -(r["bid1_lots"] or 0.0))
    return {
        "rank_size": len(candidates),
        "candidates": candidates,
        "quotes": quotes,
        "sealed": sealed,
    }


def test_seal_board_uses_the_collector_verdict_not_a_second_one() -> None:
    """封没封只看采集层算好的 ``sealed``，**不在这里重算涨停价**。

    重算等于第二份口径，两份迟早打架（R69 的教训：判定器一处、调用方一处，
    调用方传错段落就会被当成判定器错）。
    """
    snapshot = _scan(
        quotes={
            # 现价 == 涨停价，但采集层没把它放进 sealed ⇒ 本模块必须跟着当成 approaching
            "600001": {"name": "甲", "last": 11.0, "prev_close": 10.0, "bid1_vol": 5_000_000.0},
        },
        sealed_codes=[],
    )
    board = ana.seal_board(snapshot)
    assert board["counts"]["sealed"] == 0
    assert board["counts"]["approaching"] == 1


def test_seal_board_converts_shares_to_lots() -> None:
    """``bid1_vol`` 是**股**，看板单位是**手**（100 股）。

    实测 ``600293 bid1_vol=105029924.0`` ⇒ 1,050,299.24 手。
    """
    snapshot = _scan(
        quotes={
            "600293": {
                "name": "三峡新材",
                "last": 4.75,
                "prev_close": 4.32,
                "bid1_vol": 105_029_924.0,
            }
        },
        sealed_codes=["600293"],
    )
    board = ana.seal_board(snapshot)
    assert board["sealed"][0]["bid1_lots"] == pytest.approx(1_050_299.24, abs=0.01)


def test_seal_board_sorts_sealed_by_thickness_descending() -> None:
    """封单最厚的排最前 —— 看板第一眼要看的是「最硬的那几只」。"""
    snapshot = _scan(
        quotes={
            "600001": {"name": "薄", "last": 11.0, "prev_close": 10.0, "bid1_vol": 300_000.0},
            "600002": {"name": "厚", "last": 11.0, "prev_close": 10.0, "bid1_vol": 90_000_000.0},
        },
        sealed_codes=["600001", "600002"],
    )
    board = ana.seal_board(snapshot)
    assert [r["code"] for r in board["sealed"]] == ["600002", "600001"]


def test_seal_board_sorts_approaching_by_distance_ascending() -> None:
    """没封的那批按「离板多远」从近到远 —— 最近的那个才值得盯。"""
    snapshot = _scan(
        quotes={
            "600001": {"name": "远", "last": 10.9, "prev_close": 10.0, "bid1_vol": 1_000.0},
            "600002": {"name": "近", "last": 10.98, "prev_close": 10.0, "bid1_vol": 1_000.0},
        },
        sealed_codes=[],
    )
    snapshot["candidates"][0]["distance_pct"] = 0.8
    snapshot["candidates"][1]["distance_pct"] = 0.2
    board = ana.seal_board(snapshot)
    assert [r["code"] for r in board["approaching"]] == ["600002", "600001"]


def test_seal_board_marks_missing_quote_as_unknown_not_sealed() -> None:
    """候选里有一行但五档没取到 ⇒ ``unavailable``。

    不许当成「没封」混进 approaching（那是「测了没封」），也不许当成 sealed。
    """
    snapshot = {
        "rank_size": 2,
        "candidates": [
            {
                "code": "600001",
                "name": "甲",
                "last": 11.0,
                "prev_close": 10.0,
                "limit_price": 11.0,
                "distance_pct": 0.0,
                "board": "main",
            },
            {
                "code": "600002",
                "name": "乙",
                "last": 10.9,
                "prev_close": 10.0,
                "limit_price": 11.0,
                "distance_pct": 0.9,
                "board": "main",
            },
        ],
        "quotes": {"600001": {"last": 11.0, "prev_close": 10.0, "bid1_vol": 5_000_000.0}},
        "sealed": [{"code": "600001", "name": "甲", "bid1_lots": 50_000.0, "distance_pct": 0.0}],
    }
    board = ana.seal_board(snapshot)
    assert [r["code"] for r in board["unavailable"]] == ["600002"]
    assert board["counts"] == {"sealed": 1, "approaching": 0, "unavailable": 1, "candidates": 2}


def test_seal_board_counts_reconcile_with_the_candidate_total() -> None:
    """三类加起来必须等于候选总数 —— 不许有票凭空消失。"""
    snapshot = _scan(
        quotes={
            "600001": {"name": "甲", "last": 11.0, "prev_close": 10.0, "bid1_vol": 5_000_000.0},
            "600002": {"name": "乙", "last": 10.9, "prev_close": 10.0, "bid1_vol": 1_000.0},
        },
        sealed_codes=["600001"],
    )
    board = ana.seal_board(snapshot)
    c = board["counts"]
    assert c["sealed"] + c["approaching"] + c["unavailable"] == c["candidates"]


def test_seal_board_median_ignores_none() -> None:
    """没有买一量的票不进中位数 —— 用 0 顶替会把中位数拉低成假信号。"""
    snapshot = _scan(
        quotes={
            "600001": {"name": "甲", "last": 11.0, "prev_close": 10.0, "bid1_vol": 10_000_000.0},
            "600002": {"name": "乙", "last": 11.0, "prev_close": 10.0, "bid1_vol": 30_000_000.0},
            "600003": {"name": "丙", "last": 11.0, "prev_close": 10.0, "bid1_vol": None},
        },
        sealed_codes=["600001", "600002", "600003"],
    )
    board = ana.seal_board(snapshot)
    assert board["median_bid_lots"] == pytest.approx(200_000.0)  # 10万手、30万手 → 中位 20万手


def test_seal_board_empty_snapshot() -> None:
    board = ana.seal_board({})
    assert board["counts"] == {"sealed": 0, "approaching": 0, "unavailable": 0, "candidates": 0}
    assert board["median_bid_lots"] is None
    assert board["total_bid_lots"] == 0.0


# ───────────────────────── detect_weakening ─────────────────────────


def test_detect_weakening_flags_thin_seals() -> None:
    board = _board(sealed=[("600001", "厚", 300_000.0), ("600002", "薄", 10_000.0)])
    weak = ana.detect_weakening(board)
    assert [r["code"] for r in weak] == ["600002"]
    assert "10,000" in weak[0]["reason"]


def test_detect_weakening_returns_thinnest_first() -> None:
    board = _board(
        sealed=[("600001", "甲", 40_000.0), ("600002", "乙", 5_000.0), ("600003", "丙", 20_000.0)]
    )
    assert [r["code"] for r in ana.detect_weakening(board)] == ["600002", "600003", "600001"]


def test_detect_weakening_none_lots_is_not_thin() -> None:
    """封着但取不到量 ≠ 薄。说成「薄」是编造，说成「取不到」才是事实。"""
    board = _board(sealed=[("600001", "甲", None)])
    weak = ana.detect_weakening(board)
    assert len(weak) == 1
    assert "取不到" in weak[0]["reason"]
    assert "低于" not in weak[0]["reason"]


def test_detect_weakening_none_sorts_last() -> None:
    board = _board(sealed=[("600001", "甲", None), ("600002", "乙", 100.0)])
    assert [r["code"] for r in ana.detect_weakening(board)] == ["600002", "600001"]


def test_detect_weakening_respects_threshold() -> None:
    board = _board(sealed=[("600001", "甲", 60_000.0)])
    assert ana.detect_weakening(board) == []
    assert len(ana.detect_weakening(board, threshold_lots=100_000.0)) == 1


def test_detect_weakening_is_a_single_round_judgement() -> None:
    """**本函数只看绝对值，不看衰减速度** —— 衰减要跨轮比，属于 ``SealTracker``。

    这条钉住分工：把「封单掉了 70%」写成单轮判据，等于要求函数有记忆。
    """
    import inspect

    src = inspect.getsource(ana.detect_weakening)
    assert "seal_weaken" in src, "docstring 必须指向跨轮判据的正确归属"
    assert "SEAL_WEAKEN_RATIO" not in src, "不许在单轮函数里引用跨轮比率常量"


# ───────────────────────── board_health ─────────────────────────


def test_board_health_none_when_no_sample() -> None:
    """没封板样本 ⇒ ``board_strength is None``，**不是 0**。

    0 会被读成「封板情绪极低」，而事实是「今天根本没有封板的票可看」。
    """
    health = ana.board_health(ana.seal_board({}))
    assert health["board_strength"] is None
    assert health["label"] == "没样本"
    assert "没有" in health["explain"] or "无法" in health["explain"]


def test_board_health_uses_median_not_mean() -> None:
    """一只 105 万手的票不许把指标拉爆。

    实测三峡新材 105 万手、其余 40 只多在 3~20 万手：
    均值会被单只极值主导，中位数才是「典型的一只封成什么样」。
    """
    board = _board(
        sealed=[
            ("600293", "三峡新材", 1_050_299.24),
            ("600001", "甲", 100_000.0),
            ("600002", "乙", 100_000.0),
            ("600003", "丙", 100_000.0),
        ]
    )
    health = ana.board_health(board)
    assert health["median_bid_lots"] == pytest.approx(100_000.0)
    assert health["board_strength"] == pytest.approx(2.0)  # 100,000 / 50,000


def test_board_health_clamps_at_three() -> None:
    """超过 3 倍厚线就截断 —— 不截断会让指标在极值上失去区分度。"""
    board = _board(sealed=[("600001", "甲", 10_000_000.0)])
    health = ana.board_health(board)
    assert health["board_strength"] == 3.0
    assert "截断" in health["explain"]


def test_board_health_labels_are_monotonic() -> None:
    """档位必须随强度单调 —— ``薄 < 偏薄 < 正常 < 厚`` 不许乱序。"""
    got = []
    for lots in (10_000.0, 30_000.0, 80_000.0, 200_000.0):
        got.append(ana.board_health(_board(sealed=[("600001", "甲", lots)]))["label"])
    assert got == ["薄", "偏薄", "正常", "厚"]


def test_board_health_explain_names_the_numbers() -> None:
    """一句话里必须有「几只、中位多少手、厚线多少」—— 认不出数字就没法核对。"""
    board = _board(sealed=[("600001", "甲", 60_000.0), ("600002", "乙", 40_000.0)])
    explain = ana.board_health(board)["explain"]
    assert "2 只" in explain
    assert "50,000 手" in explain
    assert "1.00" in explain


def test_board_health_never_says_buy() -> None:
    """R76 红线延续：分析层不给祈使动作。"""
    board = _board(sealed=[("600001", "甲", 300_000.0)])
    text = str(ana.board_health(board))
    for word in ("买入", "卖出", "加仓", "抄底", "建议"):
        assert word not in text
