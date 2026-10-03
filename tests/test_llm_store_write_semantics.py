"""LLM 调用审计表的**写入失败语义**（R45）。

**全程离线**：不碰 DB、不联网。

## 这条 bug 比 signal_event 那条更危险，因为它**对用户撒谎**

`enqueue_call` 原来在 except 里 `return False`，于是

    写失败      → False
    重复提交    → False     ← 同一个值

调用方 `llm_cases.explain_structure` 据此回：

    {"status": "duplicate", "reason": "same_request_in_flight_or_done"}

也就是对用户说「你已经问过了」。而真相是：**一条都没写进去、LLM 从未被调用**。
本模块头的审计约束「每次调用的模型与 token 用量必须落盘」被静默违反。

## 为什么外层的 ``_write`` 救不了

`cpt/application/llm_cases._write` = ``fn(...)`` + ``conn.commit()``，
设计意图是「不让调用点忘记 commit」。但实测 PostgreSQL 18.6：

    ① 语句失败: UndefinedTable
    ② commit(): **没抛**   ← 事务在 aborted 态下 COMMIT 等于 ROLLBACK

⇒ 只要 store 函数吞掉异常，外层 commit 会**静默回滚**并原样返回那个假 False。
那道防线**只在 store 抛异常时有效**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mod(name: str, dotted: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{dotted}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


lcs = _mod("_st_llm", "cpt/storage/llm_call_store")


class _FailingConn:
    """execute 一律失败（模拟 DB 故障）。"""

    aborted = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, *a: Any, **k: Any) -> None:
        type(self).aborted = True
        raise RuntimeError("simulated DB write failure")

    @property
    def rowcount(self) -> int:
        return 0


class _OkCursor:
    """execute 成功但被 ON CONFLICT 挡掉（rowcount=0 ⇒ 重复提交）。"""

    def __init__(self, conn: "_DupConn") -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, *a: Any, **k: Any) -> None:
        pass

    @property
    def rowcount(self) -> int:
        return 0


class _DupConn:
    aborted = False

    def cursor(self) -> _OkCursor:
        return _OkCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _row() -> dict[str, Any]:
    return lcs.call_row(
        purpose="p", subject_id="s", request_hash_value=lcs.request_hash("p", "s", "u")
    )


# ---------------------------------------------------------------------------
# 核心契约
# ---------------------------------------------------------------------------


def test_write_failure_raises_not_false() -> None:
    """核心回归：写失败必须抛，**不能**与「重复」共用 ``False``。"""
    with pytest.raises(lcs.LLMCallError):
        lcs.enqueue_call(_FailingConn(), _row())


def test_duplicate_still_returns_false() -> None:
    """对照组：真·重复提交仍然返回 ``False``（业务事实，不是故障）。"""
    assert lcs.enqueue_call(_DupConn(), _row()) is False


def test_the_two_outcomes_are_distinguishable() -> None:
    """「写失败」与「重复」必须能被调用方区分开 —— 这正是 R45 之前做不到的。"""
    dup = lcs.enqueue_call(_DupConn(), _row())
    try:
        lcs.enqueue_call(_FailingConn(), _row())
        failed = None
    except lcs.LLMCallError as exc:
        failed = exc
    assert failed is not None, "写失败应当抛"
    assert dup is False, "重复仍应当回 False"
    assert failed is not None and dup is not failed


def test_llm_call_error_is_exported() -> None:
    """异常要在 ``__all__`` 里 —— 调用方要 import 它来区分。"""
    assert "LLMCallError" in lcs.__all__
