#!/usr/bin/env python3
"""SYRD-172: the release install left no record, and it is the step with the most to record.

`switchyard-record-rollout` exists so every privileged step leaves a root-owned
attempt naming the operator, `operator_source: pkexec`, the target commit, the
exit status and sha256 of stdout and stderr. Unit install, deploy-restart and the
provisioning packet all go through it. The RELEASE INSTALL did not -- the one
step that puts new root-executed code on the host, and the only one whose input
(the bundle) is produced by an unprivileged account. Everything the journal
recorded afterwards runs from what that step installed.

The Director's decision was Option A for upgrades, and a distinct trust case for
a first install. These cases run the rendered chain for real, through the REAL
recorder, with a stub PATH: the only things left unstubbed are the ones whose
behaviour is the question.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

COMMIT = "1" * 40
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def install_root(tmp: Path, *, with_recorder: bool) -> Path:
    root = tmp / "opt" / "switchyard"
    (root / "releases").mkdir(parents=True)
    if with_recorder:
        scripts = root / "current" / "scripts"
        scripts.mkdir(parents=True)
        # The real recorder, reached the way root reaches it: through the
        # installed release, never through the checkout.
        (scripts / "switchyard-record-rollout").symlink_to(
            ROOT / "scripts" / "switchyard-record-rollout"
        )
        # An installed release has an installer, and the chain calls it by
        # absolute path. This one defers to whatever the stub PATH provides, so
        # a case can make the install itself fail without rewriting the tree.
        installer = scripts / "install-switchyard"
        installer.write_text('#!/bin/sh\nexec install-switchyard "$@"\n')
        installer.chmod(0o755)
    return root


def rendered(tmp: Path, *, with_recorder: bool, source_repo: Path | None = None) -> list[str]:
    root = install_root(tmp, with_recorder=with_recorder)
    with patch.dict(os.environ, {"SWITCHYARD_SHARED_INSTALL_ROOT": str(root)}):
        return launcher.trusted_bootstrap_commands(
            source_repo or (tmp / "checkout"), COMMIT, project="demo",
            publish_remote="git@example:demo.git",
        )


def install_line(lines: list[str]) -> str:
    matches = [line for line in lines if "--label install --" in line]
    assert len(matches) == 1, lines
    return matches[0]


def run_chain(line: str, *, journal: Path, stubs: Path, failing: str = "") -> subprocess.CompletedProcess:
    """Run one rendered line with nothing on PATH but stubs.

    A command with no stub is `command not found`, so "nothing else ran" is
    enforced by the kernel rather than by reading the script.
    """
    stubs.mkdir(parents=True, exist_ok=True)
    (stubs / "sudo").write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do case "$1" in -u|-g) shift 2;; -H|-E|-n) shift;;'
        ' --) shift; break;; *) break;; esac; done\nexec "$@"\n'
    )
    for name in ("install", "git", "install-switchyard", "readlink", "test"):
        script = "#!/bin/sh\nexit 1\n" if name == failing else '#!/bin/sh\necho "$0 $@"\nexit 0\n'
        (stubs / name).write_text(script)
    for name in ("sudo", "install", "git", "install-switchyard", "readlink", "test"):
        (stubs / name).chmod(0o755)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "SWITCHYARD_ROLLOUT_JOURNAL_ROOT": str(journal),
        "SUDO_USER": "an-operator",
        "HOME": str(journal.parent),
    }
    return subprocess.run(["bash", "-c", line], capture_output=True, text=True, env=env)


def attempts(journal: Path, project: str = "demo") -> list[dict]:
    directory = journal / project
    if not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        result = entry / "result.json"
        if result.is_file():
            out.append(json.loads(result.read_text(encoding="utf-8")))
    return out


# ------------------------------------------------------------------ cases --


def test_an_upgrade_records_the_install_as_one_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd172-upgrade.") as tmp:
        lines = rendered(Path(tmp), with_recorder=True)
    line = install_line(lines)
    check("switchyard-record-rollout" in line, f"the install is recorded: {line[:120]}")
    check(f"--target-commit {COMMIT}" in line, "with the commit it installs")
    check("bash -c" in line, "as one command line")
    # Every privileged step of the bootstrap is inside it, and none outside.
    for fragment in ("install -d -m 0755 -o root -g root", "git init -q",
                     "fetch.fsckObjects=true", "checkout -q --detach", "install-switchyard --apply"):
        check(fragment in line, f"{fragment} is inside the recorded chain")
    outside = [l for l in lines if l is not line and "install-switchyard" in l]
    check(not outside, f"and nothing installs outside it: {outside}")
    check("set -euo pipefail" in line, "a failure anywhere ends the chain")
    # The unprivileged half stays where it was: the operator's own repository,
    # as themselves, unrecorded because root did not do it.
    unprivileged = [l for l in lines if l.startswith("env GIT_NO_REPLACE_OBJECTS=1 git -C")]
    check(len(unprivileged) == 2, f"update-ref and bundle create stay outside: {unprivileged}")


def test_the_recorded_install_actually_writes_an_attempt() -> None:
    """Run it, through the real recorder, and read the journal back."""
    with tempfile.TemporaryDirectory(prefix="syrd172-run.") as tmp:
        tmp_path = Path(tmp)
        lines = rendered(tmp_path, with_recorder=True)
        journal = tmp_path / "journal"
        done = run_chain(install_line(lines), journal=journal, stubs=tmp_path / "stubs")
        written = attempts(journal)
    check(done.returncode == 0, f"the chain succeeds: {done.stderr[-400:]}")
    check(len(written) == 1, f"exactly one attempt: {written}")
    record = written[0]
    check(record.get("target_commit") == COMMIT, f"naming the commit: {record}")
    check(record.get("exit_status") == 0, f"and its exit status: {record}")
    # The operator FIELD is what production fills from pkexec. Inside this
    # namespace the name has no passwd entry, and the recorder says
    # `sudo:unresolved` rather than inventing one -- which is the behaviour
    # worth pinning here: the record never claims to know who ran it.
    check("operator" in record and "operator_source" in record,
          f"the record has somewhere to name the operator: {record}")
    check(record.get("operator_source"), f"and always says how it was elevated: {record}")
    check(set(record.get("artifacts") or {}) == {"stdout.log", "stderr.log"},
          f"with output hashes: {record}")
    check("install-switchyard" in " ".join(record.get("command") or ()),
          f"and the install is what it recorded: {record}")


def test_a_failure_anywhere_is_a_failed_install_with_no_unrecorded_suffix() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd172-fail.") as tmp:
        tmp_path = Path(tmp)
        lines = rendered(tmp_path, with_recorder=True)
        journal = tmp_path / "journal"
        # git fails: the fetch of the bundle does not happen, so nothing may be
        # checked out and nothing may be installed.
        done = run_chain(install_line(lines), journal=journal, stubs=tmp_path / "stubs", failing="git")
        written = attempts(journal)
    check(done.returncode != 0, "a failed bootstrap fails the command")
    check(len(written) == 1, f"and is still one attempt: {written}")
    check(written[0].get("exit_status") not in (0, None), f"recorded as failed: {written[0]}")
    check(written[0].get("status") != "completed" or written[0].get("exit_status") != 0,
          f"never as a success: {written[0]}")
    # And the step after the failure did not run: `set -e` ended the chain.
    stdout = (journal / "demo")
    logs = [p.read_text(encoding="utf-8") for p in sorted(stdout.rglob("stdout.log"))]
    check(not any("install-switchyard" in text for text in logs),
          f"the installer never ran after the failure: {logs}")


def test_a_first_install_does_not_run_the_checkouts_recorder() -> None:
    """The gap is named, and the boundary is recorded by what the install installed."""
    with tempfile.TemporaryDirectory(prefix="syrd172-first.") as tmp:
        lines = rendered(Path(tmp), with_recorder=False)
    joined = "\n".join(lines)
    check("--label install --" not in joined, "there is no install attempt to write yet")
    check(str(ROOT / "scripts" / "switchyard-record-rollout") not in joined,
          f"and the checkout's recorder is never executed: {joined}")
    check(any(line.startswith("# First install on this host") for line in lines),
          f"the gap is stated in the sequence itself: {lines}")
    boundary = [line for line in lines if "--label install-boundary --" in line]
    check(len(boundary) == 1, f"the boundary is recorded once: {boundary}")
    check("current/scripts/switchyard-record-rollout" in boundary[0],
          f"by the recorder the install just installed: {boundary[0]}")
    check("readlink -f" in boundary[0] and COMMIT in boundary[0],
          f"and it verifies what landed rather than asserting it: {boundary[0]}")
    # The boundary comes after the install, never before it.
    check(lines.index(boundary[0]) > max(
        index for index, line in enumerate(lines) if "install-switchyard" in line),
        f"ordered after the install: {lines}")


def test_paths_with_spaces_and_metacharacters_survive_two_levels_of_quoting() -> None:
    """The chain is quoted into `bash -c`, and its own arguments are quoted again."""
    with tempfile.TemporaryDirectory(prefix="syrd172-quote.") as tmp:
        tmp_path = Path(tmp)
        # A name with a space, a command separator and a substitution -- but no
        # path separator, which would be a different directory rather than an
        # awkward one.
        awkward = tmp_path / "a checkout; rm -rf $(whoami) && echo pwned"
        awkward.mkdir()
        lines = rendered(tmp_path, with_recorder=True, source_repo=awkward)
        line = install_line(lines)
        journal = tmp_path / "journal"
        done = run_chain(line, journal=journal, stubs=tmp_path / "stubs")
        written = attempts(journal)
        logs = "\n".join(p.read_text(encoding="utf-8") for p in sorted((journal / "demo").rglob("stdout.log")))
    check(done.returncode == 0, f"the chain still runs: {done.stderr[-300:]}")
    check(len(written) == 1, f"and records once: {written}")
    check(str(awkward) in logs, f"with the path intact, not word-split: {logs[:300]}")
    check("rm -rf" not in done.stderr, "and nothing in the name was executed")


def test_an_install_root_whose_path_needs_quoting_still_runs() -> None:
    """The recorder is named by path, and the path is not always tidy."""
    with tempfile.TemporaryDirectory(prefix="syrd172-root.") as tmp:
        tmp_path = Path(tmp) / "an install root; echo pwned"
        tmp_path.mkdir()
        lines = rendered(tmp_path, with_recorder=True)
        journal = tmp_path / "journal"
        done = run_chain(install_line(lines), journal=journal, stubs=tmp_path / "stubs")
        written = attempts(journal)
    check(done.returncode == 0, f"an awkward install root still runs: {done.stderr[-300:]}")
    check(len(written) == 1, f"and records once: {written}")
    # The stubs echo their own argv, so the text of the path appears in the log.
    # What must not appear is the separator having TAKEN EFFECT: `echo pwned`
    # would print the word on a line of its own.
    printed = [line.strip() for line in (done.stdout + done.stderr).splitlines()]
    check("pwned" not in printed, f"with nothing in the path executed: {printed[:6]}")


def test_a_chain_that_already_records_itself_is_refused() -> None:
    """No nested attempts: one install, one record."""
    try:
        launcher.recorded_install_command(
            ["sudo /opt/switchyard/current/scripts/switchyard-record-rollout demo -- bash -c true"],
            project="demo", commit=COMMIT, recorder=Path("/opt/switchyard/current/scripts/switchyard-record-rollout"),
        )
    except ValueError as exc:
        check("nested" in str(exc), f"and says why: {exc}")
    else:
        raise AssertionError("a self-recording chain was wrapped again")


def test_the_upgrade_step_is_not_swallowed_into_the_install_record() -> None:
    """`switchyard upgrade` runs the reviewed code and records its own steps."""
    with tempfile.TemporaryDirectory(prefix="syrd172-select.") as tmp:
        lines = rendered(Path(tmp), with_recorder=True)
    select = [line for line in lines if line.startswith("sudo switchyard upgrade ")]
    check(len(select) == 1, f"the selection step stands alone: {select}")
    check("switchyard-record-rollout" not in select[0],
          "unwrapped here, because what it runs records itself")
    check(select[0] not in install_line(lines), "and it is not inside the install record")


#: The cases that RUN the chain need to be root, because the recorder refuses to
#: write a root-owned record as anybody else -- correctly. A user namespace is
#: how a fixture becomes root without the host having one.
EXECUTED_CASES = (
    "test_an_install_root_whose_path_needs_quoting_still_runs",
    "test_the_recorded_install_actually_writes_an_attempt",
    "test_a_failure_anywhere_is_a_failed_install_with_no_unrecorded_suffix",
    "test_paths_with_spaces_and_metacharacters_survive_two_levels_of_quoting",
)


def namespaces_available() -> bool:
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "true"], capture_output=True)
    return probe.returncode == 0


def run_cases(names) -> int:
    for name in names:
        globals()[name]()
    return CHECKS


def main() -> int:
    rendered_only = [
        name for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value) and name not in EXECUTED_CASES
    ]
    run_cases(rendered_only)
    assert namespaces_available(), (
        "the executed cases need a user namespace: the recorder writes a ROOT-OWNED "
        "record and refuses to do it as anybody else. This suite fails rather than skipping."
    )
    done = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
        text=True, capture_output=True,
    )
    if done.returncode != 0:
        sys.stderr.write(done.stdout + done.stderr)
        return done.returncode
    executed = int((done.stdout or "0").strip().splitlines()[-1])
    print(f"team_launcher_recorded_install_test: {CHECKS + executed} checks ok")
    return 0


if __name__ == "__main__":
    if "--privileged-child" in sys.argv:
        print(run_cases(EXECUTED_CASES))
        raise SystemExit(0)
    raise SystemExit(main())
