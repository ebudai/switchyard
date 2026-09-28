"""The layout a new project is generated with.

`_new_project_layout_payload` lays a project's role panes out as a single row
through `NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES` roles, and above that as a
row-major grid of at most `NEW_PROJECT_GRID_PANES_PER_ROW` panes a row, the
rows as even as the count allows (`_row_major_grid_layout_payload`). The leaves
(`_new_project_layout_leaves`) and the single row (`_single_row_layout_payload`)
are also what `scripts/legacy_layouts.py` builds the historical shapes from.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-436), in their original
order. The launcher imports this module and re-exports all six names; the six
production modules that read them -- `generated_layout_upgrade.py`,
`legacy_layouts.py`, `new_project_artifacts.py`, `presentation_controller.py`,
`project_role_add.py` and `workflow_launcher.py` -- still read them there. The
functions read each other and the two thresholds through the launcher at call
time, so a suite that rebinds one there still intercepts it. None has a
definition-time default. This module imports `team_launcher` only inside the
functions, when they run.
"""

from __future__ import annotations

import math
from typing import Any


NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES = 3
NEW_PROJECT_GRID_PANES_PER_ROW = 3


def _new_project_layout_leaves(role_count: int) -> list[dict[str, Any]]:
    return [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]


def _single_row_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    if len(leaves) <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": leaves,
    }


def _row_major_grid_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    row_count = math.ceil(len(leaves) / launcher.NEW_PROJECT_GRID_PANES_PER_ROW)
    base_row_size, extra = divmod(len(leaves), row_count)
    rows: list[dict[str, Any]] = []
    start = 0
    for row_index in range(row_count):
        row_size = base_row_size + (1 if row_index < extra else 0)
        row = leaves[start : start + row_size]
        start += row_size
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    if len(rows) == 1:
        return rows[0]
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _new_project_layout_payload(role_count: int) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    leaves = launcher._new_project_layout_leaves(role_count)
    if role_count <= launcher.NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES:
        return launcher._single_row_layout_payload(leaves)
    return launcher._row_major_grid_layout_payload(leaves)
