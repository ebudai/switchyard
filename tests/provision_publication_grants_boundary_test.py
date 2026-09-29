#!/usr/bin/env python3
"""SYRD-468: the provisioning publication grants, against project_provision they came out of.

The twelve -- the sudoers rule for the two root-owned publication programs
(`publish_sudoers_path`, `publish_sudoers_document`), where root keeps the
publication grant, key, pinned hosts and staging (`publish_grant_root`,
`publish_staging_root`, `DEFAULT_PUBLISH_GRANT_ROOT`,
`DEFAULT_PUBLISH_STAGING_ROOT`, `PUBLISH_GRANT_ROOT`, `PUBLISH_STAGING_ROOT`,
`PUBLISH_GRANT_SCHEMA`), and the commands that install the credential
(`publish_grant_path`, `publish_identity_path`, `publish_grant_commands`) --
moved unchanged into `scripts/ticket_board/provision_publication_grants.py`;
`project_provision` re-exports them all, in both branches of its import block,
and keeps `shell_quote`, `TENANT_CONTROL_ROOT` and the tenant-control and
role-control sudoers. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package. `PUBLISH_GRANT_ROOT` and
  `PUBLISH_STAGING_ROOT` are the very defaults, bound at definition time.
- **Seams (rule 24):** what they read of `project_provision` -- each other,
  the constants, `shell_quote` and `TENANT_CONTROL_ROOT` -- is read through it
  when they run, with the direct-script fallback, so a patch there reaches them;
  `tenant_publication_boundary` imports the sudoers path and document from
  `project_provision` when it runs, so a patch there is what it gets.
- **Readers:** every production module imports them from `project_provision`,
  as often as before; nothing in `project_provision` calls them.
- **The behaviour is the baseline's:** every path and root with the three
  environment overrides unset, set and blank, the sudoers document and the
  grant commands for plans built by `build_plan` for a synthetic owner.
  `GOLDEN` below was produced by running the BASELINE module's own definitions
  over the very cases embedded here (`gold468.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME,
  USER and COLUMNS, under umask 077, under several hash seeds and with another
  TMPDIR, locale and overrides in the environment.
- **The direct script answers what the package answers,** and the default,
  shaped and lean packets are byte-identical through both.

No real home, tenant, account, /etc or /var path is read or written: every
path is synthetic, and the builders only return paths and shell text. Spawns,
every exec, signals, account and group lookups and socket connections are
refused for each case.
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
from scripts.ticket_board import provision_publication_grants as m  # noqa: E402

CHECKS = 0
MOVED = ('publish_sudoers_path', 'publish_sudoers_document', 'DEFAULT_PUBLISH_GRANT_ROOT', 'DEFAULT_PUBLISH_STAGING_ROOT', 'publish_grant_root', 'publish_staging_root', 'PUBLISH_GRANT_ROOT', 'PUBLISH_STAGING_ROOT', 'PUBLISH_GRANT_SCHEMA', 'publish_grant_path', 'publish_identity_path', 'publish_grant_commands')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'publish_sudoers_document': {'TENANT_CONTROL_ROOT': 2},
    'publish_grant_root': {'DEFAULT_PUBLISH_GRANT_ROOT': 1},
    'publish_staging_root': {'DEFAULT_PUBLISH_STAGING_ROOT': 1},
    'publish_grant_path': {'publish_grant_root': 1},
    'publish_identity_path': {'publish_grant_root': 1},
    'publish_grant_commands': {'PUBLISH_GRANT_ROOT': 2, 'PUBLISH_GRANT_SCHEMA': 1, 'PUBLISH_STAGING_ROOT': 1, 'publish_grant_path': 1, 'publish_identity_path': 1, 'shell_quote': 11},
}
#: Measured on the baseline: every project_provision definition outside the twelve that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/publication_status.py': {'import publish_identity_path': 1}, 'scripts/switchyard-install-authority': {'import publish_sudoers_document': 2, 'import publish_sudoers_path': 2}, 'scripts/tenant_publication_boundary.py': {'import publish_sudoers_path': 2, 'import publish_sudoers_document': 1}, 'scripts/ticket_board/publication_boundary.py': {'import PUBLISH_GRANT_SCHEMA': 1, 'import publish_grant_path': 1, 'import publish_grant_root': 1, 'import publish_identity_path': 1, 'import publish_staging_root': 1}}
#: The BASELINE's own behaviour for the cases below (`gold468.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'sudoers path: environment unset': {'result': {'type': 'PosixPath', 'value': 'PATH /etc/sudoers.d/48-p468-publish'}, 'calls': {}},
    'sudoers path: environment set': {'result': {'type': 'PosixPath', 'value': 'PATH /p468/env/switchyard_sudoers_root/48-p468-publish'}, 'calls': {}},
    'sudoers path: environment blank': {'result': {'type': 'PosixPath', 'value': 'PATH /etc/sudoers.d/48-p468-publish'}, 'calls': {}},
    'grant root: environment unset': {'result': {'type': 'str', 'value': '/etc/switchyard/publish'}, 'calls': {}},
    'grant root: environment set': {'result': {'type': 'str', 'value': '/p468/env/switchyard_publish_root'}, 'calls': {}},
    'grant root: environment blank': {'result': {'type': 'str', 'value': '/etc/switchyard/publish'}, 'calls': {}},
    'staging root: environment unset': {'result': {'type': 'str', 'value': '/var/lib/switchyard/publish'}, 'calls': {}},
    'staging root: environment set': {'result': {'type': 'str', 'value': '/p468/env/switchyard_publish_staging_root'}, 'calls': {}},
    'staging root: environment blank': {'result': {'type': 'str', 'value': '/var/lib/switchyard/publish'}, 'calls': {}},
    'grant path: environment unset': {'result': {'type': 'str', 'value': '/etc/switchyard/publish/p468.json'}, 'calls': {'publish_grant_root': 1}},
    'grant path: environment set': {'result': {'type': 'str', 'value': '/p468/env/switchyard_publish_root/p468.json'}, 'calls': {'publish_grant_root': 1}},
    'grant path: environment blank': {'result': {'type': 'str', 'value': '/etc/switchyard/publish/p468.json'}, 'calls': {'publish_grant_root': 1}},
    'identity path: environment unset': {'result': {'type': 'str', 'value': '/etc/switchyard/publish/p468-publish-key'}, 'calls': {'publish_grant_root': 1}},
    'identity path: environment set': {'result': {'type': 'str', 'value': '/p468/env/switchyard_publish_root/p468-publish-key'}, 'calls': {'publish_grant_root': 1}},
    'identity path: environment blank': {'result': {'type': 'str', 'value': '/etc/switchyard/publish/p468-publish-key'}, 'calls': {'publish_grant_root': 1}},
    'sudoers path: a given root': {'result': {'type': 'PosixPath', 'value': 'PATH /p468/sudoers/48-p468-publish'}, 'calls': {}},
    'sudoers path: a given Path': {'result': {'type': 'PosixPath', 'value': 'PATH /p468/sudoers/48-p468-publish'}, 'calls': {}},
    'sudoers document': {'result': {'type': 'str', 'value': 'TEXT 471 chars sha256:08a16df10b69b74a'}, 'calls': {}},
    'sudoers document: another project and owner': {'result': {'type': 'str', 'value': 'TEXT 471 chars sha256:824d165ec9f1e895'}, 'calls': {}},
    'sudoers document: the control root rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 447 chars sha256:5a769bb4d6e62beb'}, 'calls': {}},
    'grant root: the default rebound on project_provision': {'result': {'type': 'str', 'value': '/p468/rebound-grant'}, 'calls': {}},
    'staging root: the default rebound on project_provision': {'result': {'type': 'str', 'value': '/p468/rebound-staging'}, 'calls': {}},
    'grant path: the root rebound on project_provision': {'result': {'type': 'str', 'value': '/p468/rebound-root/p468.json'}, 'calls': {'publish_grant_root rebound': 1}},
    'identity path: the root rebound on project_provision': {'result': {'type': 'str', 'value': '/p468/rebound-root/p468-publish-key'}, 'calls': {'publish_grant_root rebound': 1}},
    'grant commands: default': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'", "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'", "if ! sudo test -f '/etc/switchyard/publish/p468-publish-key'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard p468 publication' -f '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0600 '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0644 '/etc/switchyard/publish/p468-publish-key'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/etc/switchyard/publish/p468-publish-key'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/p468/home/.local/state/switchyard/projects/p468/control.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/etc/switchyard/publish/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/etc/switchyard/publish/known_hosts\' >/dev/null', "        sudo chmod 0644 '/etc/switchyard/publish/known_hosts'", '    fi', 'TEXT 689 chars sha256:d8f982a2928b3b3d', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /etc/switchyard/publish/p468.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote': 11}},
    'grant commands: a quoted commit store': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'", "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'", "if ! sudo test -f '/etc/switchyard/publish/p468-publish-key'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard p468 publication' -f '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0600 '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0644 '/etc/switchyard/publish/p468-publish-key'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/etc/switchyard/publish/p468-publish-key'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/p468/it\'"\'"\'s/store.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/etc/switchyard/publish/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/etc/switchyard/publish/known_hosts\' >/dev/null', "        sudo chmod 0644 '/etc/switchyard/publish/known_hosts'", '    fi', 'TEXT 689 chars sha256:d8f982a2928b3b3d', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /etc/switchyard/publish/p468.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote': 11}},
    'grant commands: pgu': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'", "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'", "if ! sudo test -f '/etc/switchyard/publish/pgu-publish-key'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard pgu publication' -f '/etc/switchyard/publish/pgu-publish-key'", "    sudo chmod 0600 '/etc/switchyard/publish/pgu-publish-key'", "    sudo chmod 0644 '/etc/switchyard/publish/pgu-publish-key'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for pgu.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/etc/switchyard/publish/pgu-publish-key'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/data/git/switchyard.git:/data/git/pgu.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/etc/switchyard/publish/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/etc/switchyard/publish/known_hosts\' >/dev/null', "        sudo chmod 0644 '/etc/switchyard/publish/known_hosts'", '    fi', 'TEXT 686 chars sha256:469eb64112773a41', 'else', "    echo 'switchyard: no remote is known for pgu; publication stays refused until one is pinned in /etc/switchyard/publish/pgu.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote': 11}},
    'grant commands: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['sudo install -d -m 0755 -o root -g root </etc/switchyard/publish>', 'sudo install -d -m 0755 -o root -g root </var/lib/switchyard/publish>', 'if ! sudo test -f </etc/switchyard/publish/p468-publish-key>; then', "    sudo ssh-keygen -q -t ed25519 -N '' -C <switchyard p468 publication> -f </etc/switchyard/publish/p468-publish-key>", '    sudo chmod 0600 </etc/switchyard/publish/p468-publish-key>', '    sudo chmod 0644 </etc/switchyard/publish/p468-publish-key>.pub', "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", '    sudo cat </etc/switchyard/publish/p468-publish-key>.pub', 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u <p468-agent> git --git-dir </p468/home/.local/state/switchyard/projects/p468/control.git> remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" </etc/switchyard/publish/known_hosts>; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a </etc/switchyard/publish/known_hosts> >/dev/null', '        sudo chmod 0644 </etc/switchyard/publish/known_hosts>', '    fi', 'TEXT 609 chars sha256:b63f41706140ad47', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /etc/switchyard/publish/p468.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote rebound': 11}},
    'grant commands: the roots and schema rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p468/pgr'", "sudo install -d -m 0755 -o root -g root '/p468/psr'", "if ! sudo test -f '/etc/switchyard/publish/p468-publish-key'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard p468 publication' -f '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0600 '/etc/switchyard/publish/p468-publish-key'", "    sudo chmod 0644 '/etc/switchyard/publish/p468-publish-key'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/etc/switchyard/publish/p468-publish-key'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/p468/home/.local/state/switchyard/projects/p468/control.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/p468/pgr/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/p468/pgr/known_hosts\' >/dev/null', "        sudo chmod 0644 '/p468/pgr/known_hosts'", '    fi', 'TEXT 659 chars sha256:a8b076160bdea7fd', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /etc/switchyard/publish/p468.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote': 11}},
    'grant commands: the key and grant paths rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'", "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'", "if ! sudo test -f '/p468/key-p468'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard p468 publication' -f '/p468/key-p468'", "    sudo chmod 0600 '/p468/key-p468'", "    sudo chmod 0644 '/p468/key-p468'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/p468/key-p468'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/p468/home/.local/state/switchyard/projects/p468/control.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/etc/switchyard/publish/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/etc/switchyard/publish/known_hosts\' >/dev/null', "        sudo chmod 0644 '/etc/switchyard/publish/known_hosts'", '    fi', 'TEXT 651 chars sha256:346933c127c050ab', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /p468/grant-p468.json.'", 'fi']}, 'calls': {'publish_grant_path rebound': 1, 'publish_identity_path rebound': 1, 'shell_quote': 11}},
    'grant commands: the grant root default is bound at definition': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'", "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'", "if ! sudo test -f '/p468/rebound-grant/p468-publish-key'; then", "    sudo ssh-keygen -q -t ed25519 -N '' -C 'switchyard p468 publication' -f '/p468/rebound-grant/p468-publish-key'", "    sudo chmod 0600 '/p468/rebound-grant/p468-publish-key'", "    sudo chmod 0644 '/p468/rebound-grant/p468-publish-key'.pub", "    echo 'switchyard: register the public key below with the forge as a WRITE key for p468.'", "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'", "    sudo cat '/p468/rebound-grant/p468-publish-key'.pub", 'fi', '# The destination is pinned in root-owned data. The project checkout names', '# a remote too, but the project account can rewrite that, and a role that', '# can choose the remote can aim a push at a server of its own.', 'PUBLISH_REMOTE="$(sudo -u \'p468-agent\' git --git-dir \'/p468/home/.local/state/switchyard/projects/p468/control.git\' remote get-url origin 2>/dev/null || true)"', 'if [ -n "$PUBLISH_REMOTE" ]; then', '    publish_host="${PUBLISH_REMOTE#*@}"', '    publish_host="${publish_host%%:*}"', '    publish_host="${publish_host%%/*}"', '    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" \'/etc/switchyard/publish/known_hosts\'; then', '        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a \'/etc/switchyard/publish/known_hosts\' >/dev/null', "        sudo chmod 0644 '/etc/switchyard/publish/known_hosts'", '    fi', 'TEXT 681 chars sha256:c01a64886fcf320e', 'else', "    echo 'switchyard: no remote is known for p468; publication stays refused until one is pinned in /p468/rebound-grant/p468.json.'", 'fi']}, 'calls': {'publish_grant_path': 1, 'publish_grant_root': 2, 'publish_identity_path': 1, 'shell_quote': 11}},
    'constants': {'result': {'type': 'list', 'value': ['/etc/switchyard/publish', '/var/lib/switchyard/publish', '/etc/switchyard/publish', '/var/lib/switchyard/publish', 'switchyard.publish-grant.v1']}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold468.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the seven and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Projects, owners and roots are synthetic (/p468/...);
# plans are built by `build_plan` for a synthetic owner. Nothing is run -- the builders only return paths and shell text.
# Each case sets the three environment overrides as it names them and restores them after. A case may rebind a
# `project_provision` name to show it is read there when the function runs.
ENV = ("SWITCHYARD_SUDOERS_ROOT", "SWITCHYARD_PUBLISH_ROOT", "SWITCHYARD_PUBLISH_STAGING_ROOT")
PLANS = {"default": {}, "a quoted commit store": {"commit_git_dir": "/p468/it's/store.git"}, "pgu": {"project": "pgu"}}
CASES = {
    **{f"{what}: environment {state}": {"call": call, "args": args, "env": state}
       for what, call, args in (("sudoers path", "publish_sudoers_path", ["p468"]), ("grant root", "publish_grant_root", []), ("staging root", "publish_staging_root", []),
                                ("grant path", "publish_grant_path", ["p468"]), ("identity path", "publish_identity_path", ["p468"]))
       for state in ("unset", "set", "blank")},
    "sudoers path: a given root": {"call": "publish_sudoers_path", "args": ["p468"], "kwargs": {"root": "/p468/sudoers"}, "env": "set"},
    "sudoers path: a given Path": {"call": "publish_sudoers_path", "args": ["p468"], "kwargs": {"root": "PATH /p468/sudoers"}},
    "sudoers document": {"call": "publish_sudoers_document", "args": ["p468", "p468-agent"]},
    "sudoers document: another project and owner": {"call": "publish_sudoers_document", "args": ["q468", "q468 agent"]},
    "sudoers document: the control root rebound on project_provision": {"call": "publish_sudoers_document", "args": ["p468", "p468-agent"], "rebind": {"TENANT_CONTROL_ROOT": "/p468/control"}},
    "grant root: the default rebound on project_provision": {"call": "publish_grant_root", "rebind": {"DEFAULT_PUBLISH_GRANT_ROOT": "/p468/rebound-grant"}},
    "staging root: the default rebound on project_provision": {"call": "publish_staging_root", "rebind": {"DEFAULT_PUBLISH_STAGING_ROOT": "/p468/rebound-staging"}},
    "grant path: the root rebound on project_provision": {"call": "publish_grant_path", "args": ["p468"], "rebind": {"publish_grant_root": True}},
    "identity path: the root rebound on project_provision": {"call": "publish_identity_path", "args": ["p468"], "rebind": {"publish_grant_root": True}},
    **{f"grant commands: {p}": {"call": "publish_grant_commands", "plan": p} for p in PLANS},
    "grant commands: the quoting rebound on project_provision": {"call": "publish_grant_commands", "plan": "default", "rebind": {"shell_quote": True}},
    "grant commands: the roots and schema rebound on project_provision": {"call": "publish_grant_commands", "plan": "default",
                                                                          "rebind": {"PUBLISH_GRANT_ROOT": "/p468/pgr", "PUBLISH_STAGING_ROOT": "/p468/psr", "PUBLISH_GRANT_SCHEMA": "p468.schema"}},
    "grant commands: the key and grant paths rebound on project_provision": {"call": "publish_grant_commands", "plan": "default", "rebind": {"publish_identity_path": True, "publish_grant_path": True}},
    "grant commands: the grant root default is bound at definition": {"call": "publish_grant_commands", "plan": "default", "rebind": {"DEFAULT_PUBLISH_GRANT_ROOT": "/p468/rebound-grant"}},
    "constants": {"call": None},
}
FUNCTIONS = ("publish_sudoers_path", "publish_sudoers_document", "publish_grant_root", "publish_staging_root", "publish_grant_path", "publish_identity_path", "publish_grant_commands")
PASSED = ("publish_grant_root", "publish_grant_path", "publish_identity_path", "shell_quote")
REBINDABLE = ("TENANT_CONTROL_ROOT", "DEFAULT_PUBLISH_GRANT_ROOT", "DEFAULT_PUBLISH_STAGING_ROOT", "PUBLISH_GRANT_ROOT", "PUBLISH_STAGING_ROOT", "PUBLISH_GRANT_SCHEMA")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import hashlib, os as _os
    from pathlib import Path as _P
    counts: dict = {}

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, _P):
            return "PATH " + str(value)
        if isinstance(value, str):
            return value if len(value) <= 400 else f"TEXT {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "build_plan")}
    env_saved = {n: _os.environ.get(n) for n in ENV}
    try:
        for n in ENV:
            state = spec.get("env", "unset")
            if state == "unset":
                _os.environ.pop(n, None)
            else:
                _os.environ[n] = f"/p468/env/{n.lower()}" if state == "set" else "   "
        args = [a for a in spec.get("args", [])]
        kwargs = {k: (_P(v[5:]) if isinstance(v, str) and v.startswith("PATH ") else v) for k, v in spec.get("kwargs", {}).items()}
        if "plan" in spec:
            base = {"project": "p468", "owner_user": "p468-agent", "owner_home": _P("/p468/home"), "port": 34468}
            args = [saved["build_plan"](**{**base, **PLANS[spec["plan"]]})]
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "shell_quote":
                t.shell_quote = lambda value: note("shell_quote rebound") or "<" + str(value) + ">"
            elif name == "publish_grant_root":
                t.publish_grant_root = lambda: note("publish_grant_root rebound") or "/p468/rebound-root"
            elif name == "publish_identity_path":
                t.publish_identity_path = lambda project: note("publish_identity_path rebound") or f"/p468/key-{project}"
            elif name == "publish_grant_path":
                t.publish_grant_path = lambda project: note("publish_grant_path rebound") or f"/p468/grant-{project}.json"
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = [saved["DEFAULT_PUBLISH_GRANT_ROOT"], saved["DEFAULT_PUBLISH_STAGING_ROOT"], saved["PUBLISH_GRANT_ROOT"], saved["PUBLISH_STAGING_ROOT"], saved["PUBLISH_GRANT_SCHEMA"]]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            try:
                got = fn(*args, **kwargs)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        for n, v in env_saved.items():
            if v is None:
                _os.environ.pop(n, None)
            else:
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


#: What the seven read on project_provision when they run, all its own.
KEPT = ("shell_quote", "TENANT_CONTROL_ROOT")
#: The five constants, as the baseline wrote them: (annotation, value). Two are the defaults, bound at definition time.
CONSTANT_TEXT = {
    "DEFAULT_PUBLISH_GRANT_ROOT": (None, "'/etc/switchyard/publish'"),
    "DEFAULT_PUBLISH_STAGING_ROOT": (None, "'/var/lib/switchyard/publish'"),
    "PUBLISH_GRANT_ROOT": (None, "DEFAULT_PUBLISH_GRANT_ROOT"),
    "PUBLISH_STAGING_ROOT": (None, "DEFAULT_PUBLISH_STAGING_ROOT"),
    "PUBLISH_GRANT_SCHEMA": (None, "'switchyard.publish-grant.v1'"),
}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The imports three of them make of their own, as the baseline wrote them.
OWN_IMPORTS = {"publish_sudoers_path": ["import os as _os"], "publish_grant_root": ["import os as _os"], "publish_staging_root": ["import os as _os"]}
#: The defaults, as the baseline wrote them.
DEFAULTS = {"publish_sudoers_path": ["None"], "publish_sudoers_document": [], "publish_grant_root": [], "publish_staging_root": [], "publish_grant_path": [],
            "publish_identity_path": [], "publish_grant_commands": []}
#: The packets rendered through the direct script and the package: the default plan, and two the plan shapes.
PACKET_VARIANTS = {
    "default": [],
    "shaped": ["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"],
    "lean": ["--no-include-designer", "--no-include-audit"],
}
#: One probe, run in the package and as the direct script: every path and document for a synthetic project and plan, with
#: the overrides unset and set -- as a digest.
GRANT_PROBE = (
    "import hashlib, json, os, pathlib\n"
    "env = ('SWITCHYARD_SUDOERS_ROOT', 'SWITCHYARD_PUBLISH_ROOT', 'SWITCHYARD_PUBLISH_STAGING_ROOT')\n"
    "plan = g['build_plan'](project='p468', owner_user='p468-agent', owner_home=pathlib.Path('/p468/home'), port=34468)\n"
    "out = []\n"
    "for value in (None, '/p468/env'):\n"
    "    [os.environ.pop(n, None) if value is None else os.environ.__setitem__(n, value) for n in env]\n"
    "    out += [str(g['publish_sudoers_path']('p468')), g['publish_grant_root'](), g['publish_staging_root'](), g['publish_grant_path']('p468'),\n"
    "            g['publish_identity_path']('p468'), g['publish_sudoers_document']('p468', 'p468-agent'), g['publish_grant_commands'](plan)]\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest())\n"
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
    result = python("import sys, scripts.ticket_board.provision_publication_grants as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_publication_grants", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_publication_grants"),
                  ("scripts.tenant_publication_boundary", "scripts.ticket_board.provision_publication_grants")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_publication_grants as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        "m.PUBLISH_GRANT_ROOT is m.DEFAULT_PUBLISH_GRANT_ROOT and m.PUBLISH_STAGING_ROOT is m.DEFAULT_PUBLISH_STAGING_ROOT, "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_publication_grants'] True True",
              f"{' then '.join(order)}: one object each, defined here; the roots the very defaults; nothing of project_provision bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.Path is Path and t.Path is m.Path, "the standard-library name is the module's own, the very object project_provision holds")


def test_a_patch_on_project_provision_reaches_the_publication_boundary() -> None:
    # tenant_publication_boundary imports the sudoers path and document from project_provision when it runs: a patch there
    # (as tenant_publication_boundary_boundary_test makes) is what it gets, and the module's own objects otherwise.
    source = ast.parse((ROOT / "scripts" / "tenant_publication_boundary.py").read_text(encoding="utf-8"))
    imports = [x for x in ast.walk(source) if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision")
               and {a.name for a in x.names} & {"publish_sudoers_path", "publish_sudoers_document"}]
    inside = {id(x) for fn in ast.walk(source) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
    check(imports and all(id(x) in inside for x in imports), "tenant_publication_boundary imports them from project_provision inside its functions, when they run")
    result = python("import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_publication_grants as m\n"
                    "saved = t.publish_sudoers_path\n"
                    "t.publish_sudoers_path = lambda project, *, root=None: 'PATCHED'\n"
                    "from scripts.ticket_board.project_provision import publish_sudoers_path as seen\n"
                    "t.publish_sudoers_path = saved\n"
                    "from scripts.ticket_board.project_provision import publish_sudoers_path as restored\n"
                    "print(seen('p468'), restored is m.publish_sudoers_path)")
    check(result.stdout.strip() == "PATCHED True", f"a patch on project_provision is what a call-time import sees; restored, it is the module's own: {result.stdout}{result.stderr[-400:]}")


def test_the_direct_script_answers_what_the_package_answers() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd468_script__'); "
                       "m = sys.modules['provision_publication_grants']; "
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + GRANT_PROBE)
    as_package = python("import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + GRANT_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("14 "),
          f"as a direct script: the script holds the module's own objects, and all fourteen answers are the package's, byte for byte: "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_publication_grants.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in node.body if isinstance(x, (ast.Import, ast.ImportFrom)) or (isinstance(x, ast.Try) and ast.unparse(x) != CALL_TIME_IMPORT
                                                                                                  and any(isinstance(y, (ast.Import, ast.ImportFrom)) for y in x.body))]
        if expected:
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and own == OWN_IMPORTS.get(name, []),
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback; its own imports the baseline's: {own}")
        else:
            check(own == OWN_IMPORTS.get(name, []) and not any(isinstance(x, ast.Try) and ast.unparse(x) == CALL_TIME_IMPORT for x in node.body),
                  f"{name}: reads nothing of project_provision; its own imports the baseline's: {own}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from pathlib import Path"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the five constants are the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the twelve, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_publication_grants" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_publication_grants" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the twelve, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # What they read on project_provision is defined there, or imported there, unaliased, in its import block.
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_publication_grants")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
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
    # Three synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd468-packet-")).resolve()
    try:
        for variant, extra in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p468", "--owner-user", "p468-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34468", "--output-dir", f"{work}/out", *extra]
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd468_script'); raise SystemExit(g['main']({argv!r}))")
                else:
                    probe = f"import scripts.ticket_board.project_provision as pp; raise SystemExit(pp.main({argv!r}))"
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
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
    def result(label):
        return GOLDEN[label]["result"]

    def value(label):
        return result(label)["value"]

    def calls(label):
        return GOLDEN[label]["calls"]

    check(value("sudoers path: environment unset") == value("sudoers path: environment blank") == "PATH /etc/sudoers.d/48-p468-publish"
          and value("sudoers path: environment set") == "PATH /p468/env/switchyard_sudoers_root/48-p468-publish"
          and value("sudoers path: a given root") == value("sudoers path: a given Path") == "PATH /p468/sudoers/48-p468-publish",
          "the sudoers path: 48-<project>-publish under the given root, else SWITCHYARD_SUDOERS_ROOT when set and not blank, else /etc/sudoers.d")
    check(value("grant root: environment unset") == value("grant root: environment blank") == "/etc/switchyard/publish"
          and value("grant root: environment set") == "/p468/env/switchyard_publish_root"
          and value("staging root: environment unset") == value("staging root: environment blank") == "/var/lib/switchyard/publish"
          and value("staging root: environment set") == "/p468/env/switchyard_publish_staging_root"
          and value("grant root: the default rebound on project_provision") == "/p468/rebound-grant"
          and value("staging root: the default rebound on project_provision") == "/p468/rebound-staging",
          "the grant and staging roots: the override when set and not blank, else the default, read through project_provision")
    check(value("grant path: environment unset") == "/etc/switchyard/publish/p468.json" and value("identity path: environment set") == "/p468/env/switchyard_publish_root/p468-publish-key"
          and value("grant path: the root rebound on project_provision") == "/p468/rebound-root/p468.json"
          and calls("identity path: the root rebound on project_provision") == {"publish_grant_root rebound": 1},
          "the grant and key: <project>.json and <project>-publish-key under the grant root, read through project_provision when they run")
    documents = [value(label) for label in ("sudoers document", "sudoers document: another project and owner", "sudoers document: the control root rebound on project_provision")]
    check(len(set(documents)) == 3 and documents[0].split(" sha256:")[0] == documents[1].split(" sha256:")[0],
          "the sudoers document names its project and owner: another four-letter project and owner give another document of the same length")
    check(documents[2].split(" sha256:")[0] != documents[0].split(" sha256:")[0],
          "the sudoers document names the programs under TENANT_CONTROL_ROOT, read through project_provision")
    default = value("grant commands: default")
    check(default[0] == "sudo install -d -m 0755 -o root -g root '/etc/switchyard/publish'" and default[1] == "sudo install -d -m 0755 -o root -g root '/var/lib/switchyard/publish'"
          and default[2] == "if ! sudo test -f '/etc/switchyard/publish/p468-publish-key'; then" and default[-1] == "fi"
          and value("grant commands: the roots and schema rebound on project_provision")[:2] == ["sudo install -d -m 0755 -o root -g root '/p468/pgr'", "sudo install -d -m 0755 -o root -g root '/p468/psr'"]
          and value("grant commands: the key and grant paths rebound on project_provision")[2] == "if ! sudo test -f '/p468/key-p468'; then"
          and value("grant commands: the quoting rebound on project_provision")[0] == "sudo install -d -m 0755 -o root -g root </etc/switchyard/publish>"
          and value("grant commands: a quoted commit store") != default and value("grant commands: pgu")[2] == "if ! sudo test -f '/etc/switchyard/publish/pgu-publish-key'; then",
          "the grant commands: the grant and staging roots, a key made only when absent, then the pinned remote -- every root, path and quote read through project_provision")
    bound = value("grant commands: the grant root default is bound at definition")
    check(bound[0] == default[0] and bound[2] == "if ! sudo test -f '/p468/rebound-grant/p468-publish-key'; then",
          "PUBLISH_GRANT_ROOT is the default bound at definition, while the key path follows publish_grant_root() when it runs")
    check(value("constants") == ["/etc/switchyard/publish", "/var/lib/switchyard/publish", "/etc/switchyard/publish", "/var/lib/switchyard/publish", "switchyard.publish-grant.v1"],
          "the roots, their bound copies and the grant schema")


def test_every_seam_is_reached() -> None:
    # Every function the seven read on project_provision is a recorder there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_a_patch_on_project_provision_reaches_the_publication_boundary", "test_the_direct_script_answers_what_the_package_answers",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_publication_grants_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
