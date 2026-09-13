#!/usr/bin/env python3
"""SYRD-128: the evidence of a privileged run, not owned by what it is evidence about.

The SYRD-126 operator wrappers captured a privileged run's output -- which was
the right instinct, and saved the User from copying and pasting it -- and then
wrote it under /home/switchyard-agent and chowned it to switchyard-agent. Every
role in a project runs as that one account (SYRD-69), so the record of a
privileged run ended up writable by every role the run was about.

The journal is root's instead: root-owned directory, completed files 0444, and a
read path every role has without sudo. That ownership is the boundary -- it is
what stops a role editing the evidence of a run it was the subject of.

The chained index is an integrity check on top of it, and the case below says
exactly what it catches: an entry removed or altered without the hashes after it
being recomputed. It is deliberately NOT tested as a proof against root, because
it is not one -- the entries and their hashes are one root-owned file, and a
root that recomputes the chain leaves a journal that verifies clean.

The recorder must run as root to be worth anything, so the cases that exercise
it run inside an unprivileged user namespace -- the same way the publication
boundary suite drives its privileged half. The cases that do not need root run
here directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board.rollout_journal import (  # noqa: E402
    JOURNAL_ROOT_ENV,
    RESULT_NAME,
    attempts,
    read_index,
    redact,
    verify_index,
)

RECORDER = ROOT / "scripts" / "switchyard-record-rollout"
PROJECT = "otto"


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, text=True
    )
    return probe.returncode == 0


def record(journal: Path, *args: str, expect: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run the recorder as root in its own namespace."""
    done = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, str(RECORDER), PROJECT, *args],
        capture_output=True,
        text=True,
        env={**os.environ, JOURNAL_ROOT_ENV: str(journal)},
    )
    if expect is not None:
        assert done.returncode == expect, (done.returncode, done.stdout, done.stderr)
    return done


def journal_attempts(journal: Path) -> list[dict]:
    previous = os.environ.get(JOURNAL_ROOT_ENV)
    os.environ[JOURNAL_ROOT_ENV] = str(journal)
    try:
        return attempts(PROJECT)
    finally:
        if previous is None:
            os.environ.pop(JOURNAL_ROOT_ENV, None)
        else:
            os.environ[JOURNAL_ROOT_ENV] = previous


def journal_problems(journal: Path) -> list[str]:
    previous = os.environ.get(JOURNAL_ROOT_ENV)
    os.environ[JOURNAL_ROOT_ENV] = str(journal)
    try:
        return verify_index(PROJECT)
    finally:
        if previous is None:
            os.environ.pop(JOURNAL_ROOT_ENV, None)
        else:
            os.environ[JOURNAL_ROOT_ENV] = previous


def result_of(record_dict: dict) -> dict:
    return json.loads((Path(record_dict["directory"]) / RESULT_NAME).read_text(encoding="utf-8"))


def test_secrets_never_reach_the_record() -> None:
    """Redaction happens as the bytes are written, so there is no window.

    Every one of these is a form that has actually appeared in this project's
    own output: the board's write token, a command-line flag, an ssh key body,
    a URL carrying a password.
    """
    assert "abc123" not in redact("TICKET_BOARD_WRITE_TOKEN=abc123\n")
    assert "<redacted>" in redact("TICKET_BOARD_WRITE_TOKEN=abc123\n")
    assert "xyz789" not in redact("ticket-board-write --write-token xyz789 submit\n")
    assert "sekrit" not in redact("Authorization: Bearer sekrit\n")
    assert "hunter2" not in redact("https://agent:hunter2@example.invalid/repo.git\n")
    body = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE\n-----END OPENSSH PRIVATE KEY-----\n"
    hidden = redact(f"before\n{body}after\n")
    assert "b3BlbnNzaC1rZXktdjE" not in hidden, hidden
    assert "before" in hidden and "after" in hidden, hidden
    # A public key is not a secret and stays legible: an operator needs to read
    # the one they are being asked to register (SYRD-74).
    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 root switchyard otto\n"
    assert redact(public) == public


