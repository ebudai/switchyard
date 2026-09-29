#!/usr/bin/env python3
"""SYRD-463: the provisioning GitHub SSH identity, against project_provision it came out of.

The fifteen -- the three constants (`GITHUB_IDENTITY_BEGIN`,
`GITHUB_IDENTITY_END`, `DEFAULT_GITHUB_HOST`) and the twelve definitions of the
owner's managed `~/.ssh/config` block, its reading, resolution and operator
commands, and the publication-host test -- moved unchanged into
`scripts/ticket_board/provision_github_identity.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps the
helpers they read. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads only its package, never `project_provision`. Every `host`
  default is the very `DEFAULT_GITHUB_HOST` object, bound at definition time.
- **Seams (rule 24):** what they read of `project_provision` -- each other,
  `shell_quote`, `_refuse_unnormalized`, `PathContainmentError` and the
  constants -- is read through it when they run, with the direct-script
  fallback, so a patch there reaches them.
- **Readers:** `render_operator_commands` names them as before, and every
  production module imports them from `project_provision`, as often as before.
- **The behaviour is the baseline's** over a synthetic owner home: the key path
  and its refusal, the block and its markers, composing into an existing
  config, reading a block back, the owner's key pairs, resolving the identity,
  the operator commands and their quoting, and the publication host. `GOLDEN`
  below was produced by running the BASELINE module's own definitions over the
  very cases embedded here (`gold463.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with another TMPDIR and locale.
- **The direct script and the package render the same packet,** byte for
  byte, for a synthetic owner with a managed block and a named key.

No real home, key, tenant or account is read or written: every owner home is a
synthetic tree in a test-owned directory. Spawns, every exec, signals, account
and group lookups and socket connections are refused for each case.
"""

from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_github_identity as m  # noqa: E402

