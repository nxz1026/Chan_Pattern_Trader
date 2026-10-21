"""盘中采集层单测（``cpt/adapters/intraday_probe.py``）。**不连网**。

所有 HTTP 都打桩：一次真实请求要 1~3 秒、还依赖交易时段，不能进测试。
真实形态的样本值取自 2026-10-21 收盘实测（见各用例 docstring）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cpt.adapters import intraday_probe as probe

#: 真实榜单行（2026-10-21 实测，字段顺序与新浪一致）。
_REAL_ROWS: list[dict[str, Any]] = [
    # 北交所新股，涨 217%（无涨停限制）
    {
        "symbol": "bj920296",
        "code": "920296",
        "name": "N文峰",
        "trade": "36.300",
        "changepercent": 217.031,
        "settlement": "11.450",
    },
    # 创业板 20% 板，正好封在涨停
    {
        "symbol": "sz300296",
        "code": "300296",
        "name": "利亚德",
        "trade": "5.630",
        "changepercent": 20.043,
        "settlement": "4.690",
    },
    # 主板 10% 板，封死（卖五档全空）
    {
        "symbol": "sh600293",
        "code": "600293",
        "name": "三峡新材",
        "trade": "4.750",
        "changepercent": 9.954,
        "settlement": "4.320",
    },
    # 停牌行：trade=0
    {
        "symbol": "sz000016",
        "code": "000016",
        "name": "*ST康佳A",
        "trade": "0.000",
        "changepercent": 0.0,
        "settlement": "3.180",
    },
]


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


def _patch_rank(monkeypatch: pytest.MonkeyPatch, rows: Any) -> None:
    body = rows if isinstance(rows, str) else json.dumps(rows, ensure_ascii=False)
    monkeypatch.setattr(probe.urllib.request, "urlopen", lambda *a, **k: _FakeResp(body))


# ───────────────────────── fetch_limit_rank ─────────────────────────


def test_fetch_limit_rank_normalises_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``code`` 是 6 位纯代码；``settlement`` 就是昨收（新浪叫结算价）。"""
    _patch_rank(monkeypatch, _REAL_ROWS)
    rank = probe.fetch_limit_rank()
    assert [r["code"] for r in rank] == ["920296", "300296", "600293"]  # 停牌行被丢
    row = rank[0]
    assert row["name"] == "N文峰"
    assert row["last"] == 36.3
    assert row["prev_close"] == 11.45
    assert row["chg_pct"] == pytest.approx(217.031, abs=0.01)


def test_fetch_limit_rank_drops_suspended_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """停牌行 ``trade=0`` 整行丢弃 —— 留着会造出「距涨停 100%」的假候选。"""
    _patch_rank(monkeypatch, _REAL_ROWS)
    codes = [r["code"] for r in probe.fetch_limit_rank()]
    assert "000016" not in codes


def test_fetch_limit_rank_raises_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """空榜单不许当「今天没有涨停」—— 非交易时段与接口变了长得一样。"""
    _patch_rank(monkeypatch, "[]")
    with pytest.raises(probe.IntradayError, match="空"):
        probe.fetch_limit_rank()


