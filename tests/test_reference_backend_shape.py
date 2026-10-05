"""``reference`` 后端两条路的**形状契约**（R45 P1-3）。

## 为什么

腾讯回落路返回的是**已归一化的 dict**（``start_time``/``end_time``，毫秒），
而后端契约 :class:`~cpt.adapters.reference_chanlun.ChanlunResult` 的
``BiRaw``/``ZsRaw`` 要的是 **bar 索引**。**两者量纲不同。**

第一版硬转，把 ``start_bar`` 全设成 0 ⇒ 这个后端**看起来能用**，
实际交出去的是**没有时间锚点的数据**。拿它做点选定位会静默错位。

⇒ 现在走腾讯路**直接抛** :class:`IncompleteReferenceError`，
让「不完整」变成**响亮的失败**而不是沉默的错值。

## 两种「拿不到」必须能区分

- :class:`ReferenceUnavailableError` = **环境不支持** ⇒ 可以回落
- :class:`IncompleteReferenceError` = **形状不对** ⇒ 回落也救不了

混成一个，调用方会以为「再试一次就好」。

全程离线：后端整体替换成假实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters.reference_backend import (  # noqa: E402
    REFERENCE_SOURCE_CZSC,
    REFERENCE_SOURCE_TENCENT,
    IncompleteReferenceError,
    ReferenceChanlunBackend,
    ReferenceUnavailableError,
)
from cpt.adapters.reference_chanlun import (  # noqa: E402
    ChanlunResult,
    ReferenceChanlunConfig,
)

BARS = [{"close": 1.0}] * 40
CFG = ReferenceChanlunConfig()


class _Stub(ReferenceChanlunBackend):
    """固定走某一条路，不碰 czsc/腾讯。"""

    def __init__(self, source: str) -> None:
        super().__init__(code="600519")
        self._source = source
        self.domain_calls = 0

    def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
        self.domain_calls += 1
        self.source = self._source
        self.detail = f"stub:{self._source}"
        return (
            [{"kind": "top", "bar_index": 1, "high": 1.0, "low": 1.0, "level": 0}],
            [{"direction": 1, "high": 1.0, "low": 1.0, "level": 0}],
            [],
        )


def test_two_failure_kinds_are_distinct() -> None:
    assert IncompleteReferenceError is not ReferenceUnavailableError
    assert not issubclass(IncompleteReferenceError, ReferenceUnavailableError)


def test_czsc_path_returns_full_result() -> None:
    """czsc 路走契约方法没问题 —— 它给的是领域对象，索引/time 都在。"""
    b = _Stub(REFERENCE_SOURCE_CZSC)
    out = b.compute_structures(BARS, CFG)
    assert isinstance(out, ChanlunResult)
    assert len(out.bi_list) == 1


def test_tencent_path_refuses_instead_of_returning_broken() -> None:
    """⚠️ 核心：走腾讯路**必须抛**，不能返回 ``start_bar=0`` 的残缺结果。"""
    b = _Stub(REFERENCE_SOURCE_TENCENT)
    with pytest.raises(IncompleteReferenceError) as ei:
        b.compute_structures(BARS, CFG)
    msg = str(ei.value)
    assert "bar 索引" in msg and "时间锚点" in msg
    assert "compute_domain_structures" in msg, "报错要说清该改用哪个接口"


def test_domain_path_still_works_for_tencent() -> None:
    """腾讯路的**正确**入口仍然可用 —— parity 层就走这条。"""
    b = _Stub(REFERENCE_SOURCE_TENCENT)
    fx, bi, zs = b.compute_domain_structures(BARS, CFG)
    assert b.domain_calls == 1
    assert len(fx) == 1 and len(bi) == 1 and zs == []


def test_unavailable_is_still_raised_by_domain_path() -> None:
    """环境不支持时仍是 ``ReferenceUnavailableError``（不是新那个）。"""

    class _Boom(_Stub):
        def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
            raise ReferenceUnavailableError("czsc 未安装且腾讯取不到")

    with pytest.raises(ReferenceUnavailableError):
        _Boom(REFERENCE_SOURCE_CZSC).compute_structures(BARS, CFG)
