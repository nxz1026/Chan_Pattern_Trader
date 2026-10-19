"""水位行**落库开关**：测试不许把假数据写进生产表（2026-10-19，审计 P2-bl）。

## 这个文件守的是什么

``cpt_run_metric`` 是**生产监控表**（`scripts/run_inspection.py` 按它出告警）。
此前它**没有任何测试闸门**，而 ``tests/conftest.py`` 的 autouse fixture
``_isolate_run_store`` 只把 ``CPT_RUN_STORE_PERSIST`` 管到 ``cpt_dashboard_run``
（R45 那次修的表），漏了这张。

后果是实测出来的，不是推的：测试里 ``_RealtimeProvider`` 被塞进一个返回合成 bar
的假 client（``cpt/web/__main__._synthetic_bar``，``1_700_000_000_000 + i*interval``，
= 2023-11-14），``_poll_once`` 的成功分支照常调 ``_record_run_metric``，而
``data_source="binance_realtime"``、``"stale": False`` 都是**硬编码字面量** ——
于是这一行在库里长这样：

    market=crypto symbol=BTCUSDT bar_count=30 bi_count=0
    last_bar_time=1700104400000 health=ok
    detail->>'data_source' = binance_realtime

跑一次 ``pytest tests/test_realtime_poll_degradation.py``，生产表就多一行
（140 → 141，实测过两轮）。累计 **141 行**，散布在 10-04 ~ 10-18 每天。

危害不止「脏数据」：巡检读这些行会报出 21 条「笔数 218 → 0」的**假故障**
（真水位是 212~222），把真信号淹掉；而且 P0-2 那种真实串号的规模
也没法可信测量——量出来的数里混着假行。

## 为什么复用 ``CPT_RUN_STORE_PERSIST``

它从 R45 起就是「测试别往生产表写」这个语义（``cpt/web/app.py:303`` 写端、
``:407`` 读端、``tests/conftest.py:345``）。新造一个变量名只会让「哪张表受哪个
开关管」变成要查两处的事，而本仓库的既有偏好是少改配置。

## 为什么闸门有两道

``MetricRecorder.record`` 是**两条市场路径的唯一咽喉**（加密侧
``cpt/web/__main__._record_run_metric``、A 股侧
``cpt/application/a_share_snapshot.build_ashare_snapshot``），闸门守在那里，
将来新加的调用点自动被覆盖。``_record_run_metric`` 里那道更早：它**在建连接
之前**就退，测试里连都不许去连生产库。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from cpt.application.run_metric import (
    RUN_STORE_PERSIST_ENV,
    MetricRecorder,
    metric_from_snapshot,
    run_store_persist_enabled,
)
from cpt.web import __main__ as web_main

# ── 开关本身 ────────────────────────────────────────────────────────────────


def test_default_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """没设变量时**照常落库** —— 生产默认行为不能变。"""
    monkeypatch.delenv(RUN_STORE_PERSIST_ENV, raising=False)
    assert run_store_persist_enabled() is True


def test_explicit_zero_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUN_STORE_PERSIST_ENV, "0")
    assert run_store_persist_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "2"])
def test_only_literal_zero_turns_it_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """判据与 ``cpt/web/app.py`` 的两处**逐字一致**（``== "0"``），不是「非空即真」。

    宽松解析会造出「设置成 ``""`` 就静默不落库」这种查不出来的停摆。
    """
    monkeypatch.setenv(RUN_STORE_PERSIST_ENV, value)
    assert run_store_persist_enabled() is True


def test_conftest_actually_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest 的 autouse fixture 必须真的把它关掉（这条会抓到 fixture 被误删）。"""
    assert run_store_persist_enabled() is False, (
        "conftest 没关掉落库开关 —— 测试会往生产 cpt_run_metric 写数据"
    )


def test_gate_reads_the_same_variable_as_app_py() -> None:
    """两张表共用一个变量名，别各自长出一个。"""
    import os  # noqa: PLC0415

    from cpt.web import app as web_app  # noqa: PLC0415

    assert web_app._RUN_STORE_PERSIST_ENV == RUN_STORE_PERSIST_ENV
    assert os.environ.get(RUN_STORE_PERSIST_ENV, "1") == "0"


# ── MetricRecorder 这一道闸门 ───────────────────────────────────────────────


