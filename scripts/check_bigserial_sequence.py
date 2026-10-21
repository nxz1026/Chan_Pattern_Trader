#!/usr/bin/env python3
"""门禁⑯：**bigserial 序列必须与表对齐**（R72 新增）。

## 为什么要这道门

生产上真实撞过三次 `UniqueViolation`（2026-10-20 / 10-21 / 10-22），全是同一个病：

    psycopg.errors.UniqueViolation: duplicate key value violates unique
    constraint "cpt_structure_event_pkey"
    DETAIL: Key (id)=(900008) already exists

``id`` 是 ``bigserial``，插入不带 id，取号靠序列。一旦**有代码带 id 显式写入**
—— 迁移脚本的 ``INSERT ... SELECT d.id``、测试的 ``setval`` 回滚 —— 显式 id
就可能超过序列当前值。此后每次 ``nextval`` 都落在已占用区间上：

- **不会自愈**：序列不会自己跳过已占用的号；
- **每次跑批都炸**：不是偶发，是恒定失败；
- **修了又坏**：手工 ``setval`` 之后，只要再跑一次带 id 的脚本就复发。

实测三次修复后仍复发，说明**手工 setval 不是解**。R72 的处置是两条：

1. 运行时自愈：``cpt/storage/structure_event_store.ensure_id_sequence``
   在每次批量写之前把它推到 ``max(id)``（幂等，已对齐时零写入）。
2. **这道门禁**：防止自愈被删、被绕过、被挪到 INSERT 之后。

## 判据

**R1 · 自愈必须存在且在写入之前。**
``append_events`` 的 AST 里，对 ``ensure_id_sequence`` 的调用**必须**出现在
``cur.executemany`` 之前。放后面等于「撞了再修」—— 这批已经废了，还得重跑。

**R2 · 自愈只允许有一份实现。**
与门禁⑥ 同一个理由：抄代码会连 bug 一起抄。``setval`` 是危险操作，
留两份就迟早有一份忘了 ``COALESCE(max(id), 1)``（空表 NULL 会让 setval 报错）。

**R3 · setval 的第三参必须是 true。**
``setval(seq, N, true)`` ⇒ 下一个 ``nextval`` 返回 N+1；
写 ``false`` 会**重发 N 本身** —— 而 N 就是 ``max(id)``，必撞。
这与回滚脚本 ``2026-10-19_r67_symbol_in_id_rollback.sql:95`` 的写法一致。

**R4 · 只允许 ``bigserial`` 表用裸 id 插入。**
反向证据：没有序列的表（``id`` 由外部提供）不该出现 ``ensure_id_sequence``，
否则是抄错了地方。这条只对**已知的 bigserial 表**做白名单核对。

## 为什么用 AST 而不是 grep

同门禁⑥：``ensure_id_sequence`` 这个名字会出现在 ``__all__``、docstring、
``def`` 定义行里。grep 数出来是「几处提及」，而判据要的是
「**调用点**在不在 executemany 前面」—— 这是结构问题，只有 AST 答得了。

## 用法

    python scripts/check_bigserial_sequence.py

退出码：0 = 全绿，1 = 有违规。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "cpt" / "storage" / "structure_event_store.py"

#: ``bigserial`` 表 → 负责它的 store 模块。
#:
#: 新增一张 bigserial 表就加一行，并在对应 store 里接上 ``ensure_id_sequence``。
#: **不要**把非 bigserial 表加进来 —— 它们不该有序列自愈。
BIGSERIAL_TABLES = {
    "cpt_structure_event": "cpt/storage/structure_event_store.py",
    "cpt_signal_event": "cpt/storage/signal_event_store.py",
}


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _attr_calls(node: ast.AST, attr: str) -> list[int]:
    """``xxx.<attr>(...)`` 的行号。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr == attr:
                out.append(sub.lineno)
    return sorted(out)


