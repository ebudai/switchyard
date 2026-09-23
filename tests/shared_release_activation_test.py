#!/usr/bin/env python3
"""SYRD-112 DAT correction: actually activating a shared release, by commit.

`/opt/switchyard/current` decides which Switchyard code root executes for every
tenant on this machine, and the operator packet moved it with a bare `ln -sfn`:
no record of what it had been, no check that it landed, no way back. The DAT
rejection was that a catalogued action which merely refuses does not satisfy
the ticket, and it was right.

So these cases are about the four properties that make the bounded version
different from that one line, and each is asserted by *effect* -- by reading
the pointer and the record back off the filesystem -- rather than by watching
which function was called:

* the previous target is recorded **before** anything moves;
* the swap is atomic, so no reader ever sees a missing `current`;
* what landed is **verified**, by the marker at the other end of the pointer;
* a release that does not verify is **rolled back** to the recorded target.

Everything runs against a real directory tree with real symlinks. `owner_uid`
is this process's, because the suite is not root and the ownership rule has its
own case; `test_the_shipped_default_demands_root` in the front-door suite pins
what ships.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import privileged_actions as pa  # noqa: E402
from scripts.ticket_board import privileged_operations as po  # noqa: E402
from scripts.ticket_board import shared_release_activation as sra  # noqa: E402

CHECKS = 0
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
MINE = os.getuid()


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """An /opt/switchyard-shaped tree this process can actually write."""

    def __init__(self, raw: str) -> None:
        self.root = Path(raw) / "opt" / "switchyard"
        (self.root / sra.RELEASES_NAME).mkdir(parents=True)

    def build(self, commit: str, *, marker: str | None = None) -> Path:
        release = self.root / sra.RELEASES_NAME / commit
        release.mkdir()
        (release / "scripts").mkdir()
        (release / po.RELEASE_MARKER_NAME).write_text(
            json.dumps({"commit": commit if marker is None else marker}) + "\n",
            encoding="utf-8",
        )
        return release

    def point_at(self, commit: str) -> None:
        pointer = self.root / sra.POINTER_NAME
        if pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(self.root / sra.RELEASES_NAME / commit)

    def pointer(self) -> str:
        return sra.read_pointer(self.root)

    def record(self) -> dict:
        return sra.recorded_rollback(self.root)

    def activate(self, commit: str, **kwargs):
        kwargs.setdefault("owner_uid", MINE)
        kwargs.setdefault("print_func", lambda _line: None)
        return sra.activate(commit, install_root=self.root, **kwargs)


def host():
    import tempfile

    class _Ctx:
        def __enter__(self):
            self.tmp = tempfile.TemporaryDirectory()
            return Host(self.tmp.__enter__())

        def __exit__(self, *exc):
            return self.tmp.__exit__(*exc)

    return _Ctx()


# -- the happy path, asserted by effect -------------------------------------


def test_activation_moves_the_pointer_and_records_the_way_back() -> None:
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)

        result = h.activate(COMMIT)

        check(
            h.pointer() == str(h.root / sra.RELEASES_NAME / COMMIT),
            f"current now points at the new release: {h.pointer()}",
        )
        check(Path(h.pointer()).is_dir(), "and it resolves to a real directory")
        record = h.record()
        check(record.get("activating") == COMMIT, f"the record names what was activated: {record}")
        check(
            record.get("previous_target") == str(h.root / sra.RELEASES_NAME / OTHER),
            f"and exactly what to go back to: {record}",
        )
        check(record.get("previous_commit") == OTHER, f"by commit as well: {record}")
        check(result.previous_commit == OTHER, result.describe())
        check(not result.rolled_back, "and nothing was undone")


def test_the_way_back_is_recorded_before_the_pointer_moves() -> None:
    """A record written afterwards does not exist for the failure it is for."""
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)

        seen: list[tuple[str, str]] = []
        real_write = sra._write_record

        def watching_write(path, record):
            seen.append(("record", h.pointer()))
            return real_write(path, record)

        real_swap = sra._swap_pointer

        def watching_swap(install_root, target):
            seen.append(("swap", h.pointer()))
            return real_swap(install_root, target)

        sra._write_record = watching_write
        sra._swap_pointer = watching_swap
        try:
            h.activate(COMMIT)
        finally:
            sra._write_record = real_write
            sra._swap_pointer = real_swap

        check([step for step, _ in seen] == ["record", "swap"], f"the order: {seen}")
        check(
            seen[0][1] == str(h.root / sra.RELEASES_NAME / OTHER),
            "and the record was written while the pointer was still the old one",
        )


def test_activating_what_is_already_current_changes_nothing() -> None:
    """A no-op recorded as an activation makes the journal describe a fiction."""
    with host() as h:
        h.build(COMMIT)
        h.point_at(COMMIT)
        before = h.pointer()

        result = h.activate(COMMIT)

        check(h.pointer() == before, "the pointer is untouched")
        check(h.record() == {}, f"and no way-back record was written: {h.record()}")
        check(
            any("already" in note for note in result.notes),
            f"and it says so: {result.notes}",
        )


# -- verification, and the rollback it exists for ---------------------------


def test_a_release_that_does_not_verify_is_rolled_back() -> None:
    """The clause the DAT rejection asked for, asserted on the filesystem."""
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)

        # It resolves and is trusted at plan time, and then fails verification
        # after the swap -- the shape of a release that is not what it claimed.
        real_verify = sra.verify

        def failing_verify(commit, **kwargs):
            return ["the release records commit deadbeef, not " + commit]

        sra.verify = failing_verify
        try:
            h.activate(COMMIT)
            check(False, "a release that did not verify was accepted")
        except sra.ActivationFailed as exc:
            check("did not verify" in str(exc), str(exc))
            check("put back" in str(exc), f"and says it was undone: {exc}")
        finally:
            sra.verify = real_verify

        check(
            h.pointer() == str(h.root / sra.RELEASES_NAME / OTHER),
            f"the pointer is back on the previous release: {h.pointer()}",
        )
        check(Path(h.pointer()).is_dir(), "and it resolves")
        check(
            h.record().get("previous_target") == str(h.root / sra.RELEASES_NAME / OTHER),
            "and the record still names the way back",
        )


def test_verification_reads_the_marker_at_the_other_end_of_the_pointer() -> None:
    """Not "the syscall returned 0" -- what is this host actually running."""
    with host() as h:
        h.build(COMMIT, marker="b" * 40)
        h.point_at(COMMIT)
        problems = sra.verify(COMMIT, install_root=h.root, owner_uid=MINE)
        check(
            any("records commit" in problem for problem in problems),
            f"a release whose marker disagrees is caught: {problems}",
        )

    with host() as h:
        h.build(COMMIT)
        h.build(OTHER)
        h.point_at(OTHER)
        problems = sra.verify(COMMIT, install_root=h.root, owner_uid=MINE)
        check(
            any("points at" in problem for problem in problems),
            f"and so is a pointer aimed elsewhere: {problems}",
        )

    with host() as h:
        h.build(COMMIT)
        problems = sra.verify(COMMIT, install_root=h.root, owner_uid=MINE)
        check(
            any("not a symbolic link" in problem for problem in problems),
            f"and an absent pointer: {problems}",
        )


def test_a_failure_with_nothing_to_return_to_says_so_rather_than_guessing() -> None:
    with host() as h:
        h.build(COMMIT)  # a first install: no previous pointer at all
        sra.verify, real = (lambda commit, **kwargs: ["nope"]), sra.verify
        try:
            h.activate(COMMIT)
            check(False, "it claimed success")
        except sra.ActivationFailed as exc:
            check("no previous release to return to" in str(exc), str(exc))
            check(COMMIT in str(exc), f"and says where the pointer was left: {exc}")
        finally:
            sra.verify = real


# -- concurrency -------------------------------------------------------------


def test_a_pointer_that_moves_while_preparing_stops_the_activation() -> None:
    """Another activation got there first. Do not overwrite its result."""
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)

        real_write = sra._write_record

        def racing_write(path, record):
            result = real_write(path, record)
            # Somebody else activates between the record and the swap.
            h.point_at(COMMIT)
            return result

        sra._write_record = racing_write
        try:
            h.activate(COMMIT)
            check(False, "it swapped over a concurrent change")
        except sra.ActivationFailed as exc:
            check("changed while this activation was preparing" in str(exc), str(exc))
            check("Nothing was changed" in str(exc), f"and nothing was: {exc}")
        finally:
            sra._write_record = real_write


def test_a_rollback_does_not_clobber_a_later_successful_activation() -> None:
    """The sharp one: this activation's failure must not undo somebody else's success."""
    with host() as h:
        third = "c" * 40
        h.build(OTHER)
        h.build(COMMIT)
        h.build(third)
        h.point_at(OTHER)

        real_verify = sra.verify

        def failing_verify(commit, **kwargs):
            # While this one was verifying, another activation succeeded.
            h.point_at(third)
            return ["did not verify"]

        sra.verify = failing_verify
        try:
            h.activate(COMMIT)
            check(False, "it claimed success")
        except sra.ActivationFailed as exc:
            check("has since been changed" in str(exc), str(exc))
            check("left alone" in str(exc), f"and it was: {exc}")
        finally:
            sra.verify = real_verify

        check(
            h.pointer() == str(h.root / sra.RELEASES_NAME / third),
            f"the other activation's result survives: {h.pointer()}",
        )


# -- refusing before anything moves ------------------------------------------


def test_a_commit_no_trusted_source_holds_never_touches_the_pointer() -> None:
    with host() as h:
        h.build(OTHER)
        h.point_at(OTHER)
        before = h.pointer()
        try:
            h.activate(COMMIT)
            check(False, "it activated a release that is not there")
        except sra.ActivationFailed as exc:
            check("no trusted source" in str(exc), str(exc))
        check(h.pointer() == before, "the pointer is untouched")
        check(h.record() == {}, "and nothing was recorded")


def test_a_release_whose_marker_disagrees_is_refused_before_the_swap() -> None:
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT, marker=OTHER)
        h.point_at(OTHER)
        before = h.pointer()
        try:
            h.activate(COMMIT)
            check(False, "a directory named after a commit was taken as evidence")
        except sra.ActivationFailed as exc:
            check("records commit" in str(exc), str(exc))
        check(h.pointer() == before, "and the pointer never moved")


def test_a_pointer_that_is_not_a_symlink_is_refused_rather_than_removed() -> None:
    with host() as h:
        h.build(COMMIT)
        (h.root / sra.POINTER_NAME).mkdir()
        try:
            h.activate(COMMIT)
            check(False, "it replaced a real directory")
        except sra.ActivationFailed as exc:
            check("not a symbolic link" in str(exc), str(exc))
            check("by hand" in str(exc), f"and leaves it to a human: {exc}")
        check((h.root / sra.POINTER_NAME).is_dir(), "the directory is still there")


def test_a_record_that_cannot_be_written_stops_the_activation() -> None:
    """No recorded way back means no move. The record is not optional."""
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)
        before = h.pointer()

        real_write = sra._write_record

        def failing_write(path, record):
            raise OSError("read-only file system")

        sra._write_record = failing_write
        try:
            h.activate(COMMIT)
            check(False, "it moved the pointer with no way back recorded")
        except sra.ActivationFailed as exc:
            check("could not record the way back" in str(exc), str(exc))
            check("nobody can undo" in str(exc), f"and says why that is fatal: {exc}")
        finally:
            sra._write_record = real_write
        check(h.pointer() == before, "the pointer is untouched")


# -- the swap itself ---------------------------------------------------------


def test_the_swap_never_leaves_the_host_without_a_current() -> None:
    """`ln -sfn` unlinks and then symlinks. A rename does not.

    Asserted by construction rather than by racing it: the new link is made
    under a different name and renamed over the pointer, so there is no moment
    at which `current` does not exist. The temporary name is checked because
    that is what makes the rename possible.
    """
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)

        observed: list[list[str]] = []
        real_rename = os.rename

        def watching_rename(src, dst):
            # At the instant of the rename, the pointer still exists and still
            # points at the old release.
            observed.append(sorted(p.name for p in h.root.iterdir()))
            return real_rename(src, dst)

        os.rename = watching_rename
        try:
            h.activate(COMMIT)
        finally:
            os.rename = real_rename

        for listing in observed:
            check(
                sra.POINTER_NAME in listing,
                f"current exists throughout the swap: {listing}",
            )
        check(
            any(f"{sra.POINTER_NAME}.activating" in listing for listing in observed),
            f"and the new link was built beside it first: {observed}",
        )


def test_rollback_returns_to_the_recorded_target() -> None:
    with host() as h:
        h.build(OTHER)
        h.build(COMMIT)
        h.point_at(OTHER)
        h.activate(COMMIT)
        check(h.pointer().endswith(COMMIT), h.pointer())

        sra.rollback(install_root=h.root, owner_uid=MINE, print_func=lambda _l: None)
        check(
            h.pointer() == str(h.root / sra.RELEASES_NAME / OTHER),
            f"back on the recorded previous release: {h.pointer()}",
        )


def test_rollback_with_nothing_recorded_refuses() -> None:
    with host() as h:
        h.build(COMMIT)
        h.point_at(COMMIT)
        try:
            sra.rollback(install_root=h.root, owner_uid=MINE, print_func=lambda _l: None)
            check(False, "it rolled back to nothing")
        except sra.ActivationFailed as exc:
            check("nothing to return to" in str(exc), str(exc))


# -- the catalogued action, and the boundary it keeps ------------------------


def test_the_catalogued_action_still_takes_a_commit_and_no_path() -> None:
    action = pa.action_for("install-shared-release")
    check([name for name, _ in action.arguments] == ["commit"], str(action.arguments))
    check(action.authentication == pa.AUTH_ADMIN, "and a human authenticates for it")
    for bad in ("/opt/switchyard/releases/x", "../etc", "HEAD", COMMIT[:7], ""):
        try:
            action.validate({"commit": bad})
            check(False, f"{bad!r} was accepted")
        except pa.ArgumentError:
            pass
    check(True, "no caller path reaches the activation")


def test_the_action_now_runs_the_activation_instead_of_refusing() -> None:
    """The DAT rejection, closed: it no longer stops at a refusal."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        h = Host(raw)
        h.build(COMMIT)
        previous = (po.SHARED_RELEASES, po.TRUSTED_OWNER_UID)
        po.SHARED_RELEASES = str(h.root / sra.RELEASES_NAME)
        po.TRUSTED_OWNER_UID = MINE
        try:
            command = po.command_for("install-shared-release", {"commit": COMMIT})
        finally:
            po.SHARED_RELEASES, po.TRUSTED_OWNER_UID = previous
    check(command[0] == po.LAUNCHER, f"the pinned launcher: {command}")
    check(command[1] == "install-shared-release", f"a real subcommand: {command}")
    check(command[2:] == ["--commit", COMMIT], f"a commit and nothing else: {command}")
    check(
        not any(str(h.root) in element for element in command),
        f"and no path from the resolution leaks into the argv: {command}",
    )