def main() -> int:
    checks = 0
    test_secrets_never_reach_the_record()
    checks += 1

    if not namespaces_available():
        print("rollout_journal_test: user namespaces unavailable; ran redaction cases only")
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd128-") as raw:
        journal = Path(raw) / "rollout"

        # 1. A SUCCESSFUL RUN. The output reaches the terminal AND the record,
        #    the record says what was run and how it ended, and it names where
        #    it is so nobody has to be told separately.
        done = record(
            journal, "--target-commit", "c" * 40, "--label", "deploy-restart",
            "--", "sh", "-c", "echo doing the thing; echo a warning >&2",
            expect=0,
        )
        assert "doing the thing" in done.stdout, done.stdout
        assert "a warning" in done.stderr, done.stderr
        assert "record:" in done.stdout, done.stdout
        records = journal_attempts(journal)
        assert len(records) == 1, records
        first = result_of(records[0])
        assert first["status"] == "completed" and first["exit_status"] == 0, first
        assert first["command"] == ["sh", "-c", "echo doing the thing; echo a warning >&2"], first
        assert first["target_commit"] == "c" * 40, first
        assert first["started_at"] and first["finished_at"], first
        assert set(first["artifacts"]) == {"stdout.log", "stderr.log"}, first
        directory = Path(records[0]["directory"])
        assert "doing the thing" in (directory / "stdout.log").read_text(encoding="utf-8")
        assert "a warning" in (directory / "stderr.log").read_text(encoding="utf-8")
        checks += 1

        # 2. WRITE REFUSAL. Completed files are read-only and root's; this
        #    process is neither root nor able to change that.
        for name in ("stdout.log", "stderr.log", RESULT_NAME):
            path = directory / name
            assert path.stat().st_mode & 0o777 == 0o444, (name, oct(path.stat().st_mode))
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("tampered\n")
            except OSError:
                pass
            else:
                raise AssertionError(f"a project role could append to {name}")
        # And read access is the other half: no sudo, plain open().
        assert json.loads((directory / RESULT_NAME).read_text(encoding="utf-8"))["attempt"]
        checks += 1

        # 3. EARLY VALIDATION FAILURE -- the command does not exist. There is no
        #    run to record, and there IS an attempt saying why it never started.
        missing = record(journal, "--", "no-such-command-syrd128", expect=127)
        assert "could not be run" in missing.stderr, missing.stderr
        failed = result_of(journal_attempts(journal)[-1])
        assert failed["status"] == "failed" and failed["exit_status"] == 127, failed
        assert "could not start" in failed["detail"], failed
        checks += 1

        # 4. A FAILING RUN, which is what a rollback failure looks like from
        #    here: a non-zero exit, kept with its output rather than lost.
        rollback = record(
            journal, "--label", "rollback",
            "--", "sh", "-c", "echo rolling back >&2; exit 3",
            expect=3,
        )
        assert "rolling back" in rollback.stderr, rollback.stderr
        rolled = result_of(journal_attempts(journal)[-1])
        assert rolled["status"] == "failed" and rolled["exit_status"] == 3, rolled
        assert "rolling back" in (
            Path(journal_attempts(journal)[-1]["directory"]) / "stderr.log"
        ).read_text(encoding="utf-8")
        checks += 1

        # 5. INTERRUPTED EXECUTION. The recorder is killed outright, so it
        #    cannot write its own ending -- and the journal still shows the
        #    attempt, because the start was recorded before the run began.
        killed = subprocess.Popen(
            ["unshare", "--user", "--map-root-user", sys.executable, str(RECORDER), PROJECT,
             "--label", "interrupted", "--", "sleep", "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, JOURNAL_ROOT_ENV: str(journal)},
        )
        deadline = 0
        while deadline < 100 and len(journal_attempts(journal)) < 5:
            import time as _time

            _time.sleep(0.05)
            deadline += 1
        killed.kill()
        killed.wait(timeout=10)
        interrupted = journal_attempts(journal)[-1]
        assert interrupted["status"] == "interrupted", interrupted
        assert interrupted["started_at"], interrupted
        assert not interrupted["finished_at"], interrupted
        # Its own file agrees: still running, because nothing ever finished it.
        assert result_of(interrupted)["status"] == "running", result_of(interrupted)
        checks += 1

        # 6. MULTIPLE ATTEMPTS are distinct, ordered, and never share a log.
        record(journal, "--", "sh", "-c", "echo second attempt", expect=0)
        listed = journal_attempts(journal)
        identifiers = [item["attempt"] for item in listed]
        assert len(set(identifiers)) == len(identifiers), identifiers
        assert identifiers == sorted(identifiers), identifiers
        directories = [item["directory"] for item in listed if item["directory"]]
        assert len(set(directories)) == len(directories), directories
        checks += 1

        # 7. THE CHAIN, and precisely what it is worth. It verifies as
        #    written; an entry cut out without recomputing what follows breaks
        #    it. That is the class it catches -- an accidental edit, a
        #    truncated write, a line deleted by hand -- and the last assertion
        #    is the honest limit: recompute the chain over the edited file and
        #    it verifies clean again, which is what a malicious root would do.
        assert journal_problems(journal) == [], journal_problems(journal)
        index = journal / PROJECT / "index.jsonl"
        lines = index.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 8, lines
        os.chmod(index, 0o644)
        index.write_text("\n".join(lines[:2] + lines[4:]) + "\n", encoding="utf-8")
        problems = journal_problems(journal)
        assert problems, "a removed pair of entries left no trace"
        assert any("follows" in problem for problem in problems), problems
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert journal_problems(journal) == [], journal_problems(journal)

        # The limit, asserted rather than described: rewrite the file with two
        # entries gone AND the chain recomputed, and verification is clean. The
        # journal cannot tell that from an honest history, so nothing here may
        # claim it proves the record against root.
        import hashlib

        kept = [json.loads(line) for line in lines[:2] + lines[4:]]
        rebuilt: list[str] = []
        previous = ""
        for entry in kept:
            entry["previous"] = previous
            line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            rebuilt.append(line)
            previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        index.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")
        assert journal_problems(journal) == [], (
            "a recomputed chain must verify -- if it does not, this test is "
            "asserting something stronger than the design provides"
        )
        assert len(journal_attempts(journal)) == len(listed) - 1, journal_attempts(journal)
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert journal_problems(journal) == [], journal_problems(journal)
        checks += 1

        # 8. A ROLE READS IT without sudo, through the command it is given.
        import scripts.team_launcher as team_launcher

        printed: list[str] = []
        previous = os.environ.get(JOURNAL_ROOT_ENV)
        os.environ[JOURNAL_ROOT_ENV] = str(journal)
        try:
            exit_status = team_launcher.rollout_log_command(
                PROJECT, output=True, print_func=printed.append
            )
        finally:
            if previous is None:
                os.environ.pop(JOURNAL_ROOT_ENV, None)
            else:
                os.environ[JOURNAL_ROOT_ENV] = previous
        text = "\n".join(printed)
        assert exit_status == 0, text
        assert "rollout journal" in text, text
        assert "second attempt" in text, text
        assert "interrupted" in text, text
        checks += 1

    # 9. AND BOTH GENERATED SEQUENCES RUN THROUGH IT. The reviewed command is
    #    handed over whole rather than rewritten, and the whole `&&` chain goes
    #    inside the recorder -- leaving it outside would record the first
    #    command and run the rest unrecorded.
    import scripts.team_launcher as team_launcher

    status = team_launcher.TenantReleaseStatus(
        board_root=Path("/srv/otto/board"), owner_user="otto-agent",
        owner_home=Path("/home/otto-agent"),
        provisioned_system_unit=Path("/etc/switchyard/provision/otto/otto-ticket-board.service"),
        commit_git_dir="", current_release=None, current_sha="", target_sha="d" * 40,
        deploy_ref="origin/main", source_repo=Path("/src"),
    )
    step = team_launcher.recorded_rollout_command(
        status, PROJECT, "sudo one 'a b' && sudo two", label="install units"
    )
    assert subprocess.run(["bash", "-n", "-c", step], capture_output=True).returncode == 0, step
    import shlex

    words = shlex.split(step)
    assert Path(words[1]).name == "switchyard-record-rollout", step
    assert words[2] == PROJECT, step
    assert "d" * 40 in step, step
    # The chain is one argument to the recorder, intact.
    assert words[-1] == "sudo one 'a b' && sudo two", words[-1]
    provisioning = team_launcher.recorded_provisioning_command(PROJECT, "operator-commands.sh")
    assert "switchyard-record-rollout" in provisioning, provisioning
    assert provisioning.endswith("-- bash operator-commands.sh"), provisioning
    checks += 1

    print(f"rollout_journal_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
