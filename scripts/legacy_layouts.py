"""The layouts earlier releases generated, recognised so an upgrade can replace them.

`_known_generated_project_layout_payloads` lists, for a role count, the current
generated layout and every shape an earlier release generated -- stacked,
column-major (a single row through four roles, then the square-root columns),
square-root column-major and chunked row-major -- so an upgrade can tell a
layout it generated from one an operator edited. Each historical shape is
frozen: it must not read the live layout rules, or existing layouts stop
upgrading.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-435), in their original
order. The launcher imports this module and re-exports all five names;
`scripts/generated_layout_upgrade.py` and `scripts/project_role_add.py` still
read the list there. Everything they read -- each other included, the current
layout payload, the layout leaves and the single-row payload -- is read through
the launcher at call time, so a suite that rebinds one there still intercepts
it. None has a definition-time default. This module imports `team_launcher`
only inside the functions, when they run.
"""

from __future__ import annotations

import math
from typing import Any


def _legacy_new_project_stacked_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]
    if role_count <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": [
            leaves[0],
            {
                "Orientation": "Vertical",
                "Widgets": leaves[1:],
            },
        ],
    }


def _legacy_new_project_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    leaves = launcher._new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return launcher._single_row_layout_payload(leaves)
    return launcher._legacy_new_project_sqrt_column_major_layout_payload(role_count)


def _legacy_new_project_sqrt_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    leaves = launcher._new_project_layout_leaves(role_count)
    if role_count <= 1:
        return launcher._single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(role_count))
    row_count = math.ceil(role_count / column_count)
    columns: list[dict[str, Any]] = []
    for start in range(0, role_count, row_count):
        column = leaves[start : start + row_count]
        if len(column) == 1:
            columns.append(column[0])
        else:
            columns.append(
                {
                    "Orientation": "Vertical",
                    "Widgets": column,
                }
            )
    return {
        "Orientation": "Horizontal",
        "Widgets": columns,
    }


def _legacy_new_project_chunked_row_major_layout_payload(role_count: int) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    leaves = launcher._new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return launcher._single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(len(leaves)))
    rows: list[dict[str, Any]] = []
    for start in range(0, len(leaves), column_count):
        row = leaves[start : start + column_count]
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _known_generated_project_layout_payloads(role_count: int) -> tuple[dict[str, Any], ...]:
    from scripts import team_launcher as launcher

    return (
        launcher._new_project_layout_payload(role_count),
        launcher._legacy_new_project_stacked_layout_payload(role_count),
        launcher._legacy_new_project_column_major_layout_payload(role_count),
        launcher._legacy_new_project_sqrt_column_major_layout_payload(role_count),
        launcher._legacy_new_project_chunked_row_major_layout_payload(role_count),
    )
