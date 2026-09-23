from __future__ import annotations

import math
import threading
import urllib.error
import urllib.request

import pytest
from cpt.application.dashboard_parity import build_parity_snapshot
from cpt.application.replay import _infer_interval_ms
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar
from cpt.web.app import serve_snapshot


def test_replay_interval_infers_smallest_positive_gap() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=index * 60000,
            close_time=index * 60000 + 59999,
            open=1,
            high=2,
            low=1,
            close=2,
        )
        for index in range(3)
    )
    assert _infer_interval_ms(bars, RulesConfig(levels=(5, 30))) == 60000


def test_parity_snapshot_uses_explicit_oracle_inputs() -> None:
    item = {"kind": "top", "bar_index": 1}
    result = build_parity_snapshot(fractals=(item,), oracle_fractals=(item,))
    assert result["fractals"]["summary"]["matched"] == 1
    assert result["fractals"]["summary"]["extra"] == 0


def test_http_adapter_rejects_nan_and_returns_provider_error() -> None:
    server = serve_snapshot(lambda: {"value": math.nan})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/dashboard/snapshot")
        assert error.value.code == 500
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    failing = serve_snapshot(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    thread = threading.Thread(target=failing.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{failing.server_port}/api/dashboard/snapshot")
        assert error.value.code == 500
    finally:
        failing.shutdown()
        failing.server_close()
        thread.join(timeout=2)
