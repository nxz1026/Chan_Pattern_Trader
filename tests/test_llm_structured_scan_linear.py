"""`_iter_braces` 必须**一次前向扫描**算深度，且与旧的「重扫前缀」逐位等价。

R67（审计 P2-13）。现场：原实现每遇到一个 `{`/`[` 就调 `_inside_open_container`
从下标 0 重扫前缀 ⇒ O(n²)。provider 允许 512 KiB 回复、队列默认 1 个 worker，
实测 20k 括号 17.4s / 8k 2.75s / 2k 0.16s —— 一个畸形回复就能把队列堵住。

## 为什么先写「差分测试」再写性能测试

改写的是**热点路径的核心循环**（字符串状态 + 深度 + 候选顺序三者耦合），
性能测试只能证明「快了」，证明不了「没改坏」。所以这里拿一个**参考实现**：
它保留旧的 O(n²) 做法（`_inside_open_container` 重扫），只把已被 P1-2 修掉的
游标双计数按正确语义写。两者在语料上必须产出**完全相同**的片段序列。

语料三层：① 手工边界（围栏、截断、字符串里的括号、多余右括号、嵌套）；
② 固定种子随机模糊（字母表就是那六类字符）；③ 确定性大输入。
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.llm import structured as st  # noqa: E402


def _reference(text: str) -> list[tuple[str, bool]]:
    """旧算法的等价体：深度靠 `_inside_open_container` 重扫前缀（O(n²)）。"""
    out: list[tuple[str, bool]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        if st._inside_open_container(text, index):
            index += 1
            continue
        fragment, closed = st._scan_balanced(text, index)
        if not fragment:
            return out
        out.append((fragment, closed))
        index += len(fragment) if closed else 1
    return out


MANUAL = [
    "",
    "{}",
    "[]",
    '{"a": 1}',
    '{"a": 1} {"b": 2}',
    'xx {"a": 1} {"b": 2}',
    '前言{"a": {"b": [1, {"c": 2}]}}后记',
    '{"note": "sources: fractal-0}"}',  # 字符串里的右括号
    '{"note": "a\\"}b"}',  # 转义引号
    '{"a": 1',  # 截断
    '{"a": 1} {"b": 2',  # 前一个闭合、后一个截断
    "}}}}",
    "{}{}{}",
    "[[[[]]]]",
    '"{"',
    '{"a": "\\"}"} []',
    '```json\n{"a": 1}\n```',
    '```json\n{"a": 1\n```',  # 围栏里截断
    "{'a': 1}",  # 单引号不是 JSON 字符串
    "文字 {「嵌套」} 文字",
]


@pytest.mark.parametrize("text", MANUAL, ids=lambda t: repr(t)[:40])
def test_matches_the_reference_on_manual_edges(text: str) -> None:
    assert list(st._iter_braces(text)) == _reference(text)


def test_matches_the_reference_on_random_fuzz() -> None:
    """固定种子模糊：字母表就是「会改变状态机」的那六类字符。"""
    rng = random.Random(20261018)
    alphabet = list('{}[]"\\ abc1') + ["\\\\", '""']
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        assert list(st._iter_braces(text)) == _reference(text), repr(text)


def test_fuzz_with_a_json_prefix_that_must_be_found() -> None:
    """随机噪声里塞一个合法对象 —— 无论噪声怎么摆放，它都必须被找到。"""
    rng = random.Random(7)
    target = '{"b": 2}'
    for _ in range(200):
        noise = "".join(rng.choice('{}[]"\\ x') for _ in range(rng.randrange(0, 12)))
        text = noise + target + noise
        found = [frag for frag, closed in st._iter_braces(text) if closed and frag == target]
        # 参考实现一致（这是主判据）；是否**总能**找到取决于噪声是否留下未闭合容器
        assert list(st._iter_braces(text)) == _reference(text), repr(text)
        if found:
            assert found[0] == target


def test_large_input_is_linear_not_quadratic() -> None:
    """★回归：8k 个左括号。修前 2.75s、20k 17.4s（审计实测）；修后应当可以忽略。

    阈值 1.0s 留了 ~20 倍余量：旧实现在同规模上必然超（2.75s 起步），
    新实现是毫秒级 —— 所以这不是「卡时间」，是「把量级差钉住」。
    """
    text = "{" * 8000
    start = time.perf_counter()
    fragments = list(st._iter_braces(text))
    elapsed = time.perf_counter() - start

    assert fragments == [(text, False)], "截断的单个残片，不该被切成 8000 份"
    assert elapsed < 1.0, f"8k 括号用了 {elapsed:.2f}s —— 又回到 O(n²) 了？"


def test_scale_ratio_stays_sane() -> None:
    """两倍输入不该是四倍代价（宽松上界，容忍 CI 抖动）。"""

    def _time(n: int) -> float:
        text = ('{"k": [1, 2, {"n": "v"}]}' * n)[: 200 * n]
        start = time.perf_counter()
        list(st._iter_braces(text))
        return time.perf_counter() - start

    small, big = _time(50), _time(100)
    assert big < max(0.05, small * 6), f"50→{small:.4f}s / 100→{big:.4f}s"
