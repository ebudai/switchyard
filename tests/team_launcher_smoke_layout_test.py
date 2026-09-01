#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_konsole_process_launcher_is_explicit_only() -> None:
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")

    assert 'getattr(runner, "process_launcher"' not in source
    assert not hasattr(FakeRunner(), "process_launcher")

def test_pgu_layout_matches_reference_six_pane_geometry() -> None:
    layout = json.loads((ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json").read_text(encoding="utf-8"))

    assert _layout_shape(layout) == {
        "Orientation": "Horizontal",
        "Widgets": [
            {
                "Orientation": "Vertical",
                "Widgets": ["leaf", "leaf"],
            },
            {
                "Orientation": "Vertical",
                "Widgets": [
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                ],
            },
        ],
    }
    assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {""}

def test_generated_new_project_layouts_are_balanced_row_major_grids() -> None:
    expected_by_count = {
        1: 0,
        2: {
            "Orientation": "Horizontal",
            "Widgets": [0, 1],
        },
        3: {
            "Orientation": "Horizontal",
            "Widgets": [0, 1, 2],
        },
        4: {
            "Orientation": "Horizontal",
            "Widgets": [0, 1, 2, 3],
        },
        5: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4]},
            ],
        },
        6: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
            ],
        },
        7: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2, 3]},
                {"Orientation": "Horizontal", "Widgets": [4, 5, 6]},
            ],
        },
        8: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2, 3]},
                {"Orientation": "Horizontal", "Widgets": [4, 5, 6, 7]},
            ],
        },
        9: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
                {"Orientation": "Horizontal", "Widgets": [6, 7, 8]},
            ],
        },
        10: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2, 3]},
                {"Orientation": "Horizontal", "Widgets": [4, 5, 6]},
                {"Orientation": "Horizontal", "Widgets": [7, 8, 9]},
            ],
        },
        11: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2, 3]},
                {"Orientation": "Horizontal", "Widgets": [4, 5, 6, 7]},
                {"Orientation": "Horizontal", "Widgets": [8, 9, 10]},
            ],
        },
        12: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2, 3]},
                {"Orientation": "Horizontal", "Widgets": [4, 5, 6, 7]},
                {"Orientation": "Horizontal", "Widgets": [8, 9, 10, 11]},
            ],
        },
    }

    for role_count, expected in expected_by_count.items():
        layout = team_launcher._new_project_layout_payload(role_count)
        tree = _layout_session_tree(layout)
        assert tree == expected
        row_lengths = _layout_row_lengths(tree)
        assert not (1 in row_lengths and 3 in row_lengths), (role_count, row_lengths)
        leaves = team_launcher._layout_leaves(layout)
        assert [leaf["SessionRestoreId"] for leaf in leaves] == list(range(role_count))
        assert {leaf.get("Command") for leaf in leaves} == {""}
        assert {leaf.get("WorkingDirectory") for leaf in leaves} == {""}

def test_new_project_default_layout_prefers_single_row_for_small_role_counts() -> None:
    assert team_launcher.NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES == 4
    assert _layout_session_tree(team_launcher._new_project_layout_payload(3)) == {
        "Orientation": "Horizontal",
        "Widgets": [0, 1, 2],
    }
    assert _layout_session_tree(team_launcher._new_project_layout_payload(4)) == {
        "Orientation": "Horizontal",
        "Widgets": [0, 1, 2, 3],
    }
    assert _layout_session_tree(team_launcher._legacy_new_project_column_major_layout_payload(3)) == {
        "Orientation": "Horizontal",
        "Widgets": [0, 1, 2],
    }

def test_new_project_default_layout_falls_back_to_grid_above_single_row_threshold() -> None:
    assert _layout_session_tree(
        team_launcher._new_project_layout_payload(team_launcher.NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES + 1)
    ) == {
        "Orientation": "Vertical",
        "Widgets": [
            {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
            {"Orientation": "Horizontal", "Widgets": [3, 4]},
        ],
    }

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_smoke_layout_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
