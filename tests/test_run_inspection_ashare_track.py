"""A 股「K 线停更」接进巡检告警（R62 · 用户 m01543「要加入」）。

## 钉的是什么

1. **可见性**：水位流是**加密轮询**在喂（30s 一行、每行一只），A 股一天才一行
   （``cpt-dashboard-ashare.timer`` 16:00 北京、每轮 50 只）。实测 2026-10-14
   ``run_inspection.py --print`` 报 ``symbols: 1, problems: 0`` —— 154 只 A 股里
   51 行 ``health=failing`` 全在 ``limit=200`` 的窗口外，**A 股在巡检里等于不存在**。
   ⇒ 必须单开一窗（``_recent_rows``）。
2. **阈值**：``stale=True`` 不等于该报警。A 股 ``asel.ref_adjust_factor`` 是**按需**
   补的、当日因子当晚才齐 ⇒ lag=1 是常态（实测跟踪清单 51 只里 20 只）。只有
   lag≥2 才是链路断了（22 只，其中 20 只卡在 09-30）。``lag=1`` 只记降级、
   不进告警正文 —— 否则每天 20+ 条噪音把真断的那 20 只淹掉。
3. **可修**：告警正文要带 ``stale_reason``（``factor_missing``/``upstream_lag``）
   与末根 K 线日期；否则收到的人只能说「stale=True」，不知道去查因子链路还是查入库。

全程离线：``rms.recent_metrics`` 与数据源探活都换假实现，不碰 DB、不发飞书。
"""

from __future__ import annotations

import json
import time
from typing import Any

from scripts import run_inspection as ri

#: 用**贴近当下**的时间戳：巡检会把「水位流已断」也记成 problem，
#: 写死 observed_at=_NOW_MS - 3_000 会让每个用例都多出一条噪音（第一版就踩了）。
_NOW_MS = int(time.time() * 1000)


def _row(
    *,
    market: str,
    symbol: str,
    observed_at: int,
    health: str = "ok",
    bar_count: int = 118,
    gap_count: int = 0,
    stale: bool = False,
    detail: Any = None,
    dataset_hash: str = "h",
) -> dict[str, Any]:
    """一条水位行（键对齐 ``run_metric_store.recent_metrics`` 的真实返回）。"""
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    return {
        "market": market,
        "symbol": symbol,
        "observed_at": observed_at,
        "health": health,
        "bar_count": bar_count,
        "gap_count": gap_count,
        "stale": stale,
        "detail": detail,
        "bi_count": 10,
        "zhongshu_count": 2,
        "fractal_count": 30,
        "dataset_hash": dataset_hash,
        "backend": "local",
    }


def _stale_detail(
    *, lag: int, reason: str = "factor_missing", day: str = "2026-10-14"
) -> dict[str, Any]:
    return {
        "ashare_staleness": {
            "stale_reason": reason,
            "stale_reason_day": day,
            "stale_lag_days": lag,
            "stale_last_bar_date": "2026-10-13",
        }
    }


