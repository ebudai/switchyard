#!/usr/bin/env python3
"""Tell each registered tenant's Director, once, that a new release is ready (SYRD-799).

The User, 2026-11-15: "what do you think about the ./install script automatically informing all active directors that
a new version is ready for them to upgrade to? I find its a lot going to each one and telling them by hand". Each
tenant stays pinned to the release it last deployed, so `sudo switchyard upgrade <tenant>` changes nothing; its
Director must run the pinned privileged actions with the new commit. ./install ends with this, as root.

For each tenant root's provision records name and root's registry registers (`database_auth_transition.root_tenants`,
which reads only files root owns):
* its current release is the one its board unit runs, as root proves it (`board_identity.effective_unit`): never a
  tenant-configured path or URL;
* a tenant already on the installed release is skipped; one whose board cannot be proved is listed as not told, with
  the reason, and nothing about the install fails;
* any other is told through its own board: `ticket_board.note_release_available`, the one write root makes, which only
  the database superuser may run, on the database root's provision record names (`runuser -u postgres`). The board
  records each release it was told and queues one Director notice, delivered as any Director notice is; a rerun sends
  nothing.

The notice names both commits, the commit subjects between them (capped), the three privileged actions with
`commit=<new>`, and `switchyard release-status <tenant>`. Nothing is upgraded, restarted or deployed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

# Run as root from the operator's checkout: it never writes that tree's bytecode (the release bytecode boundary).
sys.dont_write_bytecode = True

COMMIT = re.compile(r"^[0-9a-f]{40}$")
SOCKET_DIR = "/var/run/postgresql"
SHARED_ROOT = Path("/opt/switchyard")
MAX_SUBJECTS = 20
MAX_SUBJECT_CHARS = 140
MAX_MESSAGE_CHARS = 3800
TIMEOUT_SECONDS = 30


def installed_release(shared_root: Path = SHARED_ROOT) -> str:
    """The release ./install just installed: the commit `current` names under root's shared release root."""
    name = Path(os.readlink(shared_root / "current")).name
    if not COMMIT.fullmatch(name):
        raise ValueError(f"{shared_root / 'current'} names {name!r}, not an exact release")
    return name


def changes(repo: Path, current: str, new: str, *, user: str = "",
            runner: Callable[..., Any] = subprocess.run) -> list[str]:
    """The subjects of the commits from `current` to `new` in the operator's checkout, newest first; [] when that
    history is not there. Read as the checkout's owner, so root never runs git over a tree it does not own."""
    command = ["git", "-C", str(repo), "log", "--no-merges", "--format=%s", f"{current}..{new}"]
    if user and user != "root" and os.geteuid() == 0:
        command = ["runuser", "-u", user, "--", *command]
    try:
        done = runner(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in str(done.stdout).splitlines() if line.strip()]


def message(project: str, current: str, new: str, subjects: Sequence[str]) -> str:
    """The Director's notice: self-contained, so it can be acted on without asking anyone."""
    shown = [f"- {subject[:MAX_SUBJECT_CHARS]}" for subject in subjects[:MAX_SUBJECTS]]
    if len(subjects) > MAX_SUBJECTS:
        shown.append(f"- ... and {len(subjects) - MAX_SUBJECTS} more")
    what = (f"What changed ({len(subjects)} commit{'s' if len(subjects) != 1 else ''}):\n" + "\n".join(shown)
            if subjects else "What changed: the commits between them are not in the installing checkout.")
    commands = "\n".join(f"  switchyard privileged-action {project} {action} commit={new}"
                         for action in ("preview-upgrade", "upgrade-tenant-release", "deploy-release"))
    text = (f"Release {new} is installed on this host and ready for {project}, which runs {current}. Nothing has been "
            "upgraded: when you choose, preview, upgrade and deploy it:\n"
            f"{commands}\nthen check with `switchyard release-status {project}`.\n{what}")
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS - 3] + "..."


