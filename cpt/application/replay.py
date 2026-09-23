"""历史回放 CLI 与批量/单根回放接口。

把 K 线跑过 chanlun 反腐层（含 InMemoryChanlunBackend 占位实现），产出结构序列
并序列化为 schema v1 JSON（``docs/implementation-plan.md`` §7 M4）。

三条入口的分工：

+--------------------------------+--------+------------------+------------------+
| 入口                           | 校验   | 输入             | 用途             |
+================================+========+==================+==================+
| :func:`run_replay`             | 否     | 任意序列         | 低层：校验已由   |
|                                |        |                  | 调用方完成       |
+--------------------------------+--------+------------------+------------------+
| :func:`replay_bars`            | 是     | 任意序列         | 批量回放（协议   |
|                                |        |                  | 推荐入口）       |
+--------------------------------+--------+------------------+------------------+
| :func:`replay_incremental`     | 是     | 任意序列 → 前缀  | 单根推进/逐根    |
|                                |        |                  | 复盘             |
+--------------------------------+--------+------------------+------------------+

CLI 用法::

    python -m cpt.application.replay \\
        --input tests/fixtures/case1_simple_uptrend.json \\
        --output /tmp/out.json

    python -m cpt.application.replay \\
        --input tests/fixtures/case1_simple_uptrend.json --validate-only

输出文件含完整 schema v1 payload；stderr 打印 dataset_hash 便于复现性校验。
``--validate-only`` 只校验 K 线（不写结构文件），stdout 打印行数与时间范围 JSON。

两条 CLI 路径都先过 :func:`cpt.adapters.validators.validate_canonical_bars`，
因此 fixture 每根 K 线必须满足 Binance 契约：``close_time == open_time +
interval_ms - 1`` 且 ``low <= open, close <= high``。不满足时 CLI 以 ``rc=2``
退出，stderr 报出违规 bar 的索引、字段与期望值，不写任何输出文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from cpt.adapters.reference_chanlun import (
    ChanlunBackend,
    InMemoryChanlunBackend,
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
from cpt.adapters.validators import validate_canonical_bars
from cpt.application.export import dataset_hash, export_dataset
from cpt.domain.config import RulesConfig
from cpt.domain.models import (
    Bi,
    CanonicalBar,
    Fractal,
    Signal,
    StructureEvent,
    ZhongShu,
    make_canonical_bar,
)

__all__ = [
    "load_fixture",
    "replay_bars",
    "replay_incremental",
    "run_replay",
    "main",
]


#: 毫秒/分钟换算：``RulesConfig.levels`` 的单位是分钟。
_MS_PER_MINUTE: Final[int] = 60_000

#: 分型判定至少需要的 K 线根数（``docs/rules.md`` 分型定义）。
_MIN_BARS_FOR_STRUCTURE: Final[int] = 3


_REQUIRED_BAR_FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "close_time",
)


def load_fixture(path: str | Path) -> tuple[RulesConfig, list[CanonicalBar], dict[str, Any]]:
    """从 JSON 文件读取 ``(config, bars, metadata)``。

    JSON 结构::

        {
          "name": "<case name>",
          "description": "<brief>",
          "config": {<可选覆盖,缺省走 default_rules_config()>},
          "metadata": {<可选,会传到 export_dataset metadata>},
          "bars": [{"open_time": ..., "open": ..., ...}, ...]
        }

    缺字段或值非法时抛 ``ValueError``, 错误信息带 case 名 + bar 索引 + 字段名,
    便于排障(而非裸 KeyError)。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"fixture {path} 不是合法 JSON: {e}") from e

    case_name = data.get("name", Path(path).stem)

    config_data = data.get("config")
    if config_data is not None and not isinstance(config_data, dict):
        raise ValueError(
            f"fixture[{case_name}] config 必须是 dict, 收到 {type(config_data).__name__}"
        )
    try:
        config = RulesConfig.from_dict(config_data) if config_data else RulesConfig()
    except (TypeError, ValueError) as e:
        raise ValueError(f"fixture[{case_name}] config 无效: {e}") from e

    bars: list[CanonicalBar] = []
    for idx, raw in enumerate(data.get("bars", [])):
        if not isinstance(raw, dict):
            raise ValueError(
                f"fixture[{case_name}].bars[{idx}] 必须是 dict, 收到 {type(raw).__name__}"
            )
        missing = [f for f in _REQUIRED_BAR_FIELDS if f not in raw]
        if missing:
            raise ValueError(
                f"fixture[{case_name}].bars[{idx}] 缺字段: {missing}; 实际键: {sorted(raw)}"
            )
        try:
            bars.append(
                make_canonical_bar(
                    open_time=raw["open_time"],
                    open=raw["open"],
                    high=raw["high"],
                    low=raw["low"],
                    close=raw["close"],
                    close_time=raw["close_time"],
                    volume=raw.get("volume", 0.0),
                    quote_volume=raw.get("quote_volume", 0.0),
                    trade_count=raw.get("trade_count", 0),
                    taker_buy_base_volume=raw.get("taker_buy_base_volume", 0.0),
                    taker_buy_quote_volume=raw.get("taker_buy_quote_volume", 0.0),
                    is_closed=raw.get("is_closed", True),
                )
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"fixture[{case_name}].bars[{idx}] 字段值非法: {e}") from e

    metadata: dict[str, Any] = {
        "name": case_name,
        "description": data.get("description", ""),
    }
    if data.get("metadata"):
        if not isinstance(data["metadata"], dict):
            raise ValueError(
                f"fixture[{case_name}].metadata 必须是 dict, 收到 {type(data['metadata']).__name__}"
            )
        metadata.update(data["metadata"])
    return config, bars, metadata