CHECKS = 0
MOVED = ('GITHUB_IDENTITY_BEGIN', 'GITHUB_IDENTITY_END', 'DEFAULT_GITHUB_HOST', 'owner_github_key_path', 'github_identity_block', 'compose_ssh_config', 'OwnerGithubIdentity', 'parse_managed_github_identity', 'existing_owner_ssh_key_names', 'resolve_owner_github_identity', 'owner_github_identity_commands', 'owner_github_selection_commands', 'publication_remote_host', 'publication_uses_github', 'owner_github_block_removal_commands')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'owner_github_key_path': {'PathContainmentError': 1},
    'github_identity_block': {'GITHUB_IDENTITY_BEGIN': 1, 'GITHUB_IDENTITY_END': 1, 'owner_github_key_path': 1},
    'compose_ssh_config': {'GITHUB_IDENTITY_BEGIN': 1, 'GITHUB_IDENTITY_END': 1},
    'parse_managed_github_identity': {'DEFAULT_GITHUB_HOST': 1, 'GITHUB_IDENTITY_BEGIN': 1, 'GITHUB_IDENTITY_END': 1},
    'resolve_owner_github_identity': {'DEFAULT_GITHUB_HOST': 3, 'OwnerGithubIdentity': 5, 'existing_owner_ssh_key_names': 1, 'parse_managed_github_identity': 1},
    'owner_github_identity_commands': {'GITHUB_IDENTITY_BEGIN': 1, 'GITHUB_IDENTITY_END': 1, '_refuse_unnormalized': 1, 'github_identity_block': 1, 'owner_github_key_path': 1, 'shell_quote': 9},
    'owner_github_selection_commands': {'GITHUB_IDENTITY_BEGIN': 1, 'GITHUB_IDENTITY_END': 1, 'PathContainmentError': 1, '_refuse_unnormalized': 1, 'github_identity_block': 1, 'owner_github_key_path': 1, 'shell_quote': 6},
    'publication_uses_github': {'DEFAULT_GITHUB_HOST': 2, 'publication_remote_host': 1},
    'owner_github_block_removal_commands': {'GITHUB_IDENTITY_BEGIN': 2, 'GITHUB_IDENTITY_END': 1, '_refuse_unnormalized': 1, 'shell_quote': 5},
}
#: Measured on the baseline: every project_provision definition outside the fifteen that names them, and how often.
DISPATCH = {'render_operator_commands': {'owner_github_identity_commands': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/github_identity.py': {'import GITHUB_IDENTITY_BEGIN': 2, 'import owner_github_key_path': 2, 'import owner_github_block_removal_commands': 1, 'import publication_remote_host': 1, 'import publication_uses_github': 1, 'import owner_github_selection_commands': 1}, 'scripts/publication_status.py': {'import owner_github_key_path': 1, 'import resolve_owner_github_identity': 1}, 'scripts/tenant_publication_boundary.py': {'import owner_github_key_path': 1, 'import resolve_owner_github_identity': 1}, 'scripts/upgrade_phases.py': {'import owner_github_identity_commands': 1, 'import owner_github_key_path': 1, 'import resolve_owner_github_identity': 1, 'import publication_uses_github': 1}}
#: The BASELINE's own behaviour for the cases below (`gold463.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'key path: the default name': {'result': {'type': 'str', 'value': 'HOME/.ssh/id_ed25519'}, 'calls': []},
    'key path: a named key, stripped': {'result': {'type': 'str', 'value': 'HOME/.ssh/id_p463'}, 'calls': []},
    'key path: a separator refused': {'result': {'raised': 'PathContainmentError', 'message': 'the GitHub key name ../id_x may not contain a path separator'}, 'calls': []},
    'block: the default host': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}]]},
    'block: a host alias': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n\nHost github-p463\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}]]},
    'block: another host, a blank alias': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost ghe.example.com\n    HostName ghe.example.com\n    User git\n    IdentityFile HOME/.ssh/id_ed25519\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'block: the markers rebound on project_provision': {'result': {'type': 'str', 'value': '# P463 BEGIN\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/k\n    IdentitiesOnly yes\n# P463 END\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': 'k'}]]},
    'block: the default host bound at definition': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_ed25519\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'block: the key path rebound on project_provision': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile /p463/rebound/key\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': [['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'compose: into an empty config': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': []},
    'compose: foreign stanzas kept after the block': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\nHost example\n  User x\n\nHost *\n  ForwardAgent no\n'}, 'calls': []},
    'compose: an old block replaced': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  User a\nHost b\n  User b\n'}, 'calls': []},
    'compose: a begin with no end': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n'}, 'calls': []},
    'compose: only the block': {'result': {'type': 'str', 'value': '# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'}, 'calls': []},
    'parse: no block': {'result': {'type': 'tuple', 'value': ['', '', '', []]}, 'calls': []},
    'parse: the managed block': {'result': {'type': 'tuple', 'value': ['id_p463', 'github.com', '', []]}, 'calls': []},
    'parse: an alias and a hostname': {'result': {'type': 'tuple', 'value': ['k1', 'ghe.example.com', 'github-p463', []]}, 'calls': []},
    'parse: two key files': {'result': {'type': 'tuple', 'value': ['', '', '', ['the managed GitHub identity block selects 2 different key files (HOME/.ssh/k1, HOME/.ssh/k2), so which one this tenant publishes with cannot be read from it']]}, 'calls': []},
    'parse: a directory as the key': {'result': {'type': 'tuple', 'value': ['', '', '', ['the managed GitHub identity block names HOME/.ssh/, which is not a key file this tenant can select']]}, 'calls': []},
    'parse: no hostname takes the default host': {'result': {'type': 'tuple', 'value': ['k1', 'github.com', '', []]}, 'calls': []},
    'parse: the default host rebound on project_provision': {'result': {'type': 'tuple', 'value': ['k1', 'p463.example', '', []]}, 'calls': []},
    'keys: pairs only, sorted': {'result': {'type': 'tuple', 'value': ['id_a', 'id_b']}, 'calls': []},
    'keys: no .ssh': {'result': {'type': 'tuple', 'value': []}, 'calls': []},
    'resolve: the recorded selection': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': 'id_rec', 'host': 'github.com', 'host_alias': 'github-rec', 'source': "the tenant's recorded selection", 'problems': [], 'resolved': True}}, 'calls': []},
    'resolve: a recorded host': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': 'id_rec', 'host': 'ghe.example.com', 'host_alias': '', 'source': "the tenant's recorded selection", 'problems': [], 'resolved': True}}, 'calls': []},
    'resolve: the managed block': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': 'id_p463', 'host': 'github.com', 'host_alias': '', 'source': 'the managed block in HOME/.ssh/config', 'problems': [], 'resolved': True}}, 'calls': [['parse_managed_github_identity', ['# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'], {}]]},
    'resolve: a block with two keys': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': '', 'host': 'github.com', 'host_alias': '', 'source': 'default', 'problems': ['the managed GitHub identity block selects 2 different key files (HOME/.ssh/k1, HOME/.ssh/k2), so which one this tenant publishes with cannot be read from it'], 'resolved': False}}, 'calls': [['parse_managed_github_identity', ['# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/k1\n  IdentityFile HOME/.ssh/k2\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n'], {}]]},
    'resolve: keys but no selection': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': '', 'host': 'github.com', 'host_alias': '', 'source': 'default', 'problems': ["HOME holds id_a and nothing records which of them this tenant publishes with: HOME/.ssh/config has no managed GitHub identity block and the tenant's plan predates recording one. Nothing was changed. Re-provision the tenant, or add the block naming the key it has been using, and run this again."], 'resolved': False}}, 'calls': [['parse_managed_github_identity', ['Host x\n  User y\n'], {}], ['existing_owner_ssh_key_names', ['HOME'], {}]]},
    'resolve: a fresh owner': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': '', 'host': 'github.com', 'host_alias': '', 'source': 'the default for an owner with no keys', 'problems': [], 'resolved': True}}, 'calls': [['parse_managed_github_identity', [''], {}], ['existing_owner_ssh_key_names', ['HOME'], {}]]},
    'resolve: the parser rebound on project_provision': {'result': {'type': 'OwnerGithubIdentity', 'value': {'dataclass': 'OwnerGithubIdentity', 'key_name': 'id_rebound', 'host': 'p463.host', 'host_alias': 'p463-alias', 'source': 'the managed block in HOME/.ssh/config', 'problems': [], 'resolved': True}}, 'calls': [['parse_managed_github_identity', ['TEXT'], {}]]},
    'identity commands: a new named key': {'result': {'type': 'list', 'value': ["sudo install -d -m 0700 -o 'p463-agent' -g 'p463-agent' 'HOME/.ssh'", "if [ ! -f 'HOME/.ssh/id_p463' ]; then", "    sudo -u 'p463-agent' ssh-keygen -t ed25519 -N '' -C 'p463-agent switchyard' -f 'HOME/.ssh/id_p463'", 'fi', "sudo chown 'p463-agent':'p463-agent' 'HOME/.ssh/id_p463' 'HOME/.ssh/id_p463'.pub", "sudo chmod 0600 'HOME/.ssh/id_p463'", "sudo chmod 0644 'HOME/.ssh/id_p463'.pub", 'sudo -u \'p463-agent\' sh -c \'set -e; block=\'"\'"\'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n\nHost github-p463\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n\'"\'"\'; config=\'"\'"\'HOME/.ssh/config\'"\'"\'; tmp="$(mktemp)"; printf "%s" "$block" > "$tmp"; if [ -f "$config" ]; then sed \'"\'"\'/^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d\'"\'"\' "$config" >> "$tmp"; fi; install -m 0600 "$tmp" "$config"; rm -f "$tmp"\'', "sudo chown 'p463-agent':'p463-agent' 'HOME/.ssh/config'", "sudo chmod 0600 'HOME/.ssh/config'"]}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}], ['github_identity_block', ['HOME'], {'key_name': 'id_p463', 'host': 'github.com', 'host_alias': 'github-p463'}], ['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}]]},
    'identity commands: the default key and label': {'result': {'type': 'list', 'value': ["sudo install -d -m 0700 -o 'p463-agent' -g 'p463-agent' 'HOME/.ssh'", "if [ ! -f 'HOME/.ssh/id_ed25519' ]; then", "    sudo -u 'p463-agent' ssh-keygen -t ed25519 -N '' -C 'p463-agent switchyard' -f 'HOME/.ssh/id_ed25519'", 'fi', "sudo chown 'p463-agent':'p463-agent' 'HOME/.ssh/id_ed25519' 'HOME/.ssh/id_ed25519'.pub", "sudo chmod 0600 'HOME/.ssh/id_ed25519'", "sudo chmod 0644 'HOME/.ssh/id_ed25519'.pub", 'sudo -u \'p463-agent\' sh -c \'set -e; block=\'"\'"\'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_ed25519\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n\'"\'"\'; config=\'"\'"\'HOME/.ssh/config\'"\'"\'; tmp="$(mktemp)"; printf "%s" "$block" > "$tmp"; if [ -f "$config" ]; then sed \'"\'"\'/^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d\'"\'"\' "$config" >> "$tmp"; fi; install -m 0600 "$tmp" "$config"; rm -f "$tmp"\'', "sudo chown 'p463-agent':'p463-agent' 'HOME/.ssh/config'", "sudo chmod 0600 'HOME/.ssh/config'"]}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': ''}], ['github_identity_block', ['HOME'], {'key_name': '', 'host': 'github.com', 'host_alias': ''}], ['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'identity commands: a comment': {'result': {'type': 'list', 'value': ["sudo install -d -m 0700 -o 'p463 agent' -g 'p463 agent' 'HOME/.ssh'", "if [ ! -f 'HOME/.ssh/id_ed25519' ]; then", '    sudo -u \'p463 agent\' ssh-keygen -t ed25519 -N \'\' -C \'it\'"\'"\'s p463\' -f \'HOME/.ssh/id_ed25519\'', 'fi', "sudo chown 'p463 agent':'p463 agent' 'HOME/.ssh/id_ed25519' 'HOME/.ssh/id_ed25519'.pub", "sudo chmod 0600 'HOME/.ssh/id_ed25519'", "sudo chmod 0644 'HOME/.ssh/id_ed25519'.pub", 'sudo -u \'p463 agent\' sh -c \'set -e; block=\'"\'"\'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_ed25519\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n\'"\'"\'; config=\'"\'"\'HOME/.ssh/config\'"\'"\'; tmp="$(mktemp)"; printf "%s" "$block" > "$tmp"; if [ -f "$config" ]; then sed \'"\'"\'/^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d\'"\'"\' "$config" >> "$tmp"; fi; install -m 0600 "$tmp" "$config"; rm -f "$tmp"\'', "sudo chown 'p463 agent':'p463 agent' 'HOME/.ssh/config'", "sudo chmod 0600 'HOME/.ssh/config'"]}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': ''}], ['github_identity_block', ['HOME'], {'key_name': '', 'host': 'github.com', 'host_alias': ''}], ['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'identity commands: an unnormalized home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home HOME/../x is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['HOME/../x'], {'what': 'the owner home'}]]},
    'identity commands: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['sudo install -d -m 0700 -o <p463-agent> -g <p463-agent> <HOME/.ssh>', 'if [ ! -f <HOME/.ssh/id_ed25519> ]; then', "    sudo -u <p463-agent> ssh-keygen -t ed25519 -N '' -C <p463-agent switchyard> -f <HOME/.ssh/id_ed25519>", 'fi', 'sudo chown <p463-agent>:<p463-agent> <HOME/.ssh/id_ed25519> <HOME/.ssh/id_ed25519>.pub', 'sudo chmod 0600 <HOME/.ssh/id_ed25519>', 'sudo chmod 0644 <HOME/.ssh/id_ed25519>.pub', 'sudo -u <p463-agent> sh -c <set -e; block=<# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_ed25519\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n>; config=<HOME/.ssh/config>; tmp="$(mktemp)"; printf "%s" "$block" > "$tmp"; if [ -f "$config" ]; then sed </^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d> "$config" >> "$tmp"; fi; install -m 0600 "$tmp" "$config"; rm -f "$tmp">', 'sudo chown <p463-agent>:<p463-agent> <HOME/.ssh/config>', 'sudo chmod 0600 <HOME/.ssh/config>']}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': ''}], ['github_identity_block', ['HOME'], {'key_name': '', 'host': 'github.com', 'host_alias': ''}], ['owner_github_key_path', ['HOME'], {'key_name': ''}]]},
    'selection commands: a named key': {'result': {'type': 'list', 'value': ['sudo -u \'p463-agent\' sh -c \'set -e; block=\'"\'"\'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n\'"\'"\'; dir=\'"\'"\'HOME/.ssh\'"\'"\'; config=\'"\'"\'HOME/.ssh/config\'"\'"\'; mkdir -p "$dir"; chmod 0700 "$dir"; tmp="$(mktemp)"; printf "%s" "$block" > "$tmp"; if [ -f "$config" ]; then sed \'"\'"\'/^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d\'"\'"\' "$config" >> "$tmp"; fi; install -m 0600 "$tmp" "$config"; rm -f "$tmp"\'']}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}], ['github_identity_block', ['HOME'], {'key_name': 'id_p463', 'host': 'github.com', 'host_alias': ''}], ['owner_github_key_path', ['HOME'], {'key_name': 'id_p463'}]]},
    'selection commands: no key name': {'result': {'raised': 'PathContainmentError', 'message': 'a key name is required to select an identity'}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}]]},
    'selection commands: a separator': {'result': {'raised': 'PathContainmentError', 'message': 'the GitHub key name a/b may not contain a path separator'}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}], ['owner_github_key_path', ['HOME'], {'key_name': 'a/b'}]]},
    'selection commands: the refusal class rebound on project_provision': {'result': {'raised': 'P463Refusal', 'message': 'a key name is required to select an identity'}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}]]},
    'remote host: forms': {'result': {'': {'type': 'str', 'value': ''}, '/data/git/x': {'type': 'str', 'value': ''}, 'file:///srv/x.git': {'type': 'str', 'value': ''}, '../x.git': {'type': 'str', 'value': ''}, './x': {'type': 'str', 'value': ''}, '~/x': {'type': 'str', 'value': ''}, 'a/b:c': {'type': 'str', 'value': ''}, 'git@GitHub.com:o/r.git': {'type': 'str', 'value': 'github.com'}, 'ssh://git@github.com:22/o/r': {'type': 'str', 'value': 'github.com'}, 'https://GHE.example.com/o/r': {'type': 'str', 'value': 'ghe.example.com'}, 'github-p463:o/r': {'type': 'str', 'value': 'github-p463'}, 'host': {'type': 'str', 'value': ''}, '~host:x': {'type': 'str', 'value': ''}, 'file://p463host/srv/x.git': {'type': 'str', 'value': ''}}, 'calls': []},
    'uses github: forms': {'result': {'': {'type': 'NoneType', 'value': None}, '/data/git/x': {'type': 'bool', 'value': False}, 'git@github.com:o/r': {'type': 'bool', 'value': True}, 'https://api.github.com/o/r': {'type': 'bool', 'value': True}, 'git@gitlab.com:o/r': {'type': 'bool', 'value': False}, 'git@github-p463:o/r': {'type': 'bool', 'value': True}, 'git@notgithub.com:o/r': {'type': 'bool', 'value': False}}, 'calls': [['publication_remote_host', ['/data/git/x'], {}], ['publication_remote_host', ['git@github.com:o/r'], {}], ['publication_remote_host', ['https://api.github.com/o/r'], {}], ['publication_remote_host', ['git@gitlab.com:o/r'], {}], ['publication_remote_host', ['git@github-p463:o/r'], {}], ['publication_remote_host', ['git@notgithub.com:o/r'], {}]]},
    'uses github: a recorded alias': {'result': {'type': 'bool', 'value': True}, 'calls': [['publication_remote_host', ['git@ghe-alias:o/r'], {}]]},
    'uses github: the default host rebound on project_provision': {'result': {'type': 'bool', 'value': True}, 'calls': [['publication_remote_host', ['git@p463.example:o/r'], {}]]},
    'removal commands': {'result': {'type': 'list', 'value': ['sudo -u \'p463-agent\' sh -c \'set -e; config=\'"\'"\'HOME/.ssh/config\'"\'"\'; [ -f "$config" ] || exit 0; grep -qx \'"\'"\'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\'"\'"\' "$config" || exit 0; tmp="$(mktemp)"; sed \'"\'"\'/^# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY$/,/^# END SWITCHYARD MANAGED GITHUB IDENTITY$/d\'"\'"\' "$config" > "$tmp"; install -m 0600 "$tmp" "$config"; rm -f "$tmp"\'']}, 'calls': [['_refuse_unnormalized', ['HOME'], {'what': 'the owner home'}]]},
    'removal commands: an unnormalized home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home relative/home is not an absolute path; refusing to grant access to it'}, 'calls': [['_refuse_unnormalized', ['relative/home'], {'what': 'the owner home'}]]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts the synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold463.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the twelve and records the answer or the exact exception and, in order, every call it made of
