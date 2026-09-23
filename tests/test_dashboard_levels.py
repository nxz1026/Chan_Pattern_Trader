from __future__ import annotations

from cpt.application.dashboard_levels import level_tree


def test_level_tree_groups_and_links_parent_levels() -> None:
    result = level_tree(
        [
            {"id": "a", "level": 1, "start_time": 2},
            {"id": "b", "level": 2, "start_time": 1},
        ]
    )
    assert result[0]["level"] == 1
    assert result[0]["parent_level"] == 2
    assert result[1]["parent_level"] is None
