#!/usr/bin/env python3
"""SYRD-732: upgrade gives a tenant back the directories above its session store, not only the store.

MEFP's `.local/state/mefp-ticket-board` was root's 0755 while its `pane-sessions` was the owner's: the store's repair
(`role_state_ownership`) begins at the session directory, so an older root run's parent was never given back, and a
local Hermes role's home, walked from the owner's home, refused it. Here the owner is a stand-in account whose home is a
temporary tree, so every real directory reads as another account's; `os.fchown` records instead of changing anything,
and root is a stand-in `os.geteuid`.
"""

from __future__ import annotations

import ast
import contextlib
import os
import pwd
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import role_state_parents as parents  # noqa: E402

CHECKS = 0
OWNER_UID = os.getuid() + 1


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


@contextlib.contextmanager
def tenant(*, euid: int = 0):
    with tempfile.TemporaryDirectory(prefix="syrd732-") as tmp:
        home = Path(tmp) / "home"
        store = home / ".local" / "state" / "p732-ticket-board" / "pane-sessions"
        store.mkdir(parents=True)
        config = SimpleNamespace(project="p732", run_as_user="p732-owner", session_dir=store)
        account = SimpleNamespace(pw_uid=OWNER_UID, pw_gid=OWNER_UID, pw_dir=str(home), pw_name="p732-owner")
        chowned: list[tuple[str, int, int]] = []

        def fchown(fd, uid, gid):
            chowned.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid))

        def refused(*args, **kwargs):
            raise AssertionError(f"a chown by path: {args}")

        with patch.object(pwd, "getpwnam", lambda user: account if user == "p732-owner" else (_ for _ in ()).throw(KeyError(user))), \
                patch.object(os, "fchown", fchown), patch.object(os, "chown", refused), \
                patch.object(os, "geteuid", lambda: euid):
            yield home, config, chowned


def test_the_walk_names_each_directory_above_the_store() -> None:
    with tenant() as (home, config, _chowned):
        found, refused = parents.walk_state_parents(config, lambda path, info, chown: f"uid {info.st_uid}")
        named = [path for path, _reason in found]
        check(refused == [] and named == [home / ".local", home / ".local" / "state", home / ".local" / "state" / "p732-ticket-board"],
              f"from the owner's home down to the store's parent, each alone; not the home, not the store: {named}")


def test_root_gives_them_back_through_their_descriptors() -> None:
    with tenant() as (home, config, chowned):
        said: list[str] = []
        ok = parents.repair_state_parents(config, print_func=said.append)
        given = [path for path, uid, gid in chowned if (uid, gid) == (OWNER_UID, OWNER_UID)]
        check(given == [str(home / ".local"), str(home / ".local" / "state"), str(home / ".local" / "state" / "p732-ticket-board")],
              f"each directory above the store is given back, and nothing else: {given}")
        # The recheck reads the real owner, which a recorder did not change: the repair says so rather than claim it.
        check(ok is False and any("after the repair" in line for line in said), f"the chain is read again afterwards: {said}")


def test_a_repair_that_worked_says_so() -> None:
    with tenant() as (home, config, chowned):
        said: list[str] = []
        answers = iter([([(home / ".local", "is owned by uid 0, not p732-owner")], []), ([], []), ([], [])])
        with patch.object(parents, "walk_state_parents", lambda c, act: next(answers)):
            ok = parents.repair_state_parents(config, print_func=said.append)
        check(ok is True and said and "gave p732-owner back 1 directory(ies)" in said[-1], f"a clean recheck is reported: {said}")


def test_a_link_on_the_way_is_refused_and_nothing_is_given_away() -> None:
    with tenant() as (home, config, chowned):
        state = home / ".local" / "state"
        real = home / "elsewhere"
        state.rename(real)
        state.symlink_to(real)
        config.session_dir = state / "p732-ticket-board" / "pane-sessions"
        said: list[str] = []
        ok = parents.repair_state_parents(config, print_func=said.append)
        check(not ok and chowned == [] and "refusing to touch" in said[0] and "without following a link" in said[0],
              f"a link above the store is refused before anything is given away: {chowned} {said}")


def test_not_root_or_a_dry_run_changes_nothing() -> None:
    with tenant(euid=1000) as (_home, config, chowned):
        said: list[str] = []
        check(parents.repair_state_parents(config, print_func=said.append) is False and chowned == []
              and "sudo switchyard upgrade p732" in said[0], f"not root: the command that can, nothing changed: {said}")
    with tenant() as (_home, config, chowned):
        said = []
        check(parents.repair_state_parents(config, dry_run=True, print_func=said.append) is True and chowned == []
              and "would give p732-owner back 3 directory(ies)" in said[0], f"a dry run says what it would do: {said}")


def test_a_store_outside_the_home_has_no_parents_of_its_own() -> None:
    with tenant() as (_home, config, chowned), tempfile.TemporaryDirectory() as other:
        config.session_dir = Path(other) / "sessions"
        check(parents.walk_state_parents(config, lambda *a: "x") == ([], [])
              and parents.repair_state_parents(config, print_func=lambda line: None) is True and chowned == [],
              "a store outside the home names no parents, and nothing is given away")


def test_the_upgrade_runs_it_after_the_stores_repair() -> None:
    tree = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    check("role_state_parents.repair_state_parents" in calls, "the upgrade gives the parents back")
    source = (ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8")
    check(source.index("launcher.restore_interrupted_role_state(") < source.index("role_state_parents.repair_state_parents("),
          "after the store's own repair, in the phase every tenant runs")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_state_parents_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
