#!/usr/bin/env python3
"""Installing the privileged boundary, and proving it is still the one installed.

Three files decide whether a catalogued action can run at all:

* the **helper**, the single root-owned program every action executes through;
* the **package** beside it, which the helper imports -- the catalogue, the
  identity walk and the pre-flight all live there, so it is every bit as
  privileged as the helper itself;
* the **policy**, which is what tells polkit that the helper may be run for a
  named action, and binds that action to the helper by `exec.path`.

All three are installed host-wide rather than per tenant. A per-project policy
would have to name per-project action ids, and then "may this host install a
shared release" would mean something different depending on which tenant asked.
The project is a *typed argument* to an action instead, re-derived against
root-owned registration state inside the helper.

`verify_installation` is the other half. An installed file whose owner or mode
has drifted is not the file that was reviewed, and polkit will execute it
regardless -- `exec.path` names a path, not a hash. So the front door checks
before it asks for privilege, and upgrade repairs. This is the same rule
SYRD-165 wrote for the pinned launcher: every directory on the path to an
exec'd program is part of the program.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct execution as a program
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ticket_board import privileged_actions
else:
    from . import privileged_actions

#: Root-owned, host-wide, and deliberately the same directory the per-tenant
#: staging directories sit under: it already exists, root already owns it, and
#: it is already on the path every role executes staged tooling through.
PRIVILEGED_ROOT = Path("/usr/local/lib/switchyard")
HELPER_NAME = "switchyard-privileged-helper"
#: The package the helper imports. It cannot collide with a tenant's staging
#: directory: a project slug may not contain an underscore, so no project can
#: ever be called `ticket_board`. `test_no_project_can_shadow_the_helpers_own_package`
#: asserts that rather than trusting it.
PACKAGE_NAME = "ticket_board"
POLICY_DIR = Path("/usr/share/polkit-1/actions")
POLICY_NAME = f"{privileged_actions.ACTION_NAMESPACE}.policy"

HELPER_MODE = 0o755
POLICY_MODE = 0o644
DIRECTORY_MODE = 0o755


def helper_path(root: Path = PRIVILEGED_ROOT) -> Path:
    return root / HELPER_NAME


#: Where the policy goes when the boundary is installed somewhere other than
#: its real home. Everything else about a staged render is redirectable -- the
#: staging root is overridable precisely so a suite can exercise the real
#: renderer without writing to the host -- and a policy path that stayed
#: absolute would escape that sandbox and try to write /usr/share as the test
#: user. It did, and that is what this constant is for.
SANDBOX_POLICY_DIR_NAME = "polkit-actions"


def roots_for(staging_root: Path | str | None) -> tuple[Path, Path]:
    """The boundary's install root and policy directory, for a given staging root.

    `None` means the real host: the root-owned directory the tenant staging
    directories already live under, and polkit's real action directory. Any
    other value is a redirected render -- a suite, or a dry run against a
    temporary tree -- and then the policy is redirected with it, because a
    render that writes some of its files into a sandbox and the rest into
    /usr/share is not a sandbox at all.
    """
    if staging_root is None:
        return PRIVILEGED_ROOT, POLICY_DIR
    root = Path(staging_root)
    return root, root / SANDBOX_POLICY_DIR_NAME


def policy_path(policy_dir: Path = POLICY_DIR) -> Path:
    return policy_dir / POLICY_NAME


@dataclass(frozen=True)
class Expectation:
    """One installed path, and what it has to be for the boundary to hold."""

    path: Path
    mode: int
    #: True when a *stricter* mode is acceptable. A helper installed 0700 still
    #: runs under pkexec, which execs it as root; a policy narrower than 0644
    #: only stops polkit reading it, so that one is exact.
    allow_stricter: bool = False
    must_be_dir: bool = False


def expectations(
    root: Path = PRIVILEGED_ROOT, policy_dir: Path = POLICY_DIR
) -> tuple[Expectation, ...]:
    return (
        Expectation(root, DIRECTORY_MODE, must_be_dir=True),
        Expectation(helper_path(root), HELPER_MODE, allow_stricter=True),
        Expectation(root / PACKAGE_NAME, DIRECTORY_MODE, must_be_dir=True),
        Expectation(policy_path(policy_dir), POLICY_MODE),
    )


def verify_installation(
    root: Path = PRIVILEGED_ROOT,
    policy_dir: Path = POLICY_DIR,
    *,
    lstat: Callable[[Path], os.stat_result] = os.lstat,
) -> list[str]:
    """What is wrong with the installed boundary, in the order it is reached.

    `lstat`, never `stat`: a symlink where the helper should be is a redirect
    somebody installed, and resolving it would report on the target while
    polkit executes through the link. The same reason every directory above it
    is checked -- a writable parent is a writable program (SYRD-165).
    """
    problems: list[str] = []
    for expectation in expectations(root, policy_dir):
        try:
            info = lstat(expectation.path)
        except FileNotFoundError:
            problems.append(f"{expectation.path} is not installed")
            continue
        except OSError as exc:
            problems.append(f"{expectation.path} cannot be examined ({exc})")
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(
                f"{expectation.path} is a symbolic link, so what runs is decided "
                "somewhere other than where it was installed"
            )
            continue
        if expectation.must_be_dir and not stat.S_ISDIR(info.st_mode):
            problems.append(f"{expectation.path} is not a directory")
            continue
        if not expectation.must_be_dir and not stat.S_ISREG(info.st_mode):
            problems.append(f"{expectation.path} is not a regular file")
            continue
        if info.st_uid != 0 or info.st_gid != 0:
            problems.append(
                f"{expectation.path} is owned by uid {info.st_uid}:{info.st_gid} rather than "
                "root:root, so its owner decides what root runs"
            )
        actual = stat.S_IMODE(info.st_mode)
        if actual & ~expectation.mode:
            problems.append(
                f"{expectation.path} is mode {actual:04o}, wider than the {expectation.mode:04o} "
                "it is installed with"
            )
        elif actual != expectation.mode and not expectation.allow_stricter:
            problems.append(
                f"{expectation.path} is mode {actual:04o} rather than {expectation.mode:04o}"
            )
    return problems


def _quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def install_commands(
    release_root: str,
    *,
    root: Path = PRIVILEGED_ROOT,
    policy_dir: Path = POLICY_DIR,
    sudo: str = "sudo ",
) -> list[str]:
    """Install or repair the boundary from a selected release.

    Re-runnable by construction: `install` rewrites to exactly these bytes with
    exactly this owner and mode, so a drifted helper, a drifted policy and a
    missing one are all the same repair. That is why upgrade can call this
    unconditionally rather than deciding first whether anything is wrong.

    `-o root -g root` on every line. The existing polkit install line omits it
    and relies on the ambient root of the operator script; that works today and
    would stop working the moment anything runs these commands with a different
    effective group, which is exactly the kind of silent drift this ticket is
    about.
    """
    helper_source = f"{release_root}/scripts/{PACKAGE_NAME}/privileged_helper.py"
    package_source = f"{release_root}/scripts/{PACKAGE_NAME}"
    target_package = root / PACKAGE_NAME
    policy_target = policy_path(policy_dir)
    helper_target = helper_path(root)
    # Staged if the selected release carries it, and TAKEN AWAY if it does not.
    #
    # The release being staged is whichever one was selected, and that is not
    # always this one: a rollback deliberately selects an older release, and an
    # older release has no privileged helper at all. An unguarded install fails
    # there, and failing the staging step blocks the way back at the moment it
    # is needed -- which is the SYRD-93 failure this file would otherwise
    # reintroduce. Worse, leaving a newer helper and policy installed while
    # rolling the code back would mean polkit still authorizing a helper the
    # running release does not know about, so the removal is the safe half as
    # much as the install is.
    return [
        "# The bounded privileged action boundary: one root-owned helper, the",
        "# package it imports, and the polkit catalogue that binds them (SYRD-112).",
        "# Installed when the selected release carries it, removed when it does",
        "# not, so selecting an older release stays possible.",
        f"if [ -f {_quote(helper_source)} ]; then",
        f"    {sudo}install -d -m {DIRECTORY_MODE:04o} -o root -g root {_quote(str(root))}",
        f"    {sudo}install -m {HELPER_MODE:04o} -o root -g root {_quote(helper_source)} "
        f"{_quote(str(helper_target))}",
        # Replaced wholesale rather than merged: a module this release dropped
        # would otherwise stay importable, and the helper imports by name.
        f"    {sudo}rm -rf {_quote(str(target_package))}",
        f"    {sudo}cp -a {_quote(package_source)} {_quote(str(target_package))}",
        f"    {sudo}chown -R root:root {_quote(str(target_package))}",
        f"    {sudo}chmod -R a+rX,go-w {_quote(str(target_package))}",
        f"    {sudo}install -d -m {DIRECTORY_MODE:04o} -o root -g root {_quote(str(policy_dir))}",
        # Rendered here rather than shipped as a packet companion, and piped
        # through `install` so it lands with its owner and mode in one step.
        # The policy and the catalogue it describes then cannot drift apart:
        # there is only one place the action ids, their authentication and the
        # helper path come from, and it is the same code the suite exercises.
        f"    printf '%s' {_quote(rendered_policy(root))} "
        f"| {sudo}install -m {POLICY_MODE:04o} -o root -g root /dev/stdin "
        f"{_quote(str(policy_target))}",
        "else",
        f"    {sudo}rm -f {_quote(str(helper_target))} {_quote(str(policy_target))}",
        f"    {sudo}rm -rf {_quote(str(target_package))}",
        "fi",
    ]


def rendered_policy(root: Path = PRIVILEGED_ROOT) -> str:
    """The policy this release would install, bound to this release's helper."""
    return privileged_actions.render_policy(str(helper_path(root)))
