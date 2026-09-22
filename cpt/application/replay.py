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
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    config_data = data.get("config")
    config = RulesConfig.from_dict(config_data) if config_data else RulesConfig()

    bars: list[CanonicalBar] = []
    for raw in data.get("bars", []):
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

    metadata: dict[str, Any] = {
        "name": data.get("name", ""),
        "description": data.get("description", ""),
    }
    if data.get("metadata"):
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

    if fractals is None or bis is None or zhongshus is None:
        result = backend.compute_structures(list(bars), _to_ref_config(config))
        fractals = (
            fractals
            if fractals is not None
            else [
                map_fractal(
                    fx,
                    level=config.levels[0] if config.levels else 5,
                    source_ids=(f"b:{fx.bar_index}",),
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
                    level=config.levels[0] if config.levels else 5,
                    source_ids=(f"fx:{bi.start_bar}", f"fx:{bi.end_bar}"),
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
                    level=config.levels[0] if config.levels else 5,
                    source_ids=tuple(f"bi:{i}" for i in zs.bi_indices),
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
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
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
