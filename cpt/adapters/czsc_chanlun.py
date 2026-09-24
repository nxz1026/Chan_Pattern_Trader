"""czsc 后端：用 czsc 的分型/笔实现替换 CPT 自研实现（R14，方案 C）。

设计取舍（2026-09-24 实测决定，见 ``docs/progress-log.md`` 的 R14 记录）：

- **只借分型与笔。** czsc 的 ``get_zs_seq`` 会产出少于 3 笔的"中枢"（实测
  fixture3 有 2 个两笔中枢，其中一个还是首个），且 ``ZS`` 不带 ``bi_ids``
  溯源；CPT 自研中枢有严格三笔重叠 + ``high > low`` + ``bi_ids``，因此在
  czsc 的笔之上继续调用 :func:`cpt.domain.zhongshu.build_zhongshus`。
- **延迟导入 + 版本校验。** czsc 是可选依赖（``.[chan]`` extra），
  ``dependencies`` 保持为空。未安装与版本不符都给出可操作的报错。
- **必须传原始 K 线。** czsc 内部自己做包含处理（``remove_include``），
  所以**不要**先跑 :func:`cpt.domain.contain.merge_contained_bars`，否则
  包含关系会被处理两次。

为什么不是"只 vendor ``_native.abi3.so``"：czsc 的 Python 绑定在
``crates/czsc-core/src/objects/fx.rs`` 里硬依赖 pandas
（``create_naive_pandas_timestamp``），取 ``FX.dt`` / ``BI.sdt`` 会直接抛
``ModuleNotFoundError: pandas``，脱离 pandas 的绑定是半残的。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from cpt.adapters.binance_futures import INTERVAL_MS
from cpt.adapters.reference_chanlun import (
    BiRaw,
    ChanlunResult,
    FxRaw,
    ReferenceChanlunConfig,
    ZsRaw,
)
from cpt.domain.models import CanonicalBar
from cpt.domain.types import BarLike

__all__ = [
    "DEFAULT_MIN_BI_LEN",
    "PINNED_CZSC_VERSION",
    "CzscChanlunBackend",
    "CzscNotInstalledError",
    "CzscVersionError",
]

#: 固定的 czsc 版本；与 ``pyproject.toml`` 的 ``chan`` extra 保持一致。
PINNED_CZSC_VERSION: Final[str] = "1.0.1"

#: czsc 笔门槛默认值，量纲＝**去包含后**的 K 线根数。
#: 实测在 4–7 区间笔数几乎不敏感（3 个 fixture 恒 ~50 笔），8 起急降，
#: 因此直接采用 czsc 上游默认 6，不自造数值。
DEFAULT_MIN_BI_LEN: Final[int] = 6

#: Binance 周期标签 → czsc ``Freq`` 成员名。只覆盖 :data:`INTERVAL_MS` 里
#: 存在的标签；``1M`` 等无固定长度的周期不支持（与 ``resolve_interval_ms`` 同口径）。
_LABEL_TO_CZSC_FREQ: Final[dict[str, str]] = {
    "1s": "S",
    "1m": "F1",
    "3m": "F3",
    "5m": "F5",
    "15m": "F15",
    "30m": "F30",
    "1h": "F60",
    "2h": "F120",
    "4h": "F240",
    "6h": "F360",
    "1d": "D",
    "1w": "W",
}


class CzscNotInstalledError(RuntimeError):
    """czsc 未安装。提示安装可选依赖 extra。"""


class CzscVersionError(RuntimeError):
    """czsc 已安装但版本与 :data:`PINNED_CZSC_VERSION` 不符。"""


def _import_czsc() -> Any:
    """延迟导入 czsc 并校验版本。"""
    try:
        import czsc  # noqa: PLC0415  (延迟导入是设计的一部分)
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于运行环境
        raise CzscNotInstalledError(
            'czsc 后端需要可选依赖，请安装：pip install -e ".[chan]"'
        ) from exc
    version = getattr(czsc, "__version__", None)
    if version != PINNED_CZSC_VERSION:
        raise CzscVersionError(
            f"czsc 版本不符：期望 {PINNED_CZSC_VERSION}，实际 {version!r}；"
            f'请执行 pip install "czsc=={PINNED_CZSC_VERSION}"'
        )
    return czsc


def _to_naive_utc(open_time_ms: int) -> datetime:
    """毫秒时间戳 → **tz-naive** UTC datetime（czsc 拒绝 tz-aware）。"""
    return datetime.fromtimestamp(open_time_ms / 1000, tz=UTC).replace(tzinfo=None)


def _infer_interval_label(bars: list[CanonicalBar]) -> str:
    """由相邻 ``open_time`` 的**中位**间隔反推 Binance 周期标签。

    用中位而非均值：行情缺口（停牌、断线）会产生远超正常间隔的离群值。
    """
    if len(bars) < 2:
        raise ValueError("czsc 后端至少需要 2 根 K 线才能推断周期")
    deltas = sorted(bars[i + 1].open_time - bars[i].open_time for i in range(len(bars) - 1))
    median_ms = deltas[len(deltas) // 2]
    for label, ms in INTERVAL_MS.items():
        if ms == median_ms:
            return label
    raise ValueError(
        f"无法由相邻 K 线中位间隔 {median_ms} ms 反推周期；支持 {sorted(INTERVAL_MS.values())}"
    )


def _bi_direction(value: object) -> int:
    """czsc ``Direction`` → ``+1``/``-1``。

    同时接受枚举名（``Up``/``Down``）与中文显示串（``向上``/``向下``），
    避免绑定层 ``__str__`` 变化导致映射静默失效。
    """
    name = getattr(value, "name", None) or str(value)
    if name in ("Up", "向上"):
        return 1
    if name in ("Down", "向下"):
        return -1
    raise ValueError(f"无法识别的 czsc 笔方向：{value!r}")


def _fx_kind(value: object) -> str:
    """czsc ``Mark`` → CPT 的 ``"top"``/``"bottom"``。"""
    name = getattr(value, "name", None) or str(value)
    if name in ("G", "top", "顶"):
        return "top"
    if name in ("D", "bottom", "底"):
        return "bottom"
    raise ValueError(f"无法识别的 czsc 分型标记：{value!r}")


class CzscChanlunBackend:
    """用 czsc 算分型/笔、用 CPT 自研算法算中枢的后端。

    ``min_bi_len`` 是 czsc 的笔门槛（去包含后K线根数），默认
    :data:`DEFAULT_MIN_BI_LEN`；``freq_label`` 留空时由 K 线间隔自动推断。
    """

    def __init__(self, *, min_bi_len: int = DEFAULT_MIN_BI_LEN, freq_label: str | None = None):
        if min_bi_len < 1:
            raise ValueError(f"min_bi_len 必须 ≥ 1，收到 {min_bi_len}")
        self._min_bi_len = min_bi_len
        self._freq_label = freq_label

    @property
    def min_bi_len(self) -> int:
        return self._min_bi_len

    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        """跑 czsc 分型/笔 + CPT 中枢，产出反腐层的 Raw 结构。

        ``config`` 只用于接口兼容；czsc 的笔口径由 ``min_bi_len`` 控制，
        与 ``ReferenceChanlunConfig`` 的 chanlun-pro 参数无关。
        """
        czsc = _import_czsc()
        # czsc 需要 open/close/volume，而 ``BarLike`` 协议只有高低与时间；
        # 与 ``NativeChanlunBackend`` 同口径，只接受 ``CanonicalBar``。
        canonical: list[CanonicalBar] = [bar for bar in bars if isinstance(bar, CanonicalBar)]
        if len(canonical) < 3:
            return ChanlunResult((), (), (), {})

        label = self._freq_label or _infer_interval_label(canonical)
        freq_name = _LABEL_TO_CZSC_FREQ.get(label)
        if freq_name is None:
            raise ValueError(
                f"周期 {label!r} 无对应的 czsc Freq；支持 {sorted(_LABEL_TO_CZSC_FREQ)}"
            )
        freq = getattr(czsc.Freq, freq_name)

        # 时间戳 → 原始 K 线下标。czsc 只回吐时间，必须能反查回下标，
        # 否则下游 map_bi/map_zhongshu 的 _resolve_time 会静默拿错时间。
        index_by_dt = {_to_naive_utc(bar.open_time): i for i, bar in enumerate(canonical)}

        def resolve(dt: Any, what: str) -> int:
            key = dt.to_pydatetime().replace(tzinfo=None) if hasattr(dt, "to_pydatetime") else dt
            value = index_by_dt.get(key)
            if value is None:
                raise ValueError(
                    f"czsc 回吐的{what}时间 {key} 不在原始 K 线中"
                    f"（原始 {len(canonical)} 根 / 首根 {next(iter(index_by_dt))}）"
                )
            return value

        raw_bars = [
            czsc.RawBar(
                symbol="CPT",
                id=i,
                dt=_to_naive_utc(bar.open_time),
                freq=freq,
                open=float(bar.open),
                close=float(bar.close),
                high=float(bar.high),
                low=float(bar.low),
                vol=float(bar.volume),
                amount=float(bar.quote_volume),
            )
            for i, bar in enumerate(canonical)
        ]
        analyzer = czsc.CZSC(raw_bars, max_bi_num=0, min_bi_len=self._min_bi_len)

        fx_raw = tuple(
            FxRaw(
                bar_index=resolve(fx.dt, "分型"),
                kind=_fx_kind(fx.mark),
                high=float(fx.high),
                low=float(fx.low),
                level=0,
            )
            for fx in analyzer.fx_list
        )
        bi_raw = tuple(
            BiRaw(
                direction=_bi_direction(bi.direction),
                start_bar=resolve(bi.fx_a.dt, "笔起点"),
                end_bar=resolve(bi.fx_b.dt, "笔终点"),
                high=float(bi.high),
                low=float(bi.low),
                level=0,
            )
            for bi in analyzer.bi_list
        )
        return ChanlunResult(fx_raw, bi_raw, self._build_zhongshus(canonical, bi_raw), {})

    @staticmethod
    def _build_zhongshus(bars: list[CanonicalBar], bi_raw: tuple[BiRaw, ...]) -> tuple[ZsRaw, ...]:
        """在 czsc 的笔之上跑 CPT 自研中枢，再映射回 Raw。

        这里刻意**不**用 czsc 的 ``zs_list``：它会产出 <3 笔的假中枢且无
        ``bi_ids`` 溯源，而 CPT 的 :func:`build_zhongshus` 要求严格三笔重叠。
        """
        from cpt.domain.models import Bi
        from cpt.domain.zhongshu import build_zhongshus

        domain_bis = [
            Bi(
                level=0,
                direction=raw.direction,
                start_time=int(bars[raw.start_bar].open_time),
                end_time=int(bars[raw.end_bar].open_time),
                high=raw.high,
                low=raw.low,
                source_ids=(f"bi:{i}",),
            )
            for i, raw in enumerate(bi_raw)
        ]
        index_by_time = {int(bar.open_time): i for i, bar in enumerate(bars)}
        zs_list: list[ZsRaw] = []
        for zs in build_zhongshus(domain_bis, level=0):
            start = index_by_time.get(zs.start_time)
            end = index_by_time.get(zs.end_time)
            if start is None or end is None:
                raise ValueError(f"中枢时间 {zs.start_time}/{zs.end_time} 无法反查原始 K 线下标")
            zs_list.append(
                ZsRaw(
                    start_bar=start,
                    end_bar=end,
                    high=zs.high,
                    low=zs.low,
                    level=zs.level,
                    bi_indices=tuple(
                        int(bi_id.split(":")[1]) for bi_id in zs.bi_ids if bi_id.startswith("bi:")
                    ),
                )
            )
        return tuple(zs_list)
