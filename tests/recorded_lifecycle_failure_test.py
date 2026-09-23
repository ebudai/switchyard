#!/usr/bin/env python3
"""A recorded failure is a written record, not a failed recorder.

Live Zorin launch on release 3f49671: `switchyard start test` correctly exited
1 because the tenant had no usable owner CLIs, the boundary was journalled
exactly as it should have been, and the bridge then printed

    switchyard-tenant-control: `switchyard start test` completed (exit 1) but
    its boundary record failed: recorder exited 1

The recorder had written and closed the entry, then returned the witnessed
`--exit-status` it was handed. The bridge read that as its own outcome, so a
correctly recorded failure was reported as a lost record (SYRD-213).

The recorder runs for real here -- as root in a user namespace, because it
refuses to write a journal the tenant account could have written -- and the
bridge's own `record_privileged_boundary` is driven against it. Nothing is
re-run to learn a result: the lifecycle exit is supplied, exactly as the bridge
supplies it.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests  # noqa: E402

RECORDER = ROOT / "scripts" / "switchyard-record-rollout"
BRIDGE = ROOT / "scripts" / "switchyard-tenant-control"
#: The false line this ticket exists to remove.
FALSE_DIAGNOSTIC = "boundary record failed"


def _load_bridge():
    """The real bridge module, loaded from the program file it ships as."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "switchyard_tenant_control_under_test", str(BRIDGE)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _namespaced(command: list[str], env: dict) -> subprocess.CompletedProcess[str]:
    """Run as root where the kernel can enforce the journal's ownership."""
    result = subprocess.run(
        ["unshare", "--user", "--map-root-user", *command],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if result.returncode != 0 and "unshare" in result.stderr:
        return subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    return result


class Journal:
    """A rollout journal root this account may write, and read back."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd213."))
        self.root = self.tmp / "rollout"
        self.root.mkdir()

    @property
    def env(self) -> dict:
        return {
            **os.environ,
            "SWITCHYARD_ROLLOUT_JOURNAL_ROOT": str(self.root),
            "SUDO_UID": str(os.getuid()),
            "SWITCHYARD_PRIVILEGED_PROVISION_ROOT": str(self.tmp / "etc"),
        }

    def record_only(self, *, project: str = "test", outcome: str = "failed",
                    exit_status: int = 1) -> subprocess.CompletedProcess[str]:
        """Exactly the invocation the bridge makes after a lifecycle verb."""
        return _namespaced(
            [sys.executable, str(RECORDER), project,
             "--record-only", outcome,
             "--exit-status", str(exit_status),
             "--action", f"switchyard start {project}",
             "--operator", "eric",
             "--label", "tenant-control start",
             "--detail", f"eric ran `switchyard start {project}` as test-agent through the "
                         f"tenant control bridge (exit {exit_status})"],
            self.env,
        )

    def entries(self, project: str = "test") -> list[dict]:
        index = self.root / project / "index.jsonl"
        if not index.is_file():
            return []
        return [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _skipped_for_privilege(result: subprocess.CompletedProcess[str]) -> bool:
    return "must run as root" in (result.stdout + result.stderr)


def test_a_recorded_nonzero_lifecycle_reports_recorder_success() -> None:
    """The regression: exit 1 recorded, and the recorder says it wrote it."""
    journal = Journal()
    try:
        result = journal.record_only(outcome="failed", exit_status=1)
        if _skipped_for_privilege(result):
            return
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert ": recorded " in result.stdout, result.stdout
    finally:
        journal.close()


def test_the_record_keeps_the_failure_it_was_given() -> None:
    journal = Journal()
    try:
        result = journal.record_only(outcome="failed", exit_status=1)
        if _skipped_for_privilege(result):
            return
        closed = [entry for entry in journal.entries() if entry.get("event") == "finish"]
        assert len(closed) == 1, journal.entries()
        record = closed[0]
        assert record["status"] == "failed", record
        assert record["exit_status"] == 1, record
        detail = str(record.get("detail") or "")
        assert "tenant-control start" in detail, record
        assert "switchyard start test" in detail, record
        assert "test-agent" in detail, record
        opened = [entry for entry in journal.entries() if entry.get("event") == "start"]
        assert opened and opened[0].get("operator") == "eric", opened
        assert "switchyard" in json.dumps(opened[0]) and "start" in json.dumps(opened[0]), opened
    finally:
        journal.close()


def test_a_successful_lifecycle_is_recorded_the_same_way() -> None:
    journal = Journal()
    try:
        result = journal.record_only(outcome="ok", exit_status=0)
        if _skipped_for_privilege(result):
            return
        assert result.returncode == 0, (result.stdout, result.stderr)
        closed = [entry for entry in journal.entries() if entry.get("event") == "finish"]
        assert closed and closed[0]["status"] == "ok", journal.entries()
    finally:
        journal.close()


def test_a_real_journal_that_cannot_be_written_fails_the_recorder() -> None:
    """Not a stand-in: the real recorder, against a journal root it cannot use."""
    journal = Journal()
    try:
        # A file where the journal root must be a directory: opening the
        # attempt cannot succeed, and nothing can be closed.
        blocked = journal.tmp / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        env = {**journal.env, "SWITCHYARD_ROLLOUT_JOURNAL_ROOT": str(blocked)}
        result = _namespaced(
            [sys.executable, str(RECORDER), "test",
             "--record-only", "failed", "--exit-status", "1",
             "--action", "switchyard start test", "--operator", "eric",
             "--label", "tenant-control start", "--detail", "eric ran it"],
            env,
        )
        if _skipped_for_privilege(result):
            return
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert ": recorded " not in result.stdout, result.stdout
        assert (result.stderr or "").strip(), "a failed recorder said nothing"
        assert not journal.entries(), journal.entries()

        # And the bridge, reading exactly that, says so and keeps the reason.
        bridge = _load_bridge()
        original = bridge.ROLLOUT_RECORDER
        buffer = io.StringIO()
        relay = journal.tmp / "record-through-a-blocked-journal"
        relay.write_text(
            "#!/bin/bash\n"
            f"exec env SWITCHYARD_ROLLOUT_JOURNAL_ROOT={blocked} "
            f"SUDO_UID={os.getuid()} {sys.executable} {RECORDER} \"$@\"\n",
            encoding="utf-8",
        )
        relay.chmod(0o755)
        try:
            bridge.ROLLOUT_RECORDER = relay
            with redirect_stderr(buffer):
                bridge.record_privileged_boundary(
                    "test", "start", caller="eric", owner="test-agent", code=1
                )
        finally:
            bridge.ROLLOUT_RECORDER = original
        said = buffer.getvalue()
        assert FALSE_DIAGNOSTIC in said, said
        assert "`switchyard start test` completed (exit 1)" in said, said
    finally:
        journal.close()


class _Recorder:
    """Stand in for the recorder program, to drive the bridge's reading of it."""

    def __init__(self, tmp: Path, *, body: str) -> None:
        self.path = tmp / "switchyard-record-rollout"
        self.path.write_text(body, encoding="utf-8")
        self.path.chmod(0o755)


def _bridge_says(tmp: Path, recorder_body: str, *, code: int) -> str:
    bridge = _load_bridge()
    recorder = _Recorder(tmp, body=recorder_body)
    original = bridge.ROLLOUT_RECORDER
    buffer = io.StringIO()
    try:
        bridge.ROLLOUT_RECORDER = recorder.path
        with redirect_stderr(buffer):
            returned = bridge.record_privileged_boundary(
                "test", "start", caller="eric", owner="test-agent", code=code
            )
        assert returned is None, returned
    finally:
        bridge.ROLLOUT_RECORDER = original
    return buffer.getvalue()


def test_the_bridge_does_not_call_a_written_record_a_failure() -> None:
    """Even from a recorder that still mirrors the status it was handed."""
    tmp = Path(tempfile.mkdtemp(prefix="syrd213-bridge."))
    try:
        mirroring = """#!/bin/bash
echo "switchyard-record-rollout: recorded 0007-20260919T122659Z in /var/lib/switchyard/rollout/test"
exit 1
"""
        said = _bridge_says(tmp, mirroring, code=1)
        assert said == "", said
        assert FALSE_DIAGNOSTIC not in said, said
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_genuine_recorder_failure_is_still_reported() -> None:
    """Nothing written, and the reason the recorder gave, kept."""
    tmp = Path(tempfile.mkdtemp(prefix="syrd213-broken."))
    try:
        broken = """#!/bin/bash
echo "switchyard-record-rollout: the journal directory is not writable" >&2
exit 1
"""
        said = _bridge_says(tmp, broken, code=1)
        assert FALSE_DIAGNOSTIC in said, said
        assert "the journal directory is not writable" in said, said
        assert "`switchyard start test` completed (exit 1)" in said, said
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_recorder_that_cannot_be_run_at_all_is_reported() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="syrd213-missing."))
    try:
        bridge = _load_bridge()
        original = bridge.ROLLOUT_RECORDER
        buffer = io.StringIO()
        try:
            # A file, so the bridge attempts it, but not one that can be exec'd.
            not_a_program = tmp / "switchyard-record-rollout"
            not_a_program.write_text("not executable\n", encoding="utf-8")
            not_a_program.chmod(0o644)
            bridge.ROLLOUT_RECORDER = not_a_program
            with redirect_stderr(buffer):
                bridge.record_privileged_boundary(
                    "test", "start", caller="eric", owner="test-agent", code=1
                )
        finally:
            bridge.ROLLOUT_RECORDER = original
        said = buffer.getvalue()
        assert "could not record this boundary" in said, said
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_lifecycle_exit_is_never_the_recorders_to_change() -> None:
    """The bridge returns what the verb returned, whatever recording did."""
    body = BRIDGE.read_text(encoding="utf-8")
    call = body.index("record_privileged_boundary(project, operation")
    after = body[call:call + 400]
    # The next thing it does with a code is hand back the one it already had.
    assert "return code" in after, after
    # And the recorder's own return is not assigned to anything that leaves it.
    assert "code = completed.returncode" not in body
    assert "record_privileged_boundary" in body


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"recorded_lifecycle_failure_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