# `project_provision`'s helpers (recorded there and passed through). The owner home is a synthetic tree in a fresh
# test-owned directory: a `.ssh` with an optional config and optional key halves. Nothing is run -- the command builders
# only return shell text -- and no real home, key or account is read. A case may rebind a `project_provision` name to
# show it is read when the function runs; the `host` defaults are bound at definition time and do not follow it.
BLOCK = ("# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n"
         "    IdentitiesOnly yes\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n")
CASES = {
    "key path: the default name": {"call": "owner_github_key_path", "args": ["HOME/"]},
    "key path: a named key, stripped": {"call": "owner_github_key_path", "args": ["HOME"], "kwargs": {"key_name": " id_p463 "}},
    "key path: a separator refused": {"call": "owner_github_key_path", "args": ["HOME"], "kwargs": {"key_name": "../id_x"}},
    "block: the default host": {"call": "github_identity_block", "args": ["HOME"], "kwargs": {"key_name": "id_p463"}},
    "block: a host alias": {"call": "github_identity_block", "args": ["HOME"], "kwargs": {"key_name": "id_p463", "host_alias": "github-p463"}},
    "block: another host, a blank alias": {"call": "github_identity_block", "args": ["HOME"], "kwargs": {"host": "ghe.example.com", "host_alias": "  "}},
    "block: the markers rebound on project_provision": {"call": "github_identity_block", "args": ["HOME"], "kwargs": {"key_name": "k"},
                                                        "rebind": {"GITHUB_IDENTITY_BEGIN": "# P463 BEGIN", "GITHUB_IDENTITY_END": "# P463 END"}},
    "block: the default host bound at definition": {"call": "github_identity_block", "args": ["HOME"], "rebind": {"DEFAULT_GITHUB_HOST": "p463.example"}},
    "block: the key path rebound on project_provision": {"call": "github_identity_block", "args": ["HOME"], "rebind": {"owner_github_key_path": True}},
    "compose: into an empty config": {"call": "compose_ssh_config", "args": ["", "BLOCK"]},
    "compose: foreign stanzas kept after the block": {"call": "compose_ssh_config", "args": ["Host example\n  User x\n\nHost *\n  ForwardAgent no\n", "BLOCK"]},
    "compose: an old block replaced": {"call": "compose_ssh_config", "args": ["Host a\n  User a\n" + BLOCK + "Host b\n  User b\n", "BLOCK"]},
    "compose: a begin with no end": {"call": "compose_ssh_config", "args": ["Host a\n# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost github.com\n  User git\n", "BLOCK"]},
    "compose: only the block": {"call": "compose_ssh_config", "args": [BLOCK, "BLOCK"]},
    "parse: no block": {"call": "parse_managed_github_identity", "args": ["Host example\n  IdentityFile ~/.ssh/other\n"]},
    "parse: the managed block": {"call": "parse_managed_github_identity", "args": ["Host x\n" + BLOCK]},
    "parse: an alias and a hostname": {"call": "parse_managed_github_identity",
                                       "args": ["# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost ghe.example.com\n  hostname ghe.example.com\n  identityfile HOME/.ssh/k1\n"
                                                "Host github-p463\n  HostName ghe.example.com\n  IdentityFile HOME/.ssh/k1\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"]},
    "parse: two key files": {"call": "parse_managed_github_identity",
                             "args": ["# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/k1\nHost b\n  IdentityFile HOME/.ssh/k2\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"]},
    "parse: a directory as the key": {"call": "parse_managed_github_identity",
                                      "args": ["# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"]},
    "parse: no hostname takes the default host": {"call": "parse_managed_github_identity",
                                                  "args": ["# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/k1\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"]},
    "parse: the default host rebound on project_provision": {"call": "parse_managed_github_identity",
                                                             "args": ["# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/k1\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"],
                                                             "rebind": {"DEFAULT_GITHUB_HOST": "p463.example"}},
    "keys: pairs only, sorted": {"call": "existing_owner_ssh_key_names", "args": ["HOME"], "keys": ["id_b", "id_b.pub", "id_a", "id_a.pub", "lonely.pub", "only_private"]},
    "keys: no .ssh": {"call": "existing_owner_ssh_key_names", "args": ["HOME"], "no_ssh": True},
    "resolve: the recorded selection": {"call": "resolve_owner_github_identity", "args": ["HOME"],
                                        "kwargs": {"recorded_key_name": " id_rec ", "recorded_host": "", "recorded_host_alias": " github-rec "}},
    "resolve: a recorded host": {"call": "resolve_owner_github_identity", "args": ["HOME"], "kwargs": {"recorded_key_name": "id_rec", "recorded_host": " ghe.example.com "}},
    "resolve: the managed block": {"call": "resolve_owner_github_identity", "args": ["HOME"], "config": BLOCK, "keys": ["id_p463", "id_p463.pub"]},
    "resolve: a block with two keys": {"call": "resolve_owner_github_identity", "args": ["HOME"],
                                       "config": "# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY\nHost a\n  IdentityFile HOME/.ssh/k1\n  IdentityFile HOME/.ssh/k2\n# END SWITCHYARD MANAGED GITHUB IDENTITY\n"},
    "resolve: keys but no selection": {"call": "resolve_owner_github_identity", "args": ["HOME"], "config": "Host x\n  User y\n", "keys": ["id_a", "id_a.pub"]},
    "resolve: a fresh owner": {"call": "resolve_owner_github_identity", "args": ["HOME"], "no_ssh": True},
    "resolve: the parser rebound on project_provision": {"call": "resolve_owner_github_identity", "args": ["HOME"], "config": BLOCK, "rebind": {"parse_managed_github_identity": True}},
    "identity commands: a new named key": {"call": "owner_github_identity_commands", "args": ["p463-agent", "HOME"], "kwargs": {"key_name": "id_p463", "host_alias": "github-p463"}},
    "identity commands: the default key and label": {"call": "owner_github_identity_commands", "args": ["p463-agent", "HOME"]},
    "identity commands: a comment": {"call": "owner_github_identity_commands", "args": ["p463 agent", "HOME"], "kwargs": {"comment": " it's p463 "}},
    "identity commands: an unnormalized home": {"call": "owner_github_identity_commands", "args": ["p463-agent", "HOME/../x"]},
    "identity commands: the quoting rebound on project_provision": {"call": "owner_github_identity_commands", "args": ["p463-agent", "HOME"], "rebind": {"shell_quote": True}},
    "selection commands: a named key": {"call": "owner_github_selection_commands", "args": ["p463-agent", "HOME"], "kwargs": {"key_name": "id_p463"}},
    "selection commands: no key name": {"call": "owner_github_selection_commands", "args": ["p463-agent", "HOME"], "kwargs": {"key_name": " "}},
    "selection commands: a separator": {"call": "owner_github_selection_commands", "args": ["p463-agent", "HOME"], "kwargs": {"key_name": "a/b"}},
    "selection commands: the refusal class rebound on project_provision": {"call": "owner_github_selection_commands", "args": ["p463-agent", "HOME"],
                                                                           "kwargs": {"key_name": ""}, "rebind": {"PathContainmentError": True}},
    "remote host: forms": {"call": "publication_remote_host", "many": ["", "/data/git/x", "file:///srv/x.git", "../x.git", "./x", "~/x", "a/b:c", "git@GitHub.com:o/r.git",
                                                                       "ssh://git@github.com:22/o/r", "https://GHE.example.com/o/r", "github-p463:o/r", "host", "~host:x", "file://p463host/srv/x.git"]},
    "uses github: forms": {"call": "publication_uses_github", "many": ["", "/data/git/x", "git@github.com:o/r", "https://api.github.com/o/r", "git@gitlab.com:o/r",
                                                                       "git@github-p463:o/r", "git@notgithub.com:o/r"]},
    "uses github: a recorded alias": {"call": "publication_uses_github", "args": ["git@ghe-alias:o/r"], "kwargs": {"recorded_host_alias": " GHE-Alias "}},
    "uses github: the default host rebound on project_provision": {"call": "publication_uses_github", "args": ["git@p463.example:o/r"], "rebind": {"DEFAULT_GITHUB_HOST": "p463.example"}},
    "removal commands": {"call": "owner_github_block_removal_commands", "args": ["p463-agent", "HOME"]},
    "removal commands: an unnormalized home": {"call": "owner_github_block_removal_commands", "args": ["p463-agent", "relative/home"]},
}
FUNCTIONS = ("owner_github_key_path", "github_identity_block", "compose_ssh_config", "parse_managed_github_identity", "existing_owner_ssh_key_names",
             "resolve_owner_github_identity", "owner_github_identity_commands", "owner_github_selection_commands", "publication_remote_host",
             "publication_uses_github", "owner_github_block_removal_commands")
