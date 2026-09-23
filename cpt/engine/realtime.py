"""实时增量引擎：未收盘预警、收盘升级、小窗口增量重建（``docs/implementation-plan.md`` §8 M5）。

本模块把 M4 的批量回放入口（:func:`cpt.application.replay.run_replay`）包成**有状态**
的实时引擎，服务 ``docs/rules.md`` §8.4（历史/实时双模式）与 §8.5（Binance 数据契约）：
**正式结构只使用已收盘 K 线，未收盘 K 线只触发预警**。

设计要点：

* **一根一等**：:meth:`RealtimeEngine.feed` 每次接收一根 K 线，维护有序窗口，返回一份
  可直接落盘的 schema v1 payload（``mode`` / ``status`` 标识实时语义）。
* **未收盘只预警**：窗口尾根 ``is_closed`` 为 ``False`` 时，输出 ``status="alert"``，
  ``data`` 中的结构列表一律为空；未收盘根只出现在 ``metadata["alert"]`` 里，
  因此 t 时刻输出只依赖 ≤t 已收盘数据（无未来函数）。
* **收盘后小窗口全量重建**：尾根收盘（或未收盘根被收盘更新替换）后，对窗口内**已收盘**
  子序列跑与批量/逐根回放**完全同源**的 domain 管线（``run_replay``），产出正式结构。
  按 §8 的策略不做增量差分，窗口上限 :attr:`RealtimeEngine.max_window` 把单次重建限制为
  O(max_window)。
* **窗口截断**：窗口超过 ``max_window`` 只保留最近部分，``metadata["truncated"]`` 置位。
  与事后批量回放结果一致的前提是 ``len(bars) <= max_window``（窗口未截断）。

窗口维护规则（``feed`` 对窗口尾根的处置）：

+---------------------------+--------------------+----------------------------------------+
| 新 K 线 vs 尾根           | 条件               | 行为                                   |
+===========================+====================+========================================+
| ``open_time`` 更大        | 尾根已收盘         | 经 validators 校验间隔后追加           |
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 更大        | 间隔 > 契约周期    | :class:`~cpt.adapters.validators.DataGapError` |
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 更大        | 间隔 < 契约周期    | ``DataValidationError``（周期错配）    |
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 更大        | 尾根未收盘         | ``DataValidationError``（缺该根收盘更新）|
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 相等        | 内容完全相同       | 幂等 no-op（分页重叠可安全重放）       |
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 相等        | 尾根未收盘         | 就地更新（盘中更新 → 收盘升级）        |
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 相等        | 尾根已收盘         | ``DataValidationError``（收盘根内容冲突）|
+---------------------------+--------------------+----------------------------------------+
| ``open_time`` 更小        | —                  | ``DataValidationError``（乱序）        |
+---------------------------+--------------------+----------------------------------------+

用法::

    engine = RealtimeEngine(config)                 # 5m 口径: interval 由 config.levels[0] 推出
    for bar in stream:                              # bar.is_closed=False 表示未收盘
        payload = engine.feed(bar)                  # schema v1 payload + mode/status
        if payload["status"] == STATUS_CONFIRMED:   # 收盘升级: 正式结构已刷新
            ...

分层说明：本模块按任务要求复用既有层 —— 连续性/契约校验走
:mod:`cpt.adapters.validators`，结构管线走 :mod:`cpt.application.replay`（与历史回放同一套
domain 算法，禁止复制一份）。这两条依赖让 ``cpt/engine`` 触碰了
``.importlinter`` 的 *Engine orchestrates domain and storage only* 契约，已在 M5-01 交付报告
中作为待裁定项标注，未擅自改写 ``.importlinter``。
"""

from __future__ import annotations

from dataclasses import asdict
from importlib import import_module
from typing import Any, Final, cast

from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar


def _adapters() -> Any:
    """延迟加载适配器，保持 engine 的静态依赖边界。"""
    return (
        import_module("cpt.adapters.reference_chanlun"),
        import_module("cpt.adapters.validators"),
    )


