from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _browser() -> str | None:
    return shutil.which("chromium") or str(Path.home() / ".local/bin/chromium")


@pytest.mark.skipif(_browser() is None, reason="Chromium not installed")
def test_chromium_smoke_exposes_interaction_contracts() -> None:
    browser = _browser()
    assert browser is not None and Path(browser).exists()
    result = subprocess.run(
        [
            browser,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--dump-dom",
            f"file://{ROOT / 'dashboard/index.html'}?mode=watch",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=30,
    )
    markers = (
        'data-testid="mode-switch"',
        'data-testid="symbol-select"',
        'data-testid="realtime-refresh"',
        'data-testid="realtime-alert"',
    )
    for marker in markers:
        assert marker in result.stdout
