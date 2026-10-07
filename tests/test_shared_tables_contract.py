"""共享表契约与新鲜度检查（``scripts/check_shared_tables.py``）。

## 本文件钉住三件事

1. **休市判定** —— 「期望的最新交易日」算错，假期就会天天误报。
   这是最容易写错也最难发现的一处：今天休市时，数据停在上一交易日是**正确**的，
   检查却报「过期」，那这���检查上线一周就会被无视。
2. **契约与代码双向对齐** —— ``SHARED_TABLES`` 必须既等于
   ``docs/shared-tables-contract.md`` 里的清单，也等于 **CPT 代码真实 SELECT 的表**。
   第三条最要紧：契约文档是人写的，CPT 的 SQL 会随功能增长而变，
   只对齐前两者等于「文档对自己」，新增依赖时照样静默漏掉。
3. **失败与「查不到」不同码** —— 连不上库 / 查询出错不能返回 0，
   否则「上游断了」和「一切正常」在日报里长得一模一样。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys
from zoneinfo import ZoneInfo

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import check_shared_tables as cst  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(y: int, m: int, d: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=SHANGHAI).astimezone(dt.UTC)


# ── ① 休市判定 ──────────────────────────────────────────────────────────────

#: 2026-10 的真实交易日历（实测自生产 trade_calendar）
OCT_2026: list[tuple[dt.date, bool]] = [
    (dt.date(2026, 9, 28), True),
    (dt.date(2026, 9, 29), True),
    (dt.date(2026, 9, 30), True),
    (dt.date(2026, 10, 1), False),
    (dt.date(2026, 10, 2), False),
    (dt.date(2026, 10, 3), False),
    (dt.date(2026, 10, 4), False),
    (dt.date(2026, 10, 5), False),
    (dt.date(2026, 10, 6), False),
    (dt.date(2026, 10, 7), False),
    (dt.date(2026, 10, 8), True),
    (dt.date(2026, 10, 9), True),
]


def test_trading_day_after_upstream_run_expects_today() -> None:
    """交易日 + **上游跑完之后** ⇒ 期望就是今天。

    ⚠️ 2026-10-07 更正：原先写的是「收盘后（16:00）就期待今天」，那**漏了上游**
    —— 共享表由 emotion-core 的盘后 pipeline（17:20 北京）写入，收盘（15:00）
    到它跑之间「今天该有数据」是**错的**。按旧规则，日报排在 15:00 时
    **每个交易日都会报一次假警**。
    """
    assert cst.expected_trade_day(_at(2026, 10, 8, 18, 0), OCT_2026) == dt.date(2026, 10, 8)


def test_close_is_not_enough_upstream_must_have_run() -> None:
    """边界钉在上游执行时刻（17:20），**不是**收盘时刻（15:00）。

    收盘后到上游跑之间，今天的数据本来就不存在 —— 这一段判「陈旧」就是假警，
    而假警比漏警更毒（一周内就没人看这个检查了）。
    """
    cal = [(dt.date(2026, 10, 7), True), (dt.date(2026, 10, 8), True)]
    assert cst.expected_trade_day(_at(2026, 10, 8, 15, 0), cal) == dt.date(2026, 10, 7)
    assert cst.expected_trade_day(_at(2026, 10, 8, 17, 19), cal) == dt.date(2026, 10, 7)
    assert cst.expected_trade_day(_at(2026, 10, 8, 17, 20), cal) == dt.date(2026, 10, 8)


def test_trading_day_before_close_expects_previous() -> None:
    """交易日**收盘前** ⇒ 今天还没产生数据，期望上一个交易日。

    收盘时间是 15:00，14:59 这一刻报「过期」就是误报。

    ⚠️ 「上一个交易日」不是「昨天」：2026-10-07 是休市，所以 10-08 收盘前
    的期望值是 **09-30**。第一版这里断言成 10-07，是**测试自己写错了** ——
    实现返回 09-30 才是对的。
    """
    assert cst.expected_trade_day(_at(2026, 10, 8, 14, 59), OCT_2026) == dt.date(2026, 9, 30), (
        "上一个**交易日**，昨天休市所以要往前找到 09-30"
    )

    # 上一日确实是交易日时的纯形态
    cal = [(dt.date(2026, 10, 7), True), (dt.date(2026, 10, 8), True)]
    assert cst.expected_trade_day(_at(2026, 10, 8, 10, 0), cal) == dt.date(2026, 10, 7)


def test_upstream_cutoff_is_pinned() -> None:
    """钉住上游时刻常量 —— 它变了必须有人重新审这条推理。"""
    assert cst.UPSTREAM_DAILY_BJT == dt.time(17, 20), (
        "上游 emotion-core-daily 的时刻变了？改之前先确认 CPT 日报的时刻"
        "（deploy/cron/crontab）仍晚于它，否则每个交易日都会假警。"
    )
    assert cst.EXPECT_TODAY_AFTER == dt.time(17, 20)
    assert cst.EXPECT_TODAY_AFTER > cst.MARKET_CLOSE


def test_holiday_expects_last_trading_day() -> None:
    """国庆假期中 ⇒ 期望 09-30，数据停在那儿是**正确**的。"""
    for day in (1, 2, 3, 4, 5, 6, 7):
        got = cst.expected_trade_day(_at(2026, 10, day, 20, 0), OCT_2026)
        assert got == dt.date(2026, 9, 30), f"10-0{day} 判错了，假期会天天误报"


def test_weekend_expects_friday() -> None:
    """周六收盘后 ⇒ 期望周五（不是「空」也不是周六）。"""
    cal = [
        (dt.date(2026, 10, 8), True),
        (dt.date(2026, 10, 9), True),
        (dt.date(2026, 10, 10), False),
        (dt.date(2026, 10, 11), False),
    ]
    assert cst.expected_trade_day(_at(2026, 10, 10, 20, 0), cal) == dt.date(2026, 10, 9)


def test_empty_calendar_returns_none() -> None:
    """日历读不到 ⇒ 返回 None 而不是抛 —— 调用方据此跳过日期比较。"""
    assert cst.expected_trade_day(_at(2026, 10, 8, 16, 0), []) is None


# ── ② 契约 ⇄ 代码 双向对齐 ─────────────────────────────────────────────────


def _contract_doc_tables() -> set[str]:
    doc = (ROOT / "docs" / "shared-tables-contract.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*`(\w+)`\s*\|", doc, re.M))


def test_shared_tables_match_contract_doc() -> None:
    doc_tables = _contract_doc_tables()
    # ``trade_calendar`` 在代码里单列（判定规则不同：覆盖到今天即算新鲜，
    # 因为它是预生成的），但它同样属于本契约清单，所以要并进来比。
    code_tables = {t for t, _ in cst.SHARED_TABLES} | {cst.CALENDAR_TABLE}
    assert code_tables == doc_tables, (
        f"契约文档与代码不一致：文档多 {sorted(doc_tables - code_tables)}、"
        f"代码多 {sorted(code_tables - doc_tables)}"
    )


def test_shared_tables_are_exactly_what_cpt_reads() -> None:
    """⚠️ 本文件最要紧的一条。

    ``SHARED_TABLES`` 覆盖的是**上游**断没断。它必须等于 **CPT 代码真实
    SELECT 的共享表集合** —— 否则 CPT 新加了某个共享表的读取，而这份清单
    没跟着更新，那张表断更时**不会报警**。

    只对着文档钉是「文档对自己」：文档是人写的，CPT 的 SQL 会随功能增长而变。
    """
    cpt = ROOT / "cpt"
    own = {t for t, _ in cst.SHARED_TABLES}
    pattern = re.compile(r"(?:FROM|JOIN)\s+public\.(\w+)", re.I)
    read: set[str] = set()
    for path in cpt.rglob("*.py"):
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            name = m.group(1)
            if not name.startswith("cpt_") and name not in {
                "asel",
            }:
                read.add(name)
    # CPT 还会读 asel.* 下的共享表，不在本文件职责内，剔除
    read -= {
        "cpt_recommendation",
        "cpt_llm_call",
        "cpt_factor_epoch",
        "cpt_run_metric",
        "cpt_signal_event",
        "cpt_structure_event",
        "cpt_dashboard_run",
    }
    missing = read - own - {cst.CALENDAR_TABLE}
    assert not missing, (
        f"CPT 代码读了这些共享表，但新鲜度清单里没有：{sorted(missing)} —— 它们断更时不会报警。"
    )


# ── ③ 失败与「查不到」不同码 ────────────────────────────────────────────────


def test_dependency_import_failure_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到 DB 依赖 ⇒ 返回 2，**不是 0**。"""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        if name.startswith("psycopg") or name.endswith("_dbconfig"):
            raise ImportError("模拟依赖缺失")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert cst.main([]) == 2


def test_connection_failure_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上库 ⇒ 返回 2。

    返回 0 的话，日报会写「全部正常」，而真相是「根本没查成」——
    这与 ``run_metric_store.prune`` 那条纪律同源。
    """

    class _Boom:
        @staticmethod
        def connect(**_kw):  # noqa: ANN003
            raise RuntimeError("模拟连不上")

    monkeypatch.setitem(sys.modules, "psycopg", _Boom)
    assert cst.main([]) == 2


def test_query_failure_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询出错 ⇒ 返回 2，不是 0。"""

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return None

        def execute(self, *_a, **_kw):  # noqa: ANN002, ANN003
            raise RuntimeError("模拟查询失败")

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            return None

    class _Psy:
        @staticmethod
        def connect(**_kw):  # noqa: ANN003
            return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg", _Psy)
    assert cst.main([]) == 2
