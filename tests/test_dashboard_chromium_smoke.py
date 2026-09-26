from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import CHROME_FLAGS, CHROME_TIMEOUT_SECONDS, chromium_path

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(chromium_path() is None, reason="Chromium/Chrome not installed")
def test_dashboard_chromium_headless_smoke() -> None:
    browser = chromium_path()
    assert browser is not None
    result = subprocess.run(
        [
            browser,
            *CHROME_FLAGS,
            "--virtual-time-budget=1500",
            "--dump-dom",
            f"file://{ROOT / 'dashboard/index.html'}?mode=research",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=CHROME_TIMEOUT_SECONDS,
    )
    assert 'data-testid="dashboard-root"' in result.stdout
    assert 'data-testid="mode-switch"' in result.stdout
    assert 'data-testid="interval-select"' in result.stdout
    assert 'data-testid="level-select"' in result.stdout
    assert 'data-testid="symbol-select"' in result.stdout
    assert 'data-testid="realtime-refresh"' in result.stdout
    assert "window.CPTDashboard" not in result.stdout
