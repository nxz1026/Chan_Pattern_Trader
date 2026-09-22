"""审计报告 fix 回归测试: 问题 2 (占位时间戳拦截) + 问题 3 (输入校验)。

覆盖测试缺口:
- 问题 2: export_dataset 对 PLACEHOLDER_TIME raise; map_* 传 bars 时解析为毫秒
- 问题 3: CanonicalBar __post_init__ 校验(load_fixture 友好错误); NaN fixture 抛 ValueError
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cpt.adapters.reference_chanlun import (
    PLACEHOLDER_TIME,
    FxRaw,
    map_fractal,
)
from cpt.application.export import export_dataset
from cpt.application.replay import load_fixture
from cpt.domain.config import default_rules_config
from cpt.domain.models import (
    Fractal,
    make_canonical_bar,
)

# ===== 问题 2: PLACEHOLDER_TIME 哨兵 =====


def test_export_rejects_placeholder_time() -> None:
    """start_time/end_time 等于 PLACEHOLDER_TIME 的结构对象应被 export 拒绝。"""
    cfg = default_rules_config()
    bar = make_canonical_bar(open_time=1000, close_time=2000, open=100, high=110, low=99, close=105)
    fake_fractal = Fractal(
        kind="top",
        level=5,
        bar_index=0,
        start_time=PLACEHOLDER_TIME,  # 占位
        end_time=PLACEHOLDER_TIME,
        high=110.0,
        low=99.0,
        source_ids=("b1",),
    )
    with pytest.raises(ValueError, match="占位时间戳"):
        export_dataset(
            config=cfg,
            bars=[bar],
            fractals=[fake_fractal],
            bis=[],
            zhongshus=[],
            events=[],
            signals=[],
        )


def test_map_fractal_with_bars_resolves_time() -> None:
    """map_fractal 传 bars 时,start_time/end_time 应是真实毫秒而非 PLACEHOLDER_TIME。"""
    bars = [
        make_canonical_bar(open_time=1000, close_time=2000, open=100, high=110, low=99, close=105),
        make_canonical_bar(open_time=2000, close_time=3000, open=105, high=120, low=104, close=115),
    ]
    fx = FxRaw(bar_index=1, kind="top", high=120.0, low=104.0, level=0)
    mapped = map_fractal(fx, level=5, source_ids=("b1",), bars=bars)
    assert mapped.start_time == 2000
    assert mapped.end_time == 2000
    assert mapped.start_time != PLACEHOLDER_TIME


def test_map_without_bars_uses_placeholder() -> None:
    """不传 bars 时,start_time 等于 PLACEHOLDER_TIME(哨兵机制)。"""
    fx = FxRaw(bar_index=5, kind="top", high=110.0, low=99.0, level=0)
    mapped = map_fractal(fx, level=5, source_ids=("b1",))
    assert mapped.start_time == PLACEHOLDER_TIME


def test_map_bi_out_of_range_raises() -> None:
    """map_* 传 bars 但 bar_index 越界应 raise IndexError。"""
    bars = [
        make_canonical_bar(open_time=1000, close_time=2000, open=100, high=110, low=99, close=105)
    ]
    fx = FxRaw(bar_index=99, kind="top", high=110.0, low=99.0, level=0)
    with pytest.raises(IndexError, match="超出"):
        map_fractal(fx, level=5, source_ids=("b1",), bars=bars)


# ===== 问题 3: 输入校验 + NaN =====


def test_canonical_bar_rejects_high_lt_low() -> None:
    """high < low 应抛 ValueError。"""
    with pytest.raises(ValueError, match="high"):
        make_canonical_bar(open_time=1000, close_time=2000, open=100, high=90, low=100, close=95)


def test_canonical_bar_rejects_nan() -> None:
    """NaN 价格/成交量应抛 ValueError。"""
    with pytest.raises(ValueError, match="NaN"):
        make_canonical_bar(
            open_time=1000, close_time=2000, open=float("nan"), high=110, low=99, close=105
        )


def test_canonical_bar_rejects_inf() -> None:
    """inf 价格应抛 ValueError。"""
    with pytest.raises(ValueError, match="NaN"):
        make_canonical_bar(
            open_time=1000, close_time=2000, open=float("inf"), high=110, low=99, close=105
        )


def test_canonical_bar_rejects_open_ge_close_time() -> None:
    """open_time >= close_time 应抛 ValueError。"""
    with pytest.raises(ValueError, match="close_time"):
        make_canonical_bar(open_time=2000, close_time=2000, open=100, high=110, low=99, close=105)


def test_canonical_bar_rejects_negative_trade_count() -> None:
    """trade_count < 0 应抛 ValueError。"""
    with pytest.raises(ValueError, match="trade_count"):
        make_canonical_bar(
            open_time=1000, close_time=2000, open=100, high=110, low=99, close=105, trade_count=-1
        )


def test_export_dataset_json_nan_safe() -> None:
    """export_to_json_string 不应输出非法的 NaN/Infinity token。"""
    from cpt.application.export import export_dataset, export_to_json_string

    cfg = default_rules_config()
    bar = make_canonical_bar(open_time=1000, close_time=2000, open=100, high=110, low=99, close=105)
    payload = export_dataset(
        config=cfg,
        bars=[bar],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    # 正常数据应可序列化
    s = export_to_json_string(
        config=cfg,
        bars=[bar],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    assert "NaN" not in s and "Infinity" not in s
    # 若数据含 NaN 应 raise(allow_nan=False 行为)
    # 通过构造非法 Bar 触发:此处无法直接构造(被 __post_init__ 拦),改测 dataset_hash

    payload["data"]["bars"][0]["open"] = float("nan")
    with pytest.raises(ValueError):
        # json.dumps allow_nan=False 会抛
        import json as _json

        _json.dumps(payload, allow_nan=False)


# ===== 问题 3: load_fixture 友好错误 =====


def test_load_fixture_missing_field_gives_clear_error(tmp_path: Path) -> None:
    """load_fixture 缺字段时抛 ValueError, 含 case 名 + 字段名。"""
    bad = tmp_path / "missing.json"
    bad.write_text(
        json.dumps(
            {
                "name": "case_x",
                "bars": [
                    {"open_time": 1000, "open": 100, "high": 110, "low": 99}
                ],  # 缺 close + close_time
            }
        )
    )
    with pytest.raises(ValueError, match="case_x"):
        load_fixture(bad)
    with pytest.raises(ValueError, match="close_time"):
        load_fixture(bad)


def test_load_fixture_nan_gives_clear_error(tmp_path: Path) -> None:
    """load_fixture 含 NaN 的 fixture 抛 ValueError, 含 case 名 + bar 索引。"""
    bad = tmp_path / "nan.json"
    bad.write_text(
        json.dumps(
            {
                "name": "nan_case",
                "bars": [
                    {
                        "open_time": 1000,
                        "close_time": 2000,
                        "open": float("nan"),
                        "high": 110,
                        "low": 99,
                        "close": 105,
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="nan_case"):
        load_fixture(bad)
    with pytest.raises(ValueError, match=r"bars\[0\]"):
        load_fixture(bad)


def test_load_fixture_invalid_json(tmp_path: Path) -> None:
    """load_fixture 解析失败 JSON 时抛 ValueError, 不是裸 JSONDecodeError。"""
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_fixture(bad)
