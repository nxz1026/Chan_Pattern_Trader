"""因子口径纪元（R45）。**离线**：只测判定逻辑与 SQL 形状，不连库。

## 为什么要这个

2026-10-03 两次切表把因子表换成「公司行动重算」口径。切表前记录的 **41 条信号
里 29 条变成 ``invalidated``** —— 但那些 ``invalidated`` **不是**「信号失败了」，
而是「结构在换口径后重算，与旧口径判断不一致」。

**两件事在没有标记时长得一模一样**，于是下一个看到「信号突然失效」的人
会当成 bug 排查一轮。本模块让这个区分**可推导**。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from cpt.storage.factor_epoch_store import (
    EPOCH_DDL,
    FactorEpoch,
    current_epoch,
    is_legacy_event,
    record_epoch,
)

ROOT = Path(__file__).resolve().parents[1]
SWITCHED = dt.datetime(2026, 10, 3, 10, 34, 24, tzinfo=dt.UTC)
EPOCH = FactorEpoch(
    switched_at=SWITCHED,
    old_source="tx:fqkline",
    new_source="eastmoney:events",
    old_match_rate=0.2263,
    new_match_rate=1.0,
    note="",
)


# ---------------------------------------------------------------------------
# 判定：边界两侧
# ---------------------------------------------------------------------------


def test_before_switch_is_legacy() -> None:
    assert is_legacy_event(dt.datetime(2026, 10, 3, 9, 55, 46, tzinfo=dt.UTC), EPOCH)


def test_at_switch_is_new() -> None:
    """切换点**本身**算新口径（``created_at >= switched_at``）。"""
    assert not is_legacy_event(SWITCHED, EPOCH)


def test_after_switch_is_new() -> None:
    assert not is_legacy_event(dt.datetime(2026, 10, 3, 11, 0, tzinfo=dt.UTC), EPOCH)


# ---------------------------------------------------------------------------
# 判定：拿不到纪元时必须**保守**
# ---------------------------------------------------------------------------


def test_unknown_epoch_means_new_not_legacy() -> None:
    """没有纪元记录时**一律判新口径**。

    反向（判成旧口径）会凭空污染统计：全库所有事件都被算成旧口径，
    而实际上只有 41 行是。这个方向的错误是**静默放大**，所以默认必须是安全的那个。
    """
    assert not is_legacy_event(dt.datetime(2026, 10, 3, 9, 55, 46, tzinfo=dt.UTC), FactorEpoch())


def test_none_created_at_is_not_legacy() -> None:
    assert not is_legacy_event(None, EPOCH)


def test_incomparable_types_fall_back_to_new() -> None:
    """naive vs aware datetime 不可比 ⇒ 保守判新口径，不抛。"""
    assert not is_legacy_event(dt.datetime(2026, 10, 3, 9, 0), EPOCH)


# ---------------------------------------------------------------------------
# 表形状
# ---------------------------------------------------------------------------


def test_epoch_table_is_single_row() -> None:
    """必须是**单行**表（``CHECK (id = 1)``），否则「切换点」会退化成多口径并存。"""
    assert "CHECK (id = 1)" in EPOCH_DDL
    assert "PRIMARY KEY" in EPOCH_DDL


def test_record_epoch_is_idempotent() -> None:
    """重复登记只更新同一行（``ON CONFLICT``），不追加。

    实测这轮切了**两次**（09:55:46Z、10:34:24Z），所以切换点必须可修正 ——
    刻意不写成 DO NOTHING。
    """
    import ast
    import inspect
    import textwrap

    # ⚠️ 三种朴素做法都踩过：
    #   1) 裸 substring —— docstring 里就写着「刻意不写死成 DO NOTHING」；
    #   2) 按 '"""' 切分 —— docstring 内含 \"\"\" 转义，切出来是错的片段；
    #   3) ``ast.walk`` 收全部 Constant —— **docstring 自己就是 Constant 节点**。
    # 所以：先丢掉函数体第一条语句（docstring），再收字符串常量。
    fn = ast.parse(textwrap.dedent(inspect.getsource(record_epoch))).body[0]
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]                      # 丢掉 docstring
    sql = "\n".join(
        n.value for stmt in body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "DO NOTHING" not in sql, "重复登记必须更新同一行，不能追加"
    assert "INSERT INTO public.cpt_factor_epoch" in sql


def test_current_epoch_never_raises() -> None:
    """纪元是**说明性**的，缺表/无权限都不得让看板 500。"""
    import inspect

    src = inspect.getsource(current_epoch)
    assert "except Exception" in src
    assert "return FactorEpoch()" in src


def test_migration_script_matches_store_ddl() -> None:
    """迁移脚本与 store 的 DDL 不能漂移（两份实现的老毛病）。"""
    mig = ROOT / "scripts" / "migrations" / "2026-10-03_r45_factor_epoch.sql"
    assert mig.exists(), "迁移脚本不存在"
    text = mig.read_text(encoding="utf-8")
    for col in ("switched_at", "old_source", "new_source", "note"):
        assert col in text, f"迁移脚本缺列 {col}"
        assert col in EPOCH_DDL, f"store DDL 缺列 {col}"
    assert "CHECK (id = 1)" in text


def test_migration_states_deploy_order_safety() -> None:
    """迁移是**纯新增表**，不需停服 —— 文档必须写明，否则下个人会照抄 R27 那套停服流程。"""
    text = (ROOT / "scripts" / "migrations"
            / "2026-10-03_r45_factor_epoch.sql").read_text(encoding="utf-8")
    assert "不需要停服" in text or "不需停服" in text, (
        "迁移必须写明『纯新增表、不需要停服』，否则会被误当成交叉迁移"
    )
    # 且不得含有 ALTER TABLE（那才会需要停服）
    assert not re.search(r"ALTER\s+TABLE\s+public\.cpt_signal_event", text, re.I), (
        "这个迁移只该新增表，不该改 cpt_signal_event"
    )
