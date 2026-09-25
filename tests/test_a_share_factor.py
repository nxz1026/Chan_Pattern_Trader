"""按需取因子测试（R17-3）。**全程离线**：注入假取数函数，绝不碰腾讯/DB。

这组测试的重点是**边界**而不是"能拉到数据"：
- 默认必须**不**联网（踩过：默认开导致跑测试写脏了生产库）；
- ``unsupported``（腾讯没这标的后复权）与 ``network``（这次没连上）必须分开 ——
  前者重试无意义，后者重试有意义，混成一个会让用户白点；
- 冷却生效，避免连点打腾讯；
- 拉取失败**不能**让看板 500。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cpt.adapters.a_share_factor import (
    SOURCE_TX,
    FactorRow,
    FactorUnavailableError,
    OnDemandFactorFetcher,
    build_factor_rows,
    factor_source_ref,
    fetch_factor_rows,
    upsert_factor_rows,
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

    touched: list[str] = []
    monkeypatch.setattr(snap, "_ensure_factors_and_persist", lambda code: touched.append(code))

    class _NoFactorClient:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise snap.AShareNoFactorError("全缺因子")

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


# ------------------------------------------------------------------ 证券名称（R17-3c）


def test_snapshot_carries_security_name() -> None:
    """成功路径把名字/板块挂到 ``market``（前端顶栏与下拉都读它）。"""
    from cpt.adapters.a_share_local import SecurityName

    bars = [_Bar(i, 10.0 + i) for i in range(5)]

    class _Client:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            return type("R", (), {"bars": tuple(bars), "skipped_no_factor": ()})()

        def fetch_security_name(self, code: str) -> Any:
            return SecurityName(code, "贵州茅台", "主板")

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot("600519", client=_Client())
    assert snapshot["market"]["name"] == "贵州茅台"
    assert snapshot["market"]["board"] == "主板"


def test_degraded_snapshot_still_carries_name() -> None:
    """降级时**更**需要名字：否则用户对着空图只有一个六位数字。"""
    from cpt.adapters.a_share_local import SecurityName

    class _Client:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            raise snap.AShareNoFactorError("缺因子")

        def fetch_security_name(self, code: str) -> Any:
            return SecurityName(code, "贵州茅台", "主板")

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot("600519", client=_Client())
    assert snapshot["runtime"]["degraded_reason"] == "no_factor"
    assert snapshot["market"]["name"] == "贵州茅台"


def test_name_lookup_failure_does_not_break_snapshot() -> None:
    """名字是装饰：查名字炸了**绝不能**影响出图（否则一个装饰能挂掉整个看板）。"""
    bars = [_Bar(i, 10.0 + i) for i in range(5)]

    class _Client:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            return type("R", (), {"bars": tuple(bars), "skipped_no_factor": ()})()

        def fetch_security_name(self, code: str) -> Any:
            raise RuntimeError("security_master locked")

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot("600519", client=_Client())
    assert len(snapshot["candles"]) == 5
    assert snapshot["market"]["name"] == ""
    assert snapshot["market"]["board"] is None


def test_client_without_name_method_is_fine() -> None:
    """假客户端没有 ``fetch_security_name`` → 名字缺席，但快照照常。

    这正是我们要的：测试注入的假客户端**不会**偷偷连真库去查名字。
    """
    bars = [_Bar(i, 10.0 + i) for i in range(5)]

    class _Client:
        def fetch_validated_klines(self, *args: Any, **kwargs: Any) -> Any:
            return type("R", (), {"bars": tuple(bars), "skipped_no_factor": ()})()

        def close(self) -> None: ...

    snapshot = snap.build_ashare_snapshot("600519", client=_Client())
    assert len(snapshot["candles"]) == 5
    assert snapshot["market"]["name"] == ""


def test_empty_snapshot_takes_name_as_argument() -> None:
    """``empty_ashare_snapshot`` 收**已查好的**名字 —— 它自己不碰 DB。"""
    snapshot = snap.empty_ashare_snapshot("600519", "no_data", name="贵州茅台", board="主板")
    assert snapshot["market"]["name"] == "贵州茅台"
    assert snapshot["market"]["board"] == "主板"
    # 默认（不传）就是空，不留 None 给前端
    plain = snap.empty_ashare_snapshot("600519", "no_data")
    assert plain["market"]["name"] == ""


# ---------------------------------------------------------------- 写库（D 收口）
#
# 2026-09-25：``upsert_factor_rows`` 原先在 scripts/factor_backfill.py 有一份
# **9 列 + dry_run** 的副本，本模块这份只有 8 列且不支持 dry_run。两份已漂移：
# 脚本写的行带 ``source_ref``、按需路径写的行不带；``source`` 值还各写一个，
# 同一个腾讯接口在库里分裂成 49,730 行 ``tx:fqkline`` 与 7,200 行
# ``tencent_fqkline``（后者已于同日迁移，库里现只有 ``tx:fqkline``）。
# 现在适配器是唯一实现，下面钉住它的**列集合**与 dry_run 语义。


class _FakeCursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log
        self.rowcount = 0

    def executemany(self, sql: str, payload: Any) -> None:
        self._log.append(("executemany", (sql, list(payload))))

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """只记录 SQL 与参数，不连库。``commits`` 用来证明 dry_run 不写。"""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.log)

    def commit(self) -> None:
        self.commits += 1


def test_upsert_writes_nine_columns_including_source_ref() -> None:
    """列集合必须是 9 列（含 ``source_ref``）。

    少一列就是当年那份 8 列实现的回归：按需拉来的行没有溯源串，
    ``WHERE source_ref LIKE '%2024-01-01'`` 之类的反查会漏掉它们。
    """
    conn = _FakeConn()
    rows = (FactorRow("600519", "2024-01-01", 1.5, source_ref="ref-x"),)
    assert upsert_factor_rows(conn, rows, source_url="http://u") == 1

    sql, payload = conn.log[0][1]
    for col in ("code", "trade_date", "hfq_factor", "source", "source_url", "source_ref"):
        assert col in sql, f"{col} 没进 INSERT 列清单"
    assert "as_of" in sql and "available_at" in sql and "fetched_at" in sql
    assert len(payload[0]) == 9
    # 行自带的 source / source_ref 必须落盘（不是硬编码常量）
    assert payload[0][3] == SOURCE_TX
    assert payload[0][5] == "ref-x"
    assert payload[0][4] == "http://u"
    assert conn.commits == 1


def test_upsert_dry_run_does_not_touch_db() -> None:
    """``dry_run`` 只算行数：不 execute、不 commit。"""
    conn = _FakeConn()
    rows = (FactorRow("600519", "2024-01-01", 1.5), FactorRow("600519", "2024-01-02", 1.6))
    assert upsert_factor_rows(conn, rows, dry_run=True) == 2
    assert conn.log == []
    assert conn.commits == 0


def test_upsert_empty_rows_returns_zero_without_commit() -> None:
    conn = _FakeConn()
    assert upsert_factor_rows(conn, ()) == 0
    assert conn.log == []
    assert conn.commits == 0


def test_source_tx_value_is_the_unified_one() -> None:
    """``SOURCE_TX`` 必须是统一后的那个值。

    两处各写一个值就是这个字段踩过的坑：同一个腾讯接口在库里有两个 ``source``，
    任何 ``WHERE source = ...`` 的查询都会漏掉大部分数据。
    """
    assert SOURCE_TX == "tx:fqkline"


def test_script_no_longer_redefines_factor_writers() -> None:
    """backfill 脚本不该再有 ``FactorRow`` / ``upsert_factor_rows`` / 常量副本。

    **用 AST 判而不是字符串判**：脚本注释里*故意*留着
    ``SOURCE_TX = "tx:fqkline"`` 这句话来解释这段历史，字符串匹配会误报。
    判的是"顶层没有这个定义/赋值"，这才是收口的意思。
    """
    import ast

    source = (Path(__file__).parents[1] / "scripts" / "factor_backfill.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "FactorRow" not in defined, "脚本还在自带 FactorRow"
    assert "upsert_factor_rows" not in defined, "脚本还在自带 upsert 实现"

    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "SOURCE_TX" not in assigned, "脚本还在自带 source 常量"
    assert "TX_ENDPOINT" not in assigned, "脚本还在自带端点常量"

    # 而唯一实现确实是从适配器导入的（判导入名，不判字面量 —— 多行 import 会变格式）
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "upsert_factor_rows" in imported
    assert "TENCENT_KLINE_URL" in imported
    assert "normalize_code" in imported


def test_build_factor_rows_carries_source_ref() -> None:
    """按需路径也要带溯源串 —— 与脚本写出来的行保持同形。"""
    raw = [_Bar(0, 10.0), _Bar(1, 11.0)]
    hfq = [_Bar(0, 12.0), _Bar(1, 13.2)]
    rows = build_factor_rows("600519", raw, hfq)
    assert all(r.source_ref == factor_source_ref(r.trade_date) for r in rows)
    assert rows[0].source_ref == "web.ifzq.gtimg.cn fqkline day/hfq 2024-01-01"
    assert all(r.source == SOURCE_TX for r in rows)
