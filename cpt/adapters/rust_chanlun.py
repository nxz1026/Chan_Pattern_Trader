"""MIT Rust/PyO3 ``chanlun`` oracle 反腐层（anti-corruption layer）。

本模块把 PyPI 上 MIT 许可的 ``chanlun``（Rust/PyO3 绑定，项目主页
``https://github.com/YuYuKunKun/chanlun.rs``）包成 CPT 的**独立对照后端**：
输入 ``CanonicalBar`` 序列，输出本仓库 domain 对象（``Fractal`` / ``Bi``）。
oracle 的中文对象（``观察者`` / ``分型`` / ``虚线`` / ``中枢``）绝不越过本模块
边界，``cpt.domain`` / ``cpt.engine`` / ``cpt.application`` 对本 oracle 零感知。

设计约束：

* ``chanlun`` **延迟导入**：模块导入阶段不加载第三方库；未安装或版本不是
  ``PINNED_ORACLE_VERSION`` 时，``RustChanlunBackend.compute_structures`` 抛
  ``RuntimeError``，错误信息含可直接执行的安装命令。
* 只映射**已冻结**的口径：分型、笔、中枢**数量**。构造 ``ZhongShu`` 需要
  ``bi_ids``，而 oracle 中枢与笔的对应关系尚未冻结
  （``docs/m2-oracle-diagnostic.md``），故只返回 ``zhongshu_count``。
* 供脚本与 M2 对照使用；不导入 ``cpt.application``，不参与生产链路。

映射口径（逐项显式声明，便于 M2 对齐）：

* ``分型.结构`` 顶 / 底 → ``Fractal.kind`` ``"top"`` / ``"bottom"``；其它中文
  结构（上 / 下 / 散）不是分型端点，出现即抛 ``ValueError``。
* ``分型.中.原始起始序号`` → ``Fractal.bar_index``（原始 bars 索引，越界即抛
  ``ValueError``）。
* ``分型.时间戳``（秒）→ ``Fractal.start_time`` / ``end_time``（毫秒，两者相等）。
* ``分型.中.高`` / ``分型.中.低`` → ``Fractal.high`` / ``low``。
* ``虚线.方向`` 向上 / 向下 → ``Bi.direction`` ``+1`` / ``-1``；
  ``虚线.文.时间戳`` / ``武.时间戳``（秒）→ ``Bi.start_time`` / ``end_time``
  （毫秒）；``虚线.高`` / ``低`` → ``Bi.high`` / ``low``；``虚线.级别`` →
  ``Bi.level``。
* ``Fractal.source_ids`` / ``Bi.source_ids`` 用稳定字符串（由级别、原始 bar
  序号、方向/结构推导，同输入同输出），不含 oracle 内部对象标识。
* 分型无独立级别，统一标注为 oracle 一级结构编号 ``_BASE_LEVEL``（与
  ``虚线.级别`` 实测值一致）；M1 占位后端用 ``0`` 表示“未分级”。

时间戳语义注意：oracle 的 ``原始起始序号`` 是**包含处理后合并缠K组的首个原始
bar**，而 ``分型.时间戳`` 取该组**代表 bar**（极值 bar）的时刻，落在
``[原始起始序号, 原始结束序号]`` 组内。因此 ``Fractal.start_time`` 可能晚于
``bars[bar_index].open_time``（实测 1000 根 5m 样本中 58 个分型里有 16 个如此）。
两者都保留：索引按 ``原始起始序号``，时间按 oracle 时间戳，于是 ``Bi`` 端点时间
与其对应 ``Fractal`` 时间严格一致。
"""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cpt.adapters.reference_chanlun import ReferenceChanlunConfig
from cpt.domain.models import Bi, CanonicalBar, Fractal

__all__ = [
    "PINNED_ORACLE_VERSION",
    "UNSUPPORTED_CONFIG_FIELDS",
    "RustChanlunBackend",
    "RustOracleResult",
    "unsupported_config_fields",
]

#: 固定要求的 oracle 版本（与 ``pyproject.toml`` 的 ``oracle`` extra 同步）。
PINNED_ORACLE_VERSION: str = "2606.73"

#: oracle ``缠论配置`` 没有对应开关、只能沿用其内置规则的口径字段名。
#: 这些字段无法转发，设置后被静默忽略；用 :func:`unsupported_config_fields`
#: 检测“本次调用有哪些口径没被 oracle 承接”。
UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "use_fx_qy_middle",
    "use_fx_qj_ck",
    "use_bi_type_new",
    "zs_wzgx",
)

#: 分型的级别标注：oracle 一级结构编号（``虚线.级别`` 在笔序列中恒为 1）。
_BASE_LEVEL: int = 1

#: 周期（秒）兜底值：bars 不足以推断时使用（M2 对照样本为 5m）。
_DEFAULT_INTERVAL_SECONDS: int = 300

