"""门禁⑥：LLM 入队骨架必须是**唯一**实现（R45 P0-3）。

## 为什么要有这道门禁

``explain_structure`` 与 ``summarize_recommendation`` 曾**各抄一份**入队逻辑
（76 行 / 68 行）。抄代码会连 bug 一起抄 ——
「重复提交返回库里不存在的 call_id」那个 bug **两份各有一份**，
且 explain 早就中招了，是补 summarize 的测试才发现。

⇒ 骨架抽出来后，用 AST 锁住「**只有一份**」。

## 判据为什么必须用 AST

这个问题的本质是**调用点**统计，grep 不成立。
本次判据错了**两次**才做对（都记在下面）：

  1. ``src.count("enqueue_call")`` —— import 与注释也算进去（报 5）
  2. 排除 ``#`` 开头的行 —— **docstring 里的提及不算注释**（报 4）
     而且 ``enqueue_call`` 是**函数引用**（``_write(conn, enqueue_call, row)``），
     压根不是 ``enqueue_call(...)`` 形式，只看 ``ast.Call`` 会数出 0，
     看着像「骨架没调用它」。

验收：`_enqueue_and_submit` 是**唯一**实现吗？

⚠️⚠️ 判据错了**两次**才做对：
  1. `src.count("enqueue_call")` —— 把 import 和注释也算进去（报 5）
  2. 排除 ``#`` 开头行 —— docstring 里的提及和 ``def`` 定义行**不算注释**，
     照样被算进去（报 4）
⇒ 「有没有第二份实现」是**调用点**问题，**只能用 AST 判**，grep 不成立。
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "cpt" / "application" / "llm_cases.py"
tree = ast.parse(SRC.read_text(encoding="utf-8"))


# 函数体内对该符号的**真实调用**（排除 def 本身与属性访问）
def call_sites(name: str) -> list[int]:
    """**直接调用** ``name(...)`` 或 ``obj.name(...)`` 的行号。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == name:
            out.append(node.lineno)
        elif isinstance(f, ast.Attribute) and f.attr == name:
            out.append(node.lineno)
    return sorted(out)


def ref_sites(name: str) -> list[int]:
    """**当值传**给别的函数的行号（如 ``_write(conn, enqueue_call, row)``）。

    ``enqueue_call`` 从来不是 ``enqueue_call(...)`` 形式 —— 它是**函数引用**，
    所以只看 ``ast.Call.func`` 会数出 0，看着像「骨架没调用它」。
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            out.append(node.lineno)
    return sorted(set(out))


def defs(name: str) -> list[int]:
    return [n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]


checks = [
    ("_enqueue_and_submit 调用点", len(call_sites("_enqueue_and_submit")), 2),
    ("enqueue_call 引用点（函数引用）", len(ref_sites("enqueue_call")), 1),
    ("_existing_call_id 调用点", len(call_sites("_existing_call_id")), 1),
    ("_bootstrap 调用点", len(call_sites("_bootstrap")), 1),
    ("queue.submit 调用点", len(call_sites("submit")), 1),
]
ok = True
for name, got, want in checks:
    good = got == want
    ok &= good
    print(f"  {'✅' if good else '❌'} {name:28s} {got}  (期望 {want})")

dup = [
    n.name
    for n in tree.body
    if isinstance(n, ast.FunctionDef)
    and sum(1 for m in tree.body if isinstance(m, ast.FunctionDef) and m.name == n.name) > 1
]
print(f"  {'✅' if not dup else '❌'} 无重复定义的函数 {dup}")

# 入队逻辑是否**只**在骨架里：骨架之外不得出现 enqueue/queue 相关调用
skeleton = next(
    n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_enqueue_and_submit"
)
sk_lines = set(range(skeleton.lineno, (skeleton.end_lineno or 0) + 1))
stray = [ln for ln in call_sites("enqueue_call") + call_sites("_bootstrap") if ln not in sk_lines]
print(f"  {'✅' if not stray else '❌'} 骨架外无入队调用 {stray}")
sys.exit(0 if ok and not dup and not stray else 1)
