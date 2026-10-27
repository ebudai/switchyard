"""A turn's tool calls, as its own provider's hooks recorded them: the turn moving (SYRD-695).

Claude Code's and Codex's board hooks write the pane's state only when a turn
starts and ends. On SYRD-691 a turn of short tool calls, none long enough to
earn an execution lease, therefore looked like a pane saying busy with nothing
moving, and the Director was told so. Each tool call now runs the hook in its
`activity` mode, which writes `<target>.activity.json` beside the pane's state
-- never into it -- naming the provider process that ran it.

Only the turn in flight counts: while the pane says busy, after that turn
started, from the process that took it, and never later than now. A call left
by an earlier turn, a replaced provider or a reused pid says nothing about it.
"""

from __future__ import annotations

from typing import Any


def turn_tool_activity_at(state: Any, now: float) -> float | None:
    """When the turn in flight last called a tool, as its own provider's hook recorded it; else None."""
    activity, process = getattr(state, "tool_activity", None), getattr(state, "turn_process", None)
    started = getattr(state, "turn_started_at", None)
    if getattr(state, "state", None) != "busy" or started is None or not isinstance(activity, dict) \
            or not isinstance(process, dict):
        return None
    try:
        at = float(activity["at"])
        same = ((int(activity["pane_pid"]), int(activity["pane_start_time"]))
                == (int(process["pane_pid"]), int(process["pane_start_time"])))
    except (KeyError, TypeError, ValueError):
        return None
    return at if same and started <= at <= now else None


def hook_word_at(state: Any, now: float) -> float:
    """When the hook last spoke about the pane: its own write, or a tool call of the turn in flight."""
    activity = turn_tool_activity_at(state, now)
    return state.updated_at if activity is None else max(state.updated_at, activity)
