#!/usr/bin/env python3
"""Which ticket-board databases this host was installed for (SYRD-642).

`./install` asks before it plans or installs anything:
* `postgresql`: a local PostgreSQL cluster, as every host had before;
* `oracle`: a remote Oracle database, reached with python-oracledb;
* `both`.
`install-switchyard-prereqs` installs only what the selection needs, then
records it here, in `/etc/switchyard/databases.json` (root's, mode 0644):

    {"databases": ["oracle"]}

Readers:
* **A host with no record** was installed before the choice existed, so it has
  PostgreSQL, as it always did.
* **Capabilities are only added.** `./install --add-database postgresql` adds
  one later without reinstalling Switchyard. Re-running `./install` with
  another selection never forgets what is already installed. A host installed
  before the record existed counts as having PostgreSQL the first time a
  record is written.

`switchyard new` reads it before asking anything. A host without PostgreSQL
is told how to add it, before any PostgreSQL service, socket or database is
probed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

POSTGRESQL, ORACLE = "postgresql", "oracle"
CAPABILITIES = (POSTGRESQL, ORACLE)
SELECTIONS = {POSTGRESQL: (POSTGRESQL,), ORACLE: (ORACLE,), "both": (POSTGRESQL, ORACLE)}
RECORD_ENV = "SWITCHYARD_DATABASE_RECORD"
DEFAULT_RECORD = Path("/etc/switchyard/databases.json")
#: What a host has when nothing is recorded: everything before SYRD-642 installed PostgreSQL.
LEGACY = (POSTGRESQL,)
INSTALLER = Path(__file__).resolve().parents[1] / "install"


class CapabilityError(ValueError):
    """The selection or the record is unusable."""


def record_path() -> Path:
    return Path(os.environ.get(RECORD_ENV) or DEFAULT_RECORD)


ALIASES = {"postgresql": POSTGRESQL, "postgres": POSTGRESQL, "oracle": ORACLE, "both": "both"}


def parse_selection(value: str) -> tuple[str, ...]:
    """`postgresql`, `oracle` or `both` (also `postgres`, and `postgresql,oracle` in either order)."""
    words = [w.strip().lower() for w in str(value).replace(" ", ",").split(",") if w.strip()]
    if not words or any(w not in ALIASES for w in words) or ("both" in words and len(words) > 1):
        raise CapabilityError(f"database selection must be postgresql, oracle or both, not {value!r}")
    chosen = {name for w in words for name in SELECTIONS[ALIASES[w]]}
    return tuple(name for name in CAPABILITIES if name in chosen)


def _read(path: Path) -> tuple[str, ...] | None:
    """The recorded capabilities, or None when there is no record."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):  # lstat: a link is not a regular file
        raise CapabilityError(f"{path} must be a regular file, not a link")
    try:
        names = json.loads(path.read_text())["databases"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CapabilityError(f"{path} is not a database capability record: {exc}") from None
    if not isinstance(names, list) or not names or set(names) - set(CAPABILITIES):
        raise CapabilityError(f"{path} must list databases from {', '.join(CAPABILITIES)}")
    return tuple(name for name in CAPABILITIES if name in names)


def installed(path: Path | None = None) -> tuple[str, ...]:
    """What this host was installed for; a host with no record has PostgreSQL."""
    found = _read(path or record_path())
    return LEGACY if found is None else found


def record(selected: tuple[str, ...], *, path: Path | None = None, prior_install: bool = False) -> tuple[str, ...]:
    """Add `selected` to the record, written atomically (root's, 0644); returns what the host now has.

    `prior_install`: Switchyard was installed here before this run, so a missing record means PostgreSQL.
    """
    path = path or record_path()
    existing = _read(path)
    if existing is None:
        existing = LEGACY if prior_install else ()
    now = tuple(name for name in CAPABILITIES if name in set(existing) | set(selected))
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps({"databases": list(now)}) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return now


def new_project_refusal(path: Path | None = None) -> str:
    """Why `switchyard new` cannot provision a board on this host, or '' when it can."""
    try:
        have = installed(path)
    except CapabilityError as exc:
        return str(exc)
    if POSTGRESQL in have:
        return ""
    return ("this host was installed for the Oracle database only, so a board on local PostgreSQL cannot be "
            "provisioned here. Nothing was asked or changed. To add PostgreSQL, without reinstalling "
            f"Switchyard: sudo {INSTALLER} --add-database postgresql. Or create the board on Oracle: "
            "SWITCHYARD_NEW_DATABASE=oracle (docs/syrd-642-oracle-backend.md).")


def new_project_database(have: tuple[str, ...], *, interactive: bool, ask=input) -> str:
    """Which database this new project's board uses: SWITCHYARD_NEW_DATABASE, or asked, or the one there is."""
    from scripts.ticket_board.provision_oracle import NEW_DATABASE_ENV

    given = os.environ.get(NEW_DATABASE_ENV, "").strip()
    if given:
        chosen = parse_selection(given)
        if len(chosen) != 1:
            raise CapabilityError(f"{NEW_DATABASE_ENV} names one database for one board: postgresql or oracle")
        return chosen[0]
    if len(have) == 1:
        return have[0]
    if not interactive:
        return POSTGRESQL  # a host with both, nobody to ask, nothing given: what `switchyard new` always did
    while True:
        answer = ask("Board database for this project: 1) PostgreSQL on this host  2) Oracle, remote [1]: ").strip()
        if answer in ("", "1", "postgresql"):
            return POSTGRESQL
        if answer in ("2", "oracle"):
            return ORACLE
        print("Answer 1 or 2.")


def require_new_project_capability(args: object = None, *, interactive: bool | None = None, ask=input,
                                   ask_secret=None, path: Path | None = None) -> str:
    """`switchyard new`'s first step: which database the board uses, and, for Oracle, its connection.

    It stops, saying why, before any other question: on a host without the chosen database's support, and for
    an Oracle board without a declared workflow, or whose endpoint or accounts do not check out. For Oracle
    the connection is staged root-only for provisioning to adopt, and nothing probes local PostgreSQL after.
    """
    import getpass

    try:
        have = installed(path)
        interactive = (sys.stdin.isatty() and sys.stdout.isatty() and not getattr(args, "yes", False)
                       if interactive is None else interactive)
        chosen = new_project_database(have, interactive=interactive, ask=ask)
    except CapabilityError as exc:
        raise SystemExit(f"switchyard new: {exc}") from None
    if chosen not in have:
        if chosen == POSTGRESQL:
            raise SystemExit(f"switchyard new: {new_project_refusal(path)}")
        raise SystemExit("switchyard new: this host was not installed for the Oracle database. Nothing was asked or "
                         f"changed. To add it, without reinstalling Switchyard: sudo {INSTALLER} --add-database oracle")
    if chosen == POSTGRESQL:
        return chosen
    return _prepare_oracle_board(args, interactive=interactive, ask=ask, ask_secret=ask_secret or getpass.getpass)


def _prepare_oracle_board(args: object, *, interactive: bool, ask, ask_secret) -> str:
    """Collect, stage and check an Oracle board's connection, before anything else is asked."""
    import shutil

    from scripts import team_launcher as launcher
    from scripts.ticket_board import oracle_lifecycle, provision_oracle

    if not getattr(args, "workflow_config", None) and not getattr(args, "from_artifact", None):
        raise SystemExit("switchyard new: an Oracle board runs a declared workflow: pass --workflow-config <document> "
                         "(or --from an artifact that declares one). Nothing was asked or changed.")
    given = os.environ.get(provision_oracle.NEW_CONFIG_ENV, "").strip()
    if given:
        staged = provision_oracle.stage_from_config(given)
    elif interactive:
        print("This project's board keeps its tickets in a remote Oracle database. Its connection is checked "
              "before anything else is asked.")
        staged = provision_oracle.collect_interactively(oracle_lifecycle.Asker(True, ask, ask_secret))
    else:
        raise SystemExit(f"switchyard new: an Oracle board needs its connection: set {provision_oracle.NEW_CONFIG_ENV} "
                         "to a configuration made with `ticket-board-oracle configure` (with bootstrap credentials), "
                         "or run switchyard new in a terminal to be asked. Nothing was changed.")
    # Through the shipped CLI, not in this process: python-oracledb is in the shared venv, and switchyard runs
    # on the system Python; the CLI runs itself there.
    command = [sys.executable, str(Path(__file__).resolve().parent / "ticket-board-oracle"), "check", "--config",
               str(staged / "bootstrap.json"), "--before-migrate"]
    if provision_oracle.allow_plaintext_staged(staged):
        command.append("--allow-plaintext")
    import subprocess

    done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
                          env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)})
    print((done.stdout + done.stderr).rstrip())
    if done.returncode != 0:
        shutil.rmtree(staged, ignore_errors=True)
        os.environ.pop(provision_oracle.STAGED_ENV, None)
        raise SystemExit("switchyard new: the Oracle database did not check out (above). Nothing was created, and "
                         "nothing collected was kept.")
    provision_oracle.precheck_without_postgresql(launcher)
    return ORACLE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="database_capabilities", description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="print the databases this host was installed for")
    add = commands.add_parser("record", help="add a selection to the record (root)")
    add.add_argument("--databases", required=True)
    add.add_argument("--prior-install", action="store_true",
                     help="Switchyard was installed here before: a missing record means PostgreSQL")
    check = commands.add_parser("parse", help="validate a selection and print its databases")
    check.add_argument("selection")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print(" ".join(installed()))
        elif args.command == "parse":
            print(" ".join(parse_selection(args.selection)))
        else:
            now = record(parse_selection(args.databases), prior_install=args.prior_install)
            print(f"recorded database capabilities in {record_path()}: {' '.join(now)}")
    except CapabilityError as exc:
        print(f"database_capabilities: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
