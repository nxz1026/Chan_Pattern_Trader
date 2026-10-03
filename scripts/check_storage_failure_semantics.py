#!/usr/bin/env python3
"""CPT 分层门禁②：**store 层的失败语义**。

## 为什么需要它（`check_sql_layering.py` 抓不到的那类）

第一道门禁管「SQL 出现在哪一层」。但 SQL 落在**正确**的层里，也可能写出
**错误的失败语义** —— 而且抓不到，因为 import 图看不出异常处理。

R45 在 storage/ 复盘里实测到两个真 bug，形态一模一样：

1. ``signal_event_store.load_previous_signal`` 读失败 ``return None``，
   而调用方 ``a_share_snapshot`` **专门写了** ``_rollback_quietly``（注释里
   写明「连接留在 aborted 态连累后面所有查询」）—— **那段防御是死代码**，
   因为函数自己先吞了异常，调用方的 ``except`` 永不触发。

2. ``llm_call_store.enqueue_call`` 写失败 ``return False``，与「重复提交」
   共用同一个返回值。调用方于是对用户说「你已经问过了」，而真相是
   **一条都没写、LLM 从未被调用** —— 静默违反审计约束。

## 判据

对 ``cpt/storage/`` 与 ``cpt/adapters/`` 里每个函数：若它**执行了 DB 语句**
（``execute`` / ``executemany``）**且** ``except`` 分支里**不 raise**、
而是 ``return None/()/False`` 或裸 ``pass``，则报出来。

理由：PostgreSQL 里「事务中一条语句失败 ⇒ 同一连接后续全部
``current transaction is aborted``」（已实测，PG 18.6）。所以吞掉 DB 异常
**不是降级，是把局部失败放大成整页失败**，而调用方往往无从知晓。

## 已知例外（白名单，必须写清理由）

``# gate: allow-silent`` —— 显式声明「这里就是要降级」，并说明调用方如何知情。
白名单是**有成本的**：想加就得写理由，将来 review 看得见。

用法::

    python scripts/check_storage_failure_semantics.py          # 检查 cpt/
    python scripts/check_storage_failure_semantics.py <root>   # 指定仓库根
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CHECKED_DIRS = ("storage", "adapters")
IGNORE = {"__init__.py"}


def _docstring(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""
    except TypeError:
        return ""


def _has_db_call(fn: ast.FunctionDef) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in ("execute", "executemany"):
                return True
    return False


def _swallows(fn: ast.FunctionDef) -> tuple[bool, str]:
    """返回 (是否吞掉 DB 异常, 证据)。"""
    for handler in [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]:
        tail = ast.Module(
            body=handler.body, type_ignores=[]
        )
        # 注释掉的 raise 不算：用 AST 找真实的 Raise
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(tail))
        if raises:
            continue
        rets = [
            n for n in ast.walk(tail)
            if isinstance(n, ast.Return) and n.value is not None
        ]
        for r in rets:
            v = r.value
            # return False / None / () / []  → 降级
            if isinstance(v, ast.Constant) and v.value in (None, False, True, (), []):
                return True, f"return {v.value!r}"
            if isinstance(v, (ast.Tuple, ast.List)) and not v.elts:
                return True, "return ()"
        # 裸 pass
        if all(isinstance(n, ast.Pass) for n in handler.body):
            return True, "pass（静默吞掉）"
    return False, ""


def _allow_marker(fn: ast.FunctionDef) -> bool:
    """函数级 ``# gate: allow-silent`` 显式豁免。"""
    doc = _docstring(fn)
    return "gate: allow-silent" in doc


def check(root: Path) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    for d in CHECKED_DIRS:
        base = root / "cpt" / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name in IGNORE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                if fn.name.startswith("_") or not _has_db_call(fn):
                    continue
                bad, why = _swallows(fn)
                if not bad or _allow_marker(fn):
                    continue
                rel = path.relative_to(root)
                problems.append((str(rel), fn.name, why))
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    problems = check(root)
    if not problems:
        print("✅ store 失败语义：没有「吞掉 DB 异常」的函数")
        return 0
    print(f"❌ store 失败语义：{len(problems)} 处「吞掉 DB 异常」\n")
    for rel, fn, why in problems:
        print(f"  {rel}::{fn}  —— {why}")
    print(
        "\n为什么是问题：PostgreSQL 里「事务中一条语句失败」会让**同一连接**\n"
        "后续语句全部报 current transaction is aborted（已实测 PG 18.6）。\n"
        "吞掉异常不是降级，是把局部失败放大成整页失败，且调用方无从知晓。\n"
        "\n要么让异常抛出去（调用方 catch + rollback），要么加\n"
        "    # gate: allow-silent: <为什么这里可以降级，调用方如何知情>\n"
        "显式豁免并写清理由。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
