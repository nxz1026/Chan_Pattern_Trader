from __future__ import annotations

import math
import urllib.error
import urllib.request

import pytest
from cpt.application.dashboard_parity import build_parity_snapshot
from cpt.application.replay import _infer_interval_ms
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar

from tests.conftest import served


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
    # NaN 不是合法 JSON：适配器必须回 500，而不是写出非法 JSON 体
    with served(lambda: {"value": math.nan}) as base:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/api/dashboard/snapshot")
        assert error.value.code == 500

    def _boom() -> dict[str, object]:
        raise RuntimeError("boom")

    # provider 自身抛异常同样是 500（不能把栈泄给客户端）
    with served(_boom) as base:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/api/dashboard/snapshot")
        assert error.value.code == 500