def _application() -> Any:
    """延迟加载 application 回放/导出，避免 engine 的静态反向依赖。"""
    return import_module("cpt.application.export"), import_module("cpt.application.replay")


__all__ = [
    "MODE_REALTIME",
    "STATUS_ALERT",
    "STATUS_CONFIRMED",
    "RealtimeEngine",
]

#: payload ``mode`` 取值：实时当时可见结果（``docs/rules.md`` §5.2 要求与历史最终结果分开保存）。
MODE_REALTIME: Final[str] = "realtime"

#: payload ``status`` 取值：未收盘 K 线触发的预警（``docs/rules.md`` §8.2）。
STATUS_ALERT: Final[str] = "alert"

#: payload ``status`` 取值：收盘后已用正式结构管线刷新（``docs/rules.md`` §8.2）。
STATUS_CONFIRMED: Final[str] = "confirmed"

#: 毫秒/分钟换算：``RulesConfig.levels`` 的单位是分钟。
_MS_PER_MINUTE: Final[int] = 60_000

#: 冷启动预警原因码：窗口尾根尚未收盘。
_ALERT_REASON_UNCLOSED: Final[str] = "unclosed_bar"


def _bar_to_dict(bar: CanonicalBar) -> dict[str, Any]:
    """``CanonicalBar`` → 普通 ``dict``（补 ``direction`` 派生字段）。

    与 ``cpt.application.export`` 的 bar 序列化保持同一字段集，但**不**跨层引用其
    私有实现：预警元数据只需要一份可落盘的 K 线快照。
    """
    data: dict[str, Any] = asdict(bar)
    data["direction"] = bar.direction
    return data


