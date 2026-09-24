"""Wind 主通道适配器测试（R17）。**绝不联网、绝不消耗真实额度**：注入 runner。

Wind 的取数代价是真实积分，所以这一组测试的存在意义就是"把 CLI 协议钉死在
假回执上"，让任何协议变更都在 CI 里暴露，而不是靠人去花额度试。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cpt.adapters.wind_source import (
    DAILY_INTERVAL_MS,
    WindQuotaError,
    WindSourceClient,
    WindSourceError,
    WindUnavailableError,
    parse_wind_kline,
)

#: 实测返回体形状：数据在 ``content[0].text``（JSON 字符串）里，另附 cli_meta。
KLINE_ROWS = [
    {
        "TIME": "2026-09-22",
        "OPEN": 8850.124,
        "MATCH": 8859.410,
        "HIGH": 8927.393,
        "LOW": 8827.332,
        "VOLUME": 24573.0,
    },
    {
        "TIME": "2026-09-23",
        "OPEN": 8866.332,
        "MATCH": 8845.003,
        "HIGH": 8959.021,
        "LOW": 8843.033,
        "VOLUME": 30981.0,
    },
]


def _success(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        {
            "content": [
                {"type": "text", "text": json.dumps({"data": list(rows)}, ensure_ascii=False)}
            ],
            "cli_meta": {"server_type": "stock_data", "tool_name": "get_stock_kline"},
        },
        ensure_ascii=False,
    )


def _failure(code: str, message: str) -> str:
    return json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False)


class FakeRunner:
    """按调用序返回预设 stdout；记录 argv。"""

    def __init__(self, outputs: Sequence[str]) -> None:
        self.outputs = list(outputs)
        self.argv: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        out = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=out, stderr="")


def _client(
    tmp_path: Path, outputs: Sequence[str], **kwargs: Any
) -> tuple[WindSourceClient, FakeRunner, Path]:
    cli = tmp_path / "cli.mjs"
    cli.write_text("// fake", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text("WIND_API_KEY=test-key\n", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    runner = FakeRunner(outputs)
    client = WindSourceClient(
        cli_script=cli,
        config_path=config,
        ledger_path=ledger,
        runner=runner,
        **kwargs,
    )
    return client, runner, ledger


def test_availability_requires_cli_and_key(tmp_path: Path) -> None:
    client = WindSourceClient(
        cli_script=tmp_path / "missing.mjs", config_path=tmp_path / "missing-config"
    )
    ok, reason = client.availability()
    assert ok is False
    assert reason.startswith("wind_cli_missing")

    cli = tmp_path / "cli.mjs"
    cli.write_text("// fake", encoding="utf-8")
    client = WindSourceClient(cli_script=cli, config_path=tmp_path / "missing-config")
    ok, reason = client.availability()
    assert ok is False
    assert reason.startswith("wind_api_key_missing")


def test_call_parses_success_envelope_and_records_ledger(tmp_path: Path) -> None:
    client, runner, ledger = _client(tmp_path, [_success(KLINE_ROWS)])
    call = client.call("stock_data", "get_stock_kline", {"windcode": "600519.SH"})
    assert call.ok is True
    assert call.data["data"] == KLINE_ROWS
    # argv 形状：node <cli> call <server_type> <tool> <params_json>
    argv = runner.argv[0]
    assert argv[0] == "node"
    assert argv[2] == "call"
    assert argv[3] == "stock_data"
    assert argv[4] == "get_stock_kline"
    assert json.loads(argv[5])["windcode"] == "600519.SH"
    # 台账：每次调用一行，参数只留摘要（不落原始参数）
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["ok"] is True
    assert entry["tool_name"] == "get_stock_kline"
    assert "windcode" not in json.dumps(entry)
    assert len(entry["params_digest"]) == 16


def test_call_raises_quota_error_distinctly(tmp_path: Path) -> None:
    """额度不足 ≠ 数据源坏了：必须分类型抛出，否则运维会去查网络。"""
    client, _, _ = _client(tmp_path, [_failure("RATE_LIMIT_ERROR", "请求过于频繁")])
    with pytest.raises(WindQuotaError):
        client.call("stock_data", "get_stock_kline", {})

    client2, _, _ = _client(tmp_path, [_failure("backend_error", "试用已到期，请充值")])
    with pytest.raises(WindQuotaError):
        client2.call("stock_data", "get_stock_kline", {})


def test_call_raises_unavailable_on_auth_error(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, [_failure("AUTH_ERROR", "未配置 API Key")])
    with pytest.raises(WindUnavailableError, match="鉴权失败"):
        client.call("stock_data", "get_stock_kline", {})


def test_call_raises_source_error_on_param_error(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, [_failure("PARAM_VALIDATION_ERROR", "begin_date 必填")])
    with pytest.raises(WindSourceError, match="PARAM_VALIDATION_ERROR"):
        client.call("stock_data", "get_stock_kline", {})


def test_call_handles_empty_and_non_json_stdout(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, [""])
    with pytest.raises(WindSourceError, match="无输出"):
        client.call("stock_data", "get_stock_kline", {})
    client2, _, _ = _client(tmp_path, ["<html>502</html>"])
    with pytest.raises(WindSourceError, match="不是 JSON"):
        client2.call("stock_data", "get_stock_kline", {})


def test_call_tolerates_noise_around_json(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, [f"提示行\n{_success(KLINE_ROWS)}\n"])
    assert client.call("stock_data", "get_stock_kline", {}).ok is True


def test_call_timeout_raises_source_error(tmp_path: Path) -> None:
    cli = tmp_path / "cli.mjs"
    cli.write_text("// fake", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text("WIND_API_KEY=k\n", encoding="utf-8")

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)

    client = WindSourceClient(cli_script=cli, config_path=config, ledger_path=None, runner=runner)
    with pytest.raises(WindSourceError, match="超时"):
        client.call("stock_data", "get_stock_kline", {})


def test_fetch_daily_bars_uses_hfq_and_parses_rows(tmp_path: Path) -> None:
    client, runner, _ = _client(tmp_path, [_success(KLINE_ROWS)])
    bars = client.fetch_daily_bars("600519.SH", begin_date="2026-09-01", end_date="2026-09-24")
    params = json.loads(runner.argv[0][5])
    # 后复权口径必须显式传 1（与本地库 daily_bar × ref_adjust_factor 对齐）
    assert params["aftype"] == "1"
    assert params["period"] == "1d"
    assert params["begin_date"] == "2026-09-01"
    assert len(bars) == 2
    assert bars[0].close == pytest.approx(8859.410)
    assert bars[0].high == pytest.approx(8927.393)
    assert bars[0].is_closed is True
    expected = int(datetime(2026, 9, 22, tzinfo=UTC).timestamp() * 1000)
    assert bars[0].open_time == expected
    assert bars[0].close_time == expected + DAILY_INTERVAL_MS - 1


def test_fetch_adjust_factors_calls_twice_and_divides(tmp_path: Path) -> None:
    """因子 = 后复权价 / 不复权价 —— Wind 不返回因子，只能算。"""
    raw_rows = [
        {
            "TIME": "2026-09-22",
            "OPEN": 1250.0,
            "MATCH": 1251.0,
            "HIGH": 1260.0,
            "LOW": 1245.0,
            "VOLUME": 1.0,
        },
    ]
    hfq_rows = [
        {
            "TIME": "2026-09-22",
            "OPEN": 8850.0,
            "MATCH": 8859.0,
            "HIGH": 8920.0,
            "LOW": 8810.0,
            "VOLUME": 1.0,
        },
    ]
    client, runner, _ = _client(tmp_path, [_success(raw_rows), _success(hfq_rows)])
    factors = client.fetch_adjust_factors(
        "600519.SH", begin_date="2026-09-22", end_date="2026-09-22"
    )
    assert client.call_count == 2
    assert json.loads(runner.argv[0][5])["aftype"] == "2"
    assert json.loads(runner.argv[1][5])["aftype"] == "1"
    assert factors == {"2026-09-22": pytest.approx(8859.0 / 1251.0)}


def test_probe_uses_lightest_tool(tmp_path: Path) -> None:
    client, runner, ledger = _client(
        tmp_path, [_success([{"windcode": "600519.SH", "close": 1237.0}])]
    )
    result = client.probe()
    assert result["status"] == "ok"
    assert runner.argv[0][4] == "get_stock_price_indicators"
    assert json.loads(runner.argv[0][5]) == {"windcode": "600519.SH"}
    assert ledger.exists()


def test_parse_wind_kline_accepts_column_map_and_compact_dates() -> None:
    columns = {
        "TIME": ["20260922", "20260923"],
        "OPEN": [1.0, 2.0],
        "MATCH": [1.5, 2.5],
        "HIGH": [1.8, 2.8],
        "LOW": [0.9, 1.9],
        "VOLUME": [10.0, 20.0],
    }
    bars = parse_wind_kline({"data": columns}, windcode="600519.SH")
    assert [bar.close for bar in bars] == [1.5, 2.5]
    assert bars[0].open_time == int(datetime(2026, 9, 22, tzinfo=UTC).timestamp() * 1000)


def test_parse_wind_kline_rejects_missing_rows() -> None:
    with pytest.raises(WindSourceError, match="找不到行数据"):
        parse_wind_kline({"meta": "empty"}, windcode="600519.SH")


def test_parse_wind_kline_rejects_bad_date() -> None:
    rows = [{"TIME": "not-a-date", "OPEN": 1, "MATCH": 1, "HIGH": 1, "LOW": 1, "VOLUME": 1}]
    with pytest.raises(WindSourceError, match="不是数字"):
        parse_wind_kline({"data": rows}, windcode="600519.SH")


def test_no_real_network_in_this_module() -> None:
    """护栏：本测试模块不得出现真实网络调用（CI 无网也要绿）。"""
    source = Path(__file__).read_text(encoding="utf-8")
    # 用拼接构造被禁词，避免断言自己命中自己
    forbidden = ("url" + "open", "requests." + "get", "socket." + "create_connection")
    for token in forbidden:
        assert token not in source, f"测试模块里出现了真实网络调用：{token}"
