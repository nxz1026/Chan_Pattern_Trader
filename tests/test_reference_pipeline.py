"""参考实现归一化白名单（R59 审计 M19）。

``cpt/adapters/reference_pipeline.py`` 的 ``_COMPARE_FIELDS`` 与
``cpt/application/parity_reference.py`` 里那份**重复**的白名单此前零测试引用，
且 ``normalize_structures`` 用 ``raw.get(f)``：字段改名后两侧同时取到 ``None``，
比较恒相等 ⇒ 交叉验证门禁永真且零报错。这里把三件事钉死：

1. 白名单字段确实存在于 ``cpt.domain.models`` 的对应 dataclass；
2. 两份重复的白名单逐字一致；
3. 源对象缺键时 ``normalize_structures`` 显式失败，而不是静默给 ``None``。
"""

from __future__ import annotations

from typing import Any

import pytest
from cpt.adapters.reference_pipeline import (
    _COMPARE_FIELDS,
    StructureFieldMissingError,
    normalize_structures,
)
from cpt.domain.models import Bi, Fractal, ZhongShu

_KIND_TYPES: dict[str, Any] = {"fractal": Fractal, "bi": Bi, "zhongshu": ZhongShu}


def test_whitelist_fields_all_exist_on_domain_dataclasses() -> None:
    """白名单不得引用领域对象没有的字段（字段改名 ⇒ 这条先红）。"""
    for kind, fields in _COMPARE_FIELDS.items():
        domain_fields = set(_KIND_TYPES[kind].__dataclass_fields__)
        assert set(fields) <= domain_fields, f"{kind} 白名单出现了领域对象没有的字段"


def test_whitelist_matches_the_application_side_copy() -> None:
    """本模块与应用侧那份重复白名单必须逐字一致 —— 只改一侧正是 M19 要防的漂移。"""
    from cpt.application.parity_reference import _COMPARE_FIELDS as application_fields

    assert _COMPARE_FIELDS == application_fields


def test_whitelist_covers_exactly_the_three_structure_kinds() -> None:
    assert set(_COMPARE_FIELDS) == {"fractal", "bi", "zhongshu"}


def test_normalize_projects_real_domain_objects_in_whitelist_order() -> None:
    """键序也必须按白名单 —— 否则「顺序不同」会被比较器判成不一致。"""
    fractal = Fractal(
        kind="top",
        level=5,
        bar_index=3,
        start_time=1000,
        end_time=2000,
        high=12.0,
        low=8.0,
        source_ids=("f1",),
    )
    (out,) = normalize_structures([fractal], "fractal")
    assert out == {
        "level": 5,
        "bar_index": 3,
        "start_time": 1000,
        "end_time": 2000,
        "high": 12.0,
        "low": 8.0,
    }
    assert list(out) == list(_COMPARE_FIELDS["fractal"])


def test_normalize_accepts_plain_mappings() -> None:
    raw = {
        "level": 1,
        "start_time": 1,
        "end_time": 2,
        "direction": 1,
        "high": 3.0,
        "low": 1.0,
        "length": 2,
    }
    assert normalize_structures([raw], "bi") == [raw]


def test_normalize_missing_field_fails_loudly_instead_of_none() -> None:
    """核心回归：缺键必须显式失败，不能两侧同时变 None 让门禁恒真。"""
    broken = {"level": 1, "bar_index": 0, "start_time": 1, "end_time": 2, "high": 3.0}
    with pytest.raises(StructureFieldMissingError, match="low"):
        normalize_structures([broken], "fractal")


def test_normalize_unknown_kind_still_key_errors() -> None:
    """未知 kind 保持 ``KeyError``（不吞成空投影）。"""
    with pytest.raises(KeyError):
        normalize_structures([{}], "trend")
