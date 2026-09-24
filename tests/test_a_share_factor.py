"""按需取因子测试（R17-3）。**全程离线**：注入假取数函数，绝不碰腾讯/DB。

这组测试的重点是**边界**而不是"能拉到数据"：
- 默认必须**不**联网（踩过：默认开导致跑测试写脏了生产库）；
- ``unsupported``（腾讯没这标的后复权）与 ``network``（这次没连上）必须分开 ——
  前者重试无意义，后者重试有意义，混成一个会让用户白点；
- 冷却生效，避免连点打腾讯；
- 拉取失败**不能**让看板 500。
"""

from __future__ import annotations

from typing import Any

import pytest
from cpt.adapters.a_share_factor import (
    FactorRow,
    FactorUnavailableError,
    OnDemandFactorFetcher,
    build_factor_rows,
    fetch_factor_rows,
)
from cpt.application import a_share_snapshot as snap
from cpt.domain.models import CanonicalBar


def _Bar(day_index: int, close: float) -> CanonicalBar:
    """一根真实 ``CanonicalBar``（2024-01-01 起，每天一根）。

    不用残缺的 duck type：``validate_ashare_bars`` 会读 open/high/low，缺字段会
    让快照在校验阶段就变成 ``invalid_bars``，测不到因子路径（踩过）。
    """
    open_ms = 1_704_067_200_000 + day_index * 86_400_000
    return CanonicalBar(
        open_time=open_ms,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        close_time=open_ms + 86_400_000 - 1,
        quote_volume=1.0,
        trade_count=1,
        taker_buy_base_volume=0.5,
        taker_buy_quote_volume=0.5,
        is_closed=True,
    )


# ------------------------------------------------------------------ 纯函数


def test_build_factor_rows_is_hfq_over_raw() -> None:
    raw = [_Bar(0, 10.0), _Bar(1, 20.0)]
    hfq = [_Bar(0, 15.0), _Bar(1, 30.0)]
    rows = build_factor_rows("600519", raw, hfq)
    assert [row.trade_date for row in rows] == ["2024-01-01", "2024-01-02"]
    assert [row.hfq_factor for row in rows] == [1.5, 1.5]
    assert all(row.code == "600519" for row in rows)


def test_build_factor_rows_keeps_only_overlapping_dates() -> None:
    """因子必须同源同对：只有一边有的交易日**不能**算，否则因子是错的。"""
    raw = [_Bar(0, 10.0), _Bar(1, 20.0), _Bar(2, 30.0)]
    hfq = [_Bar(1, 30.0), _Bar(2, 45.0), _Bar(3, 60.0)]
    rows = build_factor_rows("600519", raw, hfq)
    assert [row.trade_date for row in rows] == ["2024-01-02", "2024-01-03"]
    assert [row.hfq_factor for row in rows] == [1.5, 1.5]


def test_build_factor_rows_skips_non_positive_raw_close() -> None:
    raw = [_Bar(0, 0.0), _Bar(1, 20.0)]
    hfq = [_Bar(0, 15.0), _Bar(1, 30.0)]
    rows = build_factor_rows("600519", raw, hfq)
    assert [row.trade_date for row in rows] == ["2024-01-02"]


def test_fetch_factor_rows_rejects_bad_days() -> None:
    with pytest.raises(ValueError):
        fetch_factor_rows("600519", days=0)
    with pytest.raises(ValueError):
        fetch_factor_rows("600519", days=99_999)


# ------------------------------------------------------------------ 失败分类


def test_fetch_marks_unsupported_separately_from_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """腾讯无该标的后复权 → ``unsupported``；网络失败 → ``network``。"""
    from cpt.adapters import a_share_factor as mod
    from cpt.adapters.a_share_public import (
        AShareAdjustUnsupportedError,
        ASharePublicError,
        TencentKlineClient,
    )

    real_fetch = TencentKlineClient.fetch_daily_bars

    def _fake(self: TencentKlineClient, code: str, *, limit: int = 180) -> Any:
        if self.adjust == "bfq":
            return (_Bar(0, 10.0),)
        raise AShareAdjustUnsupportedError("sz301689 无 hfqday 数据")

    monkeypatch.setattr(TencentKlineClient, "fetch_daily_bars", _fake)
    with pytest.raises(FactorUnavailableError) as excinfo:
        mod.fetch_factor_rows("301689")
    assert str(excinfo.value).startswith("unsupported:")

    def _net_fail(self: TencentKlineClient, code: str, *, limit: int = 180) -> Any:
        if self.adjust == "bfq":
            return (_Bar(0, 10.0),)
        raise ASharePublicError("腾讯日线不可达")

    monkeypatch.setattr(TencentKlineClient, "fetch_daily_bars", _net_fail)
    with pytest.raises(FactorUnavailableError) as excinfo2:
        mod.fetch_factor_rows("600519")
    assert str(excinfo2.value).startswith("network:")

    monkeypatch.setattr(TencentKlineClient, "fetch_daily_bars", real_fetch)


