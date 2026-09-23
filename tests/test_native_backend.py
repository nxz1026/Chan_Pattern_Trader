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
