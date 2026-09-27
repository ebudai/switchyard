"""The presentation layout modes the launcher and its modules choose between.

`LAYOUT_MODE_AUTO` lets the launcher decide, `LAYOUT_MODE_SEPARATE` opens one
terminal tab per slot, and `LAYOUT_MODE_VIEWER` tiles every role into one tmux
session; `LAYOUT_MODE_CHOICES` is the set a config may name. Handing the
presentation back to the caller takes `LAYOUT_MODE_SEPARATE` as a default
argument, so the modes live in this leaf, which both import, and the default
is the launcher's own object.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-316); `team_launcher`
still exports every name.
"""

from __future__ import annotations

LAYOUT_MODE_AUTO = "auto"
LAYOUT_MODE_SEPARATE = "separate"
LAYOUT_MODE_VIEWER = "viewer"
LAYOUT_MODE_CHOICES = frozenset({LAYOUT_MODE_AUTO, LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER})
