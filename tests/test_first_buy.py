"""一买/一卖结构谓词的基础测试（R14-4，精简版）。

只保留：
1. **人工构造案例** —— 逐条覆盖 check_first_buy / check_first_sell 的门槛；
2. **工具函数** —— round_to_2_digit / mean 的边界；
3. **与 czsc 交叉验证** —— 用 czsc 的信号模板逐 n 比对。

注：czsc 适配器已随 R96 按需因子链删除，但 czsc 交叉验证仍直接使用 czsc 原生 API。
"""

from __future__ import annotations

import csv
import pathlib
from datetime import UTC, datetime

import pytest
from cpt.domain.first_buy import (
    check_first_buy,
    check_first_sell,
    mean,
    round_to_2_digit,
)
from cpt.domain.models import Bi

#: czsc 的「真正不限」笔数取值（原 czsc_chanlun 模块已删，此处内联）。
MAX_BI_NUM_UNLIMITED: int = 10**9

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "oracle"

#: czsc Direction → +1/-1（原 czsc_chanlun 模块已删，此处内联）。
_N_BI_DIR_NAMES_UP = frozenset({"Up", "向上"})
_N_BI_DIR_NAMES_DOWN = frozenset({"Down", "向下"})

def _bi_direction(value: object) -> int:
    name = getattr(value, "name", None) or str(value)
    if name in _N_BI_DIR_NAMES_UP:
        return 1
    if name in _N_BI_DIR_NAMES_DOWN:
        return -1
    return int(name) if name in ("1", "-1") else 0


