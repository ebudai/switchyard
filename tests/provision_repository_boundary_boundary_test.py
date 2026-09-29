#!/usr/bin/env python3
"""SYRD-472: the repository-boundary phase reader, against project_provision it came out of.

The ten -- the fence (`REPOSITORY_BOUNDARY_BEGIN`, `REPOSITORY_BOUNDARY_END`),
what a line inside it may be (`REPOSITORY_BOUNDARY_ALLOWED`,
`REPOSITORY_BOUNDARY_GUARDS` with `_QUOTED`, `_SHELL_METACHARACTERS`,
`_outside_quotes`, `_is_boundary_line`), and the two readers the repair uses
(`repository_boundary_phase`, `repository_boundary_statements`) -- moved
unchanged into `scripts/ticket_board/provision_repository_boundary.py`;
`project_provision` re-exports them all, in both branches of its import block,
and keeps `WRITABLE_REPOSITORY_COPY_MODE`, the repository group and the
plan-path helpers. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package; its guards are built from its own `_QUOTED`.
- **Seams (rule 24):** what they read of `project_provision` -- each other and
  the fence, prefixes, guards and metacharacters -- is read through it when
  they run, with the direct-script fallback; `repository_boundary_repair`
  imports the phase and statements from `project_provision` when it runs, so a
  patch there is what it gets. Callers are counted across the re-exported
  modules.
- **The behaviour is the baseline's:** every kind of line the checker must
  accept or refuse, the statements of flat, guarded, nested and unclosed
  phases, every packet shape, and the phase of the operator script
  `project_provision` renders. `GOLDEN` below was produced by running the
  BASELINE module's own definitions over the very cases embedded here
  (`gold472.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER and COLUMNS, under umask 077, under several
  hash seeds and with another TMPDIR, locale and board Python in the
  environment.
- **The direct script answers what the package answers** and looks at no host
  path (a recorder with a positive control), and the default, shaped, lean and
  roles packets are byte-identical through both, each carrying a phase the
  reader lifts.

No real home, tenant, account, /etc, /var or /opt path is read or written:
lines and packets are synthetic, the operator script is rendered for a
synthetic plan with the board Python pinned, and nothing is run. Spawns, every
exec, signals, account and group lookups and socket connections are refused for
each case.
"""

from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_repository_boundary as m  # noqa: E402