#: 推断周期时采样的相邻 bar 间隔对数。
_INTERVAL_SAMPLE_PAIRS: int = 64


@dataclass(frozen=True, slots=True)
class RustOracleResult:
    """Rust oracle 一次结构计算映射回 domain 的结果。

    ``fx_list`` / ``bi_list`` 是 domain ``Fractal`` / ``Bi`` 元组；中枢只给数量
    （``zhongshu_count``），因为 oracle 中枢与笔的对应关系尚未冻结，无法稳定给出
    ``ZhongShu.bi_ids``。``bi_list`` 含 oracle 的未确认尾笔（``虚线.有效性`` 为
    ``False``），与 ``观察者.笔序列`` 保持一一对应。``merged_candle_count`` 是经
    包含处理后的缠K数量（``观察者.缠论K线序列``）。
    """

    fx_list: tuple[Fractal, ...]
    bi_list: tuple[Bi, ...]
    zhongshu_count: int
    merged_candle_count: int


def _load_oracle() -> Any:
    """延迟导入 oracle 并校验版本；失败时抛含修复命令的 ``RuntimeError``。

    返回类型是 ``Any``：oracle 是外部未类型化依赖，不把它的类型带进 CPT。
    """
    try:
        module: Any = importlib.import_module("chanlun")
        found = importlib.metadata.version("chanlun")
    except ImportError as exc:  # 覆盖 ModuleNotFoundError / PackageNotFoundError
        raise RuntimeError(
            "Rust chanlun oracle 未安装：请执行 "
            f"`.venv/bin/pip install 'chanlun=={PINNED_ORACLE_VERSION}'`"
        ) from exc
    if found != PINNED_ORACLE_VERSION:
        raise RuntimeError(
            f"Rust chanlun oracle 版本不匹配：要求 {PINNED_ORACLE_VERSION}，实测 {found}。"
            f"请执行 `.venv/bin/pip install 'chanlun=={PINNED_ORACLE_VERSION}'`"
        )
    return module


def unsupported_config_fields(config: ReferenceChanlunConfig) -> tuple[str, ...]:
    """返回 ``config`` 中已偏离默认、但 oracle 无对应开关的口径字段名。

    空元组表示本次调用的口径全部被 oracle 承接；非空表示这些字段的取值不会被
    oracle 采纳（它只会沿用自己的内置规则）。
    """
    defaults = ReferenceChanlunConfig()
    return tuple(
        name
        for name in UNSUPPORTED_CONFIG_FIELDS
        if getattr(config, name) != getattr(defaults, name)
    )


def _fractal_kind(value: object) -> str:
    """把 oracle 分型结构的中文表示映射为 ``"top"`` / ``"bottom"``。"""
    text = str(value)
    if "顶" in text:
        return "top"
    if "底" in text:
        return "bottom"
    raise ValueError(f"无法识别的 oracle 分型结构: {text!r}（只接受 顶 / 底）")


def _bi_direction(value: object) -> int:
    """把 oracle 笔方向映射为 ``+1``（向上）/ ``-1``（向下）。"""
    text = str(value)
    if "向上" in text:
        return 1
    if "向下" in text:
        return -1
    raise ValueError(f"无法识别的 oracle 笔方向: {text!r}（只接受 向上 / 向下）")


def _to_milliseconds(seconds: object) -> int:
    """oracle 时间戳单位是秒（整数），统一换算为毫秒。"""
    if not isinstance(seconds, (int, float, str)):
        raise ValueError(f"oracle 时间戳类型不支持: {type(seconds).__name__}")
    return round(float(seconds) * 1000)


def _infer_interval_seconds(bars: Sequence[CanonicalBar]) -> int:
    """从前若干相邻 bar 的 ``open_time`` 差推断周期（秒）。

    取采样的最小正间隔，容忍窗口内的缺 bar / 跳空；无法推断时返回
    ``_DEFAULT_INTERVAL_SECONDS``。周期只影响 oracle 的展示与周期标注（实测
    300s 与 900s 的分型/笔/中枢结果完全一致），故兜底值是安全的。
    """
    sample = bars[: _INTERVAL_SAMPLE_PAIRS + 1]
    intervals = [
        (int(cur.open_time) - int(prev.open_time)) // 1000
        for prev, cur in zip(sample, sample[1:], strict=False)
    ]
    positive = [seconds for seconds in intervals if seconds > 0]
    return min(positive) if positive else _DEFAULT_INTERVAL_SECONDS


