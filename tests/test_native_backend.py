from __future__ import annotations

import json
import subprocess
import sys

from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.adapters.reference_chanlun import ReferenceChanlunConfig
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar


def _bar(index: int, *, high: float, low: float) -> object:
    open_time = 1_700_000_000_000 + index * 60_000
    return make_canonical_bar(
        open_time=open_time,
        open=high,
        high=high,
        low=low,
        close=high,
        close_time=open_time + 59_999,
    )


def test_native_backend_produces_fractals_and_bis() -> None:
    bars = [
        _bar(0, high=100, low=99),
        _bar(1, high=110, low=108),
        _bar(2, high=105, low=104),
        _bar(3, high=112, low=110),
        _bar(4, high=107, low=106),
    ]
    backend = NativeChanlunBackend()
    result = backend.compute_structures(bars, ReferenceChanlunConfig())
    assert len(result.fx_list) >= 2
    assert len(result.bi_list) >= 1
    assert result.fx_list[0].kind in {"top", "bottom"}


def _containment_bars() -> list:
    """含包含关系的序列：bar1 被 bar0 包含，因此合并后 K 线数 < 原始根数。

    这是 2026-09-23 真实行情 bug 的最小复现形态——没有包含关系时合并 K 线
    与原始 K 线一一对应，时间映射错误会被掩盖。
    """
    return [
        _bar(0, high=120, low=100),
        _bar(1, high=115, low=105),  # 被 bar0 包含
        _bar(2, high=130, low=125),
        _bar(3, high=118, low=112),
        _bar(4, high=135, low=128),
        _bar(5, high=122, low=115),
        _bar(6, high=140, low=132),
    ]


def test_native_backend_maps_structures_back_to_raw_bar_indices() -> None:
    """回归（2026-09-23）：包含处理后必须把笔/中枢时间映射回**原始** K 线下标。

    旧实现拿分型的 ``end_time``（合并 K 线的 close_time）去匹配原始 K 线的
    ``open_time``，永远匹配不上 → 所有 ``end_bar`` 退化成 0，产出 start>end 的
    假结构。该断言在只有计数断言时不会失败（数量对、时间错）。
    """
    bars = _containment_bars()
    result = NativeChanlunBackend().compute_structures(bars, ReferenceChanlunConfig())
    raw_count = len(bars)
    assert result.bi_list, "包含序列应至少产出一条笔"
    for bi in result.bi_list:
        assert 0 <= bi.start_bar < raw_count, f"start_bar 越界: {bi.start_bar}"
        assert 0 <= bi.end_bar < raw_count, f"end_bar 越界: {bi.end_bar}"
        assert bi.start_bar < bi.end_bar, f"笔起止倒置: {bi.start_bar} -> {bi.end_bar}"
    for zs in result.zs_list:
        assert 0 <= zs.start_bar < raw_count, f"中枢 start_bar 越界: {zs.start_bar}"
        assert 0 <= zs.end_bar < raw_count, f"中枢 end_bar 越界: {zs.end_bar}"
        assert zs.start_bar <= zs.end_bar, f"中枢起止倒置: {zs.start_bar} -> {zs.end_bar}"
    # 原始 K 线下标必须落在真实 open_time 上（映射语义正确，而非凑数）
    open_times = {bar.open_time for bar in bars}
    for bi in result.bi_list:
        assert bars[bi.start_bar].open_time in open_times
        assert bars[bi.end_bar].open_time in open_times


def test_replay_cli_native_backend(tmp_path) -> None:
    fixture = tmp_path / "case.json"
    bars = [
        _bar(0, high=100, low=99),
        _bar(1, high=110, low=108),
        _bar(2, high=105, low=104),
        _bar(3, high=112, low=110),
        _bar(4, high=107, low=106),
    ]
    fixture.write_text(
        json.dumps(
            {
                "name": "native",
                "bars": [
                    {
                        "open_time": bar.open_time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "close_time": bar.close_time,
                        "volume": 0.0,
                        "quote_volume": 0.0,
                        "trade_count": 0,
                        "taker_buy_base_volume": 0.0,
                        "taker_buy_quote_volume": 0.0,
                        "is_closed": True,
                    }
                    for bar in bars
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "cpt.application.replay",
            "--input",
            str(fixture),
            "--output",
            str(output),
            "--backend",
            "native",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["data"]["fractals"]
    assert payload["data"]["bis"]
    config = RulesConfig()
    assert config.levels