CHECKS = 0
MOVED = ('REPOSITORY_BOUNDARY_BEGIN', 'REPOSITORY_BOUNDARY_END', 'REPOSITORY_BOUNDARY_ALLOWED', '_QUOTED', 'REPOSITORY_BOUNDARY_GUARDS', '_SHELL_METACHARACTERS', '_outside_quotes', '_is_boundary_line', 'repository_boundary_statements', 'repository_boundary_phase')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    '_outside_quotes': {'_SHELL_METACHARACTERS': 1},
    '_is_boundary_line': {'REPOSITORY_BOUNDARY_ALLOWED': 1, 'REPOSITORY_BOUNDARY_GUARDS': 1, '_SHELL_METACHARACTERS': 1, '_outside_quotes': 1},
    'repository_boundary_phase': {'REPOSITORY_BOUNDARY_BEGIN': 1, 'REPOSITORY_BOUNDARY_END': 1, '_is_boundary_line': 1},
}
#: Measured on the baseline: every project_provision definition outside the ten that names them, and how often.
DISPATCH = {'render_operator_commands': {'REPOSITORY_BOUNDARY_BEGIN': 1, 'REPOSITORY_BOUNDARY_END': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/repository_boundary_repair.py': {'import repository_boundary_phase': 1, 'import repository_boundary_statements': 1}}
#: The BASELINE's own behaviour for the cases below (`gold472.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    "outside quotes: 'sudo install -d /p472'": {'result': {'type': 'str', 'value': 'sudo install -d /p472'}, 'calls': {}},
    'outside quotes: "sudo setfacl -m \'g:x:rwx\' \'/p472;x\'"': {'result': {'type': 'str', 'value': 'sudo setfacl -m  '}, 'calls': {}},
    'outside quotes: "echo \'a\' ; rm"': {'result': {'type': 'str', 'value': 'echo  ; rm'}, 'calls': {}},
    'outside quotes: \'say "it\\\'s" > out\'': {'result': {'type': 'str', 'value': 'say  > out'}, 'calls': {}},
    'outside quotes: "unbalanced \'quote"': {'result': {'type': 'str', 'value': ';&|`$<>()\n'}, 'calls': {}},
    'outside quotes: \'a "b\\\'c" d\'': {'result': {'type': 'str', 'value': 'a  d'}, 'calls': {}},
    "outside quotes: 'x `y`'": {'result': {'type': 'str', 'value': 'x `y`'}, 'calls': {}},
    "outside quotes: '$(z)'": {'result': {'type': 'str', 'value': '$(z)'}, 'calls': {}},
    'outside quotes: the metacharacters rebound on project_provision': {'result': {'type': 'str', 'value': '#'}, 'calls': {}},
    "boundary line: 'fi'": {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    "boundary line: '  fi  '": {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    "boundary line: '# a comment'": {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    "boundary line: '#no space'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'boundary line: "if getent group \'p472\' >/dev/null 2>&1; then"': {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    'boundary line: "if ! getent group \'p472-repo\' >/dev/null 2>&1; then"': {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    'boundary line: "if [ -d \'/p472/repo\' ]; then"': {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    "boundary line: 'if [ -d /p472/repo ]; then'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    "boundary line: 'if getent group p472 >/dev/null 2>&1; then'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'boundary line: "sudo install -d -m 0750 \'/p472/repo\'"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo setfacl -R -m \'g:p472:rwX\' \'/p472/repo\'"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo find \'/p472/repo\' -type d -exec true {} +"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo groupadd -r \'p472-repo\'"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo gpasswd -a \'p472\' \'p472-repo\' >/dev/null"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo gpasswd -a \'p472\' \'p472-repo\' >/dev/null 2>&1"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo gpasswd -a \'p472\' \'p472-repo\' > /tmp/x"': {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo install -d \'/p472\'; rm -rf /"': {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo install -d \'/p472\' && reboot"': {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes': 1}},
    "boundary line: 'sudo install -d $(evil)'": {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes': 1}},
    'boundary line: "sudo install -d \'/p472;ok\'"': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    "boundary line: 'sudo systemctl restart x'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'boundary line: "sudo install -d \'unbalanced"': {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes': 1}},
    "boundary line: 'sudo chown x /p472'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    "boundary line: 'then'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    "boundary line: 'else'": {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'boundary line: the allowed prefixes rebound on project_provision': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: the guards rebound on project_provision': {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    'boundary line: a guard is matched from the start of the line': {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'boundary line: the metacharacters rebound on project_provision': {'result': {'type': 'bool', 'value': True}, 'calls': {'_outside_quotes': 1}},
    'boundary line: the quoting rebound on project_provision': {'result': {'type': 'bool', 'value': False}, 'calls': {'_outside_quotes rebound': 1}},
    'statements: flat': {'result': {'type': 'list', 'value': [["sudo install -d '/a'"], ["sudo groupadd -r 'g'"]]}, 'calls': {}},
    'statements: one guard': {'result': {'type': 'list', 'value': [["if ! getent group 'g' >/dev/null 2>&1; then", "    sudo groupadd -r 'g'", 'fi'], ["sudo install -d '/a'"]]}, 'calls': {}},
    'statements: nested': {'result': {'type': 'list', 'value': [["if [ -d '/a' ]; then", "if [ -d '/b' ]; then", "sudo find '/b' -x", 'fi', "sudo find '/a'", 'fi'], ["sudo install -d '/c'"]]}, 'calls': {}},
    'statements: unclosed': {'result': {'type': 'list', 'value': [["if [ -d '/a' ]; then", "sudo find '/a'"]]}, 'calls': {}},
    'statements: stray fi': {'result': {'type': 'list', 'value': [['fi'], ["sudo install -d '/a'"]]}, 'calls': {}},
    'statements: empty': {'result': {'type': 'list', 'value': []}, 'calls': {}},
    'phase: no fence': {'result': {'type': 'tuple', 'value': [[], 'this packet has no repository boundary phase in it, so it was generated before that phase existed']}, 'calls': {}},
    'phase: a clean phase': {'result': {'type': 'tuple', 'value': [["sudo install -d '/a'", "if [ -d '/a' ]; then", "sudo find '/a'", 'fi'], '']}, 'calls': {'_is_boundary_line': 4, '_outside_quotes': 2}},
    'phase: a deploy inside the fence': {'result': {'type': 'tuple', 'value': [[], "the repository boundary phase of this packet contains a line that is not part of one: 'sudo systemctl restart board'"]}, 'calls': {'_is_boundary_line': 2, '_outside_quotes': 1}},
    'phase: a second command riding along': {'result': {'type': 'tuple', 'value': [[], 'the repository boundary phase of this packet contains a line that is not part of one: "sudo install -d \'/a\'; curl evil"']}, 'calls': {'_is_boundary_line': 1, '_outside_quotes': 1}},
    'phase: the end before the begin': {'result': {'type': 'tuple', 'value': [[], 'this packet has no repository boundary phase in it, so it was generated before that phase existed']}, 'calls': {}},
    'phase: only a begin': {'result': {'type': 'tuple', 'value': [[], 'this packet has no repository boundary phase in it, so it was generated before that phase existed']}, 'calls': {}},
    'phase: an empty fence': {'result': {'type': 'tuple', 'value': [[], '']}, 'calls': {}},
    'phase: two fences': {'result': {'type': 'tuple', 'value': [["sudo install -d '/a'"], '']}, 'calls': {'_is_boundary_line': 1, '_outside_quotes': 1}},
    'phase: other markers': {'result': {'type': 'tuple', 'value': [[], 'this packet has no repository boundary phase in it, so it was generated before that phase existed']}, 'calls': {}},
    'phase: the markers rebound on project_provision': {'result': {'type': 'tuple', 'value': [["sudo install -d '/a'"], '']}, 'calls': {'_is_boundary_line': 1, '_outside_quotes': 1}},
    'phase: the line check rebound on project_provision': {'result': {'type': 'tuple', 'value': [["sudo install -d '/a'", 'sudo systemctl restart board'], '']}, 'calls': {'_is_boundary_line rebound': 2}},
    "phase: the operator script's own": {'result': {'type': 'tuple', 'value': [["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local'", "sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state'", "sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard'", "sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects'", "sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects/p472'", "sudo install -d -m 0770 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects/p472/control.git'", "sudo setfacl -m u:boardsvc:--x '/p472/home'", "sudo setfacl -m u:boardsvc:--x '/p472/home/.local'", "sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state'", "sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard'", "sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard/projects'", "sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard/projects/p472'", "sudo setfacl -R -m u:boardsvc:rX '/p472/home/.local/state/switchyard/projects/p472/control.git'", "sudo setfacl -R -m d:u:boardsvc:rX '/p472/home/.local/state/switchyard/projects/p472/control.git'", "sudo install -d -m 0750 -o 'p472-owner' -g 'p472-owner' '/p472/home/p472-worktrees'", "if [ -d '/p472/home/p472-worktrees' ]; then", "    sudo find '/p472/home/p472-worktrees' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +", "    sudo setfacl -m 'd:u::rwx,d:g::r-x,d:o::---' '/p472/home/p472-worktrees'", 'fi', "if getent group 'p472-roles' >/dev/null 2>&1; then", "    sudo setfacl -x d:g:p472-roles '/p472/home/p472-worktrees'", "    sudo setfacl -x g:p472-roles '/p472/home/p472-worktrees'", "    sudo setfacl -R -x d:g:p472-roles '/p472/home/.local/state/switchyard/projects/p472/control.git'", "    sudo setfacl -R -x g:p472-roles '/p472/home/.local/state/switchyard/projects/p472/control.git'", 'fi'], '']}, 'calls': {'_is_boundary_line': 25, '_outside_quotes': 21}},
    "statements: the operator script's own phase": {'result': {'type': 'list', 'value': [["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local'"], ["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state'"], ["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard'"], ["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects'"], ["sudo install -d -m 0755 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects/p472'"], ["sudo install -d -m 0770 -o 'p472-owner' -g 'p472-owner' '/p472/home/.local/state/switchyard/projects/p472/control.git'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home/.local'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard/projects'"], ["sudo setfacl -m u:boardsvc:--x '/p472/home/.local/state/switchyard/projects/p472'"], ["sudo setfacl -R -m u:boardsvc:rX '/p472/home/.local/state/switchyard/projects/p472/control.git'"], ["sudo setfacl -R -m d:u:boardsvc:rX '/p472/home/.local/state/switchyard/projects/p472/control.git'"], ["sudo install -d -m 0750 -o 'p472-owner' -g 'p472-owner' '/p472/home/p472-worktrees'"], ["if [ -d '/p472/home/p472-worktrees' ]; then", "    sudo find '/p472/home/p472-worktrees' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +", "    sudo setfacl -m 'd:u::rwx,d:g::r-x,d:o::---' '/p472/home/p472-worktrees'", 'fi'], ["if getent group 'p472-roles' >/dev/null 2>&1; then", "    sudo setfacl -x d:g:p472-roles '/p472/home/p472-worktrees'", "    sudo setfacl -x g:p472-roles '/p472/home/p472-worktrees'", "    sudo setfacl -R -x d:g:p472-roles '/p472/home/.local/state/switchyard/projects/p472/control.git'", "    sudo setfacl -R -x g:p472-roles '/p472/home/.local/state/switchyard/projects/p472/control.git'", 'fi']]}, 'calls': {}},
    'constants': {'result': {'type': 'list', 'value': ['# >>> switchyard repository boundary', '# <<< switchyard repository boundary', ['sudo install -d ', 'sudo setfacl ', 'sudo find ', 'sudo groupadd ', 'sudo gpasswd '], ['^if (?:! )?getent group (?:\'[^\']*\'|\\"\'\\")+ >/dev/null 2>&1; then$', '^if \\[ -d (?:\'[^\']*\'|\\"\'\\")+ \\]; then$'], ';&|`$<>()\n']}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'roles': 10}

# --- the cases, shared verbatim with `gold472.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the four and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Lines and packets are synthetic; one case takes the
# phase of the operator script `project_provision` renders for a synthetic plan, with TICKET_BOARD_PYTHON pinned so no
# host path is checked. A case may rebind a `project_provision` name to show it is read there when the function runs.
LINES = ["fi", "  fi  ", "# a comment", "#no space", "if getent group 'p472' >/dev/null 2>&1; then", "if ! getent group 'p472-repo' >/dev/null 2>&1; then",
         "if [ -d '/p472/repo' ]; then", "if [ -d /p472/repo ]; then", "if getent group p472 >/dev/null 2>&1; then",
         "sudo install -d -m 0750 '/p472/repo'", "sudo setfacl -R -m 'g:p472:rwX' '/p472/repo'", "sudo find '/p472/repo' -type d -exec true {} +",
         "sudo groupadd -r 'p472-repo'", "sudo gpasswd -a 'p472' 'p472-repo' >/dev/null", "sudo gpasswd -a 'p472' 'p472-repo' >/dev/null 2>&1",
         "sudo gpasswd -a 'p472' 'p472-repo' > /tmp/x", "sudo install -d '/p472'; rm -rf /", "sudo install -d '/p472' && reboot", "sudo install -d $(evil)",
         "sudo install -d '/p472;ok'", "sudo systemctl restart x", "sudo install -d 'unbalanced", "sudo chown x /p472", "then", "else"]
QUOTING = ["sudo install -d /p472", "sudo setfacl -m 'g:x:rwx' '/p472;x'", "echo 'a' ; rm", "say \"it's\" > out", "unbalanced 'quote", 'a "b\'c" d', "x `y`", "$(z)"]
PHASES = {
    "flat": ["sudo install -d '/a'", "sudo groupadd -r 'g'"],
    "one guard": ["if ! getent group 'g' >/dev/null 2>&1; then", "    sudo groupadd -r 'g'", "fi", "sudo install -d '/a'"],
    "nested": ["if [ -d '/a' ]; then", "if [ -d '/b' ]; then", "sudo find '/b' -x", "fi", "sudo find '/a'", "fi", "sudo install -d '/c'"],
    "unclosed": ["if [ -d '/a' ]; then", "sudo find '/a'"],
    "stray fi": ["fi", "sudo install -d '/a'"],
    "empty": [],
}
B, E = "# >>> switchyard repository boundary", "# <<< switchyard repository boundary"
PACKETS = {
    "no fence": "#!/bin/sh\nsudo install -d '/a'\n",
    "a clean phase": f"#!/bin/sh\necho before\n{B}\nsudo install -d '/a'\n\nif [ -d '/a' ]; then\nsudo find '/a'\nfi\n{E}\necho after\n",
    "a deploy inside the fence": f"{B}\nsudo install -d '/a'\nsudo systemctl restart board\n{E}\n",
    "a second command riding along": f"{B}\nsudo install -d '/a'; curl evil\n{E}\n",
    "the end before the begin": f"{E}\nsudo install -d '/a'\n{B}\n",
    "only a begin": f"{B}\nsudo install -d '/a'\n",
    "an empty fence": f"{B}\n{E}\n",
    "two fences": f"{B}\nsudo install -d '/a'\n{E}\n{B}\nsudo reboot\n{E}\n",
    "other markers": "# >>> p472\nsudo install -d '/a'\n# <<< p472\n",
}
CASES = {
    **{f"outside quotes: {line!r}": {"call": "_outside_quotes", "args": [line]} for line in QUOTING},
    "outside quotes: the metacharacters rebound on project_provision": {"call": "_outside_quotes", "args": ["unbalanced 'quote"], "rebind": {"_SHELL_METACHARACTERS": "#"}},
    **{f"boundary line: {line!r}": {"call": "_is_boundary_line", "args": [line]} for line in LINES},
    "boundary line: the allowed prefixes rebound on project_provision": {"call": "_is_boundary_line", "args": ["sudo systemctl restart x"],
                                                                        "rebind": {"REPOSITORY_BOUNDARY_ALLOWED": ("sudo systemctl ",)}},
    "boundary line: the guards rebound on project_provision": {"call": "_is_boundary_line", "args": ["if true; then"], "rebind": {"REPOSITORY_BOUNDARY_GUARDS": "true"}},
    "boundary line: a guard is matched from the start of the line": {"call": "_is_boundary_line", "args": ["if true; then"], "rebind": {"REPOSITORY_BOUNDARY_GUARDS": "unanchored"}},
    "boundary line: the metacharacters rebound on project_provision": {"call": "_is_boundary_line", "args": ["sudo install -d '/p472' && reboot"],
                                                                       "rebind": {"_SHELL_METACHARACTERS": "#"}},
    "boundary line: the quoting rebound on project_provision": {"call": "_is_boundary_line", "args": ["sudo install -d '/p472'"], "rebind": {"_outside_quotes": True}},
    **{f"statements: {label}": {"call": "repository_boundary_statements", "args": [phase]} for label, phase in PHASES.items()},
    **{f"phase: {label}": {"call": "repository_boundary_phase", "args": [packet]} for label, packet in PACKETS.items()},
    "phase: the markers rebound on project_provision": {"call": "repository_boundary_phase", "args": [PACKETS["other markers"]],
                                                        "rebind": {"REPOSITORY_BOUNDARY_BEGIN": "# >>> p472", "REPOSITORY_BOUNDARY_END": "# <<< p472"}},
    "phase: the line check rebound on project_provision": {"call": "repository_boundary_phase", "args": [PACKETS["a deploy inside the fence"]], "rebind": {"_is_boundary_line": True}},
    "phase: the operator script's own": {"call": "repository_boundary_phase", "operator": True},
    "statements: the operator script's own phase": {"call": "repository_boundary_statements", "operator": True},
    "constants": {"call": None},
}
FUNCTIONS = ("_outside_quotes", "_is_boundary_line", "repository_boundary_statements", "repository_boundary_phase")
PASSED = ("_outside_quotes", "_is_boundary_line")
REBINDABLE = ("REPOSITORY_BOUNDARY_BEGIN", "REPOSITORY_BOUNDARY_END", "REPOSITORY_BOUNDARY_ALLOWED", "REPOSITORY_BOUNDARY_GUARDS", "_SHELL_METACHARACTERS")


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import hashlib, os as _os, pathlib as _pl, re as _re
    counts = {}

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, str):
            return value if len(value) <= 400 else f"TEXT {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    ENV = ("TICKET_BOARD_PYTHON", "SWITCHYARD_SHARED_PYTHON")
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "build_plan", "render_operator_commands")}
    saved_env = {n: _os.environ.get(n) for n in ENV}
    try:
        for n in ENV:
            _os.environ.pop(n, None)
        _os.environ["TICKET_BOARD_PYTHON"] = "/p472/python"
        args = list(spec.get("args", []))
        if spec.get("operator"):
            plan = saved["build_plan"](project="p472", owner_user="p472-owner", owner_home=_pl.Path("/p472/home"), port=34472, source_repo=_pl.Path("/p472/source"))
            phase, reason = saved["repository_boundary_phase"](saved["render_operator_commands"](plan))
            args = [saved["render_operator_commands"](plan)] if spec["call"] == "repository_boundary_phase" else [phase]
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "_outside_quotes":
                t._outside_quotes = lambda line: note("_outside_quotes rebound") or ";"
            elif name == "_is_boundary_line":
                t._is_boundary_line = lambda line: note("_is_boundary_line rebound") or True
            elif name == "REPOSITORY_BOUNDARY_GUARDS":
                t.REPOSITORY_BOUNDARY_GUARDS = (_re.compile(r"^if true; then$"),) if value == "true" else (_re.compile(r"true; then"),)
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = [saved["REPOSITORY_BOUNDARY_BEGIN"], saved["REPOSITORY_BOUNDARY_END"], list(saved["REPOSITORY_BOUNDARY_ALLOWED"]),
                   [r.pattern for r in saved["REPOSITORY_BOUNDARY_GUARDS"]], saved["_SHELL_METACHARACTERS"]]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            try:
                got = fn(*args)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        for n, v in saved_env.items():
            _os.environ.pop(n, None)
            if v is not None:
                _os.environ[n] = v
# ----------------------------------------------------------------------------------------------------------------------


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


EXECS = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")


class contained:
    """Spawns, every exec, signals, connections and real account/group lookups refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), system=refuse("os.system"), **{name: refuse(f"os.{name}") for name in EXECS}),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(grp, getgrgid=refuse("grp.getgrgid"), getgrnam=refuse("grp.getgrnam")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(holder: object, spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, holder, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_an_exec_a_lookup_and_a_connection() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.system("true"),
                lambda: pwd.getpwuid(0), lambda: grp.getgrgid(0), lambda: socket.socket().connect(("127.0.0.1", 9)),
                *(lambda name=name: getattr(os, name)("true", ["true"]) for name in EXECS)]
    for attempt in attempts:
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, every exec, a signal, an account or group lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


#: What the four read on project_provision when they run: only each other and the slice's own constants -- nothing it keeps.
KEPT = ()
#: The six constants, as the baseline wrote them: (annotation, value). Measured on the baseline, not typed.
CONSTANT_TEXT = {'REPOSITORY_BOUNDARY_BEGIN': (None, "'# >>> switchyard repository boundary'"), 'REPOSITORY_BOUNDARY_END': (None, "'# <<< switchyard repository boundary'"), 'REPOSITORY_BOUNDARY_ALLOWED': (None, "('sudo install -d ', 'sudo setfacl ', 'sudo find ', 'sudo groupadd ', 'sudo gpasswd ')"), '_QUOTED': (None, '\'(?:\\\'[^\\\']*\\\'|\\\\"\\\'\\\\")+\''), 'REPOSITORY_BOUNDARY_GUARDS': (None, "(re.compile(f'^if (?:! )?getent group {_QUOTED} >/dev/null 2>&1; then$'), re.compile(f'^if \\\\[ -d {_QUOTED} \\\\]; then$'))"), '_SHELL_METACHARACTERS': (None, "';&|`$<>()\\n'")}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them: none.
DEFAULTS = {name: [] for name in ("_outside_quotes", "_is_boundary_line", "repository_boundary_statements", "repository_boundary_phase")}
#: The repair imports the phase and statements from project_provision inside a function, when it runs; its boundary test patches them there.
CALL_TIME_READERS = {"scripts/repository_boundary_repair.py": ("repository_boundary_phase", "repository_boundary_statements")}
#: The packets rendered through the direct script and the package, the board Python pinned; each carries the boundary fence.
ROLE_ACCOUNTS = (("director", "p472-director"), ("main", "p472-main"))
PACKET_VARIANTS = {
    "default": ([], False),
    "shaped": (["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"], False),
    "lean": (["--no-include-designer", "--no-include-audit"], False),
    "roles": ([], True),
}
#: Refuses and records any stat/lstat/open/access under the host's switchyard, /etc, /var and /opt paths, after proving it
#: catches one (a positive control).
HOST_RECORDER = (
    "import builtins, os, pathlib\n"
    "PREFIXES = ('/usr/local/lib/switchyard', '/etc', '/var', '/opt')\n"
    "hits = []\n"
    "def guarded(real, name):\n"
    "    def f(p, *a, **k):\n"
    "        s = os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else ''\n"
    "        if s.startswith(PREFIXES):\n"
    "            hits.append((name, s)); raise FileNotFoundError(2, 'refused', s)\n"
    "        return real(p, *a, **k)\n"
    "    return f\n"
    "os.stat, os.lstat, builtins.open, os.access = guarded(os.stat, 'stat'), guarded(os.lstat, 'lstat'), guarded(builtins.open, 'open'), guarded(os.access, 'access')\n"
    "pathlib.Path('/opt/switchyard/syrd472-positive-control').is_file()\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in the package and as the direct script: the checkers on a few lines, statements, a packet's phase, and
#: the phase of the operator script project_provision renders for a synthetic plan -- as a digest, and the host paths looked at.
BOUNDARY_PROBE = (
    "import hashlib, json, os, pathlib\n"
    "os.environ['TICKET_BOARD_PYTHON'] = '/p472/python'; os.environ.pop('SWITCHYARD_SHARED_PYTHON', None)\n"
    "b, e = g['REPOSITORY_BOUNDARY_BEGIN'], g['REPOSITORY_BOUNDARY_END']\n"
    "plan = g['build_plan'](project='p472', owner_user='p472-owner', owner_home=pathlib.Path('/p472/home'), port=34472, source_repo=pathlib.Path('/p472/source'))\n"
    "own = g['repository_boundary_phase'](g['render_operator_commands'](plan))\n"
    "out = [g['_outside_quotes'](\"a 'b;c' d\"), g['_is_boundary_line'](\"sudo install -d '/a'\"), g['_is_boundary_line']('sudo reboot'),\n"
    "       g['repository_boundary_statements'](['if [ -d \\'/a\\' ]; then', 'sudo find \\'/a\\'', 'fi', 'sudo install -d \\'/b\\'']),\n"
    "       g['repository_boundary_phase'](b + '\\nsudo install -d \\'/a\\'\\n' + e + '\\n'), g['repository_boundary_phase']('no fence'),\n"
    "       own, g['repository_boundary_statements'](own[0]), len(own[0]) > 0 and own[1] == '']\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
)


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        return n.target.id
    return None


def annotation_ids(tree: ast.AST) -> set[int]:
    """Every node inside an annotation: names there are not read when the code runs."""
    out: set[int] = set()
    for x in ast.walk(tree):
        parts = []
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [x.returns, *(a.annotation for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs + [v for v in (x.args.vararg, x.args.kwarg) if v])]
        elif isinstance(x, ast.AnnAssign):
            parts = [x.annotation]
        for part in parts:
            if part is not None:
                out |= {id(y) for y in ast.walk(part)}
    return out


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.ticket_board.provision_repository_boundary as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_repository_boundary", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_repository_boundary"),
                  ("scripts.repository_boundary_repair", "scripts.ticket_board.provision_repository_boundary")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_repository_boundary as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        "m.REPOSITORY_BOUNDARY_GUARDS[0].pattern.count(m._QUOTED) == 1, "
                        "not any(hasattr(m, n) for n in ('provision', 'project_provision', 'WRITABLE_REPOSITORY_COPY_MODE', 'repository_group_commands')))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_repository_boundary'] True True",
              f"{' then '.join(order)}: one object each, defined here; the guards built from the module's own _QUOTED; nothing of project_provision bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.re is re and t.re is m.re, "the standard-library name is the module's own, the very object project_provision holds")


def test_a_patch_on_project_provision_reaches_the_repair() -> None:
    # repository_boundary_repair imports the phase and statements from project_provision inside a function, when it runs:
    # a patch there (as its boundary test makes) is what it gets, and the module's own objects otherwise.
    for path, names in CALL_TIME_READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        imports = [x for x in ast.walk(source) if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision") and set(names) <= {a.name for a in x.names}]
        inside = {id(x) for fn in ast.walk(source) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
        check(imports and all(id(x) in inside for x in imports), f"{path} imports {names} from project_provision inside its functions, when they run")
    result = python("import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_repository_boundary as m\n"
                    "out = []\n"
                    "for name in ('repository_boundary_phase', 'repository_boundary_statements'):\n"
                    "    saved = getattr(t, name); setattr(t, name, lambda *a, **k: 'PATCHED')\n"
                    "    seen = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "    setattr(t, name, saved)\n"
                    "    restored = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "    out.append((seen('x'), restored is getattr(m, name)))\n"
                    "print(out)")
    check(result.stdout.strip() == "[('PATCHED', True), ('PATCHED', True)]",
          f"a patch on project_provision is what the repair's call-time import sees; restored, it is the module's own: {result.stdout}{result.stderr[-400:]}")


def test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it. Both runs record any host path they look at.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(HOST_RECORDER + f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd472_script__'); "
                       "m = sys.modules['provision_repository_boundary']; import project_provision\n"
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + BOUNDARY_PROBE)
    as_package = python(HOST_RECORDER + "import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + BOUNDARY_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("9 ")
          and lines[1].endswith(" control 1 host paths 0"),
          f"as a direct script: the script holds the module's own objects, and all nine answers are the package's, byte for byte, "
          f"with no host path looked at (the recorder's positive control caught): {as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_repository_boundary.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and len(own) == 2,
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else: {own}")
        else:
            check(own == [], f"{name}: reads nothing of project_provision and imports nothing: {own}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import re"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the six constants are the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the ten, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_repository_boundary" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_repository_boundary" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the ten, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_repository_boundary")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read, its own or re-exported")
    # The definitions that name them are counted wherever they live -- project_provision, or a later slice's module, whose
    # read through project_provision (provision.X) counts as the name -- so this check survives the next slice.
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_repository_boundary"]
    uses: dict = {}
    for fn in [*tree.body, *(n for later_tree in later for n in later_tree.body)]:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                name = x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision" else None
                if name in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(name, 0)
                    uses[fn.name][name] += 1
    check(uses == DISPATCH, f"project_provision's own definitions name them exactly as often as before, by the re-exported names: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Try)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing reads them at module level: {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision"):
                for a in x.names:
                    if a.name in MOVED:
                        got[f"import {a.name}"] = got.get(f"import {a.name}", 0) + 1
        check(got == counts, f"{path} still imports them from project_provision, as often as before: {got}")


def test_the_direct_script_and_the_package_render_the_same_packets() -> None:
    # Four synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory, the board Python pinned; the phase each carries is
    # the one the reader lifts.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd472-packet-")).resolve()
    try:
        for variant, (extra, roles) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p472", "--owner-user", "p472-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34472", "--output-dir", f"{work}/out", *extra]
                given = f"dataclasses.replace(build(*a, **k), role_accounts={ROLE_ACCOUNTS!r}, roles_group='p472-roles')" if roles else "build(*a, **k)"
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                pin = "import os; os.environ['TICKET_BOARD_PYTHON'] = '/p472/python'; "
                if mode == "script":
                    probe = (pin + f"import dataclasses, runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd472_script'); main = g['main']; build = main.__globals__['build_plan']; "
                             f"main.__globals__['build_plan'] = lambda *a, **k: {given}; raise SystemExit(main({argv!r}))")
                else:
                    probe = (pin + "import dataclasses, scripts.ticket_board.project_provision as pp; build = pp.build_plan; "
                             f"pp.build_plan = lambda *a, **k: {given}; raise SystemExit(pp.main({argv!r}))")
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES[variant],
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            commands = outputs["package"][2].get("operator-commands.sh", b"").decode()
            phase, reason = m.repository_boundary_phase(commands)
            check(commands.count(m.REPOSITORY_BOUNDARY_BEGIN) == commands.count(m.REPOSITORY_BOUNDARY_END) == 1 and phase and reason == ""
                  and all(m._is_boundary_line(line) for line in phase),
                  f"{variant}: the operator script carries one fenced boundary phase the reader lifts and accepts: {reason} {len(phase)}")
    finally:
        shutil.rmtree(base)


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def value(label):
        return GOLDEN[label]["result"]["value"]

    def calls(label):
        return GOLDEN[label]["calls"]

    check(value("outside quotes: 'sudo install -d /p472'") == "sudo install -d /p472" and value("outside quotes: \"echo 'a' ; rm\"") == "echo  ; rm"
          and value(f"outside quotes: {QUOTING[1]!r}") == QUOTING[1][:QUOTING[1].index("'")] + " "
          and value("outside quotes: \"unbalanced 'quote\"") == ";&|`$<>()\n" and value("outside quotes: the metacharacters rebound on project_provision") == "#",
          "outside quotes: what the shell reads as syntax, quoted text dropped; an unbalanced quote is every metacharacter -- read through project_provision")
    accepted = [line for line in LINES if value(f"boundary line: {line!r}") is True]
    refused = ["#no space", "if [ -d /p472/repo ]; then", "if getent group p472 >/dev/null 2>&1; then", "sudo gpasswd -a 'p472' 'p472-repo' > /tmp/x",
               "sudo install -d '/p472'; rm -rf /", "sudo install -d '/p472' && reboot", "sudo install -d $(evil)", "sudo systemctl restart x",
               "sudo install -d 'unbalanced", "sudo chown x /p472", "then", "else"]
    check(accepted == [line for line in LINES if line not in refused] and len(accepted) == len(LINES) - len(refused) == 13,
          f"a boundary line: fi, a comment, a whole quoted guard, or an allowed command with no metacharacter outside quotes but its one /dev/null: {accepted}")
    check(value("boundary line: the allowed prefixes rebound on project_provision") is True and value("boundary line: the guards rebound on project_provision") is True
          and value("boundary line: the metacharacters rebound on project_provision") is True and value("boundary line: the quoting rebound on project_provision") is False
          and value("boundary line: a guard is matched from the start of the line") is False,
          "the allowed prefixes, guards, metacharacters and quoting are read through project_provision when the check runs")
    check(value("statements: flat") == [["sudo install -d '/a'"], ["sudo groupadd -r 'g'"]] and len(value("statements: nested")) == 2
          and value("statements: unclosed") == [["if [ -d '/a' ]; then", "sudo find '/a'"]] and value("statements: stray fi") == [["fi"], ["sudo install -d '/a'"]]
          and value("statements: empty") == [],
          "the statements: a guard and its body to the matching fi are one statement, nested guards included; an unclosed one is kept whole")
    no_phase = value("phase: no fence")[1]
    check(no_phase.startswith("this packet has no repository boundary phase") and value("phase: the end before the begin")[1] == no_phase
          and value("phase: only a begin")[1] == no_phase and value("phase: other markers")[1] == no_phase
          and value("phase: a clean phase") == [["sudo install -d '/a'", "if [ -d '/a' ]; then", "sudo find '/a'", "fi"], ""]
          and value("phase: a deploy inside the fence")[0] == [] and "sudo systemctl restart board" in value("phase: a deploy inside the fence")[1]
          and value("phase: a second command riding along")[0] == [] and value("phase: an empty fence") == [[], ""]
          and value("phase: two fences") == [["sudo install -d '/a'"], ""],
          "the phase: the non-blank lines between the first begin and the next end, every one a boundary line -- else none, and why")
    check(value("phase: the markers rebound on project_provision") == [["sudo install -d '/a'"], ""]
          and value("phase: the line check rebound on project_provision")[0] == ["sudo install -d '/a'", "sudo systemctl restart board"],
          "the markers and the line check are read through project_provision when the phase is lifted")
    own = value("phase: the operator script's own")
    check(isinstance(own, list) and own[1] == "" and len(own[0]) > 0 and calls("phase: the operator script's own")["_is_boundary_line"] == len(own[0]),
          "the operator script project_provision renders carries a phase the reader lifts and accepts, every line checked")
    check(value("constants")[:2] == ["# >>> switchyard repository boundary", "# <<< switchyard repository boundary"]
          and len(value("constants")[2]) == 5 and all(p.startswith("sudo ") and p.endswith(" ") for p in value("constants")[2]) and value("constants")[4] == ";&|`$<>()\n",
          "the fence, the allowed prefixes and the metacharacters")


def test_every_seam_is_reached() -> None:
    # Every function the four read on project_provision is a recorder there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_a_patch_on_project_provision_reaches_the_repair", "test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_repository_boundary_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
