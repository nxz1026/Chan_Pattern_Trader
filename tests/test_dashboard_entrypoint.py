from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request


def test_demo_entrypoint_serves_schema_snapshot() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "cpt.web", "--host", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 5
        line = ""
        while not line and time.monotonic() < deadline:
            line = process.stdout.readline().strip()
        port = int(re.search(r":(\d+)", line).group(1))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard/snapshot") as response:
            payload = json.load(response)
        assert payload["schema_version"] == "dashboard.v2"
        assert payload["runtime"]["data_source"] == "fixture"
        assert payload["market_24h"]["available"] is False
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_fixture_entrypoint_serves_inspect_endpoint() -> None:
    """B3: /api/dashboard/inspect 在 fixture 模式下可用，调用 trace_containment。"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cpt.web",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--mode",
            "fixture",
            "--limit",
            "120",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 10
        line = ""
        while not line and time.monotonic() < deadline:
            line = process.stdout.readline().strip()
        port = int(re.search(r":(\d+)", line).group(1))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/dashboard/inspect?bar_index=10"
        ) as response:
            payload = json.load(response)
        assert payload["bar_index"] == 10
        assert "raw_bar" in payload
        assert "merged_bar" in payload
        assert "containment_chain" in payload
        # demo 模式（provider 无 inspect）应返回 available:false
        demo_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cpt.web",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--mode",
                "demo",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert demo_proc.stdout is not None
            deadline = time.monotonic() + 5
            demo_line = ""
            while not demo_line and time.monotonic() < deadline:
                demo_line = demo_proc.stdout.readline().strip()
            demo_port = int(re.search(r":(\d+)", demo_line).group(1))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{demo_port}/api/dashboard/inspect?bar_index=0"
            ) as demo_resp:
                demo_payload = json.load(demo_resp)
            assert demo_payload["available"] is False
            assert demo_payload["reason"] == "inspect_unavailable_in_mode"
        finally:
            demo_proc.terminate()
            demo_proc.wait(timeout=5)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_fixture_entrypoint_serves_multi_level_and_config_compare() -> None:
    """多级别真实叠加 + R8 config_compare：fixture 模式注入 multi_level + config_compare 字段。"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cpt.web",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--mode",
            "fixture",
            "--limit",
            "200",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 10
        line = ""
        while not line and time.monotonic() < deadline:
            line = process.stdout.readline().strip()
        port = int(re.search(r":(\d+)", line).group(1))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard/snapshot") as response:
            payload = json.load(response)
        assert payload["multi_level"]["available"] is True
        # 默认 RulesConfig().levels = (5, 30)
        assert "5" in payload["multi_level"]["levels"]
        assert "30" in payload["multi_level"]["levels"]
        assert payload["config_compare"]["available"] is True
        # demo 模式（provider 无 snapshot_for_level）传 level 应被忽略
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/dashboard/snapshot?level=30"
        ) as level_resp:
            level_payload = json.load(level_resp)
        assert level_payload["selected_level"] == 30
        # level=30 应替换 structures 来自 30 分钟级别
        assert level_payload["multi_level"]["available"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_demo_entrypoint_parity_does_not_pretend_zero_match() -> None:
    """A2：demo 模式下 parity 应为 available:false 而非假 0% 匹配的伪结果。"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cpt.web",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--mode",
            "demo",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 5
        line = ""
        while not line and time.monotonic() < deadline:
            line = process.stdout.readline().strip()
        port = int(re.search(r":(\d+)", line).group(1))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard/parity") as response:
            payload = json.load(response)
        assert payload["available"] is False
        assert "reason" in payload
    finally:
        process.terminate()
        process.wait(timeout=5)
