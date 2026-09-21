#!/usr/bin/env python3
"""Root's `new` must write a role's provider state as the tenant owner.

Live UAT on test12 at e9bb37a: startup printed Permission denied for every role,
creating `pane-sessions/roles/<role>/.<role>.provider-state.json.*.tmp`, and the
three Claude roles' session records never appeared. `switchyard new` launches as
root, and `record_provider_state_generation` wrote in-process: its `mkdir`
created each `roles/<role>/` root-owned, and every later write the OWNER made
there -- the record on the next launch, each session hook -- was refused
(SYRD-221 UAT).

Root cannot be had in a suite, so identity is injected: `geteuid` says root, and
`drop` records what it was asked to become instead of doing it. What is
asserted is the shape that matters -- the drop happens, in a child, to the
owner's uid and gid, before anything is written.
"""

from __future__ import annotations

import os
import pwd
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

ME = pwd.getpwuid(os.getuid()).pw_name


def _tenant(tmp: Path, owner: str = ME):
    config = types.SimpleNamespace(
        run_as_user=owner, session_dir=tmp / "pane-sessions", role_state_isolation=True,
        project="t12",
    )
    role = types.SimpleNamespace(role="director", cli="claude", command=["claude"],
                                 run_as_user=owner)
    return config, role


def _record_path(config, role) -> Path:
    return team_launcher._provider_state_record_path(config, role)


def test_root_writes_as_the_owner_in_a_child_after_dropping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config, role = _tenant(tmp_path)
        journal = tmp_path / "journal"
        record = _record_path(config, role)

        def drop(uid: int, gid: int) -> None:
            # Written by the process that drops, before anything else is.
            journal.write_text(f"drop {uid} {gid} pid={os.getpid()} "
                               f"record_existed={record.exists()}")

        team_launcher.record_provider_state_generation(
            config, role, "gen-1", geteuid=lambda: 0, drop=drop
        )
        entry = pwd.getpwnam(ME)
        said = journal.read_text()
        assert said.startswith(f"drop {entry.pw_uid} {entry.pw_gid} "), said
        assert f"pid={os.getpid()}" not in said, "root dropped privileges in ITSELF, not a child"
        assert "record_existed=False" in said, "something was written before the drop"
        assert team_launcher.recorded_provider_state_generation(config, role) == "gen-1"


def test_root_writes_nothing_for_an_owner_that_does_not_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, role = _tenant(Path(tmp), owner="no-such-account-syrd221")
        dropped: list = []
        team_launcher.record_provider_state_generation(
            config, role, "gen-1", geteuid=lambda: 0, drop=lambda *a: dropped.append(a)
        )
        assert dropped == [], dropped
        assert not _record_path(config, role).exists()
        assert not (Path(tmp) / "pane-sessions").exists(), "root created the owner's tree itself"


def test_a_failed_owner_write_is_said_not_swallowed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, role = _tenant(Path(tmp))

        def drop_then_refuse(uid: int, gid: int) -> None:
            raise PermissionError("as the owner, this directory is not writable")

        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            team_launcher.record_provider_state_generation(
                config, role, "gen-1", geteuid=lambda: 0, drop=drop_then_refuse
            )
        assert f"as {ME}" in err.getvalue(), err.getvalue()
        assert not _record_path(config, role).exists()


def test_an_ordinary_owner_run_still_writes_in_process() -> None:
    """The bridge's owner half, and every other non-root launch, is unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        config, role = _tenant(Path(tmp))
        dropped: list = []
        team_launcher.record_provider_state_generation(
            config, role, "gen-2", drop=lambda *a: dropped.append(a)
        )
        assert dropped == [], "a non-root run tried to change identity"
        assert team_launcher.recorded_provider_state_generation(config, role) == "gen-2"
        assert _record_path(config, role).stat().st_uid == os.getuid()


def test_a_child_cannot_be_made_to_write_as_someone_else() -> None:
    """Unprivileged, the helper refuses to act for another uid rather than act as itself."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "written"
        assert not team_launcher._run_as_account(
            os.getuid() + 1, os.getgid(), lambda: marker.write_text("x")
        )
        assert not marker.exists()
        assert team_launcher._run_as_account(os.getuid(), os.getgid(), lambda: marker.write_text("x"))
        assert marker.read_text() == "x"


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"provider_state_written_as_owner_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
