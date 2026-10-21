"""门禁⑯ R5（R81）：``tests/`` 里不许调 ``setval``。

``setval`` 不受事务回滚 —— 这是 PostgreSQL 的明确语义。R81 实测：

    序列 = 910570 → setval(900009) → rollback() → 序列 = 900009

所以测试里 setval 会**永久**污染共享序列，而 pytest 的 rollback 看起来已经
收拾干净了 —— 这是最坏的一类污染：**证据自证清白**。R72 的运行时自愈只是
保险带，主修手段是「测试根本不碰序列」。

本文件是 :data:`scripts.check_bigserial_sequence.check_r5` 的钉子。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_bigserial_sequence.py"


def _gate() -> object:
    name = "_cpt_bigserial_gate_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _problems(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str]) -> list[str]:
    """在临时仓库里跑 R5（把 ROOT 换成 tmp_path）。"""
    gate = _gate()
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)  # type: ignore[attr-defined]
    return gate.check_r5()  # type: ignore[attr-defined]


def test_sql_string_setval_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """真实形态：``cur.execute("SELECT setval(...)")`` —— setval 在**字符串里**。

    这是 psycopg 的常态，也是「只查 AST 调用点」那版判据漏掉的一种。
    """
    problems = _problems(
        monkeypatch,
        tmp_path,
        {
            "tests/test_x.py": (
                "def test_x(cur):\n    cur.execute(\"SELECT setval('seq', 900009, true)\")\n"
            )
        },
    )
    assert len(problems) == 1
    assert "setval" in problems[0]


def test_bare_setval_call_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """另一种形态：直接调 ``setval(...)``。"""
    problems = _problems(
        monkeypatch,
        tmp_path,
        {"tests/test_x.py": "def test_x(c):\n    c.setval('seq', 1, True)\n"},
    )
    assert len(problems) == 1


def test_docstring_mention_is_not_a_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docstring 里提到 setval 是**说明**，不是调用 —— 不许误报。

    这正是 R5 第一版（行文本匹配）踩的坑：它被自己的 docstring 骗了。
    """
    problems = _problems(
        monkeypatch,
        tmp_path,
        {
            "tests/test_x.py": (
                '"""这里说明：原实现调过 ``setval("seq", 1, true)``，已删。"""\n\n'
                "def test_x():\n    assert True\n"
            )
        },
    )
    assert problems == []


def test_non_test_dirs_are_not_scanned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """只查 ``tests/`` —— 生产代码里 setval 是自愈实现的一部分，必须允许。"""
    problems = _problems(
        monkeypatch,
        tmp_path,
        {
            "cpt/storage/x.py": "cur.execute(\"SELECT setval('s', 1, true)\")\n",
        },
    )
    assert problems == []


def test_the_real_repo_is_clean() -> None:
    """真仓里测试一个 setval 都不许有（这是本门禁的存在意义）。"""
    gate = _gate()
    assert gate.check_r5() == []  # type: ignore[attr-defined]


def test_the_migration_fixture_never_touches_the_sequence() -> None:
    """``test_migration_r67_symbol_in_id.py`` 的夹具不许出现 setval 调用。

    它是 R81 的真凶：夹具 setval 到 900009，序列被永久拉回，而真实行在 91 万级
    ⇒ 下一次跑批必撞 ``Key (id)=(900008) already exists``。
    """
    source = (ROOT / "tests" / "test_migration_r67_symbol_in_id.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setval"
    ]
    sql_strings = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "setval(" in node.value
        and "SELECT" in node.value
    ]
    assert calls == []
    assert sql_strings == []


def test_fixture_ids_start_after_the_sequence() -> None:
    """夹具必须把合成行放在「序列之后 + 表的 max(id) 之后」。

    判据不是「等于某个数」——那会随生产数据漂移；而是**起点由
    ``max(max(id), last_value)`` 算出来**，所以插进去的行一定不撞主键。
    """
    source = (ROOT / "tests" / "test_migration_r67_symbol_in_id.py").read_text(encoding="utf-8")
    assert "max(max_id or 0, seq_value or 0)" in source
    assert "pg_sequences" in source