def test_fetch_limit_rank_raises_on_null(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_rank(monkeypatch, "null")
    with pytest.raises(probe.IntradayError):
        probe.fetch_limit_rank()


def test_fetch_limit_rank_raises_on_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_rank(monkeypatch, "<html>502</html>")
    with pytest.raises(probe.IntradayError, match="JSON"):
        probe.fetch_limit_rank()


def test_fetch_limit_rank_raises_when_all_rows_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    """全是停牌行 ⇒ 也算失败，不是「空成功」。"""
    _patch_rank(monkeypatch, [{"code": "600519", "trade": "0", "settlement": "10"}])
    with pytest.raises(probe.IntradayError, match="没有一行可用"):
        probe.fetch_limit_rank()


def test_fetch_limit_rank_wraps_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def boom(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr(probe.urllib.request, "urlopen", boom)
    with pytest.raises(probe.IntradayError, match="取不到"):
        probe.fetch_limit_rank()


# ───────────────────────── pick_candidates ─────────────────────────


def _rank(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], ...]:
    _patch_rank(monkeypatch, _REAL_ROWS)
    return probe.fetch_limit_rank()


def test_pick_candidates_uses_board_thresholds_not_9_7(monkeypatch: pytest.MonkeyPatch) -> None:
    """**核心口径**：9.7% 一刀切会把 20% 板判成「已涨停」。

    利亚德涨 20.04%，按 9.7 筛确实「过线」，但它的涨停价是 5.63 —— 涨幅 20%
    才有意义。用 9.7 就得出「主板票涨 10% 就封板」，对创业板完全不成立。
    """
    cands = probe.pick_candidates(_rank(monkeypatch), limit=10)
    by_code = {c["code"]: c for c in cands}
    assert by_code["300296"]["limit_price"] == pytest.approx(5.63)
    assert by_code["300296"]["board"] == "other"  # 创业板
    assert by_code["600293"]["limit_price"] == pytest.approx(4.75)
    assert by_code["600293"]["board"] == "main"


def test_pick_candidates_excludes_new_stock_far_from_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """920296 涨 217%，涨停价 14.89，现价 36.30 —— 距涨停 -144%，不是候选。

    这条钉住「排序用涨幅」的错法：新股永远排第一，对封板毫无意义。
    """
    cands = probe.pick_candidates(_rank(monkeypatch), limit=10)
    assert "920296" not in {c["code"] for c in cands}


def test_pick_candidates_rejects_negative_distance() -> None:
    """**负距离必须拒**：现价高于涨停价只可能是新股首日或数据异常。

    第一版只判 ``distance > margin``，于是 920296（涨 217%，距离 −144%）
    因为「−144 < 0.5」被当成**最接近涨停**排在第一。换成距离排序
    照样能以另一种形式犯按涨幅排序的错。
    """
    rows = [
        {
            "code": "920296",
            "symbol": "bj920296",
            "name": "N文峰",
            "last": 36.30,
            "prev_close": 11.45,
            "chg_pct": 217.03,
        },
        {
            "code": "600293",
            "symbol": "sh600293",
            "name": "三峡新材",
            "last": 4.75,
            "prev_close": 4.32,
            "chg_pct": 9.95,
        },
    ]
    codes = [c["code"] for c in probe.pick_candidates(rows, limit=10)]
    assert codes == ["600293"]


def test_pick_candidates_sorts_by_distance_not_by_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """由近到远。两只都是 0% 时按代码排，保证可复现。"""
    cands = probe.pick_candidates(_rank(monkeypatch), limit=10)
    distances = [abs(c["distance_pct"]) for c in cands]
    assert distances == sorted(distances)


def test_pick_candidates_main_board_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """C3 的统计切片只做主板 —— 采集用 False（多采不亏），分析用 True。"""
    all_c = probe.pick_candidates(_rank(monkeypatch), limit=10, main_board_only=False)
    main_c = probe.pick_candidates(_rank(monkeypatch), limit=10, main_board_only=True)
    assert "300296" in {c["code"] for c in all_c}
    assert "300296" not in {c["code"] for c in main_c}


def test_pick_candidates_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(probe.pick_candidates(_rank(monkeypatch), limit=1)) == 1


def test_pick_candidates_marks_st(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST 也留（不采是信息丢失），但打 ``board='st'`` 让判据侧自己决定。"""
    rows = [
        {
            "code": "600519",
            "symbol": "sh600519",
            "name": "*ST康佳",
            "last": 10.5,
            "prev_close": 10.0,
            "chg_pct": 5.0,
        },
    ]
    cands = probe.pick_candidates(rows, limit=10)
    assert cands[0]["board"] == "st"
    assert cands[0]["limit_price"] == pytest.approx(10.5)  # 5% 板


# ───────────────────────── IntradayCollector ─────────────────────────


class _FakeQuoteClient:
    """按 E3 形状返回；只实现对应用例需要的键。"""

    def __init__(self, mapping: dict[str, dict[str, Any]]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def fetch_quotes(self, codes: Any) -> dict[str, dict[str, Any]]:
        self.calls.append(list(codes))
        return {c: self.mapping[c] for c in codes if c in self.mapping}


def test_collector_returns_sealed_sorted_by_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_rank(monkeypatch, _REAL_ROWS)
    client = _FakeQuoteClient(
        {
            "600293": {
                "code": "sh600293",
                "last": 4.75,
                "prev_close": 4.32,
                "bid1_vol": 105_029_924.0,
            },
            "300296": {
                "code": "sz300296",
                "last": 5.63,
                "prev_close": 4.69,
                "bid1_vol": 1_000_000.0,
            },
        }
    )
    result = probe.IntradayCollector(quote_client=client).scan()
    sealed = {s["code"]: s for s in result["sealed"]}
    assert sealed["600293"]["bid1_lots"] == pytest.approx(1_050_299.24, abs=0.01)
    # 按买一手数降序
    assert [s["code"] for s in result["sealed"]] == ["600293", "300296"]


def test_collector_does_not_call_quotes_when_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有候选就不发五档请求 —— 每轮 1 个请求而不是 2 个。"""
    _patch_rank(monkeypatch, [dict(_REAL_ROWS[0])])  # 只有那只 217% 的新股
    client = _FakeQuoteClient({})
    result = probe.IntradayCollector(quote_client=client).scan()
    assert result["candidates"] == []
    assert client.calls == []


def test_collector_propagates_quote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """取五档失败要**抛**，不返回半个结果（E3 契约第 4 条）。

    静默返回一半会让「今天没触发」与「今天没采到」变成同一件事。
    """
    _patch_rank(monkeypatch, _REAL_ROWS)

    class _Boom:
        def fetch_quotes(self, codes: Any) -> dict[str, dict[str, Any]]:
            raise RuntimeError("新浪挂了")

    with pytest.raises(RuntimeError, match="新浪挂了"):
        probe.IntradayCollector(quote_client=_Boom()).scan()


def test_collector_reports_at_limit_none_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """``at_limit is None``（判不了）不放进 sealed，也不许当成 False 记账。"""
    _patch_rank(monkeypatch, _REAL_ROWS)
    client = _FakeQuoteClient(
        {"600293": {"code": "sh600293", "last": None, "prev_close": None, "bid1_vol": None}}
    )
    result = probe.IntradayCollector(quote_client=client).scan()
    assert result["sealed"] == []


# ───────────────────────── 不接线 ─────────────────────────


def test_nothing_imports_the_probe_in_production_paths() -> None:
    """用户 2026-10-21 指示：**代码先写起来，但不接进项目**。

    这条钉住那句指示 —— 任何生产模块 import 了 intraday_probe，
    说明它被接线了，必须先把 ADR 0003 D10 走完再谈。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "cpt"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "intraday_probe.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if "intraday_probe" in name:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} import {name}")
    # 判据只认 **import 语句**，不认全文 —— 第一版搜整份文件文本，于是
    # ``intraday_analysis.py`` 的 docstring 里写了一句「见 cpt.adapters.intraday_probe」
    # 就被判成「接线了」。注释里提一句不是接线。
    assert offenders == [], f"这些生产模块 import 了未接线的采集器：{offenders}"
