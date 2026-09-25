"""``scripts/factor_backfill.py`` 的测试。

这个脚本是 364 行的运维入口，2026-09-25 之前**不在任何门禁覆盖范围内**（CI 只跑
``ruff check cpt tests`` + ``mypy cpt``），而且没有任何测试 —— 于是它自带的两份
重复实现（``FactorRow`` / ``upsert_factor_rows``）和一份有顺序 bug 的市场推断
（``_code_to_tx``）都没人发现。本文件把它的**纯逻辑**钉住。

网络与 DB 一律注入假实现，绝不真连（踩过：这类脚本"默认联网"会让跑测试写脏生产库）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from cpt.adapters.a_share_factor import factor_source_ref
from cpt.adapters.a_share_public import TENCENT_KLINE_URL

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "factor_backfill.py"


@pytest.fixture(scope="module")
def script() -> Any:
    """按路径加载脚本模块。

    **必须先把模块注册进 ``sys.modules`` 再 ``exec_module``** —— 脚本用了
    ``@dataclass``，dataclass 在 exec 期间会回头查 ``sys.modules[cls.__module__]``，
    只调 ``module_from_spec`` 会拿到 None 并抛
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    name = "factor_backfill_script"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ 市场前缀


def test_fetch_uses_authoritative_market_prefix(script: Any, monkeypatch: Any) -> None:
    """**回归**：``920201`` 必须推成 ``bj920201``，不能是 ``sh920201``。

    脚本原先自带 ``_code_to_tx``，把 ``9``（沪 B）写在 ``92``（北交所新代码段）
    之前 —— ``startswith(("6","9","5"))`` 抢先命中，``92`` 那一支成了死代码。
    同一个顺序 bug 在 ``a_share_public.normalize_code`` 的注释里被点名过
    （``a_share_local._to_wind_code``，R17 修掉），这第三份一直没跟上。
    """
    seen: list[str] = []

    def fake_pair(tx_sym: str, days: int) -> Any:
        seen.append(tx_sym)
        return ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]])

    monkeypatch.setattr(script, "_fetch_tx_pair", fake_pair)
    script.fetch_tx_factor_rows("920201")
    assert seen == ["bj920201"], "920201 又被推成沪市了"


def test_fetch_handles_bj_prefixes_the_old_helper_rejected(script: Any, monkeypatch: Any) -> None:
    """``83/87/88`` 开头的北交所代码：旧实现直接 ``ValueError``，现在要能拉。"""
    seen: list[str] = []

    def fake_pair(tx_sym: str, days: int) -> Any:
        seen.append(tx_sym)
        return ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]])

    monkeypatch.setattr(script, "_fetch_tx_pair", fake_pair)
    script.fetch_tx_factor_rows("830799")
    assert seen == ["bj830799"]


def test_script_has_no_local_market_inference(script: Any) -> None:
    """本地市场推断必须已删除（判 AST，不判字符串 —— 注释里还留着历史说明）。"""
    import ast

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "_code_to_tx" not in defined


# ------------------------------------------------------------------ 因子计算


def test_fetch_computes_hfq_over_raw(script: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]]),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert len(rows) == 1
    assert rows[0].code == "600519"
    assert rows[0].trade_date == "2000-01-01"
    assert rows[0].hfq_factor == pytest.approx(1.2)
    assert rows[0].source_ref == factor_source_ref("2000-01-01")


def test_fetch_keeps_only_overlapping_dates(script: Any, monkeypatch: Any) -> None:
    """只有 raw 或只有 hfq 的交易日必须丢掉 —— 因子必须同源同对。"""
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: (
            [["2000-01-01", "0", "10"], ["2000-01-02", "0", "11"]],
            [["2000-01-01", "0", "12"], ["2000-01-03", "0", "13"]],
        ),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert [r.trade_date for r in rows] == ["2000-01-01"]


def test_fetch_skips_zero_raw_close(script: Any, monkeypatch: Any) -> None:
    """``raw == 0`` 会除零 —— 必须跳过而不是抛异常。"""
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: (
            [["2000-01-01", "0", "0"], ["2000-01-02", "0", "11"]],
            [["2000-01-01", "0", "12"], ["2000-01-02", "0", "13"]],
        ),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert [r.trade_date for r in rows] == ["2000-01-02"]


# ------------------------------------------------------------------ 端点收口


def test_fetch_pair_uses_the_shared_endpoint(script: Any, monkeypatch: Any) -> None:
    """HTTP 请求必须打 ``TENCENT_KLINE_URL``（脚本不再自带端点常量）。

    同时钉住"两次请求"（``bfq`` + ``hfq``）：一次不够，因为因子要 raw 与 hfq 相除。
    """
    seen: list[str] = []

    class _Resp:
        def read(self) -> bytes:
            payload = {
                "data": {
                    "sh600519": {
                        "day": [["2000-01-01", "0", "10"]],
                        "hfqday": [["2000-01-01", "0", "12"]],
                    }
                }
            }
            return json.dumps(payload).encode("gbk")

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen)
    raw, hfq = script._fetch_tx_pair("sh600519", 800)

    assert raw == [["2000-01-01", "0", "10"]]
    assert hfq == [["2000-01-01", "0", "12"]]
    assert len(seen) == 2, "应各请求一次 bfq 与 hfq"
    assert all(url.startswith(f"{TENCENT_KLINE_URL}?param=sh600519,day,,,800,") for url in seen)
    # 第一次是不复权（adj 为空串，URL 以逗号收尾），第二次才是 hfq
    assert seen[0].endswith(",") and not seen[0].endswith("hfq")
    assert seen[1].endswith(",hfq")