def _default_backend() -> InMemoryChanlunBackend:
    return InMemoryChanlunBackend()


def _to_ref_config(config: RulesConfig) -> ReferenceChanlunConfig:
    """把 CPT RulesConfig 投影到反腐层配置。"""
    return ReferenceChanlunConfig(
        use_fx_qy_middle=config.fx_qy_middle,
        use_fx_qj_ck=config.fx_qj_ck,
        use_bi_type_new=config.bi_type_new,
        zs_wzgx=config.zs_wzgx,
        macd_fast=config.macd_fast,
        macd_slow=config.macd_slow,
        macd_signal=config.macd_signal,
    )


def run_replay(
    *,
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    backend: ChanlunBackend | None = None,
    fractals: Sequence[Fractal] | None = None,
    bis: Sequence[Bi] | None = None,
    zhongshus: Sequence[ZhongShu] | None = None,
    events: Sequence[StructureEvent] | None = None,
    signals: Sequence[Signal] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一次 replay 并返回 schema v1 payload。

    未提供 ``backend`` 时使用 ``InMemoryChanlunBackend``（fixture 生成用）。
    ``fractals/bis/zhongshus`` 缺省时由 backend 算出；可显式传入做 oracle 对照。

    Note:
        本函数**不做数据校验**（缺口/连续性/OHLC 均不检查），是低层入口。
        需要数据守卫时用 :func:`replay_bars` 或 :func:`replay_incremental`。
    """
    backend = backend or _default_backend()
    primary_level = config.levels[0]  # __post_init__ 保证 levels 非空

    if fractals is None or bis is None or zhongshus is None:
        result = backend.compute_structures(list(bars), _to_ref_config(config))
        # 传给 map_* 真实 bars, 让 start_time/end_time 解析为毫秒,
        # 避免 PLACEHOLDER_TIME 被 export_dataset 拒绝。
        fractals = (
            fractals
            if fractals is not None
            else [
                map_fractal(
                    fx,
                    level=primary_level,
                    source_ids=(f"b:{fx.bar_index}",),
                    bars=bars,
                )
                for fx in result.fx_list
            ]
        )
        bis = (
            bis
            if bis is not None
            else [
                map_bi(
                    bi,
                    level=primary_level,
                    source_ids=(f"fx:{bi.start_bar}", f"fx:{bi.end_bar}"),
                    bars=bars,
                )
                for bi in result.bi_list
            ]
        )
        zhongshus = (
            zhongshus
            if zhongshus is not None
            else [
                map_zhongshu(
                    zs,
                    level=primary_level,
                    source_ids=tuple(f"bi:{i}" for i in zs.bi_indices),
                    bars=bars,
                )
                for zs in result.zs_list
            ]
        )

    return export_dataset(
        config=config,
        bars=bars,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        events=list(events) if events else [],
        signals=list(signals) if signals else [],
        metadata=metadata,
    )


def _infer_interval_ms(bars: Sequence[CanonicalBar], config: RulesConfig) -> int:
    """从序列自身推断契约周期（毫秒）。

    取相邻 ``open_time`` 的**最小正间隔**：缺口表现为更大的间隔，因此会被
    ``validate_canonical_bars`` 判为 :class:`~cpt.adapters.validators.DataGapError`
    而不是"周期错配"；比基准更细的混入数据则被判为周期错配。

    序列不足两根（无正间隔）时回落到配置的名义周期
    ``config.levels[0]`` 分钟 —— 此时没有任何相邻关系需要校验，周期取值不影响结论。
    """
    configured_interval = config.levels[0] * _MS_PER_MINUTE
    positive_diffs = [
        current.open_time - previous.open_time
        for previous, current in pairwise(bars)
        if current.open_time > previous.open_time
    ]
    if positive_diffs:
        return configured_interval
    return configured_interval


def _validate_bars(
    bars: Sequence[CanonicalBar],
    config: RulesConfig,
) -> tuple[CanonicalBar, ...]:
    """校验并规范化 K 线序列（去重 + 递增 + 无缺口 + OHLC/边界合法）。

    周期由 :func:`_infer_interval_ms` 推断；需要强制某个名义周期（例如真实
    Binance 5m 回填）时，调用方先自行用
    :func:`cpt.adapters.validators.validate_canonical_bars` 校验一次，此处会
    对已校验序列幂等通过。
    """
    return validate_canonical_bars(bars, _infer_interval_ms(bars, config))


def replay_bars(
    bars: Sequence[CanonicalBar],
    config: RulesConfig,
    backend: ChanlunBackend | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验后批量回放，返回 schema v1 payload。

    与 :func:`run_replay` 的唯一差别：**先**调用
    :func:`cpt.adapters.validators.validate_canonical_bars` 把数据接入守卫生效
    （``docs/rules.md`` §8.5：缺口阻断、不自动填充），再把去重后的序列交给
    :func:`run_replay`。这是 M4 推荐的批量入口。

    Args:
        bars: 待回放 K 线，顺序按调用方给定（本函数不重排）。
        config: 规则口径配置。
        backend: 结构计算后端；``None`` 时用 ``InMemoryChanlunBackend``。
        metadata: 写入导出的元数据。

    Returns:
        schema v1 payload（``export_dataset`` 的产物）。

    Raises:
        DataValidationError: 序列自身非法（重复内容冲突、乱序、OHLC 非法、
            ``close_time`` 不满足契约边界、间隔小于周期）。
        DataGapError: 序列存在缺口（禁止跨缺口生成正式结构）。
    """
    validated = _validate_bars(bars, config)
    return run_replay(config=config, bars=validated, backend=backend, metadata=metadata)


def replay_incremental(
    bars: Sequence[CanonicalBar],
    config: RulesConfig,
    backend: ChanlunBackend | None = None,
) -> tuple[dict[str, Any], ...]:
    """逐根推进回放：对每个输入前缀各跑一次，返回等长 payload 元组。

    ``result[i]`` 是 ``bars[: i + 1]`` 的回放结果，用于"每来一根 K 线看一次结构"
    的复盘/实时演练（``docs/implementation-plan.md`` §7：单根推进与历史模式共享
    同一套 domain 算法）。

    流程与保证：

    1. **先校验完整序列**（:func:`_validate_bars`）。一旦存在缺口立即抛
       :class:`~cpt.adapters.validators.DataGapError`，因此**任何前缀都不会跨缺口**
       —— 已验证序列是契约周期上均匀连续的，其任意子区间同样均匀连续，
       无需逐前缀重校验；
    2. 前缀不足 :data:`_MIN_BARS_FOR_STRUCTURE`（3 根，分型下限）时不调用后端，
       直接返回 schema v1 的空结构 payload（``bars`` 为该前缀，结构列表为空）；
    3. 其余前缀走 :func:`run_replay`，因此结构字段与批量回放完全同源。

    Args:
        bars: 完整 K 线序列（不是切片）。
        config: 规则口径配置。
        backend: 结构计算后端；``None`` 时用 ``InMemoryChanlunBackend``。

    Returns:
        长度为 ``len(validated)`` 的元组，第 ``i`` 项对应前缀 ``bars[: i + 1]``；
        每个元素都是 schema v1 payload，可直接序列化。

    Raises:
        DataValidationError: 完整序列自身非法。
        DataGapError: 完整序列存在缺口（此时不产出任何前缀结果）。
    """
    validated = _validate_bars(bars, config)
    payloads: list[dict[str, Any]] = []
    for end in range(1, len(validated) + 1):
        prefix = validated[:end]
        if len(prefix) < _MIN_BARS_FOR_STRUCTURE:
            payloads.append(
                export_dataset(
                    config=config,
                    bars=prefix,
                    fractals=(),
                    bis=(),
                    zhongshus=(),
                    events=(),
                    signals=(),
                )
            )
        else:
            payloads.append(run_replay(config=config, bars=prefix, backend=backend))
    return tuple(payloads)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cpt.application.replay",
        description="Run a fixture through the CPT replay pipeline and export schema v1 JSON.",
    )
    parser.add_argument("--input", required=True, help="Path to fixture JSON.")
    parser.add_argument(
        "--output",
        help=(
            "Path to write the exported schema v1 JSON payload. "
            "Required unless --validate-only is given."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Only validate the fixture bars (dedup/order/contiguity/OHLC); print "
            "row count and time range as JSON to stdout and write no structure file."
        ),
    )
    args = parser.parse_args(argv)

    if not args.validate_only and not args.output:
        parser.error("--output is required unless --validate-only is given")

    try:
        config, bars, metadata = load_fixture(args.input)
        if args.validate_only:
            validated = _validate_bars(bars, config)
            print(
                json.dumps(
                    {
                        "input": args.input,
                        "rows": len(validated),
                        "first_open_time": (validated[0].open_time if validated else None),
                        "last_open_time": (validated[-1].open_time if validated else None),
                        "interval_ms": _infer_interval_ms(validated, config),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        payload = replay_bars(bars, config, metadata=metadata)
        # Path(parent) 确保父目录存在;复用同一序列化参数(allow_nan=False)。
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    except (ValueError, OSError) as exc:
        # ValueError: fixture 非法 / 数据校验失败 / export 拒绝占位时间戳。
        # OSError: fixture 读写失败。
        print(f"replay 失败: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "input": args.input,
                "output": args.output,
                "bars": len(bars),
                "dataset_hash": dataset_hash(payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
