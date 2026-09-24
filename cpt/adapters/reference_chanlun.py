"""缠论后端契约（backend contract）与反腐层数据类。

本模块是 CPT 领域层与外部缠论实现之间唯一的边界：外部实现的对象
（``DataFrame``、Rust 对象、内部结构等）**绝不泄漏进 ``cpt/domain/``**。
边界只做三件事：

1. 把 ``CanonicalBar`` 转成外部实现期望的输入；
2. 调用其公开接口，取回分型 / 新笔 / 笔中枢 / ``level`` / 力度度量；
3. 把结果映射回 CPT 的不可变领域对象（``Fractal`` / ``Bi`` / ``ZhongShu``）。

这样即便未来替换或移除某个后端，``domain/`` 与 ``engine/`` 都零改动。

**当前实现**（2026-09-24 起）：

- :mod:`cpt.adapters.czsc_chanlun` —— 生产后端，走可选依赖 extra ``chan``；
- :mod:`cpt.adapters.native_chanlun` —— CPT 自研后端，回放与离线用；
- :class:`InMemoryChanlunBackend` —— 测试占位后端，基于 bar 高低点做极简
  顶底分型 / 笔判定，不追求真实缠论精度。

本模块本身不 import 任何外部实现，只声明接口与数据类。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cpt.domain.models import Bi, Fractal, ZhongShu
from cpt.domain.types import PLACEHOLDER_TIME, BarLike

__all__ = [
    "ReferenceChanlunConfig",
    "ChanlunBackend",
    "ChanlunResult",
    "FxRaw",
    "BiRaw",
    "ZsRaw",
    "map_fractal",
    "map_bi",
    "map_zhongshu",
    "InMemoryChanlunBackend",
]

#: czsc 固定版本 commit（``docs/reference-audit.md`` 与
#: ``scripts/fetch_references.sh`` 中锁定）。
_CZSC_FIXED_COMMIT = "701e480a545004f945bb1721e510ae610ad90c4c"


@dataclass(frozen=True, slots=True)
class ReferenceChanlunConfig:
    """转发给后端的口径参数（与 ``RulesConfig`` v0 冻结口径对齐）。

    输入口径通过本配置显式声明，避免反腐层内部散落魔法值。
    ``fixed_commit`` 记录依赖的固定版本，便于结果元数据追溯。

    注意 ``use_fx_qy_middle`` / ``use_fx_qj_ck`` / ``use_bi_type_new`` 是
    **历史字段**：czsc 后端不接受这些开关（其分型/笔规则固定），保留它们
    只为兼容既有 fixture 与配置序列化，传了也不生效。
    """

    use_fx_qy_middle: bool = True
    use_fx_qj_ck: bool = True
    use_bi_type_new: bool = True
    zs_wzgx: str = "zgd"
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    fixed_commit: str = _CZSC_FIXED_COMMIT


@dataclass(frozen=True, slots=True)
class FxRaw:
    """后端产出的原始分型（未映射到 domain）。"""

    bar_index: int
    kind: str  # "top" | "bottom"
    high: float
    low: float
    level: int


@dataclass(frozen=True, slots=True)
class BiRaw:
    """后端产出的原始笔（未映射到 domain）。

    ``power_price`` / ``power_volume`` / ``length`` 是**力度度量**（一买/一卖
    背驰比较用，见 :mod:`cpt.domain.first_buy`）；默认 ``0`` 表示后端未填充。
    """

    direction: int  # +1 | -1
    start_bar: int
    end_bar: int
    high: float
    low: float
    level: int
    power_price: float = 0.0
    power_volume: float = 0.0
    length: int = 0


@dataclass(frozen=True, slots=True)
class ZsRaw:
    """后端产出的原始笔中枢（未映射到 domain）。"""

    start_bar: int
    end_bar: int
    high: float
    low: float
    level: int
    bi_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChanlunResult:
    """后端产出的原始结果（尚未映射到 domain）。"""

    fx_list: tuple[FxRaw, ...]
    bi_list: tuple[BiRaw, ...]
    zs_list: tuple[ZsRaw, ...]
    level_map: dict[int, int]  # bar_index -> level


@runtime_checkable
class ChanlunBackend(Protocol):
    """抽象后端：既能跑 czsc（生产），也能跑 CPT 自研或 in-memory（测试）。

    ``bars`` 是 ``BarLike``（典型为 ``CanonicalBar``）序列；真实后端内部
    会把它转成外部实现期望的输入，但该对象绝不越过本接口边界。
    """

    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult: ...


#: ``start_time``/``end_time`` 哨兵值,表示"尚未解析为毫秒时间戳"。
#: 导出侧 (``cpt.application.export.export_dataset``) 会拒绝把含此值的
#: 结构放进 schema v1 输出,防止占位语义污染已冻结格式。
# PLACEHOLDER_TIME is defined in the dependency-free domain types module.


def _resolve_time(bar_index: int, bars: Sequence[BarLike] | None) -> int:
    """把 bar 索引解析为毫秒时间戳;无 bars 时返回 PLACEHOLDER_TIME。"""
    if bars is None:
        return PLACEHOLDER_TIME
    if not (0 <= bar_index < len(bars)):
        raise IndexError(f"bar_index={bar_index} 超出 bars 范围 [0, {len(bars)})")
    return int(bars[bar_index].open_time)


def map_fractal(
    raw: FxRaw,
    *,
    level: int,
    source_ids: tuple[str, ...],
    bars: Sequence[BarLike] | None = None,
) -> Fractal:
    """把原始分型映射为 ``Fractal``。

    ``bars`` 可选:传入则把 ``raw.bar_index`` 解析为真实毫秒时间戳;
    不传则 ``start_time``/``end_time`` 保留 PLACEHOLDER_TIME, 导出侧会拦截。
    """
    start = _resolve_time(raw.bar_index, bars)
    return Fractal(
        kind=raw.kind,
        level=level,
        bar_index=raw.bar_index,
        start_time=start,
        end_time=start,
        high=raw.high,
        low=raw.low,
        source_ids=source_ids,
    )


def map_bi(
    raw: BiRaw,
    *,
    level: int,
    source_ids: tuple[str, ...],
    bars: Sequence[BarLike] | None = None,
) -> Bi:
    """把原始笔映射为 ``Bi``(时间字段可解析为毫秒)。"""
    return Bi(
        level=level,
        direction=raw.direction,
        start_time=_resolve_time(raw.start_bar, bars),
        end_time=_resolve_time(raw.end_bar, bars),
        high=raw.high,
        low=raw.low,
        source_ids=source_ids,
        power_price=raw.power_price,
        power_volume=raw.power_volume,
        length=raw.length,
    )


def map_zhongshu(
    raw: ZsRaw,
    *,
    level: int,
    source_ids: tuple[str, ...],
    bars: Sequence[BarLike] | None = None,
) -> ZhongShu:
    """把原始笔中枢映射为 ``ZhongShu``(时间字段可解析为毫秒)。"""
    return ZhongShu(
        level=level,
        start_time=_resolve_time(raw.start_bar, bars),
        end_time=_resolve_time(raw.end_bar, bars),
        high=raw.high,
        low=raw.low,
        bi_ids=tuple(f"bi:{i}" for i in raw.bi_indices),
    )


class InMemoryChanlunBackend:
    """占位后端：基于 bar 高低点做极简顶底分型 / 笔判定。

    仅供 M1 fixture 与单元测试用，不追求真实缠论精度。分型取局部极值
    （顶：``high`` 同时高于左右邻 bar；底：``low`` 同时低于左右邻 bar），
    笔由相邻异类分型连接而成。不计算中枢与 ``level``。
    """

    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        bars = list(bars)
        n = len(bars)
        if n < 3:
            return ChanlunResult((), (), (), {})

        # 1) 局部极值分型
        fractals: list[FxRaw] = []
        for i in range(1, n - 1):
            prev_h, prev_l = bars[i - 1].high, bars[i - 1].low
            cur_h, cur_l = bars[i].high, bars[i].low
            next_h, next_l = bars[i + 1].high, bars[i + 1].low
            if cur_h > prev_h and cur_h > next_h:
                fractals.append(FxRaw(bar_index=i, kind="top", high=cur_h, low=cur_l, level=0))
            elif cur_l < prev_l and cur_l < next_l:
                fractals.append(FxRaw(bar_index=i, kind="bottom", high=cur_h, low=cur_l, level=0))

        # 2) 相邻同类分型只保留更极端者，得到顶底交替序列
        alternated: list[FxRaw] = []
        for fx in fractals:
            if not alternated:
                alternated.append(fx)
                continue
            last = alternated[-1]
            if last.kind == fx.kind:
                if fx.kind == "top" and fx.high > last.high:
                    alternated[-1] = fx
                elif fx.kind == "bottom" and fx.low < last.low:
                    alternated[-1] = fx
            else:
                alternated.append(fx)

        # 3) 相邻异类分型连成笔
        bis: list[BiRaw] = []
        for a, b in zip(alternated, alternated[1:], strict=False):
            direction = 1 if a.kind == "bottom" else -1
            bis.append(
                BiRaw(
                    direction=direction,
                    start_bar=a.bar_index,
                    end_bar=b.bar_index,
                    high=max(a.high, b.high),
                    low=min(a.low, b.low),
                    level=0,
                )
            )

        return ChanlunResult(
            fx_list=tuple(alternated),
            bi_list=tuple(bis),
            zs_list=(),
            level_map={},
        )
