from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import CHROME_FLAGS, CHROME_TIMEOUT_SECONDS, chromium_path

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(chromium_path() is None, reason="Chromium/Chrome not installed")
def test_chromium_smoke_exposes_interaction_contracts() -> None:
    browser = chromium_path()
    assert browser is not None
    result = subprocess.run(
        [
            browser,
            *CHROME_FLAGS,
            "--virtual-time-budget=1500",
            "--dump-dom",
            f"file://{ROOT / 'dashboard/index.html'}?mode=watch",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=CHROME_TIMEOUT_SECONDS,
    )
    markers = (
        'data-testid="mode-switch"',
        'data-testid="symbol-select"',
        'data-testid="realtime-refresh"',
        'data-testid="realtime-alert"',
    )
    for marker in markers:
        assert marker in result.stdout