def _build_oracle_config(chanlun: Any, config: ReferenceChanlunConfig) -> Any:
    """构造 oracle 的 ``缠论配置``：只开分型/笔/笔中枢，关掉线段、图表与推送。

    该组合与 ``scripts/compare_oracle.py`` 的诊断口径一致。MACD 周期按
    ``config`` 转发；``计算指标=False`` 时不影响分型/笔/中枢（已实测同口径下
    开/关 ``计算指标`` 的三类结构完全一致）。
    """
    return chanlun.缠论配置(
        分析线段=False,
        分析扩展线段=False,
        分析线段中枢=False,
        图表展示=False,
        推送K线=False,
        推送笔=False,
        推送线段=False,
        推送中枢=False,
        计算指标=False,
        平滑异同移动平均线_快线周期=config.macd_fast,
        平滑异同移动平均线_慢线周期=config.macd_slow,
        平滑异同移动平均线_信号周期=config.macd_signal,
    )


def _feed(
    chanlun: Any, observer: Any, bars: Sequence[CanonicalBar], interval: int, symbol: str
) -> None:
    """把 ``CanonicalBar`` 逐根投喂给 oracle 观察者（毫秒时间戳截断到秒）。

    假定 ``open_time`` 对齐到整秒（Binance 契约成立）；``增加原始K线`` 内部完成
    增量分析，无需再调用 ``静态重新分析``。
    """
    for index, bar in enumerate(bars):
        observer.增加原始K线(
            chanlun.K线.创建普K(
                symbol,
                int(bar.open_time) // 1000,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                index,
                interval,
            )
        )


def _map_fractals(bars: Sequence[CanonicalBar], raw_fractals: Sequence[Any]) -> tuple[Fractal, ...]:
    """把 oracle 分型序列映射为 domain ``Fractal`` 元组。"""
    bar_count = len(bars)
    fractals: list[Fractal] = []
    for raw in raw_fractals:
        center = raw.中
        bar_index = int(center.原始起始序号)
        if not 0 <= bar_index < bar_count:
            raise ValueError(f"oracle 分型原始起始序号 {bar_index} 超出 bars 范围 [0, {bar_count})")
        kind = _fractal_kind(raw.结构)
        timestamp = _to_milliseconds(raw.时间戳)
        fractals.append(
            Fractal(
                kind=kind,
                level=_BASE_LEVEL,
                bar_index=bar_index,
                start_time=timestamp,
                end_time=timestamp,
                high=float(center.高),
                low=float(center.低),
                source_ids=(f"rust:fx:{_BASE_LEVEL}:{bar_index}:{kind}",),
            )
        )
    return tuple(fractals)


def _map_bis(raw_bis: Sequence[Any]) -> tuple[Bi, ...]:
    """把 oracle 笔序列映射为 domain ``Bi`` 元组。"""
    bis: list[Bi] = []
    for raw in raw_bis:
        direction = _bi_direction(raw.方向)
        level = int(raw.级别)
        start_bar = int(raw.文.中.原始起始序号)
        end_bar = int(raw.武.中.原始起始序号)
        bis.append(
            Bi(
                level=level,
                direction=direction,
                start_time=_to_milliseconds(raw.文.时间戳),
                end_time=_to_milliseconds(raw.武.时间戳),
                high=float(raw.高),
                low=float(raw.低),
                source_ids=(
                    f"rust:bi:{level}:{start_bar}:{end_bar}:{'up' if direction > 0 else 'down'}",
                ),
            )
        )
    return tuple(bis)


@dataclass(frozen=True, slots=True)
class RustChanlunBackend:
    """Rust oracle 后端：``CanonicalBar`` 序列 → :class:`RustOracleResult`。

    ``symbol`` 只用于 oracle 内部标识与展示；``interval_seconds`` 为 ``None`` 时
    从 ``bars`` 相邻 ``open_time`` 推断（见 :func:`_infer_interval_seconds`）。
    两个字段都不影响分型/笔/中枢的判定结果（实测改周期不改变结构）。
    """

    symbol: str = "CPT"
    interval_seconds: int | None = None

    def compute_structures(
        self, bars: Sequence[CanonicalBar], config: ReferenceChanlunConfig
    ) -> RustOracleResult:
        """跑一次 oracle 结构计算并映射回 domain。

        未安装 oracle 或版本不符时抛 ``RuntimeError``；分型结构/方向无法识别、
        或分型原始起始序号越界时抛 ``ValueError``。
        """
        chanlun = _load_oracle()
        interval = (
            self.interval_seconds
            if self.interval_seconds is not None
            else _infer_interval_seconds(bars)
        )
        observer = chanlun.观察者(self.symbol, interval, _build_oracle_config(chanlun, config))
        _feed(chanlun, observer, bars, interval, self.symbol)
        return RustOracleResult(
            fx_list=_map_fractals(bars, observer.分型序列),
            bi_list=_map_bis(observer.笔序列),
            zhongshu_count=len(observer.笔_中枢序列),
            merged_candle_count=len(observer.缠论K线序列),
        )
