"""我的追踪 — 建议点计算的纯函数测试（无 DB）。"""

from __future__ import annotations

from typing import Any

from cpt.application import track_points


def _bi(direction: int, high: float, low: float) -> dict[str, Any]:
    return {"direction": direction, "high": high, "low": low, "level": 1}


def _zs(high: float, low: float) -> dict[str, Any]:
    return {"high": high, "low": low, "level": 1}


def _snap(bis: list, zhongshus: list, *, degraded: bool = False) -> dict[str, Any]:
    return {"overlays": {"bis": bis, "zhongshus": zhongshus}, "degraded": degraded}


def _rec(*, status: str, action: str = "hold", price_ratio: float = 7.06) -> dict[str, Any]:
    return {
        "status": status,
        "action": action,
        "raw_close": 1258.62,
        "price_ratio": price_ratio,
        "signal_type": "first_buy",
        "divergence_status": "not_checked",
    }


# ── 缺关键量时的兜底 ──────────────────────────────────────────────────


def test_returns_empty_sides_when_price_ratio_missing() -> None:
    """没 price_ratio ⇒ 后复权→raw 转换不能做 ⇒ 全 None，**不假装有数**。"""
    out = track_points.compute_points(
        _snap([_bi(-1, 100, 80)], [_zs(100, 80)]), _rec(status="x", price_ratio=None)
    )
    assert out["buy"] == {"reference": None, "confirmed": None}
    assert out["sell"] == {"reference": None, "confirmed": None}
    assert out["stop_loss_reference"] is None


def test_returns_empty_sides_when_price_ratio_zero() -> None:
    out = track_points.compute_points(_snap([], []), _rec(status="x", price_ratio=0))
    assert out["buy"]["reference"] is None
    assert out["buy"]["confirmed"] is None


def test_reference_is_none_with_no_overlays() -> None:
    """无结构数据时，reference 仍是 dict 但 ``price_level=None``，condition 显式说明。"""
    out = track_points.compute_points(
        _snap([], []), _rec(status="structure_ready", price_ratio=1.0)
    )
    ref = out["buy"]["reference"]
    assert ref is not None
    assert ref["price_level"] is None
    assert "无结构数据" in ref["condition"]
    # 哪怕只有 1 笔也能算 —— 说明"无结构"是特指空 overlays
    out2 = track_points.compute_points(
        _snap([_bi(-1, 100, 80)], []), _rec(status="structure_ready", price_ratio=1.0)
    )
    assert out2["buy"]["reference"]["price_level"] is not None


# ── reference 计算（核心） ───────────────────────────────────────────


def test_buy_reference_uses_down_bi_lows_and_zhongshu_lows() -> None:
    """向下笔的 low 才算买点附近；向上笔忽略。中枢下沿优先。"""
    snap = _snap(
        bis=[
            _bi(-1, 110, 90),  # 向下笔：低点 90
            _bi(1, 120, 100),  # 向上笔：忽略
            _bi(-1, 130, 80),  # 向下笔：低点 80（最低）
        ],
        zhongshus=[_zs(140, 100)],  # 中枢下沿 100
    )
    out = track_points.compute_points(snap, _rec(status="structure_ready", price_ratio=7.06))
    # 最低 = 80（向下笔），× 1.02 = 81.6，÷ 7.06 = 11.56
    assert out["buy"]["reference"]["price_level"] == round(80 * 1.02 / 7.06, 2)
    # rationale 应能追溯到哪条是最低
    assert "80" in out["buy"]["reference"]["rationale"]


def test_sell_reference_uses_up_bi_highs_and_zhongshu_highs() -> None:
    snap = _snap(
        bis=[
            _bi(1, 110, 90),  # 向上笔：高点 110
            _bi(-1, 120, 100),  # 向下笔：忽略
            _bi(1, 150, 130),  # 向上笔：高点 150（最高）
        ],
        zhongshus=[_zs(140, 100)],
    )
    out = track_points.compute_points(snap, _rec(status="structure_ready", price_ratio=7.06))
    # 最高 = 150，× 0.97 = 145.5，÷ 7.06 = 20.61
    assert out["sell"]["reference"]["price_level"] == round(150 * 0.97 / 7.06, 2)


def test_buy_takes_min_of_low_candidates() -> None:
    """多个候选里取**最低**（最保守）。"""
    snap = _snap(
        bis=[_bi(-1, 100, 90), _bi(-1, 200, 50), _bi(-1, 300, 80)],
        zhongshus=[],
    )
    out = track_points.compute_points(snap, _rec(status="x", price_ratio=1.0))
    assert out["buy"]["reference"]["price_level"] == round(50 * 1.02, 2)


def test_sell_takes_max_of_high_candidates() -> None:
    """多笔候选里取**最高**（最保守）。"""
    snap = _snap(
        bis=[_bi(1, 100, 90), _bi(1, 200, 150), _bi(1, 300, 180)],
        zhongshus=[],
    )
    out = track_points.compute_points(snap, _rec(status="x", price_ratio=1.0))
    # 最高 = 300，× 0.97 = 291.0
    assert out["sell"]["reference"]["price_level"] == round(300 * 0.97, 2)


