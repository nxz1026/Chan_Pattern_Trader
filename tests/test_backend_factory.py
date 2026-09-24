"""``cpt.adapters.backend_factory`` 测试 —— 后端档位解析（R16-4）。

**刻意不断言「auto 一定选中哪个后端」**：czsc 是可选依赖，CI 只装
``requirements-dev.txt``（不含 czsc），本地 dev venv 装了 czsc。若断言具体类型，
CI 与本地就会分叉。这里只断言：

- 档位名字的解析规则与报错；
- 两个后端都满足 ``ChanlunBackend`` 协议（能算出结构）；
- ``czsc`` 档在未安装时**响亮报错**、``auto`` 档**静默回落**（用 monkeypatch
  伪造两种环境，不依赖本机是否真装了 czsc）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.adapters.backend_factory import (
    BACKEND_CHOICES,
    DEFAULT_BACKEND,
    UnknownBackendError,
    resolve_backend,
)
from cpt.adapters.czsc_chanlun import CzscNotInstalledError
from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.adapters.reference_chanlun import ReferenceChanlunConfig
from cpt.domain.models import CanonicalBar

_CONFIG = ReferenceChanlunConfig()

_MS_PER_DAY = 24 * 3600 * 1000


def _zigzag_bars(n: int = 120) -> list[CanonicalBar]:
    """正弦式振荡 K 线 —— 保证两个后端都能算出分型/笔。"""
    import math

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
    assert BACKEND_CHOICES == ("auto", "czsc", "native")
    assert DEFAULT_BACKEND in BACKEND_CHOICES


def test_native_is_forced_backend() -> None:
    assert isinstance(resolve_backend("native"), NativeChanlunBackend)


def test_name_is_case_and_space_insensitive() -> None:
    assert isinstance(resolve_backend("  NATIVE "), NativeChanlunBackend)


def test_unknown_backend_raises_with_actionable_message() -> None:
    with pytest.raises(UnknownBackendError) as excinfo:
        resolve_backend("rust")
    assert "auto/czsc/native" in str(excinfo.value)


def test_czsc_forces_error_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``czsc`` 档：环境没装就抛错，**不静默回落**（否则用户以为在用 czsc）。"""

    def _boom() -> Any:
        raise CzscNotInstalledError('czsc 后端需要可选依赖，请安装：pip install -e ".[chan]"')

    monkeypatch.setattr("cpt.adapters.czsc_chanlun._import_czsc", _boom)
    with pytest.raises(CzscNotInstalledError):
        resolve_backend("czsc")


def test_auto_falls_back_to_native_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` 档：环境没装 czsc 时回落 native（可用性优先，CI 走这条）。"""

    def _boom() -> Any:
        raise CzscNotInstalledError("not installed")

    monkeypatch.setattr("cpt.adapters.czsc_chanlun._import_czsc", _boom)
    assert isinstance(resolve_backend("auto"), NativeChanlunBackend)


def test_auto_selects_czsc_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` 档：环境装了 czsc 就选 czsc（生产意图，决策 A1）。"""
    czsc = pytest.importorskip("czsc")
    del czsc
    backend = resolve_backend("auto")
    assert type(backend).__name__ == "CzscChanlunBackend"


def test_min_bi_len_reaches_czsc_backend() -> None:
    pytest.importorskip("czsc")
    backend = resolve_backend("czsc", min_bi_len=10)
    assert getattr(backend, "min_bi_len", None) == 10


def test_min_bi_len_defaults_to_upstream_six() -> None:
    pytest.importorskip("czsc")
    assert getattr(resolve_backend("czsc"), "min_bi_len", None) == 6


def test_native_backend_satisfies_contract() -> None:
    """native 后端必须满足协议（能算出结构、不抛）。"""
    result = resolve_backend("native").compute_structures(_zigzag_bars(), _CONFIG)
    assert len(result.fx_list) > 0
    assert len(result.bi_list) > 0


def test_czsc_backend_satisfies_contract() -> None:
    pytest.importorskip("czsc")
    result = resolve_backend("czsc").compute_structures(_zigzag_bars(), _CONFIG)
    assert len(result.fx_list) > 0
    assert len(result.bi_list) > 0


def test_web_cli_exposes_backend_flag() -> None:
    """两个 web 入口都要有 ``--backend``（否则看板只能跑自研后端）。"""
    from cpt.web.__main__ import build_parser as build_crypto_parser
    from cpt.web.a_share import main as _ashare_main  # noqa: F401  (import 即注册)

    args = build_crypto_parser().parse_args([])
    assert args.backend == DEFAULT_BACKEND

    for name in BACKEND_CHOICES:
        assert build_crypto_parser().parse_args(["--backend", name]).backend == name
