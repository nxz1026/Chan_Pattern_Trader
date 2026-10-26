"""``cpt.adapters.backend_factory`` 测试 —— 后端档位解析（R16-4）。

**刻意不断言「auto 一定选中哪个后端」**：czsc 是可选依赖，CI 只装
``requirements-dev.txt``（不含 czsc），本地 dev venv 装了 czsc。若断言具体类型，
CI 与本地就会分叉。这里只断言：

- 档位名字的解析规则与报错；
- ``auto`` 档**静默回落**（用 monkeypatch 伪造环境）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cpt.adapters.backend_factory import (
    BACKEND_CHOICES,
    DEFAULT_BACKEND,
    UnknownBackendError,
    resolve_backend,
)
from cpt.adapters.native_chanlun import NativeChanlunBackend

_MS_PER_DAY = 24 * 3600 * 1000


def _zigzag_bars(n: int = 120) -> list:
    """正弦式振荡 K 线。"""
    import math

    from cpt.domain.models import CanonicalBar

    end_ms = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    bars = []
    for i in range(n):
        close = 10.0 + 3.0 * math.sin(i / 3.0)
        open_time = end_ms - (n - 1 - i) * _MS_PER_DAY
        bars.append(
            CanonicalBar(
                open_time=open_time,
                open=close - 0.2,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=100.0 + i,
                close_time=open_time + _MS_PER_DAY - 1,
                quote_volume=1000.0,
                trade_count=1,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
    return bars


def test_choices_and_default_are_consistent() -> None:
    assert BACKEND_CHOICES == ("auto", "czsc", "native", "reference")
    assert DEFAULT_BACKEND in BACKEND_CHOICES
    assert DEFAULT_BACKEND == "native"


def test_native_is_forced_backend() -> None:
    assert isinstance(resolve_backend("native"), NativeChanlunBackend)


def test_name_is_case_and_space_insensitive() -> None:
    assert isinstance(resolve_backend("  NATIVE "), NativeChanlunBackend)


def test_unknown_backend_raises_with_actionable_message() -> None:
    with pytest.raises(UnknownBackendError) as excinfo:
        resolve_backend("rust")
    assert "auto/czsc/native" in str(excinfo.value)


def test_web_cli_exposes_backend_flag() -> None:
    from cpt.web.__main__ import build_parser as build_crypto_parser

    args = build_crypto_parser().parse_args([])
    assert args.backend == DEFAULT_BACKEND

    for name in BACKEND_CHOICES:
        assert build_crypto_parser().parse_args(["--backend", name]).backend == name