def _install(
    monkeypatch: Any,
    *,
    main_rows: list[dict[str, Any]],
    cn_rows: list[dict[str, Any]],
    calls: list[dict[str, Any]] | None = None,
) -> None:
    """把 ``recent_metrics`` 与数据源探活换成假实现。"""

    def fake_recent_metrics(conn: Any, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        if calls is not None:
            calls.append(kwargs)
        market = kwargs.get("market")
        if market is None:
            return tuple(main_rows)
        if market == "cn":
            return tuple(cn_rows)
        return ()

    monkeypatch.setattr(ri.rms, "recent_metrics", fake_recent_metrics)
    from cpt.adapters import source_registry

    monkeypatch.setattr(source_registry, "capabilities_payload", lambda **_: {"sources": []})


# ── 可见性：A 股单开一窗 ────────────────────────────────────────
def test_ashare_rows_are_fetched_in_a_separate_wide_window(monkeypatch) -> None:
    """cn 必须单开一窗，且窗口要宽（A 股一天一轮、每轮 50 只）。"""
    calls: list[dict[str, Any]] = []
    _install(
        monkeypatch,
        main_rows=[_row(market="crypto", symbol="BTCUSDT", observed_at=_NOW_MS - 1_000)],
        cn_rows=[_row(market="cn", symbol="600519", observed_at=_NOW_MS - 3_000)],
        calls=calls,
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    cn_calls = [c for c in calls if c.get("market") == "cn"]
    assert len(cn_calls) == 1, "A 股必须单独要一次（否则被加密轮询挤出窗口）"
    assert cn_calls[0]["limit"] >= 1000, "A 股窗口要取满 store 上限 —— 154 只才看得全"
    assert report["symbols"] == 2


def test_ashare_rows_are_visible_even_outside_the_main_window(monkeypatch) -> None:
    """核心回归：主窗里看不见的 A 股 failing，必须出现在 problems 里。

    复现 2026-10-14 线上实况：主窗全是加密行、A 股一行都没有。
    """
    _install(
        monkeypatch,
        main_rows=[_row(market="crypto", symbol="BTCUSDT", observed_at=_NOW_MS - 1_000)],
        cn_rows=[
            _row(
                market="cn",
                symbol="600519",
                observed_at=_NOW_MS - 3_000,
                health="failing",
                stale=True,
                detail=_stale_detail(lag=5),
            )
        ],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    assert report["symbols"] == 2
    assert any("cn/600519" in p for p in report["problems"]), report["problems"]


def test_missing_market_kwarg_does_not_crash_the_inspection(monkeypatch) -> None:
    """假实现/旧签名不支持 ``market=`` 时，巡检退化成「只有主窗」，不能崩。"""

    def old_signature(conn: Any, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        if kwargs.get("market") is not None:
            raise TypeError("recent_metrics() got an unexpected keyword argument 'market'")
        return (_row(market="crypto", symbol="BTCUSDT", observed_at=_NOW_MS - 1_000),)

    monkeypatch.setattr(ri.rms, "recent_metrics", old_signature)
    from cpt.adapters import source_registry

    monkeypatch.setattr(source_registry, "capabilities_payload", lambda **_: {"sources": []})
    report = ri.build_report(None)  # type: ignore[arg-type]
    assert report["symbols"] == 1


# ── 阈值：lag=1 只记降级，lag≥2 才告警 ─────────────────────────
def test_shallow_lag_is_degraded_not_alerted(monkeypatch) -> None:
    """lag=1（当日因子未发布，常态）⇒ 降级，不进告警正文。"""
    _install(
        monkeypatch,
        main_rows=[],
        cn_rows=[
            _row(
                market="cn",
                symbol="600519",
                observed_at=_NOW_MS - 3_000,
                health="failing",
                stale=True,
                detail=_stale_detail(lag=ri.ASHARE_STALE_LAG_ALERT_DAYS - 1),
            )
        ],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    assert report["problems"] == [], "lag=1 不该进告警正文（每天 20+ 条噪音）"
    assert any("cn/600519" in d and "仅记录" in d for d in report["degraded"]), report["degraded"]


def test_deep_lag_is_alerted_with_reason_and_last_bar(monkeypatch) -> None:
    """lag≥2 ⇒ 告警，正文带原因与末根 K 线（received 的人照着这句话就能去修）。"""
    lag = ri.ASHARE_STALE_LAG_ALERT_DAYS
    _install(
        monkeypatch,
        main_rows=[],
        cn_rows=[
            _row(
                market="cn",
                symbol="300299",
                observed_at=_NOW_MS - 3_000,
                health="failing",
                stale=True,
                detail=_stale_detail(lag=lag, reason="factor_missing", day="2026-09-30"),
            )
        ],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    msg = next(p for p in report["problems"] if "cn/300299" in p)
    assert f"滞后 {lag} 个交易日" in msg
    assert "factor_missing@2026-09-30" in msg
    assert "末根 K 线 2026-10-13" in msg


def test_unreachable_stale_reason_still_alerts(monkeypatch) -> None:
    """探不出原因（``_stale_reason`` 返回 None）时也要告警，只是不带那两段。"""
    _install(
        monkeypatch,
        main_rows=[],
        cn_rows=[
            _row(
                market="cn",
                symbol="600519",
                observed_at=_NOW_MS - 3_000,
                health="failing",
                stale=True,
                detail={"ashare_staleness": {"stale_lag_days": 3}},
            )
        ],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    msg = next(p for p in report["problems"] if "cn/600519" in p)
    assert "滞后 3 个交易日" in msg
    assert "原因" not in msg


def test_legacy_row_without_diagnosis_is_treated_as_problem(monkeypatch) -> None:
    """R62 之前落的水位行没有诊断（detail 里没 ``ashare_staleness``）⇒ 判不了就报警。

    fail-closed 而不是 fail-open：判不出深度时宁可吵一声，也不能静默。
    这类行会被下一轮批次（16:00 北京）带诊断的新行覆盖掉。
    """
    _install(
        monkeypatch,
        main_rows=[],
        cn_rows=[
            _row(
                market="cn",
                symbol="600418",
                observed_at=_NOW_MS - 3_000,
                health="failing",
                stale=True,
            )
        ],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    assert any("cn/600418" in p for p in report["problems"]), report["problems"]


def test_crypto_rows_are_untouched_by_the_ashare_grading(monkeypatch) -> None:
    """加密侧没有（也不该有）这套分级：failing 就是 failing。"""
    _install(
        monkeypatch,
        main_rows=[
            _row(
                market="crypto",
                symbol="BTCUSDT",
                observed_at=_NOW_MS - 1_000,
                health="failing",
                stale=True,
            )
        ],
        cn_rows=[],
    )
    report = ri.build_report(None)  # type: ignore[arg-type]
    assert any("crypto/BTCUSDT" in p for p in report["problems"])


# ── 诊断读取（detail 的形状可以很多样） ────────────────────────
def test_stale_lag_reads_json_string_and_dict_detail() -> None:
    assert ri._ashare_stale_lag({"detail": json.dumps(_stale_detail(lag=4))}) == 4
    assert ri._ashare_stale_lag({"detail": _stale_detail(lag=4)}) == 4


def test_stale_lag_is_none_for_junk() -> None:
    assert ri._ashare_stale_lag({"detail": None}) is None
    assert ri._ashare_stale_lag({"detail": "{不是 json"}) is None
    assert ri._ashare_stale_lag({"detail": json.dumps({"ashare_staleness": "nope"})}) is None
    assert ri._ashare_stale_lag({"detail": json.dumps({"ashare_staleness": {}})}) is None
    # 布尔是 int 的子类 —— 别把 True 当成「滞后 1 天」
    assert (
        ri._ashare_stale_lag({"detail": json.dumps({"ashare_staleness": {"stale_lag_days": True}})})
        is None
    )


def test_stale_suffix_is_empty_without_diagnosis() -> None:
    assert ri._ashare_stale_suffix({"detail": None}) == ""
    assert ri._ashare_stale_suffix({"detail": json.dumps({"ashare_staleness": {}})}) == ""


# ── 合并后的顺序 ──────────────────────────────────────────────
def test_rows_from_both_windows_are_merged_in_time_order(monkeypatch) -> None:
    """两条窗口合并后必须**整体按时间正序**，否则突变/指纹检测的 prev→cur 会反。"""
    calls: list[dict[str, Any]] = []
    _install(
        monkeypatch,
        main_rows=[
            _row(market="cn", symbol="600519", observed_at=_NOW_MS - 2_000, dataset_hash="new"),
            _row(market="crypto", symbol="BTCUSDT", observed_at=_NOW_MS - 1_000),
        ],
        cn_rows=[
            _row(market="cn", symbol="600519", observed_at=_NOW_MS - 3_000, dataset_hash="old"),
            _row(market="cn", symbol="600519", observed_at=_NOW_MS - 2_000, dataset_hash="new"),
        ],
        calls=calls,
    )
    merged = ri._recent_rows(None)  # type: ignore[arg-type]
    times = [r["observed_at"] for r in merged]
    assert times == sorted(times), f"合并后必须时间正序，实际 {times}"
    # 去重：主窗与 cn 窗各有一条 (cn,600519,_NOW_MS-2_000) → 合并后只留 1 条
    assert len([t for t in times if t == _NOW_MS - 2_000]) == 1
