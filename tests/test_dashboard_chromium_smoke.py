from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _chromium() -> str | None:
    candidates = (
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        Path.home() / ".local/bin/chromium",
        Path.home() / ".cache/ms-playwright/chromium-1243/chrome-linux-arm64/chrome",
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


@pytest.mark.skipif(_chromium() is None, reason="Chromium not installed")
def test_dashboard_chromium_headless_smoke() -> None:
    browser = _chromium()
    assert browser is not None
    env = {**os.environ, "HOME": str(Path.home())}
    result = subprocess.run(
        [
            browser,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=1500",
            "--dump-dom",
            f"file://{ROOT / 'dashboard/index.html'}?mode=research",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=30,
    )
    assert 'data-testid="dashboard-root"' in result.stdout
    assert 'data-testid="mode-switch"' in result.stdout
    assert 'data-testid="interval-select"' in result.stdout
    assert 'data-testid="level-select"' in result.stdout
    assert 'data-testid="symbol-select"' in result.stdout
    assert 'data-testid="realtime-refresh"' in result.stdout
    assert "window.CPTDashboard" not in result.stdout