class RealtimeEngine:
    """实时增量引擎：窗口化 K 线 + 未收盘预警 + 收盘升级。

    状态只有一份：递增有序的 K 线窗口（末根可能是未收盘的进行中 K 线）。所有正式结构
    计算都在``feed`` 内、基于窗口的**已收盘**子序列完成，因此同一输入序列无论分几次
    ``feed`` 进来，结构结果都与批量回放一致（窗口未截断时）。

    Args:
        config: 规则口径配置，同时决定默认契约周期（``config.levels[0]`` 分钟）。
        backend: 结构计算后端；``None`` 时用 :class:`InMemoryChanlunBackend`。
        max_window: 窗口上限（根）。超出只保留最近 ``max_window`` 根，单次重建成本因此
            上限为 O(max_window)。
        interval_ms: 契约周期覆盖（毫秒）；``None`` 时取 ``config.levels[0] * 60_000``。
            仅在数据流周期与 ``config.levels`` 不一致时显式传入（例如非默认周期的
            fixture 演练），不改变 ``config`` 本身的语义。

    Raises:
        ValueError: ``max_window < 1`` 或 ``interval_ms <= 0``。
    """

    def __init__(
        self,
        config: RulesConfig,
        backend: Any | None = None,
        max_window: int = 500,
        *,
        interval_ms: int | None = None,
    ) -> None:
        if max_window < 1:
            raise ValueError(f"max_window 必须 >= 1, 实测 {max_window!r}")
        if interval_ms is not None and interval_ms <= 0:
            raise ValueError(f"interval_ms 必须为正整数, 实测 {interval_ms!r}")
        self._config = config
        reference_chanlun, _ = _adapters()
        self._backend: Any = (
            backend if backend is not None else reference_chanlun.InMemoryChanlunBackend()
        )
        self._max_window = max_window
        self._interval_ms = (
            interval_ms if interval_ms is not None else config.levels[0] * _MS_PER_MINUTE
        )
        self._window: list[CanonicalBar] = []
        self._truncated = False

    # ------------------------------------------------------------------ 只读属性

    @property
    def config(self) -> RulesConfig:
        """本次引擎使用的规则口径配置（只读引用）。"""
        return self._config

    @property
    def backend(self) -> Any:
        """已解析的结构计算后端（``None`` 已由 ``InMemoryChanlunBackend`` 兜底）。"""
        return self._backend

    @property
    def max_window(self) -> int:
        """窗口根数上限。"""
        return self._max_window

    @property
    def interval_ms(self) -> int:
        """契约周期（毫秒）。"""
        return self._interval_ms

    @property
    def window_size(self) -> int:
        """当前窗口根数（含未收盘根）。"""
        return len(self._window)

    @property
    def truncated(self) -> bool:
        """窗口是否已因 ``max_window`` 上限丢弃过最早 K 线。"""
        return self._truncated

    @property
    def pending_bar(self) -> CanonicalBar | None:
        """窗口尾根若未收盘则返回它，否则返回 ``None``（正式结构只用已收盘 K 线）。"""
        if self._window and not self._window[-1].is_closed:
            return self._window[-1]
        return None

    # ------------------------------------------------------------------ 公共接口

    def feed(self, bar: CanonicalBar) -> dict[str, Any]:
        """接收一根 K 线，推进窗口并返回 schema v1 payload（含 ``mode`` / ``status``）。

        处理顺序：先做单根契约校验（OHLC + ``close_time`` 边界，复用
        :func:`cpt.adapters.validators.validate_canonical_bars`），再按类文档的规则表
        把 ``bar`` 并入窗口（首根直接建立基线，无前驱可比故不做缺口检查），最后裁剪窗口
        并重建输出。

        未收盘根不会进入结构计算：``status="alert"`` 时 ``data`` 内结构列表为空，
        未收盘根只出现在 ``metadata["alert"]``。

        Args:
            bar: 新到达的 K 线；``is_closed=False`` 表示尚未收盘。

        Returns:
            schema v1 payload 字典，额外含顶层 ``mode``（恒为 ``"realtime"``）与
            ``status``（``"alert"`` / ``"confirmed"``），并在 ``metadata`` 内镜像同样的
            状态字段与窗口信息。

        Raises:
            DataValidationError: 单根契约非法、``open_time`` 乱序、收盘根内容冲突、
                同 ``open_time`` 相邻间隔小于契约周期、或上一根未收盘就要推进。
            DataGapError: 与前一收盘根间隔大于契约周期（缺口，禁止跨缺口生成正式结构）。
        """
        _, validators = _adapters()
        validators.validate_canonical_bars((bar,), self._interval_ms)
        if self._window:
            self._merge(bar)
        else:
            self._window.append(bar)
        self._trim()
        return self._payload()

    def snapshot(self) -> tuple[CanonicalBar, ...]:
        """返回当前窗口的不可变副本（含未收盘根，按 ``open_time`` 递增）。"""
        return tuple(self._window)

    def reset(self) -> None:
        """清空窗口与截断标记，回到未建基线状态（配置与周期保持不动）。"""
        self._window.clear()
        self._truncated = False

    # ------------------------------------------------------------------ 窗口维护

    def _merge(self, bar: CanonicalBar) -> None:
        """把 ``bar`` 并入窗口尾部：追加 / 就地更新 / 报错（见类文档规则表）。"""
        last = self._window[-1]
        if bar.open_time < last.open_time:
            raise _out_of_order(last, bar)
        if bar.open_time == last.open_time:
            self._merge_same_open_time(bar, last)
            return
        if not last.is_closed:
            raise _unclosed_tail_blocks_advance(last, bar)
        # 间隔校验交给 validators：> 周期 → DataGapError，< 周期 → DataValidationError。
        _, validators = _adapters()
        validators.validate_canonical_bars((last, bar), self._interval_ms)
        self._window.append(bar)

    def _merge_same_open_time(self, bar: CanonicalBar, last: CanonicalBar) -> None:
        """同一 ``open_time``：内容相同幂等跳过；尾根未收盘则就地更新；否则报冲突。"""
        if bar == last:
            return
        if last.is_closed:
            raise _closed_bar_conflict(last, bar)
        self._window[-1] = bar

    def _trim(self) -> None:
        """按 ``max_window`` 截断窗口，保留最近部分并记录截断标记。"""
        overflow = len(self._window) - self._max_window
        if overflow <= 0:
            return
        del self._window[:overflow]
        self._truncated = True

    # ------------------------------------------------------------------ 输出构造

    def _closed_bars(self) -> tuple[CanonicalBar, ...]:
        """窗口内的已收盘子序列：正式结构的唯一输入（``docs/rules.md`` §8.5）。"""
        return tuple(candidate for candidate in self._window if candidate.is_closed)

    def _metadata(self, status: str, closed_count: int) -> dict[str, Any]:
        """构造实时元数据：模式、状态、窗口信息（全部来自 ≤t 数据）。"""
        return {
            "mode": MODE_REALTIME,
            "status": status,
            "interval_ms": self._interval_ms,
            "window_size": len(self._window),
            "max_window": self._max_window,
            "closed_bar_count": closed_count,
            "truncated": self._truncated,
            "last_open_time": self._window[-1].open_time,
        }

    def _payload(self) -> dict[str, Any]:
        """按窗口尾根是否收盘，产出预警（空结构）或收盘升级（正式结构）payload。"""
        closed = self._closed_bars()
        pending = self.pending_bar
        if pending is not None:
            metadata = self._metadata(STATUS_ALERT, len(closed))
            metadata["alert"] = {
                "reason": _ALERT_REASON_UNCLOSED,
                "bar_open_time": pending.open_time,
                "bar_close_time": pending.close_time,
                "direction": pending.direction,
                "bar": _bar_to_dict(pending),
            }
            payload = _export_empty(config=self._config, bars=closed, metadata=metadata)
            payload["mode"] = MODE_REALTIME
            payload["status"] = STATUS_ALERT
            return payload

        _, replay = _application()
        payload = replay.run_replay(
            config=self._config,
            bars=closed,
            backend=self._backend,
            metadata=self._metadata(STATUS_CONFIRMED, len(closed)),
        )
        payload["mode"] = MODE_REALTIME
        payload["status"] = STATUS_CONFIRMED
        return cast(dict[str, Any], payload)


