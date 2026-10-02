"""domain 层的**语义契约**门禁（R30）。

## 这条门禁防的是什么

R28-9 抓到一个已上生产的 bug：``RulesConfig.levels`` 的 docstring 写「级别链，
元素为**分钟**级别（单位：分钟）」，而 A 股喂的是 ``daily_bar`` **日线**。LLM
读到后如实照讲，把 A 股日线结构解释成「**5 分钟级别**的一笔」。

**测试全绿、CI 全绿、domain 域内计算全对** —— 因为域内只比较 level 的相对大小，
`5` 在两个市场都能跑通。错的只有**标签**，而错误的标签会被 LLM 当成结论讲给人。

R28-9 修了**消费端**（`domain/levels.py` + 提示词），但**源头那句错话一直留着**。
本文件把它钉住。

## 为什么用「文档断言」这种看着奇怪的测试

这类 bug 没有任何运行时症状可测 —— 域内计算是对的。所以唯一能防的���是
**让错误的前提没法以文档形式留在权威位置**。这不是为了测文档本身，
而是因为本仓把「口径」写进 docstring 是既定风格（``docs/rules.md`` 同理），
那么口径写错就是 bug，文档断言就是它的回归测试。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[1] / "cpt" / "domain"

#: 出现「单位是分钟」这种**无条件**断言的位置，就该被盯上。
#: 出现「分钟」本身没问题（A 股那条要说明「不是 5 分钟」），
#: 有问题的是**没有限定市场**就断言单位。
_UNQUALIFIED = re.compile(r"(?:元素为|单位[：:]\s*)\s*\*?\*?分钟级别|单位[：:]\s*分钟")


def _doc_text(path: Path) -> str:
    """抽出**真正的 docstring** 与注释行。

    第一版按「行首是不是 ``#`` / ``\"\"\"`` / ``:param``」过滤 —— 结果漏掉了
    docstring 的**正文行**（它们不以任何标记开头），于是真话放回去时门禁没响，
    只有另一条测试抓到。是红绿对照把这个洞逼出来的。
    改用 ``ast`` 取每个模块/类/函数的 docstring，注释单独按行首取。
    """
    import ast

    src = path.read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:  # 文件坏了就不谈语义门禁了，交给语法检查报错
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc:
                out.append(doc)
    out.extend(line for line in src.splitlines() if line.lstrip().startswith("#"))
    return "\n".join(out)


#: **元叙述**标记：出现在命中处**上下文**里时，说明这行是在*描述*这条错误断言的
#: 历史，而不是*断言*它自己。
#:
#: 第一版只按「同一行」判断，于是「这句话原本无条件写『单位：分钟』」这种说明
#: 文字把自己判成了违规 —— 门禁分不清「断言 X」与「描述我们曾断言 X」。第二版
#: 改成看所在行，还是漏：解释里的「原本」被换行拆到**上一行**去了。
#: 现在看命中处前后各 160 字符 —— 解释与断言本就在同一段里。
_META = ("原本", "曾经", "已改", "R28-9", "R30", "错在", "错的", "误", "不是", "而非")
_META_WINDOW = 160


def _is_meta(text: str, start: int) -> bool:
    window = text[max(0, start - _META_WINDOW) : start + _META_WINDOW]
    return any(marker in window for marker in _META)


@pytest.mark.parametrize(
    "name", ["config.py", "models.py", "recursion.py", "structure_events.py", "levels.py"]
)
def test_no_unqualified_minute_claim(name: str) -> None:
    """任何文件都不得**无条件**断言 level 的单位是分钟。"""
    doc = _doc_text(DOMAIN / name)
    hits = [
        (i, line.strip())
        for i, line in enumerate(doc.splitlines(), 1)
        if _UNQUALIFIED.search(line) and not _is_meta(doc, doc.find(line))
    ]
    assert not hits, f"{name} 仍在无条件断言「单位是分钟」：\n" + "\n".join(
        f"  {i}: {line}" for i, line in hits
    )


def test_models_documents_that_level_unit_is_market_dependent() -> None:
    """``models.py`` 是六个 ``level: int`` 字段的权威出处，必须说清单位按市场而异。

    它之前**一个字都没写** —— 读者唯一的依据就是 ``config.py`` 那句错的。
    """
    doc = (DOMAIN / "models.py").read_text(encoding="utf-8")
    assert "按市场而异" in doc
    assert "level_label" in doc, "应指向 cpt.domain.levels.level_label 而不是让人自己换算"
    # 日线反例要写出来，否则读者仍可能以为「分钟」是默认值
    assert "日线" in doc


def test_config_points_at_the_label_table() -> None:
    """``config.py`` 的 ``levels`` 字段要把人导向 ``level_label``。"""
    doc = (DOMAIN / "config.py").read_text(encoding="utf-8")
    i = doc.find("levels:")
    assert i > 0, "找不到 levels 字段的文档"
    field_doc = doc[i : i + 600]
    assert "按市场而异" in field_doc
    assert "level_label" in field_doc
    assert "日线" in field_doc


def test_semantic_fact_is_pinned_by_a_runtime_test() -> None:
    """纯文档断言不够 —— 语义本身（同一个数字两个市场含义不同）必须有运行时测试。

    ``tests/test_market_levels.py``（R28-9）就是这个运行时测试：
    ``level_label("a_share", 5) != level_label("crypto", 5)``。
    这里只做一次「它还在」的交叉引用，避免将来被当成冗余测试删掉。
    """
    target = Path(__file__).resolve().parents[1] / "tests" / "test_market_levels.py"
    assert target.exists(), "test_market_levels.py 没了 —— 语义本身的钉子被拔掉了"
    src = target.read_text(encoding="utf-8")
    assert "def test_a_share_level_5_is_daily_not_five_minutes" in src
    assert "def test_crypto_level_5_stays_five_minutes" in src


def test_domain_still_has_no_third_party_imports() -> None:
    """顺手复钉一条：domain 只依赖 stdlib 与包内相对导入。

    与 ``.importlinter`` 的「Domain has no third-party deps」重复，但放在这里
    能让「读这一层的人」不必先知道 import-linter 的存在。
    """
    stdlib = {
        "__future__",
        "typing",
        "dataclasses",
        "collections",
        "abc",
        "enum",
        "math",
        "functools",
        "itertools",
        "json",
        "re",
        "sys",
        "time",
        "datetime",
        "types",
        "contextlib",
    }
    bad: list[str] = []
    for path in sorted(DOMAIN.glob("*.py")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            mod = stripped.split()[1].split(".")[0]
            if mod not in stdlib and mod != "cpt":
                bad.append(f"{path.name}: {stripped}")
    assert not bad, "domain 引入了非 stdlib 依赖：\n" + "\n".join(bad)