def tell(database: str, new: str, current: str, text: str, *, runner: Callable[..., Any] = subprocess.run) -> str:
    """`note_release_available` on `database`, as the database superuser; its answer. The values go in as psql
    variables (`:'name'`), quoted by psql, never spliced into the statement."""
    command = ["runuser", "-u", "postgres", "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
               "PGPASSFILE=/dev/null", "PGSERVICEFILE=/dev/null", "psql", "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1",
               "-v", f"commit={new}", "-v", f"current={current}", "-v", f"notice={text}",
               f"host={SOCKET_DIR} dbname={database}", "-f", "-"]
    done = runner(command, input="SELECT ticket_board.note_release_available(:'commit', :'current', :'notice');\n",
                  capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
    if done.returncode != 0:
        error = str(done.stderr or "")
        if "note_release_available" in error and "does not exist" in error:
            # Its release predates pgu1046, which arrives with its next deploy: this release is the one to relay.
            raise RuntimeError("its board predates release notices: tell it of this release yourself; once it deploys "
                               "it, later releases are announced to its Director")
        raise RuntimeError(error.strip()[-300:] or "psql failed")
    return str(done.stdout).strip()


def run(*, new: str, repo: Path, user: str, tenants: Callable[[], tuple[list[Any], list[str]]],
        registered: Callable[[str], bool], current_release: Callable[[str], str],
        tell_board: Callable[[str, str, str, str], str] = tell,
        subjects_for: Callable[[str, str], list[str]] | None = None) -> str:
    """Tell every registered tenant behind `new`; the one line ./install ends with."""
    told, skipped, not_told = [], [], []
    found, problems = tenants()
    not_told += [problem for problem in problems]
    for tenant in found:
        project, database = tenant.project, tenant.database
        if not registered(project):
            continue
        try:
            current = current_release(project)
        except Exception as exc:  # each tenant on its own: one unreachable board stops nothing
            not_told.append(f"{project} ({str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__})")
            continue
        if current == new:
            skipped.append(project)
            continue
        if not COMMIT.fullmatch(current or ""):
            not_told.append(f"{project} (its board runs no exact release: {current!r})")
            continue
        subjects = (subjects_for or (lambda old, fresh: changes(repo, old, fresh, user=user)))(current, new)
        try:
            answer = tell_board(database, new, current, message(project, current, new, subjects))
        except Exception as exc:
            not_told.append(f"{project} ({str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__})")
            continue
        if answer == "told":
            told.append(project)
        elif answer == "already":
            skipped.append(f"{project} (already told)")
        else:
            not_told.append(f"{project} ({'no ticket to carry a Director notice' if answer == 'no_anchor' else answer})")
    line = f"switchyard: told {len(told)} Director{'s' if len(told) != 1 else ''} release {new[:12]} is ready"
    line += f": {', '.join(told)}" if told else ""
    line += f"; skipped {', '.join(skipped)} (already on it or told)" if skipped else ""
    line += f"; not told: {'; '.join(not_told)}" if not_told else ""
    return line


def main(argv: Sequence[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--source-user", default="")
    parser.add_argument("--shared-root", default=str(SHARED_ROOT), type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("switchyard: no Director told of the new release: only root can tell them (rerun ./install with sudo)")
        return 0
    from scripts import database_auth_transition as transition
    from scripts.privileged_provision_records import switchyard_privileged_provision_root
    from scripts.ticket_board.board_identity import effective_unit

    try:
        new = installed_release(args.shared_root)
    except (OSError, ValueError) as exc:
        print(f"switchyard: no Director told of the new release: {exc}")
        return 0
    registry = transition.REGISTRY
    print(run(new=new, repo=args.source_repo, user=args.source_user,
              tenants=lambda: transition.root_tenants(provision_root=switchyard_privileged_provision_root(),
                                                      registry=registry),
              registered=lambda project: (registry / f"{project}.json").is_file(),
              current_release=lambda project: effective_unit(project).release_commit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