def _export_empty(
    *,
    config: RulesConfig,
    bars: tuple[CanonicalBar, ...],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """构造结构全空的 schema v1 payload（预警路径：不生成任何正式结构）。"""
    export, _ = _application()
    return cast(
        dict[str, Any],
        export.export_dataset(
            config=config,
            bars=bars,
            fractals=(),
            bis=(),
            zhongshus=(),
            events=(),
            signals=(),
            metadata=metadata,
        ),
    )


def _out_of_order(last: CanonicalBar, bar: CanonicalBar) -> Any:
    """乱序（``open_time`` 回退）错误。"""
    _, validators = _adapters()
    return validators.DataValidationError(
        f"feed 的 bar.open_time={bar.open_time} 早于窗口尾根 {last.open_time}; "
        "实时流必须按 open_time 递增到达, 乱序请先在上游重排"
    )


def _unclosed_tail_blocks_advance(last: CanonicalBar, bar: CanonicalBar) -> Any:
    """上一根未收盘就推进到更晚 K 线：缺少该根的收盘更新，会撕开已收盘序列。"""
    _, validators = _adapters()
    return validators.DataValidationError(
        f"窗口尾根 open_time={last.open_time} 仍未收盘 (is_closed=False), "
        f"不能推进到 open_time={bar.open_time}; "
        "该根的收盘更新缺失会让已收盘序列出现空洞, 请先补投该根的收盘版本"
    )


def _closed_bar_conflict(last: CanonicalBar, bar: CanonicalBar) -> Any:
    """已收盘根被同 ``open_time`` 的不同内容覆盖：数据冲突，不可猜测取舍。"""
    _, validators = _adapters()
    return validators.DataValidationError(
        f"open_time={bar.open_time} 的已收盘 K 线内容冲突: "
        f"窗口内 O/H/L/C=({last.open}, {last.high}, {last.low}, {last.close}), "
        f"新到 O/H/L/C=({bar.open}, {bar.high}, {bar.low}, {bar.close})"
    )