def _name_calls(node: ast.AST, name: str) -> list[int]:
    """裸调用 ``name(...)`` 的行号。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == name:
                out.append(sub.lineno)
    return sorted(out)


def check_r1(tree: ast.Module) -> tuple[bool, str]:
    """R1：``ensure_id_sequence`` 必须在 ``executemany`` 之前调用。"""
    fn = _find_function(tree, "append_events")
    if fn is None:
        return False, "找不到 append_events —— 结构变了，本门禁需要更新"

    self_heal = _name_calls(fn, "ensure_id_sequence")
    inserts = _attr_calls(fn, "executemany")
    if not self_heal:
        return False, "append_events 里没有调用 ensure_id_sequence（自愈被删了）"
    if not inserts:
        return False, "append_events 里没有 executemany —— 结构变了"
    if min(self_heal) > min(inserts):
        return (
            False,
            f"ensure_id_sequence 在 L{min(self_heal)}，晚于 executemany L{min(inserts)}"
            " —— 撞了再修，这批已经废了",
        )
    return True, f"自愈 L{min(self_heal)} 早于写入 L{min(inserts)}"


#: 允许出现在 store 里的自愈调用名。
#:
#: ``ensure_sequence_at`` 是**唯一实现**（参数化表/序列）；
#: ``ensure_id_sequence`` 只是它对本表的绑定包装。两者都算「接上了自愈」。
SELF_HEAL_CALLS = ("ensure_sequence_at", "ensure_id_sequence")


def check_r2(tree: ast.Module) -> tuple[bool, str]:
    """R2：序列自愈只有一份实现（``ensure_sequence_at``）。"""
    defs = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "ensure_sequence_at"
    ]
    if not defs:
        return False, "ensure_sequence_at 没有定义"
    if len(defs) > 1:
        return False, f"ensure_sequence_at 定义了 {len(defs)} 份"
    # 包装函数只许转调，不许自己再写一份 setval
    wrapper = _find_function(tree, "ensure_id_sequence")
    if wrapper is not None:
        body_src_lines = [
            n.lineno
            for n in ast.walk(wrapper)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("execute", "executemany")
        ]
        if body_src_lines:
            return (
                False,
                f"ensure_id_sequence L{body_src_lines[0]} 自己写了 SQL —— "
                "它只能是 ensure_sequence_at 的包装",
            )
    return True, "唯一定义（包装函数只转调）"


def check_r3(source: str, tree: ast.Module) -> tuple[bool, str]:
    """R3：``setval`` 的第三参必须是 ``true``。"""
    fn = _find_function(tree, "ensure_sequence_at")
    if fn is None:
        return False, "找不到 ensure_sequence_at"
    lines = source.splitlines()
    body = lines[fn.lineno - 1 : (fn.end_lineno or fn.lineno)]
    hits = [i for i, line in enumerate(body) if "setval(" in line]
    if not hits:
        return False, "ensure_id_sequence 里没有 setval —— 那它没在修任何东西"
    for i in hits:
        window = "\n".join(body[i : i + 4])
        if ", true)" not in window:
            return (
                False,
                f"L{fn.lineno + i} 的 setval 第三参不是 true —— "
                "false 会重发 max(id) 本身，必撞主键",
            )
    return True, f"setval(..., true) × {len(hits)}"


def _is_insert(source: str, line_no: int) -> bool:
    """L{line_no} 附近的语句是不是 INSERT（SQL 常跨多行）。"""
    lines = source.splitlines()
    window = "\n".join(lines[max(0, line_no - 2) : line_no + 3]).upper()
    return "INSERT INTO" in window


def check_r4() -> list[str]:
    """R4：白名单里的每个 store 都得真的用上自愈。"""
    problems = []
    for table, rel in BIGSERIAL_TABLES.items():
        path = ROOT / rel
        if not path.exists():
            problems.append(f"{rel} 不存在（{table} 的 store 找不到）")
            continue
        src = path.read_text(encoding="utf-8")
        if not any(call in src for call in SELF_HEAL_CALLS):
            problems.append(f"{rel} 没自愈序列（{table} 的序列会落后）")
            continue
        # 必须有 executor 在 INSERT 之前调用它 —— 只看字面量会出现
        # 「import 了但没调用」的假绿。
        tree = ast.parse(src)
        found = False
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            heal = [ln for name in SELF_HEAL_CALLS for ln in _name_calls(fn, name)]
            inserts = [
                ln
                for ln in _attr_calls(fn, "execute") + _attr_calls(fn, "executemany")
                if _is_insert(src, ln)
            ]
            if heal and inserts and min(heal) < min(inserts):
                found = True
                break
        if not found:
            problems.append(f"{rel} 没有在 INSERT 之前调用自愈（只会撞了再修）")
    return problems


def main() -> int:
    source = STORE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    print("CPT bigserial 序列自愈门禁")
    print("=" * 60)

    ok = True
    for label, result in (
        ("R1 自愈在写入之前", check_r1(tree)),
        ("R2 自愈只有一份实现", check_r2(tree)),
        ("R3 setval 第三参为 true", check_r3(source, tree)),
    ):
        good, detail = result
        ok &= good
        print(f"  {'✅' if good else '❌'} {label}：{detail}")

    r4 = check_r4()
    ok &= not r4
    if r4:
        for problem in r4:
            print(f"  ❌ R4 {problem}")
    else:
        print(f"  ✅ R4 白名单 {len(BIGSERIAL_TABLES)} 张表都已接自愈")

    print("-" * 60)
    print("✅ 全绿" if ok else "❌ 有违规")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