class _SpyConn:
    """记下所有写操作的假连接。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> Any:  # noqa: ANN401
        raise AssertionError("闸门关着时不该碰游标")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _snapshot() -> dict[str, Any]:
    return {
        "candles": [{"open_time": 1_792_000_000_000}],
        "data_quality": {},
        "runtime": {"generated_at": 1_792_000_000_000},
        "reproducibility": {"config_hash": "c", "dataset_hash": "d", "rules_version": "r"},
        "overlays": {"bis": [], "fractals": [], "zhongshus": [], "trend_types": []},
    }


def test_recorder_does_not_touch_the_connection_when_disabled() -> None:
    """★核心：关着时**一行都不写**，也不 commit（conftest 已把它设成 0）。"""
    conn = _SpyConn()
    MetricRecorder(conn).record(
        _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend"
    )
    assert conn.calls == []
    assert conn.commits == 0, "关着落库还在 commit —— 事务边界不该被它动"


def test_recorder_still_returns_the_computed_row() -> None:
    """返回值**照常给**：调用方可能拿它记日志或断言。

    凭空从 ``None`` 炸掉是把「测试不写库」变成「测试跑不动」——
    修污染不该顺带废掉纯函数的可用性。
    """
    row = MetricRecorder(_SpyConn()).record(
        _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend"
    )
    expected = metric_from_snapshot(
        _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend"
    )
    assert row["market"] == "crypto"
    assert row["symbol"] == "BTCUSDT"
    assert row["last_bar_time"] == expected["last_bar_time"]


def test_recorder_writes_when_explicitly_re_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式反向打开时必须真的落库 —— 否则闸门会退化成「永远不写」。

    这里只断言**SQL 与值确实发出去了**，不复刻 ``append_metrics`` 的插入细节
    （那由 ``tests/test_run_metric_store_prune.py`` 守）。重点是：闸门是「可开可关
    的真开关」，不是把写入路径整个剪掉。
    """
    monkeypatch.setenv(RUN_STORE_PERSIST_ENV, "1")
    sqls: list[str] = []
    params: list[Any] = []

    class _Cur:
        def __init__(self, conn: _WritingConn) -> None:
            self.conn = conn
            self.rowcount = 0

        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, sql: str, values: Any = ()) -> None:  # noqa: ANN401
            self.conn.calls.append(sql)
            if "INSERT INTO public.cpt_run_metric" in sql:
                sqls.append(sql)
                params.append(values)
                self.rowcount = 1

    class _WritingConn(_SpyConn):
        def cursor(self) -> Any:  # noqa: ANN401
            return _Cur(self)

    conn = _WritingConn()
    MetricRecorder(conn).record(
        _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend"
    )
    assert sqls, "打开时应当真的发 INSERT"
    # 列与值是**分开**传的：``append_metrics`` 按 ``_COLUMNS`` 拼列名、只放值与占位符
    # （值为 None 的列整列省略，让 DB 的 DEFAULT 生效）。
    assert "symbol" in sqls[0]
    assert "BTCUSDT" in params[0]


# ── _record_run_metric 这一道闸门（更早，在建连接之前）──────────────────────


def test_record_run_metric_skips_before_opening_a_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """★核心：闸门在**建连接之前** —— 测试里连都不该去连生产库。

    用一个会炸的假 client 顶替 ``AShareLocalClient``：只要它被 import 出来
    构造过，就说明闸门位置太晚。
    """

    class _ExplodingClient:
        def __init__(self) -> None:
            raise AssertionError("闸门关着时不该构造 AShareLocalClient（那是生产连接）")

    monkeypatch.setattr(
        "cpt.adapters.a_share_local.AShareLocalClient", _ExplodingClient, raising=True
    )
    with caplog.at_level(logging.DEBUG):
        web_main._record_run_metric(
            _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend"
        )
    assert any("落库开关已关" in r.message for r in caplog.records), (
        "跳过时应当留一条 debug —— 否则「为什么这轮没水位」要靠猜"
    )


def test_record_run_metric_passes_bars_through_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闸门只在最前面短一次；后面那条路径不能因为参数形态不同就绕过。"""
    seen: list[str] = []

    class _Client:
        def _get_conn(self) -> Any:  # noqa: ANN401
            seen.append("connected")
            return _SpyConn()

        def close(self) -> None:
            seen.append("closed")

    monkeypatch.setenv(RUN_STORE_PERSIST_ENV, "1")
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _Client, raising=True)
    web_main._record_run_metric(
        _snapshot(), market="crypto", symbol="BTCUSDT", backend="NativeBackend", bars=()
    )
    assert seen == ["connected", "closed"], "打开时应当建连接并收尾"
