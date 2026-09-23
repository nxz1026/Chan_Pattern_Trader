"""Compare the M1 placeholder backend with the MIT Rust chanlun oracle.

This is a diagnostic, not a claim that CPT and the oracle implement identical
rules. Frozen Binance snapshots make each comparison reproducible offline.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "tests" / "fixtures" / "oracle"
DEFAULT_REPORT = ROOT / "docs" / "m2-oracle-diagnostic.md"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
INTERVAL_MS = 300_000
RANGE_STARTS = (
    "2024-02-01T00:00:00+00:00",
    "2024-09-01T00:00:00+00:00",
    "2025-04-01T00:00:00+00:00",
)
CSV_FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)
ORACLE_VERSION = "2606.73"


def _fetch_rows(start_time: int) -> list[list[Any]]:
    query = urllib.parse.urlencode(
        {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": start_time,
            "limit": 1000,
        }
    )
    request = urllib.request.Request(
        f"https://fapi.binance.com/fapi/v1/klines?{query}",
        headers={"User-Agent": "CPT-oracle-diagnostic/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, list) or len(payload) != 1000:
        raise ValueError(f"Expected 1000 Binance candles, received {len(payload)}")
    return payload


def fetch_snapshots(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for start_text in RANGE_STARTS:
        start_ms = int(datetime.fromisoformat(start_text).timestamp() * 1000)
        rows = _fetch_rows(start_ms)
        path = data_dir / f"btcusdt_5m_{start_text[:10]}.csv"
        if path.exists():
            print(f"preserved existing snapshot: {path}")
            continue
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(CSV_FIELDS)
            writer.writerows(rows)
        try:
            label = path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            label = str(path)
        print(f"saved {label}: {len(rows)} rows")


def load_snapshot(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError(f"Unexpected columns in {path}")
        rows: list[dict[str, Any]] = []
        for row in reader:
            item = dict(row)
            for key in ("open_time", "close_time", "trade_count"):
                item[key] = int(item[key])
            for key in (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_volume",
                "taker_buy_base_volume",
                "taker_buy_quote_volume",
            ):
                item[key] = float(item[key])
            rows.append(item)
    if len(rows) != 1000:
        raise ValueError(f"{path} has {len(rows)} candles; expected exactly 1000")
    times = [row["open_time"] for row in rows]
    if any(b - a != INTERVAL_MS for a, b in zip(times, times[1:], strict=False)):
        raise ValueError(f"{path} has missing, duplicate, or out-of-order candles")
    if any(row["close_time"] - row["open_time"] != INTERVAL_MS - 1 for row in rows):
        raise ValueError(f"{path} has unexpected close-time boundaries")
    return rows, digest


def _kind(value: object) -> str:
    text = str(value)
    if "顶" in text:
        return "top"
    if "底" in text:
        return "bottom"
    raise ValueError(f"Unrecognized Rust oracle fractal kind: {text}")


def _direction(value: object) -> str:
    text = str(value)
    if "向上" in text:
        return "up"
    if "向下" in text:
        return "down"
    raise ValueError(f"Unrecognized Rust oracle direction: {text}")


def _price(value: float) -> float:
    return round(float(value), 8)


def _analyze(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    actual_version = importlib.metadata.version("chanlun")
    if actual_version != ORACLE_VERSION:
        raise RuntimeError(f"Expected chanlun=={ORACLE_VERSION}; found {actual_version}")

    from cpt.adapters.reference_chanlun import InMemoryChanlunBackend, ReferenceChanlunConfig
    from cpt.adapters.rust_chanlun import RustChanlunBackend
    from cpt.domain.models import make_canonical_bar

    bars = [
        make_canonical_bar(
            open_time=row["open_time"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            close_time=row["close_time"],
            volume=row["volume"],
            quote_volume=row["quote_volume"],
            trade_count=row["trade_count"],
            taker_buy_base_volume=row["taker_buy_base_volume"],
            taker_buy_quote_volume=row["taker_buy_quote_volume"],
        )
        for row in rows
    ]
    config = ReferenceChanlunConfig()
    placeholder = InMemoryChanlunBackend().compute_structures(bars, config)
    rust_result = RustChanlunBackend(symbol=SYMBOL, interval_seconds=300).compute_structures(
        bars, config
    )
    rust_fx = [
        (fx.bar_index, fx.kind, _price(fx.high if fx.kind == "top" else fx.low))
        for fx in rust_result.fx_list
    ]
    placeholder_fx = [
        (fx.bar_index, fx.kind, _price(fx.high if fx.kind == "top" else fx.low))
        for fx in placeholder.fx_list
    ]
    rust_fx_set = set(rust_fx)
    placeholder_fx_set = set(placeholder_fx)
    rust_bis = [
        (
            bi.start_time,
            bi.end_time,
            "up" if bi.direction > 0 else "down",
            _price(bi.high),
            _price(bi.low),
        )
        for bi in rust_result.bi_list
    ]
    return {
        "bars": len(rows),
        "oracle": {
            "name": "chanlun",
            "version": actual_version,
            "merged_candles": rust_result.merged_candle_count,
            "fractals": len(rust_result.fx_list),
            "bis": len(rust_result.bi_list),
            "bi_centers": rust_result.zhongshu_count,
        },
        "placeholder": {
            "name": "InMemoryChanlunBackend",
            "fractals": len(placeholder_fx),
            "bis": len(placeholder.bi_list),
            "bi_centers": len(placeholder.zs_list),
        },
        "fractal_exact_match": {
            "matched": len(rust_fx_set & placeholder_fx_set),
            "oracle_only": len(rust_fx_set - placeholder_fx_set),
            "placeholder_only": len(placeholder_fx_set - rust_fx_set),
            "oracle_only_examples": sorted(rust_fx_set - placeholder_fx_set)[:5],
            "placeholder_only_examples": sorted(placeholder_fx_set - rust_fx_set)[:5],
        },
        "oracle_bi_examples": rust_bis[:3],
    }


def build_report(data_dir: Path) -> dict[str, Any]:
    results = []
    for start_text in RANGE_STARTS:
        path = data_dir / f"btcusdt_5m_{start_text[:10]}.csv"
        rows, digest = load_snapshot(path)
        end_time = datetime.fromtimestamp(rows[-1]["open_time"] / 1000, tz=UTC).isoformat()
        results.append(
            {
                "window_start": start_text,
                "window_end_last_open": end_time,
                "snapshot": path.name,
                "snapshot_sha256": digest,
                **_analyze(rows),
            }
        )
    return {
        "report": "CPT M2 diagnostic oracle comparison",
        "scope": "diagnostic_only_not_algorithm_equivalence",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "source": "https://fapi.binance.com/fapi/v1/klines",
        "oracle": f"PyPI chanlun=={ORACLE_VERSION} (MIT, Rust/PyO3)",
        "reference_chanlun_pro": {
            "commit": "78ffa470f1e9463809d8fe2a2802e9e84b896dfe",
            "status": "not_executed: PyArmor runtime reports missing license",
        },
        "windows": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# CPT M2 Oracle 诊断对照",
        "",
        "生成方式：`python scripts/compare_oracle.py --data-dir tests/fixtures/oracle "
        "--output docs/m2-oracle-diagnostic.md`",
        "",
        "> **诊断结果，不是规则等价验收。** CPT M1 后端是用于 fixture 的极简占位实现；"
        "本报告量化其与独立 Rust 实现的差异，不据此评价正式 CPT 算法正确性，也不宣称 M2 完成。",
        "",
        "## 范围与复现",
        "",
        f"- 标的：`{report['symbol']}` 永续；周期：`{report['interval']}`。",
        f"- 行情来源：[Binance USDⓈ-M K 线 API]({report['source']})；"
        "每个窗口冻结 1000 根完整、连续 K 线 CSV。",
        f"- Oracle：`{report['oracle']}`；通过 PyO3 独立运行，"
        "不调用 CPT 占位算法生成 oracle 结果。"
        "安装：`.venv/bin/pip install -e '.[oracle]'`。",
        f"- 原计划 chanlun-pro 固定版：`{report['reference_chanlun_pro']['commit']}`；"
        "状态：未执行（本机运行时明确报“缺少运行许可文件”）。",
        "- CPT 对照对象：`InMemoryChanlunBackend`；每根普通 K 线严格使用相同原始 OHLCV 输入。",
        "- 分型精确匹配键：原始 K 线索引、顶/底类别、特征价格（8 位小数）。"
        "笔中枢仅报告数量，不声称概念或映射完全等价。",
        "",
        "## 结果",
        "",
        "| UTC 窗口起始 | K线数 | 缠K数 (oracle) | 分型 oracle / CPT "
        "| 精确匹配 / oracle-only / CPT-only | 笔 oracle / CPT | 笔中枢 oracle / CPT |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["windows"]:
        counts = item["fractal_exact_match"]
        lines.append(
            f"| {item['window_start']} | {item['bars']} | {item['oracle']['merged_candles']} "
            f"| {item['oracle']['fractals']} / {item['placeholder']['fractals']} "
            f"| {counts['matched']} / {counts['oracle_only']} / {counts['placeholder_only']} "
            f"| {item['oracle']['bis']} / {item['placeholder']['bis']} "
            f"| {item['oracle']['bi_centers']} / {item['placeholder']['bi_centers']} |"
        )
    lines.extend(
        [
            "",
            "## 差异归因与解释边界",
            "",
            "1. **CPT 分型/笔数量较多属于预期的实现差异**：M1 占位仅检查原始 K 线左右邻居的"
            "严格高低点，"
            "并做同类极值替换；Rust oracle 先做包含合并，再运行自己的分型、新笔递归算法。"
            "两套算法不是可互换实现。",
            "2. **CPT 笔中枢恒为 0 是占位后端的明确能力缺口**："
            "`InMemoryChanlunBackend` 文档说明不计算中枢；Rust 结果来自独立笔中枢序列。"
            "不能把此差异解释为 oracle 错误。",
            "3. **索引坐标不同**：Rust 分型中心的 `原始起始序号` 指回合并缠K包含的原始K线；"
            "CPT 占位索引是未经包含处理的普通K线。匹配使用对应原始 K 线索引，"
            "但被合并的 candle 不保证一一映射。",
            "4. **仍需正式算法才能逐项归因**：当前占位版本没有缠K包含规则、新笔规则或中枢结果；"
            "本诊断只能将差异归因为已知实现边界，"
            "不覆盖“CPT 正式算法 vs oracle”的逐结构 M2 审计。",
            "5. **替代 oracle 的口径边界**：PyPI MIT Rust 版用于黑盒对照；"
            "其规则/API 与 chanlun-pro 配置字段并非天然等价，"
            "不能验证 `level` / `zs_wzgx` 等 chanlun-pro 专属口径。",
            "",
            "## 快照校验",
            "",
            "| 文件 | SHA-256 | 最后一根开盘时间 (UTC) |",
            "|---|---|---|",
        ]
    )
    for item in report["windows"]:
        lines.append(
            f"| `{item['snapshot']}` | `{item['snapshot_sha256']}` "
            f"| {item['window_end_last_open']} |"
        )
    lines.extend(
        [
            "",
            "快照可离线复跑；重新抓取网络数据会产生新快照哈希，不覆盖原快照，"
            "需由调用者显式确认后替换。",
            "",
            "## 外部证据",
            "",
            "- [PyPI chanlun 2606.73 项目页与 API 示例]"
            "(https://pypi.org/project/chanlun/2606.73/)：Rust/PyO3 绑定、Python 3.9+、MIT。",
            "- [chanlun.rs 仓库](https://github.com/YuYuKunKun/chanlun.rs)："
            "公开说明其 MIT 许可证、Python 绑定及参考 chan.py API。",
            "- [Binance USDⓈ-M K 线接口]"
            "(https://binance-docs.github.io/apidocs/futures/en/#kline-candlestick-data)："
            "公开 K 线源。",
            "",
            "## 踩坑记录",
            "",
            "- 当前 CPT venv 为 Python 3.14、Linux aarch64；PyPI 未提供可直接使用的 wheel，"
            "本次通过包构建后端编译成功。首次构建因用户缓存目录不可写失败；"
            "改用已允许的环境后重试安装成功。安装依赖组已记录为 `oracle` optional extra。",
            "- chanlun-pro 的 `cl.py` 可见文本仅有 PyArmor 启动封装；"
            "`import chanlun.cl` 实跑失败：`RuntimeError: 缺少运行许可文件 (1:10672)`。"
            "本诊断不尝试绕过授权。",
            "- oracle 的分型/笔字段来自中文 PyO3 扩展；时间戳单位为秒、Binance 输入为毫秒。"
            "导出映射统一为毫秒，避免 1000 倍偏差。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=("Fetch missing fixed Binance windows; never overwrite existing snapshots."),
    )
    args = parser.parse_args(argv)
    if args.fetch:
        fetch_snapshots(args.data_dir)
    try:
        report = build_report(args.data_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"oracle comparison failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
