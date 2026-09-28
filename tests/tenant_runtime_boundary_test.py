#!/usr/bin/env python3
"""SYRD-404: the tenant runtime state and description, against the launcher they came out of.

`TENANT_RUNTIME_STATES`, the frozen `TenantRuntime` record and
`describe_tenant_runtime` moved unchanged into `scripts/tenant_runtime.py`,
and the launcher re-exports all three. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module imports nothing of Switchyard's, and none of the three reads the
  launcher; the record is frozen, with the baseline's fields, order, defaults
  and one property; the tuple is the baseline's five states.
- **The behaviour is the baseline's:** the state and the one-line description
  for every combination of board, listener, sessions (none, one, several),
  window, escaped processes and unproved parts -- `GOLDEN` below was produced
  by the BASELINE launcher's own record and description (`gold404.py`), not
  typed.
- **Its neighbours stay:** `suspend_tenant` is the baseline's, node for node.

Pure: no tenant, command, service or process is involved.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import tenant_runtime as m  # noqa: E402

CHECKS = 0
MOVED = ("TENANT_RUNTIME_STATES", "TenantRuntime", "describe_tenant_runtime")
#: The BASELINE's own answers (`gold404.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'states': ['running', 'presentation-closed', 'partially-stopped', 'suspended', 'unregistered'],
    'combinations': [
        [True, True, 'none', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open)'],
        [True, True, 'none', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open) -- not proved: the listener did not answer'],
        [True, True, 'none', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open) -- not proved: the board did not answer; no window probe'],
        [True, True, 'none', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open, escaped processes 2)'],
        [True, True, 'none', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'none', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, True, 'none', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed)'],
        [True, True, 'none', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed) -- not proved: the listener did not answer'],
        [True, True, 'none', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed) -- not proved: the board did not answer; no window probe'],
        [True, True, 'none', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed, escaped processes 2)'],
        [True, True, 'none', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'none', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener up, sessions 0, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, True, 'one', True, 'none', 'none', 'running', 'atlas: running (board up, listener up, sessions 1, window open)'],
        [True, True, 'one', True, 'none', 'one', 'running', 'atlas: running (board up, listener up, sessions 1, window open) -- not proved: the listener did not answer'],
        [True, True, 'one', True, 'none', 'several', 'running', 'atlas: running (board up, listener up, sessions 1, window open) -- not proved: the board did not answer; no window probe'],
        [True, True, 'one', True, 'some', 'none', 'running', 'atlas: running (board up, listener up, sessions 1, window open, escaped processes 2)'],
        [True, True, 'one', True, 'some', 'one', 'running', 'atlas: running (board up, listener up, sessions 1, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'one', True, 'some', 'several', 'running', 'atlas: running (board up, listener up, sessions 1, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, True, 'one', False, 'none', 'none', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed)'],
        [True, True, 'one', False, 'none', 'one', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed) -- not proved: the listener did not answer'],
        [True, True, 'one', False, 'none', 'several', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed) -- not proved: the board did not answer; no window probe'],
        [True, True, 'one', False, 'some', 'none', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed, escaped processes 2)'],
        [True, True, 'one', False, 'some', 'one', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'one', False, 'some', 'several', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 1, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, True, 'several', True, 'none', 'none', 'running', 'atlas: running (board up, listener up, sessions 3, window open)'],
        [True, True, 'several', True, 'none', 'one', 'running', 'atlas: running (board up, listener up, sessions 3, window open) -- not proved: the listener did not answer'],
        [True, True, 'several', True, 'none', 'several', 'running', 'atlas: running (board up, listener up, sessions 3, window open) -- not proved: the board did not answer; no window probe'],
        [True, True, 'several', True, 'some', 'none', 'running', 'atlas: running (board up, listener up, sessions 3, window open, escaped processes 2)'],
        [True, True, 'several', True, 'some', 'one', 'running', 'atlas: running (board up, listener up, sessions 3, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'several', True, 'some', 'several', 'running', 'atlas: running (board up, listener up, sessions 3, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, True, 'several', False, 'none', 'none', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed)'],
        [True, True, 'several', False, 'none', 'one', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed) -- not proved: the listener did not answer'],
        [True, True, 'several', False, 'none', 'several', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed) -- not proved: the board did not answer; no window probe'],
        [True, True, 'several', False, 'some', 'none', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed, escaped processes 2)'],
        [True, True, 'several', False, 'some', 'one', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, True, 'several', False, 'some', 'several', 'presentation-closed', 'atlas: presentation-closed (board up, listener up, sessions 3, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'none', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open)'],
        [True, False, 'none', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open) -- not proved: the listener did not answer'],
        [True, False, 'none', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open) -- not proved: the board did not answer; no window probe'],
        [True, False, 'none', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open, escaped processes 2)'],
        [True, False, 'none', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'none', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'none', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed)'],
        [True, False, 'none', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed) -- not proved: the listener did not answer'],
        [True, False, 'none', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed) -- not proved: the board did not answer; no window probe'],
        [True, False, 'none', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed, escaped processes 2)'],
        [True, False, 'none', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'none', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 0, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'one', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open)'],
        [True, False, 'one', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open) -- not proved: the listener did not answer'],
        [True, False, 'one', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open) -- not proved: the board did not answer; no window probe'],
        [True, False, 'one', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open, escaped processes 2)'],
        [True, False, 'one', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'one', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'one', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed)'],
        [True, False, 'one', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed) -- not proved: the listener did not answer'],
        [True, False, 'one', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed) -- not proved: the board did not answer; no window probe'],
        [True, False, 'one', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed, escaped processes 2)'],
        [True, False, 'one', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'one', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 1, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'several', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open)'],
        [True, False, 'several', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open) -- not proved: the listener did not answer'],
        [True, False, 'several', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open) -- not proved: the board did not answer; no window probe'],
        [True, False, 'several', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open, escaped processes 2)'],
        [True, False, 'several', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'several', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [True, False, 'several', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed)'],
        [True, False, 'several', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed) -- not proved: the listener did not answer'],
        [True, False, 'several', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed) -- not proved: the board did not answer; no window probe'],
        [True, False, 'several', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed, escaped processes 2)'],
        [True, False, 'several', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [True, False, 'several', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board up, listener down, sessions 3, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'none', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open)'],
        [False, True, 'none', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open) -- not proved: the listener did not answer'],
        [False, True, 'none', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open) -- not proved: the board did not answer; no window probe'],
        [False, True, 'none', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open, escaped processes 2)'],
        [False, True, 'none', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'none', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'none', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed)'],
        [False, True, 'none', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed) -- not proved: the listener did not answer'],
        [False, True, 'none', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed) -- not proved: the board did not answer; no window probe'],
        [False, True, 'none', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed, escaped processes 2)'],
        [False, True, 'none', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'none', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 0, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'one', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open)'],
        [False, True, 'one', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open) -- not proved: the listener did not answer'],
        [False, True, 'one', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open) -- not proved: the board did not answer; no window probe'],
        [False, True, 'one', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open, escaped processes 2)'],
        [False, True, 'one', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'one', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'one', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed)'],
        [False, True, 'one', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed) -- not proved: the listener did not answer'],
        [False, True, 'one', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed) -- not proved: the board did not answer; no window probe'],
        [False, True, 'one', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed, escaped processes 2)'],
        [False, True, 'one', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'one', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 1, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'several', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open)'],
        [False, True, 'several', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open) -- not proved: the listener did not answer'],
        [False, True, 'several', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open) -- not proved: the board did not answer; no window probe'],
        [False, True, 'several', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open, escaped processes 2)'],
        [False, True, 'several', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'several', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, True, 'several', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed)'],
        [False, True, 'several', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed) -- not proved: the listener did not answer'],
        [False, True, 'several', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed) -- not proved: the board did not answer; no window probe'],
        [False, True, 'several', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed, escaped processes 2)'],
        [False, True, 'several', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, True, 'several', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener up, sessions 3, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'none', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open)'],
        [False, False, 'none', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open) -- not proved: the listener did not answer'],
        [False, False, 'none', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open) -- not proved: the board did not answer; no window probe'],
        [False, False, 'none', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open, escaped processes 2)'],
        [False, False, 'none', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'none', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'none', False, 'none', 'none', 'suspended', 'atlas: suspended (board down, listener down, sessions 0, window closed)'],
        [False, False, 'none', False, 'none', 'one', 'suspended', 'atlas: suspended (board down, listener down, sessions 0, window closed) -- not proved: the listener did not answer'],
        [False, False, 'none', False, 'none', 'several', 'suspended', 'atlas: suspended (board down, listener down, sessions 0, window closed) -- not proved: the board did not answer; no window probe'],
        [False, False, 'none', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window closed, escaped processes 2)'],
        [False, False, 'none', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'none', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 0, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'one', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open)'],
        [False, False, 'one', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open) -- not proved: the listener did not answer'],
        [False, False, 'one', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open) -- not proved: the board did not answer; no window probe'],
        [False, False, 'one', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open, escaped processes 2)'],
        [False, False, 'one', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'one', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'one', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed)'],
        [False, False, 'one', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed) -- not proved: the listener did not answer'],
        [False, False, 'one', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed) -- not proved: the board did not answer; no window probe'],
        [False, False, 'one', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed, escaped processes 2)'],
        [False, False, 'one', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'one', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 1, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'several', True, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open)'],
        [False, False, 'several', True, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open) -- not proved: the listener did not answer'],
        [False, False, 'several', True, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open) -- not proved: the board did not answer; no window probe'],
        [False, False, 'several', True, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open, escaped processes 2)'],
        [False, False, 'several', True, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'several', True, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window open, escaped processes 2) -- not proved: the board did not answer; no window probe'],
        [False, False, 'several', False, 'none', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed)'],
        [False, False, 'several', False, 'none', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed) -- not proved: the listener did not answer'],
        [False, False, 'several', False, 'none', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed) -- not proved: the board did not answer; no window probe'],
        [False, False, 'several', False, 'some', 'none', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed, escaped processes 2)'],
        [False, False, 'several', False, 'some', 'one', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed, escaped processes 2) -- not proved: the listener did not answer'],
        [False, False, 'several', False, 'some', 'several', 'partially-stopped', 'atlas: partially-stopped (board down, listener down, sessions 3, window closed, escaped processes 2) -- not proved: the board did not answer; no window probe'],
    ],
    'defaults': [[], [], 'suspended', 'atlas: suspended (board down, listener down, sessions 0, window closed)'],
    'fields': [['project', 'MISSING'], ['board_active', 'MISSING'], ['listener_active', 'MISSING'], ['live_sessions', 'MISSING'], ['presentation_open', 'MISSING'], ['residual', '()'], ['unknown', '()']],
    'frozen': True,
    'mutation': 'FrozenInstanceError',
    'properties': ['state'],
}
#: `ast.dump` of the baseline's `suspend_tenant` (read from the baseline commit when this file was generated), the neighbour below the moved block.
SUSPEND_TENANT = 'FunctionDef(name=\'suspend_tenant\', args=arguments(args=[arg(arg=\'config\', annotation=Name(id=\'ProjectConfig\', ctx=Load()))], kwonlyargs=[arg(arg=\'config_path\', annotation=Name(id=\'Path\', ctx=Load())), arg(arg=\'gui_user\', annotation=Name(id=\'str\', ctx=Load())), arg(arg=\'runner\', annotation=Subscript(value=Name(id=\'Callable\', ctx=Load()), slice=Tuple(elts=[Constant(value=Ellipsis), Subscript(value=Attribute(value=Name(id=\'subprocess\', ctx=Load()), attr=\'CompletedProcess\', ctx=Load()), slice=Name(id=\'Any\', ctx=Load()), ctx=Load())], ctx=Load()), ctx=Load())), arg(arg=\'proc_root\', annotation=BinOp(left=Name(id=\'Path\', ctx=Load()), op=BitOr(), right=Constant(value=None))), arg(arg=\'signaller\', annotation=Subscript(value=Name(id=\'Callable\', ctx=Load()), slice=Tuple(elts=[List(elts=[Name(id=\'int\', ctx=Load()), Name(id=\'int\', ctx=Load())], ctx=Load()), Constant(value=None)], ctx=Load()), ctx=Load())), arg(arg=\'print_func\', annotation=Subscript(value=Name(id=\'Callable\', ctx=Load()), slice=Tuple(elts=[List(elts=[Name(id=\'str\', ctx=Load())], ctx=Load()), Constant(value=None)], ctx=Load()), ctx=Load()))], kw_defaults=[None, Constant(value=\'\'), Attribute(value=Name(id=\'subprocess\', ctx=Load()), attr=\'run\', ctx=Load()), Constant(value=None), Attribute(value=Name(id=\'os\', ctx=Load()), attr=\'kill\', ctx=Load()), Name(id=\'print\', ctx=Load())]), body=[Expr(value=Constant(value="Suspend one tenant: reversible, project-scoped, and honest about failure.\\n\\n    The order is the dependency order read backwards, because each step\'s\\n    consumer has to go first: the window before the sessions it frames, the\\n    sessions before the listener that wakes them, the listener before the board\\n    it reads. Nothing here removes state -- no database, no worktree, no\\n    registration, no journal -- which is the whole difference between this and\\n    `teardown`.\\n\\n    Every step runs even if an earlier one failed. A window that would not close\\n    is no reason to leave a board serving, and reporting the first failure while\\n    silently skipping the rest is how a tenant ends up half suspended with one\\n    line of output about it.\\n    ")), AnnAssign(target=Name(id=\'problems\', ctx=Store()), annotation=Subscript(value=Name(id=\'list\', ctx=Load()), slice=Name(id=\'str\', ctx=Load()), ctx=Load()), value=List(ctx=Load()), simple=1), Expr(value=Call(func=Attribute(value=Name(id=\'problems\', ctx=Load()), attr=\'extend\', ctx=Load()), args=[Call(func=Name(id=\'close_presentation_window\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())], keywords=[keyword(arg=\'config_path\', value=Name(id=\'config_path\', ctx=Load())), keyword(arg=\'gui_user\', value=Name(id=\'gui_user\', ctx=Load())), keyword(arg=\'proc_root\', value=Name(id=\'proc_root\', ctx=Load())), keyword(arg=\'signaller\', value=Name(id=\'signaller\', ctx=Load())), keyword(arg=\'print_func\', value=Name(id=\'print_func\', ctx=Load()))])])), If(test=Compare(left=Call(func=Name(id=\'stop_project\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())], keywords=[keyword(arg=\'runner\', value=Name(id=\'runner\', ctx=Load())), keyword(arg=\'print_func\', value=Name(id=\'print_func\', ctx=Load()))]), ops=[NotEq()], comparators=[Constant(value=0)]), body=[Expr(value=Call(func=Attribute(value=Name(id=\'problems\', ctx=Load()), attr=\'append\', ctx=Load()), args=[JoinedStr(values=[Constant(value=\'not every \'), FormattedValue(value=Attribute(value=Name(id=\'config\', ctx=Load()), attr=\'project\', ctx=Load()), conversion=-1), Constant(value=\' session could be stopped\')])]))]), Expr(value=Call(func=Attribute(value=Name(id=\'problems\', ctx=Load()), attr=\'extend\', ctx=Load()), args=[Call(func=Name(id=\'contain_residual_project_processes\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())], keywords=[keyword(arg=\'proc_root\', value=Name(id=\'proc_root\', ctx=Load())), keyword(arg=\'signaller\', value=Name(id=\'signaller\', ctx=Load())), keyword(arg=\'print_func\', value=Name(id=\'print_func\', ctx=Load()))])])), Assign(targets=[Name(id=\'listener_problems\', ctx=Store())], value=Call(func=Name(id=\'stop_owner_listener\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())], keywords=[keyword(arg=\'runner\', value=Name(id=\'runner\', ctx=Load())), keyword(arg=\'config_path\', value=Name(id=\'config_path\', ctx=Load()))])), Expr(value=Call(func=Attribute(value=Name(id=\'problems\', ctx=Load()), attr=\'extend\', ctx=Load()), args=[Name(id=\'listener_problems\', ctx=Load())])), If(test=UnaryOp(op=Not(), operand=Name(id=\'listener_problems\', ctx=Load())), body=[Expr(value=Call(func=Name(id=\'print_func\', ctx=Load()), args=[JoinedStr(values=[Constant(value=\'stopped listener: \'), FormattedValue(value=Call(func=Name(id=\'_listener_user_unit\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())]), conversion=-1)])]))]), Assign(targets=[Name(id=\'board_problems\', ctx=Store())], value=Call(func=Name(id=\'_board_system_unit_action\', ctx=Load()), args=[Name(id=\'config\', ctx=Load()), Constant(value=\'stop\')], keywords=[keyword(arg=\'runner\', value=Name(id=\'runner\', ctx=Load()))])), Expr(value=Call(func=Attribute(value=Name(id=\'problems\', ctx=Load()), attr=\'extend\', ctx=Load()), args=[Name(id=\'board_problems\', ctx=Load())])), If(test=UnaryOp(op=Not(), operand=Name(id=\'board_problems\', ctx=Load())), body=[Expr(value=Call(func=Name(id=\'print_func\', ctx=Load()), args=[JoinedStr(values=[Constant(value=\'stopped board: \'), FormattedValue(value=Call(func=Name(id=\'_board_system_unit\', ctx=Load()), args=[Name(id=\'config\', ctx=Load())]), conversion=-1)])]))]), Return(value=Name(id=\'problems\', ctx=Load()))], returns=Subscript(value=Name(id=\'list\', ctx=Load()), slice=Name(id=\'str\', ctx=Load()), ctx=Load()))'
SESSIONS = {"none": (), "one": ("main",), "several": ("main", "audit", "ops")}
RESIDUAL = {"none": (), "some": (4242, 4343)}
UNKNOWN = {"none": (), "one": ("the listener did not answer",), "several": ("the board did not answer", "no window probe")}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_runtime as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.tenant_runtime", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_runtime")):
        result = python("import importlib, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_runtime as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), m.dataclass is dataclasses.dataclass, "
                        "m.TenantRuntime.__module__ == 'scripts.tenant_runtime')")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_record_and_the_vocabulary_are_the_baselines() -> None:
    check(list(m.TENANT_RUNTIME_STATES) == GOLDEN["states"] and isinstance(m.TENANT_RUNTIME_STATES, tuple), f"the five states: {m.TENANT_RUNTIME_STATES}")
    fields = [[f.name, "MISSING" if f.default is dataclasses.MISSING else repr(f.default)] for f in dataclasses.fields(m.TenantRuntime)]
    check(fields == GOLDEN["fields"] and m.TenantRuntime.__dataclass_params__.frozen is GOLDEN["frozen"] is True
          and sorted(n for n, v in vars(m.TenantRuntime).items() if isinstance(v, property)) == GOLDEN["properties"],
          f"frozen, the same fields, order and defaults, one property: {fields}")
    d = m.TenantRuntime(project="atlas", board_active=False, listener_active=False, live_sessions=(), presentation_open=False)
    check([list(d.residual), list(d.unknown), d.state, m.describe_tenant_runtime(d)] == GOLDEN["defaults"], "the defaults")
    try:
        d.board_active = True  # type: ignore[misc]
        mutation = "allowed"
    except dataclasses.FrozenInstanceError as exc:
        mutation = type(exc).__name__
    check(mutation == GOLDEN["mutation"], f"and it cannot be changed: {mutation}")


def test_the_module_reads_nothing_of_the_launcher() -> None:
    tree = ast.parse((ROOT / "scripts" / "tenant_runtime.py").read_text(encoding="utf-8"))
    check(not [x for x in ast.walk(tree) if isinstance(x, ast.Name) and x.id == "launcher"]
          and not [x for x in ast.walk(tree) if isinstance(x, (ast.Import, ast.ImportFrom)) and "team_launcher" in ast.unparse(x)],
          "no launcher read and no launcher import, at the top or inside")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from dataclasses import dataclass"], f"only dataclass at the top: {top}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(order == list(MOVED), f"the three in the launcher's order, and nothing else: {order}")
    record = next(n for n in tree.body if getattr(n, "name", None) == "TenantRuntime")
    check([ast.unparse(d) for d in record.decorator_list] == ["dataclass(frozen=True)"], "frozen by the decorator bound when it is defined")


def launcher_definition(tree: ast.Module, name: str) -> ast.FunctionDef:
    """The launcher's `name`: its own, or -- once a later slice moves it on (SYRD-405) -- the definition its
    unaliased re-export names, with the call-time `launcher` import dropped and `launcher.X` read as the global `X`."""
    node = next((n for n in tree.body if getattr(n, "name", None) == name), None)
    if node is not None:
        return node
    source = next(n.module for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                  and any(a.name == name and a.asname is None for a in n.names))
    node = next(n for n in ast.parse((ROOT / f"{source.replace('.', '/')}.py").read_text(encoding="utf-8")).body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    node.body = [s for s in node.body if ast.unparse(s) != "from scripts import team_launcher as launcher"]

    class AsGlobal(ast.NodeTransformer):
        def visit_Attribute(self, x: ast.Attribute) -> ast.AST:
            self.generic_visit(x)
            return ast.Name(id=x.attr, ctx=x.ctx) if isinstance(x.value, ast.Name) and x.value.id == "launcher" else x

    return AsGlobal().visit(node)


def test_the_launcher_reexports_the_three_and_keeps_its_neighbours() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_runtime"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_plan_data_from_config", "suspend_tenant"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours, its own or re-exported")
    suspend = launcher_definition(tree, "suspend_tenant")
    check(ast.dump(suspend, include_attributes=False) == SUSPEND_TENANT, "suspend_tenant is the baseline's, node for node")


def test_every_state_and_description_is_the_baselines() -> None:
    got = []
    for board, listener, sessions, window, residual, unknown in itertools.product((True, False), (True, False), SESSIONS, (True, False), RESIDUAL, UNKNOWN):
        r = m.TenantRuntime(project="atlas", board_active=board, listener_active=listener, live_sessions=SESSIONS[sessions],
                            presentation_open=window, residual=RESIDUAL[residual], unknown=UNKNOWN[unknown])
        got.append([board, listener, sessions, window, residual, unknown, r.state, m.describe_tenant_runtime(r)])
    check(len(got) == len(GOLDEN["combinations"]), f"every combination measured: {len(got)}")
    for row, want in zip(got, GOLDEN["combinations"]):
        check(row == want, f"{want[:6]}: the baseline's state and line: {row[6:]} != {want[6:]}")
    check({row[6] for row in got} == set(GOLDEN["states"]) - {"unregistered"}, "every state but unregistered is reachable from the record")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in names:
        globals()[name]()
    print(f"tenant_runtime_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