PASSED = ("owner_github_key_path", "github_identity_block", "parse_managed_github_identity", "existing_owner_ssh_key_names", "publication_remote_host",
          "_refuse_unnormalized")
REBINDABLE = ("GITHUB_IDENTITY_BEGIN", "GITHUB_IDENTITY_END", "DEFAULT_GITHUB_HOST", "shell_quote", "PathContainmentError", "OwnerGithubIdentity")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import dataclasses, shutil, tempfile
    from pathlib import Path as _P
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd463-")).resolve()
    home = str(tmp / "home")

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dataclass": type(value).__name__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)},
                    "resolved": getattr(value, "resolved", None)}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(home, "HOME").replace(str(tmp), "TMP")
        if isinstance(value, str):
            return value.replace(home, "HOME").replace(str(tmp), "TMP")
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE)}
    try:
        if not spec.get("no_ssh"):
            (tmp / "home" / ".ssh").mkdir(parents=True)
            if "config" in spec:
                (tmp / "home" / ".ssh" / "config").write_text(spec["config"].replace("HOME", home), encoding="utf-8")
            for name in spec.get("keys", []):
                (tmp / "home" / ".ssh" / name).write_text("synthetic\n", encoding="utf-8")
        else:
            (tmp / "home").mkdir()
        block = saved["github_identity_block"](home, key_name="id_p463")
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "owner_github_key_path":
                t.owner_github_key_path = lambda owner_home, *, key_name="": note("owner_github_key_path", owner_home, key_name=key_name) or "/p463/rebound/key"
            elif name == "parse_managed_github_identity":
                t.parse_managed_github_identity = lambda text: note("parse_managed_github_identity", "TEXT") or ("id_rebound", "p463.host", "p463-alias", ())
            elif name == "shell_quote":
                t.shell_quote = lambda value: "<" + str(value) + ">"
            elif name == "PathContainmentError":
                class P463Refusal(ValueError):
                    pass
                t.PathContainmentError = P463Refusal
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        sub = lambda v: v.replace("HOME", home) if isinstance(v, str) else v

        def call(args, kwargs):
            args = [block if a == "BLOCK" else sub(a) for a in args]
            try:
                got = fn(*args, **{k: sub(v) for k, v in kwargs.items()})
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"raised": type(exc).__name__, "message": norm(str(exc))}
            return {"type": type(got).__name__, "value": norm(got)}
        if "many" in spec:
            result = {v: call([v], {}) for v in spec["many"]}
        else:
            result = call(spec.get("args", []), spec.get("kwargs", {}))
        return {"result": result, "calls": calls}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        shutil.rmtree(tmp)
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