def test_only_last_three_bis_considered() -> None:
    """早于 3 笔的低/高点**不**参与取最低/最高。"""
    snap = _snap(
        bis=[
            _bi(-1, 100, 10),  # 太老：应被忽略
            _bi(-1, 100, 20),  # 太老：应被忽略
            _bi(-1, 100, 80),  # 进窗口
            _bi(-1, 100, 90),  # 进窗口
            _bi(-1, 100, 70),  # 进窗口
        ],
        zhongshus=[],
    )
    out = track_points.compute_points(snap, _rec(status="x", price_ratio=1.0))
    # 窗口内最低 = 70
    assert out["buy"]["reference"]["price_level"] == round(70 * 1.02, 2)


def test_zhongshu_low_preferred_over_bi_low() -> None:
    """中枢下沿参与候选，与向下笔低点一起取最低。"""
    snap = _snap(
        bis=[_bi(-1, 200, 50)],  # 向下笔低点 50
        zhongshus=[_zs(300, 80)],  # 中枢下沿 80
    )
    out = track_points.compute_points(snap, _rec(status="x", price_ratio=1.0))
    # 两者都参与，min(50, 80) = 50
    assert out["buy"]["reference"]["price_level"] == round(50 * 1.02, 2)


# ── confirmed（信号确凿才出） ──────────────────────────────────────


def test_buy_confirmed_only_when_status_confirmed_and_action_buy_or_watch() -> None:
    snap = _snap(bis=[_bi(-1, 100, 80)], zhongshus=[_zs(100, 80)])

    # 不满足任一
    assert (
        track_points.compute_points(snap, _rec(status="structure_ready", action="hold"))["buy"][
            "confirmed"
        ]
        is None
    )
    assert (
        track_points.compute_points(snap, _rec(status="confirmed", action="sell"))["buy"][
            "confirmed"
        ]
        is None
    )
    assert (
        track_points.compute_points(snap, _rec(status="invalidated", action="buy"))["buy"][
            "confirmed"
        ]
        is None
    )

    # 满足：confirmed + buy
    out = track_points.compute_points(snap, _rec(status="confirmed", action="buy", price_ratio=1.0))
    assert out["buy"]["confirmed"] is not None
    # confirmed 用最新一笔 / 中枢的下沿（80），÷ 1.0
    assert out["buy"]["confirmed"]["price_level"] == 80.0


def test_sell_confirmed_only_when_status_confirmed_and_action_sell() -> None:
    snap = _snap(bis=[_bi(1, 200, 150)], zhongshus=[_zs(200, 150)])
    assert (
        track_points.compute_points(snap, _rec(status="confirmed", action="buy", price_ratio=1.0))[
            "sell"
        ]["confirmed"]
        is None
    )
    out = track_points.compute_points(
        snap, _rec(status="confirmed", action="sell", price_ratio=1.0)
    )
    assert out["sell"]["confirmed"] is not None
    assert out["sell"]["confirmed"]["price_level"] == 200.0


def test_confirmed_price_differs_from_reference_when_buffers_differ() -> None:
    """confirmed 不加缓冲，reference 加 2%/3% ⇒ 数字应不同。"""
    snap = _snap(bis=[_bi(-1, 110, 80)], zhongshus=[])
    out = track_points.compute_points(snap, _rec(status="confirmed", action="buy", price_ratio=1.0))
    # reference: min(80) × 1.02 = 81.6
    # confirmed: 直接 80
    assert out["buy"]["reference"]["price_level"] == 81.6
    assert out["buy"]["confirmed"]["price_level"] == 80.0


# ── 止损 ─────────────────────────────────────────────────────────────


def test_stop_loss_appears_when_note_mentions_long_term() -> None:
    snap = _snap(bis=[_bi(-1, 100, 80)], zhongshus=[])
    rec = _rec(status="structure_ready", price_ratio=1.0)
    out = track_points.compute_points(snap, rec, note="长期持有")
    # reference 80 × 1.02 = 81.6；stop_loss = 81.6 × 0.95 = 77.52
    assert out["stop_loss_reference"] == round(81.6 * 0.95, 2)


def test_stop_loss_hidden_when_note_omits_long_term() -> None:
    snap = _snap(bis=[_bi(-1, 100, 80)], zhongshus=[])
    rec = _rec(status="structure_ready", price_ratio=1.0)
    out = track_points.compute_points(snap, rec, note="观察")
    assert out["stop_loss_reference"] is None


def test_stop_loss_hidden_when_no_buy_reference() -> None:
    """reference 是 None 时止损也不该凭空冒出来。"""
    out = track_points.compute_points(
        _snap([], []), _rec(status="x", price_ratio=1.0), note="长期持有"
    )
    assert out["stop_loss_reference"] is None


# ── 变异验证（手测，失败得红）─────────────────────────────────────


def test_mutation_uses_max_instead_of_min_in_buy() -> None:
    """⚠️ 变异测试：临时把 ``_buy_reference`` 内部换成 ``max``，断言这会**反**过来失败。

    这条**只**在原实现为 ``min`` 时 PASS（原实现给 50，突变给 90）——
    等于把"我们的测试能抓到这种突变"这件事钉成可重跑的代码。

    ⚠️ 实际变异脚本（``_mut_track.py``）按字节还原、断言 rc≠0，绕开这里
    pytest 缓存带来的假象。
    """
    import cpt.application.track_points as tp

    snap = _snap(bis=[_bi(-1, 100, 50), _bi(-1, 100, 90)], zhongshus=[])
    out = tp.compute_points(snap, _rec(status="x", price_ratio=1.0))
    # 原实现：min(50, 90) × 1.02 = 51.0
    assert out["buy"]["reference"]["price_level"] == round(50 * 1.02, 2)
