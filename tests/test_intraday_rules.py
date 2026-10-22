"""盘中判据（CPT 侧）单测。**纯函数，零 IO。**

有一条特殊测试：``test_matches_emotion_core_implementation`` 把本模块与
emotion-core 的 ``algorithms/intraday.py`` 逐条对账 —— 两份实现是有意的
（口径 vs 判据），但不许漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cpt.domain import intraday_rules as ir

#: emotion-core 仓库位置（对账用；不存在则跳过那条测试）。
_EMOTION_CORE = Path("/home/ubuntu/DSH/emotion-core/src")


# ───────────────────────── 板块 ─────────────────────────


@pytest.mark.parametrize(
    ("code", "pct"),
    [
        ("600519", 10.0),
        ("000002", 10.0),
        ("002415", 10.0),
        ("300750", 20.0),
        ("301029", 20.0),
        ("688981", 20.0),
        ("689009", 20.0),
        ("830799", 30.0),
        ("430047", 30.0),
        ("920296", 30.0),
    ],
)
def test_board_pct_limit(code: str, pct: float) -> None:
    """按前缀判，**不是 ±10% 一刀切**。20%/30% 板会永远漏判。"""
    assert ir.board_pct_limit(code) == pct


def test_board_of_naming() -> None:
    assert ir.board_of("600519") == "main"
    assert ir.board_of("300750") == "growth"
    assert ir.board_of("830799") == "bse"
    assert ir.board_of("600519", name="*ST康佳") == "st_main"
    assert ir.board_of("300750", name="ST某某") == "st_growth"


def test_main_board_st_is_five_percent() -> None:
    """ST 主板 5%。只能靠名字判 —— 代码里没有 ST 信息。"""
    assert ir.board_pct_limit("600519", name="*ST康佳") == 5.0


def test_is_st_case_insensitive() -> None:
    assert ir.is_st_name("*st康佳") is True
    assert ir.is_st_name("贵州茅台") is False
    assert ir.is_st_name(None) is False
    assert ir.is_st_name("") is False


def test_unknown_st_is_optimistic() -> None:
    """不传名字按非 ST —— 宁可算出偏高的涨停价（少报）而不是偏低的（错报）。"""
    assert ir.board_pct_limit("600519") == 10.0


def test_is_main_board() -> None:
    assert ir.is_main_board("600519") is True
    assert ir.is_main_board("002415") is True
    assert ir.is_main_board("300750") is False
    assert ir.is_main_board("688981") is False


# ───────────────────────── 涨停价 ─────────────────────────


def test_limit_up_price_rounds_half_up_to_cent() -> None:
    """四舍五入到分。``10.0 * 1.1`` 在浮点里是 ``11.000000000000002``。"""
    assert ir.limit_up_price("600519", 10.0) == 11.0


@pytest.mark.parametrize(
    ("code", "pre_close", "expected"),
    [
        ("600519", 3.03, 3.33),
        ("600519", 7.77, 8.55),
        ("600519", 12.34, 13.57),
        ("600519", 1.01, 1.11),
        # 实测：利亚德 300296（创业板 20%）昨收 4.69 → 涨停 5.63
        ("300296", 4.69, 5.63),
        # 实测：600293 三峡新材 昨收 4.32 → 涨停 4.75
        ("600293", 4.32, 4.75),
    ],
)
def test_limit_up_price_examples(code: str, pre_close: float, expected: float) -> None:
    assert ir.limit_up_price(code, pre_close) == pytest.approx(expected, abs=0.005)


def test_limit_up_price_growth_board() -> None:
    """创业板 20%：昨收 10 涨到 12 才是涨停。"""
    assert ir.limit_up_price("300750", 10.0) == 12.0


def test_bse_920_segment_is_thirty_percent() -> None:
    """**920 是新号段**（北交所 6 位统一代码），不在 8/4 前缀里。

    漏判会掉进兜底按 10% 算：920296 昨收 11.45，10% 涨停价 12.60、
    30% 涨停价 14.89 —— 差 2.29 元，足以把「还差两块」误判成「已封板」。
    这条是在实跑真实涨幅榜时发现 920296 被判成 main 之后补的。
    """
    assert ir.board_of("920296") == "bse"
    assert ir.board_pct_limit("920296") == 30.0
    assert ir.limit_up_price("920296", 11.45) == pytest.approx(14.89, abs=0.01)


def test_limit_up_price_bse_thirty_percent() -> None:
    """北交所 30%。实测 920296 N文峰：昨收 11.45，现 36.30（涨 217%，新股首日无限制）。"""
    assert ir.limit_up_price("920296", 11.45) == pytest.approx(14.89, abs=0.01)


def test_limit_up_price_none_without_prev_close() -> None:
    assert ir.limit_up_price("600519", 0.0) is None
    assert ir.limit_up_price("600519", -1.0) is None


# ───────────────────────── 是否涨停 ─────────────────────────


def test_is_at_limit_up_true_at_exact_price() -> None:
    """**`>=` 不是 `>`** —— 成交价可以正好等于涨停价。"""
    assert ir.is_at_limit_up("600519", last=11.0, pre_close=10.0) is True


def test_is_at_limit_up_false_below() -> None:
    assert ir.is_at_limit_up("600519", last=10.99, pre_close=10.0) is False


def test_is_at_limit_up_none_when_undecidable() -> None:
    """停牌返回 **None 不是 False**。混起来会让「没封板」与「没数据」变成一件事。"""
    assert ir.is_at_limit_up("600519", last=None, pre_close=None) is None
    assert ir.is_at_limit_up("600519", last=11.0, pre_close=None) is None
    assert ir.is_at_limit_up("600519", last=None, pre_close=10.0) is None


def test_is_at_limit_up_respects_board_width() -> None:
    """同一价格，20% 板不算涨停。"""
    assert ir.is_at_limit_up("300750", last=11.0, pre_close=10.0) is False
    assert ir.is_at_limit_up("300750", last=12.0, pre_close=10.0) is True


# ───────────────────────── seal_strength ─────────────────────────


def test_seal_strength_reports_degrees_not_just_bool() -> None:
    """要看的是**程度**：封单 3 万手 vs 30 万手是两种局面。"""
    s = ir.seal_strength("600519", last=11.0, pre_close=10.0, bid1_vol=30_000_000.0)
    assert s["at_limit"] is True
    assert s["limit_price"] == 11.0
    assert s["bid1_lots"] == 300_000.0  # 股 → 手
    assert s["distance_pct"] == pytest.approx(0.0, abs=1e-9)


def test_seal_strength_distance_when_not_sealed() -> None:
    s = ir.seal_strength("600519", last=10.5, pre_close=10.0, bid1_vol=1_000_000.0)
    assert s["at_limit"] is False
    # (11.0 - 10.5) / 11.0 * 100
    assert s["distance_pct"] == pytest.approx(4.545, abs=0.01)


def test_seal_strength_missing_bid_is_none_not_zero() -> None:
    """停牌没档位 → None。读成 0 手等于说「买盘消失」，方向完全相反。"""
    s = ir.seal_strength("600519", last=11.0, pre_close=10.0, bid1_vol=None)
    assert s["bid1_lots"] is None


def test_seal_strength_real_form_shape() -> None:
    """真实形态：涨停封死，买一 105 万手，卖五档全空。

    取自 2026-10-21 收盘实测 600293 三峡新材。
    """
    s = ir.seal_strength("600293", last=4.75, pre_close=4.32, bid1_vol=105_029_924.0)
    assert s["at_limit"] is True
    assert s["limit_price"] == 4.75
    assert s["bid1_lots"] == pytest.approx(1_050_299.24, abs=0.01)


# ───────────────────────── 与 emotion-core 对账 ─────────────────────────


def test_matches_emotion_core_implementation() -> None:
    """本模块（口径）与 emotion-core 的 ``algorithms/intraday.py``（判据）不许漂移。

    **两份是有意的**：CPT 这份被采集与分析用，emotion-core 那份被触发判据用。
    但口径只能是同一个 —— 两份对同一只票算出不同的涨停价，是比任何 bug 都坏的
    事（两个系统各说各话，且都拿得出「我这边算的」）。

    这条测试是对账，不是重复实现：它逐条比较两边的公开函数在**同一批输入**上
    的输出。任何一边改了规则（比如把 ST 从 5% 改成 10%），这条必红。
    """
    if not _EMOTION_CORE.is_dir():
        pytest.skip("emotion-core 仓库不在预期位置")

    if str(_EMOTION_CORE) not in sys.path:
        sys.path.insert(0, str(_EMOTION_CORE))
    try:
        from emotion_core.algorithms import intraday as ec  # noqa: PLC0415
    except ImportError:
        pytest.skip("emotion-core 未安装/导入不了")

    codes = [
        "600519",
        "000002",
        "002415",
        "300750",
        "301029",
        "688981",
        "689009",
        "830799",
        "430047",
    ]
    for code in codes:
        assert ir.board_pct_limit(code) == ec.board_pct_limit(code), f"{code} 板块幅度不一致"

    # emotion-core 在 931f43d 把 ``limit_up_price`` 更名 ``limit_price``
    # （它的 caliber 门禁要求「涨停价只许有一个实现」，所以那边改成走
    #  ``utils.price.limit_up_price_cents``）。两个名字都认，少一个就红 ——
    # 对账测试要抓的是口径漂移，不是函数名。
    ec_limit = getattr(ec, "limit_price", None) or getattr(ec, "limit_up_price", None)
    assert ec_limit is not None, "emotion-core 既没有 limit_price 也没有 limit_up_price"

    for pre_close in (1.01, 3.03, 4.69, 7.77, 10.0, 12.34, 31.30):
        for code in ("600519", "300750", "830799"):
            assert ir.limit_up_price(code, pre_close) == ec_limit(code, pre_close), (
                f"{code} 昨收 {pre_close} 的涨停价不一致"
            )

    # 穷举分位 1..3000（0.01~30.00 元）× 11 个代码，覆盖四舍五入的 .5 边界。
    # 只在抽样点上比对，改一位四舍五入（HALF_UP vs HALF_EVEN）就可能漏网。
    bad: list[tuple[str, int, float, float]] = []
    for code in (*codes, "920296", "600001"):
        for cents in range(1, 3001):
            pre_close = cents / 100
            a = ir.limit_up_price(code, pre_close)
            b = ec_limit(code, pre_close)
            if a is not None and b is not None and abs(a - b) > 1e-9:
                bad.append((code, cents, a, b))
    assert bad == [], f"涨停价穷举对账不一致 {len(bad)} 组：{bad[:5]}"

    for name in ("*ST康佳", "贵州茅台", "ST某某", None):
        assert ir.is_st_name(name) == ec.is_st_name(name), f"{name} 的 ST 判定不一致"

    assert ir.SHARES_PER_LOT == ec._SHARES_PER_LOT


def test_constants_match_adr():
    """ADR 0003 D3 的常量。改任一条要同步改那边的表格。"""
    from emotion_core.algorithms import intraday as ec  # noqa: PLC0415

    assert ir.BOARD_PCT_MAIN == 10.0
    assert ir.BOARD_PCT_GROWTH == 20.0
    assert ir.BOARD_PCT_BSE == 30.0
    assert ir.BOARD_PCT_ST == 5.0
    # 与 emotion-core 的判据常量不冲突（那边是触发门槛，这边是口径宽度）
    assert ec.SEAL_BID1_VOL_MIN == 30_000
