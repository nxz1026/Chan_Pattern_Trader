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

#: 40 根**各不相同**的日线。原来的 ``[{"close": 1.0}] * 40`` 既没有
#: ``open_time``、40 根还完全相同 —— 锚点换算根本无从验证，所以「丢时间锚点」
#: 这个缺陷能在测试全绿的情况下活下来（2026-10-06 修）。形状契约要能守住
#: 缺陷，前提是夹具本身是真实的。
_DAY_MS = 86_400_000
_BASE_T = 1_700_000_000_000
BARS = [{"open_time": _BASE_T + i * _DAY_MS, "close": 1.0 + i} for i in range(40)]
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
        # 真实的领域对象**都带时间**（Bi/ZhongShu 的 start_time/end_time），
        # 原来的 stub 不带，正是为了迁就硬编码 0 的实现。
        return (
            [{"kind": "top", "bar_index": 1, "high": 1.0, "low": 1.0, "level": 0}],
            [
                {
                    "direction": 1,
                    "high": 1.0,
                    "low": 1.0,
                    "level": 0,
                    "start_time": _BASE_T + 5 * _DAY_MS,
                    "end_time": _BASE_T + 20 * _DAY_MS,
                    "source_ids": ("fx:5:top", "fx:20:bottom"),
                }
            ],
            [
                {
                    "high": 1.0,
                    "low": 1.0,
                    "level": 0,
                    "start_time": _BASE_T + 8 * _DAY_MS,
                    "end_time": _BASE_T + 18 * _DAY_MS,
                    "bi_ids": ("fx:5:top",),
                }
            ],
        )


def test_two_failure_kinds_are_distinct() -> None:
    assert IncompleteReferenceError is not ReferenceUnavailableError
    assert not issubclass(IncompleteReferenceError, ReferenceUnavailableError)


def test_czsc_path_returns_full_result() -> None:
    """czsc 路走契约方法没问题 —— 它给的是领域对象，索引/time 都在。

    2026-10-06 补：原来只断言 ``len(out.bi_list) == 1``，**从不断言
    ``start_bar``** —— 所以「每笔每中枢都锚在第 0 根 K 线上」这个缺陷
    （R45 只修了腾讯分支，czsc 分支照旧落进 ``_from_domain``）能在测试全绿的
    情况下长期存活。这条断言就是那个缺口。
    """
    b = _Stub(REFERENCE_SOURCE_CZSC)
    out = b.compute_structures(BARS, CFG)
    assert isinstance(out, ChanlunResult)
    assert len(out.bi_list) == 1
    # 真实锚点：stub 里的笔跨 5 → 20 号 bar
    (bi,) = out.bi_list
    assert (bi.start_bar, bi.end_bar) == (5, 20)
    # 中枢同理，且成员笔不再是空元组（bi_indices 是「第几笔」，与 native 同语义）
    (zs,) = out.zs_list
    assert (zs.start_bar, zs.end_bar) == (8, 18)
    assert zs.bi_indices == (0,), f"中枢成员笔下标丢了：{zs.bi_indices}"


def test_czsc_path_raises_when_anchor_is_unresolvable() -> None:
    """锚点**解析不到**时必须抛，不许静默回落成 0。

    0 不是「缺省位置」，它是**第一根 K 线** —— 所有笔和中枢被锚到同一根上，
    图看着「有结构」但每个位置都错，比直接报错难查得多。
    """

    class _OffGrid(_Stub):
        def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
            fx, bi, zs = super().compute_domain_structures(bars, config)
            return fx, [{**bi[0], "start_time": 12345}], zs  # 时间轴上不存在

    with pytest.raises(IncompleteReferenceError, match="时间锚点"):
        _OffGrid(REFERENCE_SOURCE_CZSC).compute_structures(BARS, CFG)


def test_tencent_path_refuses_instead_of_returning_broken() -> None:
    """⚠️ 核心：走腾讯路**必须抛**，不能返回 ``start_bar=0`` 的残缺结果。"""
    b = _Stub(REFERENCE_SOURCE_TENCENT)
    with pytest.raises(IncompleteReferenceError) as ei:
        b.compute_structures(BARS, CFG)
    msg = str(ei.value)
    assert "bar 索引" in msg and "时间锚点" in msg
    assert "compute_domain_structures" in msg, "报错要说清该改用哪个接口"


def test_domain_path_still_works_for_tencent() -> None:
    """腾讯路的**正确**入口仍然可用 —— parity 层就走这条。

    这条接口直接交出**带时间**的结构（不做 bar 下标换算），所以断言的是时间
    锚点在、而不是下标 —— 这正是它与 ``compute_structures`` 的量纲差别。
    """
    b = _Stub(REFERENCE_SOURCE_TENCENT)
    fx, bi, zs = b.compute_domain_structures(BARS, CFG)
    assert b.domain_calls == 1
    assert len(fx) == 1 and len(bi) == 1 and len(zs) == 1
    assert bi[0]["start_time"] == _BASE_T + 5 * _DAY_MS
    assert zs[0]["end_time"] == _BASE_T + 18 * _DAY_MS


def test_unavailable_is_still_raised_by_domain_path() -> None:
    """环境不支持时仍是 ``ReferenceUnavailableError``（不是新那个）。"""

    class _Boom(_Stub):
        def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
            raise ReferenceUnavailableError("czsc 未安装且腾讯取不到")

    with pytest.raises(ReferenceUnavailableError):
        _Boom(REFERENCE_SOURCE_CZSC).compute_structures(BARS, CFG)