#: What project_provision keeps and the moved code reads there when it runs.
KEPT = ("shell_quote", "_refuse_unnormalized", "PathContainmentError", "render_operator_commands")
#: The three constants, as the baseline wrote them.
CONSTANT_TEXT = {
    "GITHUB_IDENTITY_BEGIN": "'# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY'",
    "GITHUB_IDENTITY_END": "'# END SWITCHYARD MANAGED GITHUB IDENTITY'",
    "DEFAULT_GITHUB_HOST": "'github.com'",
}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
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
    result = python("import sys, scripts.ticket_board.provision_github_identity as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_same_defaults() -> None:
    for order in (("scripts.ticket_board.provision_github_identity", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_github_identity")):
        result = python("import importlib, inspect, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_github_identity as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS + ('OwnerGithubIdentity',)!r}}}), "
                        "all(inspect.signature(getattr(m, f)).parameters['host'].default is m.DEFAULT_GITHUB_HOST "
                        "for f in ('github_identity_block', 'owner_github_identity_commands', 'owner_github_selection_commands')), "
                        "{f.name: f.default for f in dataclasses.fields(m.OwnerGithubIdentity)}['host'] is m.DEFAULT_GITHUB_HOST, "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_github_identity'] True True True",
              f"{' then '.join(order)}: one object each, defined here; every host default the very constant; nothing of project_provision "
              f"bound at load: {result.stdout}{result.stderr[-600:]}")
    import dataclasses
    import pathlib
    check(m.dataclass is dataclasses.dataclass and m.Path is pathlib.Path and t.dataclass is m.dataclass and t.Path is m.Path,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_github_identity.py").read_text(encoding="utf-8"))
    for name in (*FUNCTIONS, "OwnerGithubIdentity"):
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling included, read through it exactly as often as before: {through}")
        imports = [x for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        tries = [x for x in node.body if isinstance(x, ast.Try)]
        first = 1 if ast.get_docstring(node) is not None else 0
        if expected:
            check(len(imports) == 2 and ast.unparse(node.body[first]) == CALL_TIME_IMPORT and len(tries) == 1 or
                  (len(tries) >= 1 and ast.unparse(node.body[first]) == CALL_TIME_IMPORT and len(imports) == 2),
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else imported")
        else:
            check(imports == [], f"{name}: reads nothing of project_provision and imports nothing")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from dataclasses import dataclass", "from pathlib import Path"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library only, at load: {top}")
    consts = {top_name(n): ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(consts == CONSTANT_TEXT, f"the three constants are the baseline's literals: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {"owner_github_key_path": ["''"], "github_identity_block": ["''", "DEFAULT_GITHUB_HOST", "''"], "compose_ssh_config": [],
                       "parse_managed_github_identity": [], "existing_owner_ssh_key_names": [], "resolve_owner_github_identity": ["''", "''", "''"],
                       "owner_github_identity_commands": ["''", "DEFAULT_GITHUB_HOST", "''", "''"], "owner_github_selection_commands": ["DEFAULT_GITHUB_HOST", "''"],
                       "publication_remote_host": [], "publication_uses_github": ["''"], "owner_github_block_removal_commands": []},
          f"the defaults are the baseline's: literals, and the host constant bound at definition time: {defaults}")
    cls = next(n for n in tree.body if top_name(n) == "OwnerGithubIdentity")
    fields = [(ast.unparse(x.target), ast.unparse(x.value) if x.value is not None else None) for x in cls.body if isinstance(x, ast.AnnAssign)]
    check([ast.unparse(d) for d in cls.decorator_list] == ["dataclass(frozen=True)"]
          and fields == [("key_name", None), ("host", "DEFAULT_GITHUB_HOST"), ("host_alias", "''"), ("source", "'default'"), ("problems", "()")],
          f"OwnerGithubIdentity: frozen, its fields and defaults in order: {fields}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the fifteen, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_github_identity" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_github_identity" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the fifteen, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # A name stays reachable on project_provision: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in ast.walk(guard) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and set(KEPT) <= defined | exported, "project_provision defines none of them, and keeps the helpers they read and their caller, its own or re-exported")
    # The definitions that name them are counted wherever they now live -- project_provision, or a later slice's module,
    # whose read through project_provision (provision.X) counts as the name (SYRD-474: their caller moved on).
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_github_identity"]
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


def test_the_direct_script_and_the_package_render_the_same_packet() -> None:
    # One synthetic provisioning packet, rendered by `project_provision.py` run as a script (its import fallback taken) and
    # through the package entry, in a test-owned directory: a synthetic owner home with a managed GitHub block and a named key.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd463-packet-")).resolve()
    try:
        outputs = {}
        for mode in ("script", "package"):
            work = base / "run"
            shutil.rmtree(work, ignore_errors=True)
            (work / "home" / ".ssh").mkdir(parents=True)
            (work / "source").mkdir()
            (work / "home" / ".ssh" / "config").write_text(
                f"Host example\n  User x\n\n{t.GITHUB_IDENTITY_BEGIN}\nHost github.com\n  HostName github.com\n  User git\n"
                f"  IdentityFile {work}/home/.ssh/id_p463\n  IdentitiesOnly yes\n{t.GITHUB_IDENTITY_END}\n", encoding="utf-8")
            for name in ("id_p463", "id_p463.pub"):
                (work / "home" / ".ssh" / name).write_text("synthetic\n", encoding="utf-8")
            argv = ["--project", "p463", "--owner-user", "p463-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                    "--port", "34463", "--output-dir", f"{work}/out"]
            script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
            # The CLI takes no GitHub key, so the synthetic named key is given the way `switchyard new` gives it: to build_plan.
            key = "{'owner_github_key_name': 'id_p463', 'owner_github_host_alias': 'github-p463'}"
            if mode == "script":
                probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                         f"g = runpy.run_path({str(script)!r}, run_name='syrd463_script'); main = g['main']; build = main.__globals__['build_plan']; "
                         f"main.__globals__['build_plan'] = lambda *a, **k: build(*a, **{{**k, **{key}}}); raise SystemExit(main({argv!r}))")
            else:
                probe = (f"import scripts.ticket_board.project_provision as pp; build = pp.build_plan; "
                         f"pp.build_plan = lambda *a, **k: build(*a, **{{**k, **{key}}}); raise SystemExit(pp.main({argv!r}))")
            result = python(probe)
            files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
            outputs[mode] = (result.returncode, result.stdout, files)
        check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
              and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
              f"the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
        script_text = outputs["package"][2]["operator-commands.sh"].decode("utf-8")
        check(t.GITHUB_IDENTITY_BEGIN in script_text and f"IdentityFile {base}/run/home/.ssh/id_p463" in script_text and "Host github-p463" in script_text
              and "id_ed25519" not in script_text,
              "the operator commands carry the managed GitHub identity for the synthetic named key and its alias, not the default key")
    finally:
        shutil.rmtree(base)


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


#: The owner prefix the baseline builds: an expected argv prefix, compared and never run.
OWNER_PREFIX = ["sudo", "-u"]


#: The command word the baseline builds: an expected argv head, compared and never run.
CHOWN = "chown"


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def value(label):
        return result(label)["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    BEGIN, END = "# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY", "# END SWITCHYARD MANAGED GITHUB IDENTITY"
    STANZA = "Host github.com\n    HostName github.com\n    User git\n    IdentityFile HOME/.ssh/id_p463\n    IdentitiesOnly yes\n"
    check(value("key path: the default name") == "HOME/.ssh/id_ed25519" and value("key path: a named key, stripped") == "HOME/.ssh/id_p463"
          and result("key path: a separator refused") == {"raised": "PathContainmentError", "message": "the GitHub key name ../id_x may not contain a path separator"},
          "the key: id_ed25519 by default, a stripped name under the home's .ssh, a path separator refused")
    check(value("block: the default host") == f"{BEGIN}\n{STANZA}{END}\n"
          and value("block: a host alias") == f"{BEGIN}\n{STANZA}\n{STANZA.replace('Host github.com', 'Host github-p463')}{END}\n"
          and value("block: another host, a blank alias").count("Host ") == 1 and "HostName ghe.example.com" in value("block: another host, a blank alias")
          and value("block: the markers rebound on project_provision").startswith("# P463 BEGIN\n") and value("block: the markers rebound on project_provision").endswith("# P463 END\n")
          and "Host github.com\n" in value("block: the default host bound at definition")
          and "IdentityFile /p463/rebound/key" in value("block: the key path rebound on project_provision"),
          "the block: one stanza per host pattern between the markers, the markers and key path read through project_provision, the host default bound at definition")
    check(value("compose: into an empty config") == value("compose: only the block") == f"{BEGIN}\n{STANZA}{END}\n"
          and value("compose: foreign stanzas kept after the block") == f"{BEGIN}\n{STANZA}{END}\nHost example\n  User x\n\nHost *\n  ForwardAgent no\n"
          and value("compose: an old block replaced") == f"{BEGIN}\n{STANZA}{END}\nHost a\n  User a\nHost b\n  User b\n"
          and value("compose: a begin with no end") == f"{BEGIN}\n{STANZA}{END}\nHost a\n",
          "composing: exactly one managed block, first; everything outside the markers kept; a begin with no end takes the rest of the file")
    check(value("parse: no block") == ["", "", "", []] and value("parse: the managed block") == ["id_p463", "github.com", "", []]
          and value("parse: an alias and a hostname") == ["k1", "ghe.example.com", "github-p463", []]
          and value("parse: two key files")[3][0].startswith("the managed GitHub identity block selects 2 different key files")
          and value("parse: a directory as the key")[3][0].endswith("which is not a key file this tenant can select")
          and value("parse: no hostname takes the default host")[1] == "github.com" and value("parse: the default host rebound on project_provision")[1] == "p463.example",
          "reading a block: the one key file, the hostname (else the default, read when it runs) and the second host pattern as the alias; ambiguity refused")
    check(value("keys: pairs only, sorted") == ["id_a", "id_b"] and value("keys: no .ssh") == [],
          "the owner's keys: only names with both halves, sorted; none without a .ssh")
    resolved = {k: value(k) for k in GOLDEN if k.startswith("resolve:")}
    check(resolved["resolve: the recorded selection"]["key_name"] == "id_rec" and resolved["resolve: the recorded selection"]["host_alias"] == "github-rec"
          and calls("resolve: the recorded selection") == [] and resolved["resolve: the recorded selection"]["host"] == "github.com"
          and resolved["resolve: a recorded host"]["host"] == "ghe.example.com"
          and resolved["resolve: the managed block"]["key_name"] == "id_p463" and resolved["resolve: the managed block"]["source"] == "the managed block in HOME/.ssh/config"
          and resolved["resolve: a block with two keys"]["resolved"] is False and resolved["resolve: keys but no selection"]["resolved"] is False
          and "Nothing was changed." in resolved["resolve: keys but no selection"]["problems"][0]
          and resolved["resolve: a fresh owner"]["source"] == "the default for an owner with no keys" and resolved["resolve: a fresh owner"]["resolved"] is True
          and resolved["resolve: the parser rebound on project_provision"]["key_name"] == "id_rebound",
          "resolving: the recorded selection, else the managed block, else nothing for an owner with keys, else the default -- each helper read through project_provision")
    ids = value("identity commands: a new named key")
    check(ids[0] == "sudo install -d -m 0700 -o 'p463-agent' -g 'p463-agent' 'HOME/.ssh'" and ids[1] == "if [ ! -f 'HOME/.ssh/id_p463' ]; then"
          and "ssh-keygen -t ed25519 -N '' -C 'p463-agent switchyard'" in ids[2] and ids[-2:] == ["sudo chown 'p463-agent':'p463-agent' 'HOME/.ssh/config'", "sudo chmod 0600 'HOME/.ssh/config'"]
          and len(ids) == 10 and "-C 'it'\"'\"'s p463'" in value("identity commands: a comment")[2]
          and result("identity commands: an unnormalized home")["raised"] == "PathContainmentError"
          and value("identity commands: the quoting rebound on project_provision")[0] == "sudo install -d -m 0700 -o <p463-agent> -g <p463-agent> <HOME/.ssh>",
          "the identity commands: the .ssh, the key generated only when absent, modes, the block rewritten as the owner; every quote through project_provision")
    sel = value("selection commands: a named key")
    check(len(sel) == 1 and sel[0].startswith("sudo -u 'p463-agent' sh -c ") and "ssh-keygen" not in sel[0] and "chown" not in sel[0]
          and result("selection commands: no key name")["message"] == "a key name is required to select an identity"
          and result("selection commands: a separator")["raised"] == "PathContainmentError"
          and result("selection commands: the refusal class rebound on project_provision")["raised"] == "P463Refusal",
          "selecting: one command as the owner, no key created or chowned; a missing or separated name refused with project_provision's refusal class")
    hosts = {k: v["value"] for k, v in result("remote host: forms").items()}
    check(hosts == {"": "", "/data/git/x": "", "file:///srv/x.git": "", "../x.git": "", "./x": "", "~/x": "", "a/b:c": "", "git@GitHub.com:o/r.git": "github.com",
                    "ssh://git@github.com:22/o/r": "github.com", "https://GHE.example.com/o/r": "ghe.example.com", "github-p463:o/r": "github-p463", "host": "", "~host:x": "", "file://p463host/srv/x.git": ""},
          f"the remote host: local forms empty, scp and URL forms their lower-cased host: {hosts}")
    uses = {k: v["value"] for k, v in result("uses github: forms").items()}
    check(uses == {"": None, "/data/git/x": False, "git@github.com:o/r": True, "https://api.github.com/o/r": True, "git@gitlab.com:o/r": False,
                   "git@github-p463:o/r": True, "git@notgithub.com:o/r": False}
          and value("uses github: a recorded alias") is True and value("uses github: the default host rebound on project_provision") is True,
          f"GitHub: none without a remote, never local, github.com and its subdomains, a github- alias or the recorded alias: {uses}")
    rm = value("removal commands")
    check(len(rm) == 1 and "|| exit 0" in rm[0] and "grep -qx" in rm[0] and result("removal commands: an unnormalized home")["raised"] == "PathContainmentError",
          "removing: as the owner, nothing done without a config or without the block, only the block deleted")


def test_every_seam_is_reached() -> None:
    # Every function the twelve read on project_provision is a recorder there; the constants, the quoting and the refusal class are
    # rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = {"owner_github_key_path", "github_identity_block", "parse_managed_github_identity", "existing_owner_ssh_key_names", "publication_remote_host",
                 "_refuse_unnormalized"}
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_the_same_defaults",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packet")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_github_identity_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
