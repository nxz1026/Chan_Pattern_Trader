from __future__ import annotations

from cpt.application.dashboard_quality import quality_report
from cpt.domain.models import make_canonical_bar


def test_quality_report_lists_gap_and_stale_severity() -> None:
    first = make_canonical_bar(open_time=0, close_time=299999, open=1, high=2, low=1, close=2)
    second = make_canonical_bar(open_time=600000, close_time=899999, open=2, high=3, low=2, close=3)
    report = quality_report((first, second), stale=True)
    assert report["severity"] == "stale"
    assert report["gap_count"] == 1
    assert report["stale"] is True