def test_the_subcommand_exists_and_parses_exactly_that_argv() -> None:
    """Driven through the real parser, because I have invented flags before."""
    launcher = ROOT / "scripts"
    if str(launcher) not in sys.path:
        sys.path.insert(0, str(launcher))
    import team_launcher as tl

    check("install-shared-release" in tl.SWITCHYARD_COMMANDS, "the command exists")
    check(
        "install-shared-release" in tl.SWITCHYARD_PRIVILEGED_COMMANDS,
        "and it is privileged: it changes what every tenant on the host runs",
    )
    parsed = tl._build_switchyard_install_shared_release_parser().parse_args(
        ["--commit", COMMIT]
    )
    check(parsed.commit == COMMIT, f"{parsed}")
    check(parsed.rollback is False and parsed.dry_run is False, f"{parsed}")


def test_the_command_refuses_a_bad_commit_and_a_non_root_caller() -> None:
    launcher = ROOT / "scripts"
    if str(launcher) not in sys.path:
        sys.path.insert(0, str(launcher))
    import team_launcher as tl

    printed: list[str] = []
    code = tl.switchyard_install_shared_release_command("HEAD", print_func=printed.append)
    check(code == 2, f"a ref is refused before anything else: {code}")
    check("content-addressed" in "\n".join(printed), "\n".join(printed))

    printed.clear()
    code = tl.switchyard_install_shared_release_command(COMMIT, print_func=printed.append)
    if os.geteuid() != 0:
        check(code == 1, f"{code}")
        report = "\n".join(printed)
        check("must run as root" in report, report)
        check("privileged-action" in report, f"and names the front door: {report}")
    else:  # pragma: no cover - the suite is not run as root
        check(True, "running as root; the non-root refusal cannot be exercised here")

    printed.clear()
    code = tl.switchyard_install_shared_release_command(
        COMMIT, rollback=True, print_func=printed.append
    )
    check(code == 2, f"--commit and --rollback together are refused: {code}")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("shared_release_activation_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"shared_release_activation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
