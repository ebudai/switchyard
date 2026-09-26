#!/usr/bin/env python3
"""What each catalogued action actually runs, once it has been allowed.

The catalogue in `privileged_actions` describes the *caller's* surface: names
and typed values. This describes the root side: for each of those names, one
fixed program and a fixed flag shape, with the validated values substituted
into declared positions and nowhere else.

Kept apart from the catalogue on purpose. The catalogue is the file an auditor
reads to answer "what can be asked for"; it carries no program, no path and no
argv, and a table-wide test asserts that it never will. This file answers the
other question -- "and then what runs" -- and its own property is just as
narrow: every argv begins with the pinned launcher, and every element after it
is either a literal from this file or a value that already passed the
catalogue's validator.

The launcher is the shared release's, not the tenant's deployed board. A
tenant's tree lives under its owner's home, where the owner can replace the
`current` symlink or any directory above it -- so the deployed copy is not a
pinned entrypoint however root-owned its last component happens to be. Same
reasoning as `TENANT_CONTROL_LAUNCHER` (SYRD-50 review).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Callable, Mapping

#: Root-owned for its whole length, and the only program any action runs.
_INSTALL_ROOT = "/opt/switchyard"
SHARED_RELEASE_CURRENT = f"{_INSTALL_ROOT}/current"
LAUNCHER = f"{SHARED_RELEASE_CURRENT}/switchyard"


#: Where root keeps the releases it built itself, one directory per commit.
#: Nothing outside this file contributes to the path: the caller supplies the
#: commit, and the commit has already been proved to be forty hex characters,
#: so there is no traversal to express and nowhere else for it to point.
SHARED_RELEASES = f"{_INSTALL_ROOT}/releases"

#: Who a trusted release must belong to. Root, because the helper runs as root
#: and a release owned by anybody else is a release somebody else chose. It is
#: a module-level default rather than a parameter threaded through every
#: operation, so a suite can point the whole module at a tree it can write --
#: and `test_the_shipped_default_demands_root` asserts what ships.
TRUSTED_OWNER_UID = 0


def _deploy_release(values: Mapping[str, str]) -> list[str]:
    # `--deploy-ref` takes the validated 40-character commit, so the release
    # that runs is the release that was approved rather than whatever a branch
    # points at by the time root reads it.
    return [LAUNCHER, "upgrade", values["project"], "--deploy-ref", values["commit"]]


def _upgrade_tenant(values: Mapping[str, str]) -> list[str]:
    return [LAUNCHER, "upgrade", values["project"]]


def _preview_upgrade(values: Mapping[str, str]) -> list[str]:
    # Always --dry-run: this action cannot be turned into the upgrade itself.
    return [LAUNCHER, "upgrade", values["project"], "--deploy-ref", values["commit"], "--dry-run"]


def _upgrade_tenant_release(values: Mapping[str, str]) -> list[str]:
    return [LAUNCHER, "upgrade", values["project"], "--deploy-ref", values["commit"]]


#: The file a built release carries, naming the commit it was built from.
#: The same name `team_launcher.SWITCHYARD_RELEASE_MARKER_NAME` writes;
#: `test_the_marker_name_matches_what_the_build_writes` keeps the two honest.
RELEASE_MARKER_NAME = ".switchyard-release.json"


class NoTrustedSource(Exception):
    """No root-controlled source holds this commit. Raised BEFORE any privilege."""


def trusted_release_root(
    commit: str,
    *,
    releases: str | None = None,
    lstat: Callable[[str], object] = os.lstat,
    read_marker: Callable[[str], str] | None = None,
    owner_uid: int | None = None,
) -> str:
    """Root's own copy of this exact release, or a refusal that says what is missing.

    The whole difficulty of the shared-release action is that today's installer
    takes the operator's checkout as a source. A catalogued action may not: a
    caller-supplied path to a tree root would then read is precisely the
    payload this boundary removes, and a full sha does not make a caller's tree
    safe -- `refs/replace` and repository config can both redirect what root
    executes (SYRD-97 review).

    So the commit is resolved against root's own release cache instead, and it
    has to survive three checks, every one of which fails closed:

    1. the release directory for that exact commit exists under root's cache,
       which is a path this file builds and the caller only ever contributes
       forty validated hex characters to;
    2. it is a real directory, not a symlink -- otherwise what root installs is
       decided by whoever planted the link, not by the commit;
    3. its own release marker records that same commit, so the directory's
       *name* is not the evidence. A directory named after a commit is a claim;
       the marker inside it is what the build wrote.

    When no trusted source holds it, this raises rather than falling back to
    anything the caller named. The build half genuinely cannot be catalogued,
    so the honest outcome is an explicit refusal naming the operator packet
    that produces the release -- not a silently missing action, and not a path
    argument dressed up as a commit.
    """
    releases = SHARED_RELEASES if releases is None else releases
    owner_uid = TRUSTED_OWNER_UID if owner_uid is None else owner_uid
    root = f"{releases}/{commit}"
    marker = f"{root}/{RELEASE_MARKER_NAME}"
    try:
        info = lstat(root)
    except OSError:
        raise NoTrustedSource(
            f"no trusted source on this host holds release {commit}: {root} does not exist. "
            "Root builds a release from a bundle the operator produces in their own "
            "repository, and that step cannot be a catalogued action because it would "
            "have to take that repository as an argument. Run the shared-release install "
            f"packet for {commit} first, then ask for this action again"
        ) from None
    if stat.S_ISLNK(getattr(info, "st_mode", 0)):
        raise NoTrustedSource(
            f"{root} is a symbolic link, so what would be installed is decided somewhere "
            "other than root's own release cache"
        )
    if not stat.S_ISDIR(getattr(info, "st_mode", 0)):
        raise NoTrustedSource(f"{root} is not a release directory")
    # Root by default, because that is what the helper runs as. A suite that
    # must build a release tree it can write overrides `TRUSTED_OWNER_UID`, and
    # `test_the_shipped_default_demands_root` pins the shipped value so the
    # branch that actually runs is not the one no case ever exercises.
    if getattr(info, "st_uid", -1) != owner_uid:
        raise NoTrustedSource(
            f"{root} is owned by uid {getattr(info, 'st_uid', -1)} rather than "
            f"uid {owner_uid}, so it is not a root-controlled source"
        )
    reader = read_marker or _read_marker_commit
    recorded = reader(marker)
    if recorded != commit:
        raise NoTrustedSource(
            f"{root} records commit {recorded or 'nothing'}, not {commit}; a directory named "
            "after a commit is a claim, and the marker the build wrote is the evidence"
        )
    return root


def _read_marker_commit(marker: str) -> str:
    try:
        payload = json.loads(Path(marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((payload or {}).get("commit") or "").strip()


class NotExecutableYet(Exception):
    """Catalogued, pre-flown, and honestly not runnable. Never a silent success.

    Nothing raises this today. It stays because the distinction it draws is
    the one that matters when an action cannot be wired: a catalogued action
    whose execution is missing must refuse loudly rather than quietly do
    nothing, and the front door and helper both still handle it that way.
    """


def _install_shared_release(values: Mapping[str, str]) -> list[str]:
    """Make an already-built release this host's current one, by commit alone.

    The trusted-source resolution runs first and is the reason a commit is
    sufficient: it proves the release is in root's own cache, is not a symlink,
    is root-owned, and carries a marker recording that same commit. Nothing the
    caller supplied reaches the argv below except the forty hex characters.

    The activation itself is `switchyard install-shared-release --commit`,
    which records what `current` pointed at before it moves anything, swaps the
    pointer atomically, verifies what landed, and puts the previous target back
    if it does not verify. That sequence is why this is a bounded action rather
    than the bare `ln -sfn` the operator packet used to carry: an interrupted
    `ln -sfn` leaves no `current` at all and no note of what it had been.
    """
    commit = values["commit"]
    # Resolved here as well as inside the command, so a commit no trusted
    # source holds is refused before privilege is requested rather than after.
    trusted_release_root(commit)
    return [LAUNCHER, "install-shared-release", "--commit", commit]


def _select_shared_release(values: Mapping[str, str]) -> list[str]:
    """Point one tenant at a shared release root already has, by commit.

    The last line of `shared_release_install_commands` -- the durable
    re-selection -- whose every input is either a commit or root's own
    directory. Resolved through the same trusted-source check, so a tenant
    cannot be pointed at a release this host never built.
    """
    commit = values["commit"]
    release = trusted_release_root(commit)
    return [
        LAUNCHER,
        "upgrade",
        values["project"],
        "--source-repo",
        release,
        "--deploy-ref",
        commit,
    ]


def _repair_boundary(values: Mapping[str, str]) -> list[str]:
    return [LAUNCHER, "repair-boundary", values["project"], "--apply"]


#: One entry per catalogued action. `privileged_operations_test` asserts the
#: two tables have exactly the same names, so an action added to the catalogue
#: without an operation here is a failing test rather than a helper that
#: authorizes something and then does nothing.
OPERATIONS: dict[str, Callable[[Mapping[str, str]], list[str]]] = {
    "deploy-release": _deploy_release,
    "upgrade-tenant": _upgrade_tenant,
    "preview-upgrade": _preview_upgrade,
    "upgrade-tenant-release": _upgrade_tenant_release,
    "install-shared-release": _install_shared_release,
    "select-shared-release": _select_shared_release,
    "repair-boundary": _repair_boundary,
}


def command_for(action_name: str, values: Mapping[str, str]) -> list[str]:
    """The exact argv for an action whose values have already been validated.

    Raises rather than falling back: an action with no operation must not
    silently become a no-op that reports success.
    """
    try:
        build = OPERATIONS[action_name]
    except KeyError:
        raise KeyError(
            f"{action_name!r} is catalogued but has no privileged operation, so there is "
            "nothing for it to run"
        ) from None
    return build(values)