def test_fetch_reports_no_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    from cpt.adapters.a_share_public import TencentKlineClient

    def _fake(self: TencentKlineClient, code: str, *, limit: int = 180) -> Any:
        # 两个序列日期完全不重叠 → 算不出任何因子
        return (_Bar(0, 10.0),) if self.adjust == "bfq" else (_Bar(9, 10.0),)

    monkeypatch.setattr(TencentKlineClient, "fetch_daily_bars", _fake)
    with pytest.raises(FactorUnavailableError) as excinfo:
        fetch_factor_rows("600519")
    assert str(excinfo.value).startswith("no_overlap:")


# ------------------------------------------------------------------ 冷却


def _stub_fetcher(
    *results: Any,
    cooldown: float = 600.0,
    clock: Any = None,
) -> tuple[OnDemandFactorFetcher, list[str]]:
    calls: list[str] = []

    def _fetch(code: str, days: int) -> tuple[FactorRow, ...]:
        calls.append(code)
        outcome = results[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ticks = clock if clock is not None else iter([0.0, 1.0, 2.0, 1000.0, 2000.0])
    fetcher = OnDemandFactorFetcher(
        cooldown_seconds=cooldown, clock=lambda: next(ticks), fetch=_fetch
    )
    return fetcher, calls


def test_cooldown_blocks_second_attempt() -> None:
    rows = (FactorRow("600519", "2024-01-01", 1.5),)
    fetcher, calls = _stub_fetcher(rows, rows)
    first = fetcher.ensure("600519")
    second = fetcher.ensure("600519")
    assert first.fetched is True and first.rows == 1
    assert second.fetched is False and second.reason == "cooldown"
    assert calls == ["600519"]  # 第二次根本没打腾讯


def test_cooldown_expires() -> None:
    rows = (FactorRow("600519", "2024-01-01", 1.5),)
    ticks = iter([0.0, 700.0, 700.0])  # 第二次已过 600s 冷却
    fetcher, calls = _stub_fetcher(rows, rows, clock=ticks)
    fetcher.ensure("600519")
    again = fetcher.ensure("600519")
    assert again.fetched is True
    assert calls == ["600519", "600519"]


def test_cooldown_zero_never_blocks() -> None:
    rows = (FactorRow("600519", "2024-01-01", 1.5),)
    fetcher, calls = _stub_fetcher(rows, rows, cooldown=0.0)
    fetcher.ensure("600519")
    fetcher.ensure("600519")
    assert calls == ["600519", "600519"]


def test_ensure_folds_failures_into_result() -> None:
    """失败**不抛异常**（否则看板 500）。"""
    fetcher, _ = _stub_fetcher(FactorUnavailableError("unsupported:no hfqday"))
    outcome = fetcher.ensure("301689")
    assert outcome.fetched is False
    assert outcome.reason == "unsupported"
    assert outcome.detail == "no hfqday"


def test_ensure_folds_unexpected_exceptions() -> None:
    fetcher, _ = _stub_fetcher(RuntimeError("boom"))
    outcome = fetcher.ensure("600519")
    assert outcome.fetched is False
    assert outcome.reason == "error"
    assert "boom" in (outcome.detail or "")


def test_ensure_empty_rows_is_not_success() -> None:
    fetcher, _ = _stub_fetcher(())
    outcome = fetcher.ensure("600519")
    assert outcome.fetched is False
    assert outcome.reason == "empty"


# ------------------------------------------------------------------ 安全默认


def test_ensurer_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归**：默认必须关。

    踩过：早期默认"开"，跑一次 pytest 就让缺因子测试真的去腾讯拉了 600519 的
    800 行因子并写进生产库（因子表 94 → 95 只）。
    """
    monkeypatch.delenv(snap.ENV_ONDEMAND_FACTOR, raising=False)
    assert snap.factor_ensurer_from_env() is None
    # 生产入口显式打开
    assert snap.factor_ensurer_from_env(default=True) is not None


def test_ensurer_env_can_disable_even_when_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(snap.ENV_ONDEMAND_FACTOR, value)
        assert snap.factor_ensurer_from_env(default=True) is None, value


def test_ensurer_env_can_enable_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(snap.ENV_ONDEMAND_FACTOR, "1")
    assert snap.factor_ensurer_from_env() is not None


def test_bare_build_call_does_not_use_ondemand(monkeypatch: pytest.MonkeyPatch) -> None:
    """不给 ``ensure_factors`` 时，缺因子路径**不得**触发任何补取。"""
    from cpt.adapters.a_share_local import AShareNoFactorError

    touched: list[str] = []
    monkeypatch.setattr(snap, "_ensure_factors_and_persist", lambda code: touched.append(code))

    class _NoFactorClient:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise AShareNoFactorError("全缺因子")

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot("600519", client=_NoFactorClient())
    assert snapshot["runtime"]["degraded_reason"] == "no_factor"
    assert touched == []


# ------------------------------------------------------------------ 端到端（注入）


def _fake_client(bars: list[Any], skipped: tuple[str, ...] = ()) -> Any:
    class _Result:
        def __init__(self) -> None:
            self.bars = tuple(bars)
            self.skipped_no_factor = skipped

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1 and skipped:
                return _Result()
            return _Result()

        def close(self) -> None: ...

    return _Client()


def test_ondemand_result_is_attached_to_snapshot() -> None:
    """拉了就把结果挂到快照上（审计：这次到底拉没拉、拉了多少）。"""
    outcome = snap.FactorEnsureResult(code="600519", fetched=True, rows=800)
    snapshot = snap.empty_ashare_snapshot("600519", "no_factor")
    snap._attach_factor_fetch(snapshot, outcome)
    assert snapshot["data_quality"]["factor_fetch"]["fetched"] is True
    assert snapshot["data_quality"]["factor_fetch"]["rows"] == 800
    assert snapshot["runtime"]["factor_fetch"]["rows"] == 800


def test_ondemand_result_none_attaches_nothing() -> None:
    snapshot = snap.empty_ashare_snapshot("600519", "no_factor")
    snap._attach_factor_fetch(snapshot, None)
    assert "factor_fetch" not in snapshot["data_quality"]


def test_reason_mapping() -> None:
    """reason 必须让用户能据此行动：unsupported 重试无意义，network 有意义。"""
    mk = snap.FactorEnsureResult
    assert snap._reason_for_failure(mk("x", False, 0, "unsupported")) == "no_factor_unsupported"
    assert snap._reason_for_failure(mk("x", False, 0, "cooldown")) == "no_factor_cooldown"
    assert (
        snap._reason_for_failure(mk("x", False, 0, "network")) == "no_factor_fetch_failed:network"
    )
    assert snap._reason_for_failure(mk("x", False, 0, "persist_failed")) == (
        "no_factor_fetch_failed:persist_failed"
    )
    assert snap._reason_for_failure(mk("x", False, 0, None)) == "no_factor"
    assert snap._reason_for_failure(None) == "no_factor"


def test_partial_factor_gap_triggers_ondemand() -> None:
    """**部分**缺因子也要补 —— 空洞比整段缺更隐蔽（画出来的笔是错的）。"""
    triggered: list[str] = []
    bars = [_Bar(0, 10.0)]

    class _Client:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            return type("R", (), {"bars": tuple(bars), "skipped_no_factor": ("2024-01-02",)})()

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot(
        "600519",
        client=_Client(),
        ensure_factors=lambda code: (
            triggered.append(code)
            or snap.FactorEnsureResult(code=code, fetched=False, rows=0, reason="unsupported")
        ),
    )
    assert triggered == ["600519"]
    assert snapshot["runtime"]["factor_fetch"]["reason"] == "unsupported"


def test_fetcher_singleton_is_reset_by_helper() -> None:
    """冷却状态必须跨请求保留（单例），测试可显式重置。"""
    snap.reset_factor_fetcher()
    first = snap._fetcher()
    assert snap._fetcher() is first
    snap.reset_factor_fetcher()
    assert snap._fetcher() is not first
    snap.reset_factor_fetcher()


def test_unsupported_is_permanent_and_not_subject_to_cooldown() -> None:
    """**回归**：``unsupported`` 是永久结论，不能被冷却降级成 ``cooldown``。

    踩过：同一只票第一次报 ``unsupported``、第二次报 ``cooldown``，用户看到的
    原因随请求变化；审计脚本也因此不可重复（同一断言时而通过时而失败）。
    """
    fetcher, calls = _stub_fetcher(FactorUnavailableError("unsupported:no hfqday"))
    first = fetcher.ensure("301689")
    second = fetcher.ensure("301689")
    assert first.reason == "unsupported" and first.detail == "no hfqday"
    assert second.reason == "unsupported" and second.detail == "no hfqday"
    assert calls == ["301689"]  # 只问了一次腾讯


def test_retryable_failure_still_uses_cooldown() -> None:
    """可重试的失败（网络）仍然走冷却 —— 与 unsupported 区别对待。"""
    fetcher, calls = _stub_fetcher(
        FactorUnavailableError("network:unreachable"),
        FactorUnavailableError("network:unreachable"),
    )
    assert fetcher.ensure("600519").reason == "network"
    assert fetcher.ensure("600519").reason == "cooldown"
    assert calls == ["600519"]
