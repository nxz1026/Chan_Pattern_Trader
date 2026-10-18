"""因子重算**进度账本**的记账单位契约（R67）。**离线**：不碰 DB、不联网。

## 这个 bug 的形状

``done`` 是一个集合：``done=[code, …]``，含义是「这只票**算过**」。``todo`` 则是
``[c for c in priority_codes(...) if c not in done]``。

于是**「新交易日到来」不是失效条件**：2026-10-15 那一轮跑完全市场之后 ``done``
覆盖 5208 只，10-16、10-17 的 ``todo`` 都只剩从未成功的 18 只 —— 日跑「成功」，
日志写着「已完成 5208 只」，而生产因子表的最新日冻结在 10-15，全市场静默退到
**不复权价**（``price_basis=raw``），且不自愈。

判据：**进度账本的失效条件必须来自输入窗口（这里 = 本地最新交易日），不能来自
上一次执行本身。** 记账单位从「对象全集」换成「输入窗口」：``covered[code]`` 记的
是「这只票已重算**到哪一天**」。详见 ``docs/known-traps.md`` 第 53 号。

所以本文件的第 3 个用例是**专门用来杀老写法的**：上一轮全市场都算过之后，
目标日只要前进一天，全市场必须重新变成待处理。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "factor_recompute_ledger_mod", ROOT / "scripts" / "factor_recompute.py"
)
assert _spec is not None and _spec.loader is not None
fr = importlib.util.module_from_spec(_spec)
sys.modules["factor_recompute_ledger_mod"] = fr
_spec.loader.exec_module(fr)

SOURCE = (ROOT / "scripts/factor_recompute.py").read_text(encoding="utf-8")


def _main_body() -> str:
    """``main()`` 的函数体（它是 ``if __name__`` 之前的最后一个函数）。"""
    start = SOURCE.index("def main(")
    end = SOURCE.index('if __name__ == "__main__":')
    return SOURCE[start:end]


# ── 账本读取：老文件必须退化成「空账本」 ────────────────────────────────


def test_legacy_done_ledger_is_read_as_empty() -> None:
    """老的 ``done`` 列表**不能**被当成「已覆盖到最新日」。

    它里面没有日期，无法反推每只票算到了哪一天。唯一诚实的做法是全量重算一轮
    （≈2.1 小时，在 cron 的 10h ``timeout`` 内）；反过来「假定已覆盖」就是 P0-1。
    """
    assert fr.coverage_of({"done": ["000001", "000002"], "failed": {}}) == {}


def test_coverage_of_reads_and_cleans() -> None:
    assert fr.coverage_of({"covered": {"000001": "2026-10-16"}}) == {"000001": "2026-10-16"}
    # 空值/异常形状不该把账本污染成「已覆盖」
    assert fr.coverage_of({"covered": {"000001": "", "": "2026-10-16"}}) == {}
    assert fr.coverage_of({"covered": "not-a-dict"}) == {}
    assert fr.coverage_of({}) == {}


# ── 核心回归：输入窗口前进 ⇒ 全市场重新待处理 ───────────────────────────


def test_target_advance_reopens_the_whole_market() -> None:
    """**这条用例专门杀老写法。**

    老写法：``done`` 里有的就跳过。目标日从 10-15 走到 10-16 之后，``done`` 一个字
    都没变，于是 ``todo`` 恒为空 —— 这正是 10-16/10-17 生产上发生的事。
    """
    codes = [f"{i:06d}" for i in range(500)]
    covered = {c: "2026-10-15" for c in codes}

    assert fr.pending_codes([codes], covered, "2026-10-15") == []
    assert fr.pending_codes([codes], covered, "2026-10-16") == codes


def test_code_already_covered_on_target_is_skipped() -> None:
    """同一个目标日重复跑不该重算 —— 断点续跑仍然要成立。"""
    covered = {"000001": "2026-10-16", "000002": "2026-10-15"}
    assert fr.pending_codes([["000001", "000002"]], covered, "2026-10-16") == ["000002"]


# ── 排序：分组优先级优先，组内「覆盖最旧」优先（轮转） ──────────────────


def test_group_priority_beats_recency() -> None:
    """自选（组 1）即使**更新**，也要排在「其余」（组 3）前面。

    按覆盖日期一把梭排序会把热门票排到从未算过的票后面 —— 人在看的票必须新鲜。
    """
    groups: list[list[str]] = [[], ["600519"], [], ["000001"]]
    covered = {"600519": "2026-10-16", "000001": "2026-10-15"}
    assert fr.pending_codes(groups, covered, "2026-10-17") == ["600519", "000001"]


def test_rotation_within_group_is_oldest_first() -> None:
    """组内按覆盖日期升序，账本里没有记录的排最前 —— 一轮没跑完时先补最落后的。"""
    groups = [["A", "B", "C"]]
    covered = {"A": "2026-10-16", "B": "2026-10-14"}  # C 没有任何记录
    assert fr.pending_codes(groups, covered, "2026-10-17") == ["C", "B", "A"]


def test_pending_is_deduped_across_groups() -> None:
    groups = [["600519"], ["600519", "000001"]]
    assert fr.pending_codes(groups, {}, "2026-10-16") == ["600519", "000001"]


# ── 目标日来自本地 K 线，不来自日历 ─────────────────────────────────────


class _FakeCursor:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.sql = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql

    def fetchone(self) -> Any:
        return self.row

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, row: Any) -> None:
        self.cur = _FakeCursor(row)

    def cursor(self) -> _FakeCursor:
        return self.cur


def test_target_is_latest_local_bar_date() -> None:
    import datetime as dt

    conn = _FakeConn((dt.date(2026, 10, 16),))
    assert fr.coverage_target(conn) == "2026-10-16"  # type: ignore[arg-type]
    assert "max(date)" in conn.cur.sql and "daily_bar" in conn.cur.sql


def test_target_is_none_without_bars() -> None:
    conn = _FakeConn((None,))
    assert fr.coverage_target(conn) is None  # type: ignore[arg-type]


# ── 接线门禁：main() 必须真的用输入窗口，而不是老的 done 集合 ────────────


def test_main_computes_todo_from_input_window() -> None:
    body = _main_body()
    assert "coverage_target(conn)" in body, "main() 没有取「本地最新交易日」作为目标"
    assert "pending_codes(" in body, "main() 没有按输入窗口算待处理"
    assert "priority_groups(conn" in body, "main() 丢了分组边界，轮转排序会失准"


def test_main_no_longer_filters_by_done_set() -> None:
    """老写法必须**彻底消失**：``done`` 集合做过滤就是 P0-1 本体。

    唯一允许留下的 ``done`` 是**迁移**：跑之前告警并 ``pop`` 掉，逼出一次全量重算。
    所以这里钉的是「出现次数」而不是「不存在」—— 多出一处就是有人把过滤写回来了。
    """
    body = _main_body()
    assert "c not in done" not in body, "main() 仍在用 done 集合过滤候选集"
    assert body.count('state.get("done")') == 1, "done 只允许出现在迁移告警里"
    assert 'state.pop("done", None)' in body, "老账本没有被清掉，会一直告警"


def test_success_records_the_covered_trade_date() -> None:
    """成功的含义是「覆盖到 target 这一天」，不是「算过了」。"""
    body = _main_body()
    assert "covered[code] = target" in body, "成功分支没有写下覆盖日期"
    assert "state[COVERED_KEY] = covered" in body, "覆盖日没有写回 state"


def test_ledger_key_name_is_stable() -> None:
    """``covered`` 这个键名是对外契约（老文件没有它 ⇒ 全量重算），不能随手改名。"""
    assert fr.COVERED_KEY == "covered"
