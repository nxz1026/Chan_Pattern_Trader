#!/usr/bin/env python3
"""CPT 分层门禁：**SQL 只允许出现在 ``cpt/adapters/`` 与 ``cpt/storage/``**。

禁入名单：``cpt/domain``、``cpt/application``、``cpt/web``、``cpt/llm``。

> ``cpt/llm`` 在 2026-10-01（R25）加进名单：LLM 层只调 provider、只拼提示词，
> 结果落库必须走 ``cpt/storage/llm_call_store``。它自己碰 SQL 就等于绕过
> 事务边界约定（store 层不 commit，边界归调用方）。

## 为什么需要它（import-linter 抓不到）

``.importlinter`` 的 3 条契约查的是「有没有 import 上层」。它对下面这种代码
**完全无感**::

    def fetch(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT ... FROM public.derived_bar")   # 这是 domain 层

因为 ``conn`` 只是个 ``Any`` 形参，模块没有 ``import psycopg`` —— 依赖图上看不出
越界，职责却已经跑到 domain 去了。R24 之前就是这样：SQL 铺在 domain /
application / adapters / web **四层**里，而 3 条契约全绿。

## 判定方式

把「注释与 docstring 剥离后的代码」按 token 扫，找
``execute / executemany`` 的**参数里**出现 SQL 语句起始关键字。

这样处理是为了避开两类误报/漏报：

- docstring 里**提到** ``SELECT``（本仓大量模块 docstring 写了列顺序）→ 不误报；
- 行尾注释里的 SQL → 不误报；
- **多行 SQL**（``cur.execute(`` 换行后 SQL 在下一行的三引号里）→ **要能抓到**。
  第一版正则只认单引号，整个 ``signal_event_store`` 因此被判成「无 SQL」，
  是自测时才发现的。

## 已知盲区（诚实声明）

``cur.execute(sql_text)`` 里 SQL 来自变量 / 拼接的，静态扫不出来。本门禁只保证
「**字面量 SQL 不越层**」，不是完备证明。真要完备得引入 AST + 污点分析，那超出
本仓的工具链现状。

用法::

    python scripts/check_sql_layering.py            # 检查 cpt/
    python scripts/check_sql_layering.py <root>     # 指定仓库根
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ALLOWED_DIRS = frozenset({"adapters", "storage"})
FORBIDDEN_DIRS = ("domain", "application", "web", "llm")

#: execute / executemany 的调用点
#:
#: 注意是 ``execute(?:many)?`` 而不是 ``executemany?`` —— 后者匹配的是字面量
#: "executeman" + 可选的 "y"，压根匹配不到 "execute"。这个错让单行 SQL 全部漏网。
_EXEC_CALL = re.compile(r"\bexecute(?:\s*many)?\s*\(", re.IGNORECASE)

#: SQL 语句起始关键字
_SQL_START = re.compile(
    r"\b(?:SELECT\s|INSERT\s+INTO\s|DELETE\s+FROM\s|UPDATE\s|DELETE\s)",
    re.IGNORECASE,
)

#: 从 execute( 往后看多少字符找 SQL 关键字。
#:
#: 不做精确的「参数边界」解析 —— 宁可多报也不要漏报。这是一道**门禁**，
#: 误报的代价是多写一行注释把它挪到 adapters；漏报的代价是分层重新烂掉。
#: 300 字符足够覆盖多行三引号 SQL 的开头（第一版就是这里栽的：正则只认单引号，
#: 整个 signal_event_store 因而被判成「无 SQL」，是自测才发现的）。
_SQL_LOOKAHEAD = 300


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """返回该文件里「execute 的参数含 SQL 字面量」的 (行号, 原文片段)。"""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    cleaned = _clean_source(source)
    raw_lines = source.splitlines()
    hits: list[tuple[int, str]] = []
    for call in _EXEC_CALL.finditer(cleaned):
        tail = cleaned[call.end() : call.end() + _SQL_LOOKAHEAD]
        if not _SQL_START.search(tail):
            continue
        line_no = cleaned.count("\n", 0, call.start()) + 1
        original = raw_lines[line_no - 1].strip() if line_no - 1 < len(raw_lines) else ""
        hits.append((line_no, original[:100]))
    return hits


def _docstring_line_ranges(source: str) -> set[int]:
    """用 ast 精确找出所有 docstring 占据的行号（1-based）。

    只抹 docstring，**不抹普通字符串字面量** —— SQL 本来就是字符串字面量，
    一起抹掉等于把要抓的东西擦掉了（第一版就是这么写的，结果门禁对任何 SQL
    都睁眼瞎）。ast 知道哪些字符串是 docstring，tokenize 知道哪些是注释，
    两者合起来才能既不误报也不漏报。
    """
    ranges: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ranges
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                first = node.body[0]
                end = getattr(first, "end_lineno", first.lineno) or first.lineno
                ranges.update(range(first.lineno, end + 1))
    return ranges


def _clean_source(source: str) -> str:
    """返回与 source **等长**的清洗版：docstring 与注释被替换为空格，行号不变。"""
    chars = list(source)
    for line_no in _docstring_line_ranges(source):
        start = sum(len(x) + 1 for x in source.splitlines()[: line_no - 1])
        # 抹到该行行尾
        end = source.find("\n", start)
        end = len(chars) if end == -1 else end
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    # 注释：按 token 逐个抹掉井号到行尾
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                (row, col) = tok.start
                start = sum(len(x) + 1 for x in source.splitlines()[: row - 1]) + col
                for i in range(start, len(chars)):
                    if chars[i] == "\n":
                        break
                    chars[i] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "".join(chars)


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    cpt = root / "cpt"
    if not cpt.is_dir():
        print(f"✗ 找不到 {cpt}", file=sys.stderr)
        return 2

    violations: list[tuple[str, int, str]] = []
    scanned = 0
    for directory in FORBIDDEN_DIRS:
        target = cpt / directory
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            for line_no, text in _scan_file(path):
                violations.append((str(path.relative_to(root)), line_no, text))

    if violations:
        allowed = "/".join(sorted(ALLOWED_DIRS))
        print(f"\n✗ SQL 越层：SQL 只允许出现在 cpt/{allowed}", file=sys.stderr)
        print(
            "  （cpt/domain、cpt/application、cpt/web、cpt/llm 都不该直接写 SQL）\n",
            file=sys.stderr,
        )
        for rel, line_no, text in violations:
            print(f"  {rel}:{line_no}", file=sys.stderr)
            print(f"      {text}", file=sys.stderr)
        print(
            "\n把查询下沉到 cpt/adapters（外部数据源）或 cpt/storage（CPT 自有表）。\n",
            file=sys.stderr,
        )
        return 1

    print(f"✓ SQL 只在 adapters/ 与 storage/（已扫描 {scanned} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
