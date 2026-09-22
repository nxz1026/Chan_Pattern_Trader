"""历史回放 CLI。

把 fixture K 线跑过 chanlun 反腐层（含 InMemoryChanlunBackend 占位实现），
产出结构序列并序列化为 schema v1 JSON。

CLI 用法::

    python -m cpt.application.replay \\
        --input tests/fixtures/case1_simple_uptrend.json \\
        --output /tmp/out.json

输出文件含完整 schema v1 payload；stderr 打印 dataset_hash 便于复现性校验。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cpt.adapters.reference_chanlun import (
    ChanlunBackend,
    InMemoryChanlunBackend,
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
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
    "run_replay",
    "main",
]


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cpt.application.replay",
        description="Run a fixture through the CPT replay pipeline and export schema v1 JSON.",
    )
    parser.add_argument("--input", required=True, help="Path to fixture JSON.")
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the exported schema v1 JSON payload.",
    )
    args = parser.parse_args(argv)

    config, bars, metadata = load_fixture(args.input)
    payload = run_replay(config=config, bars=bars, metadata=metadata)

    # Path(parent) 确保父目录存在;复用同一序列化参数(allow_nan=False)。
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    h = dataset_hash(payload)
    print(
        json.dumps(
            {
                "input": args.input,
                "output": args.output,
                "bars": len(bars),
                "dataset_hash": h,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
