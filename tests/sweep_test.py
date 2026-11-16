#!/usr/bin/env python3
"""SYRD-815: `tests/sweep.py` runs each suite where it can work, cold, and says where and why.

Fake suites in a temporary tree stand in for real ones; the declared lists are the real ones.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sweep  # noqa: E402
import sweep_requirements  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def test_every_declared_suite_exists() -> None:
    declared = [*(s for suites in sweep_requirements.HOST_ONLY.values() for s in suites), *sweep_requirements.LOAD_SENSITIVE]
    missing = [s for s in declared if not (HERE / f"{s}.py").exists() and not (HERE / f"{s}.sh").exists()]
    check(not missing and len(declared) == len(set(declared)),
          f"every declared suite is a suite of this tree, declared once: missing {missing}")


def test_each_suite_is_planned_where_it_can_work() -> None:
    host = sweep.plan("tests/board_ui_delegation_test.py", bwrap="/usr/bin/bwrap")
    check((host.where, host.why) == ("host", "needs user namespaces"), f"a declared suite runs on the host: {host}")
    plain = sweep.plan("schema_test", bwrap="/usr/bin/bwrap")
    check((plain.suite, plain.where, plain.why) == ("tests/schema_test.py", "sandbox", ""),
          f"any other runs in the sandbox, named however it was given: {plain}")
    check(sweep.plan("schema_test", sandbox=False, bwrap="/usr/bin/bwrap").where == "host"
          and sweep.plan("schema_test", bwrap="").why == "bwrap is not installed",
          "--no-sandbox, or no bwrap, runs it on the host and says so")


def _tree(suites: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="sweep-tree."))
    (root / "tests").mkdir()
    for name, body in suites.items():
        (root / "tests" / f"{name}.py").write_text(body)
    return root


def test_the_host_environment_is_cold_and_traversable() -> None:
    root = _tree({"isolating_test": "import live_boundary; live_boundary.isolate()\n", "plain_test": "pass\n"})
    scratch = sweep._scratch()
    pane = {"TICKET_BOARD_PANE_SESSION_DIR": "/live/sessions", "TICKET_BOARD_PANE_STATE_DIR": "/live/state",
            "TICKET_BOARD_SOCKET": "/live/socket", "PGU_TICKET_BOARD_PANE_TARGET": "p:0.0"}
    try:
        with mock.patch.dict(os.environ, pane):
            isolating = sweep.host_env(sweep.Plan("tests/isolating_test.py", "host", "x"), scratch, root)
            plain = sweep.host_env(sweep.Plan("tests/plain_test.py", "host", "x"), scratch, root)
        check(isolating["HOME"] == "" and not any(k.startswith("XDG_") for k in isolating)
              and isolating.get("TICKET_BOARD_PANE_SESSION_DIR") == "/live/sessions"
              and isolating.get("TICKET_BOARD_PANE_STATE_DIR") == "/live/state"
              and "TICKET_BOARD_SOCKET" not in isolating,
              f"a suite that isolates itself gets no HOME, so it makes its own root, and only the live pane's two "
              f"store names, which isolate() reads and removes: {isolating}")
        check(plain["HOME"] == str(scratch / "home") and not any(k.startswith(("TICKET_BOARD", "PGU_")) for k in plain)
              and set(plain) == {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "HOME", "XDG_CONFIG_HOME",
                                 "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"},
              f"any other gets a disposable HOME and nothing of the pane: {plain}")
        modes = {name: stat.S_IMODE((scratch / name).stat().st_mode) for name in ("", "tmp", "home")}
        check(modes == {"": 0o711, "tmp": 0o1777, "home": 0o700},
              f"another uid can reach the run's temporary directory, and only that: {modes}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_the_sandbox_is_closed() -> None:
    scratch = Path("/tmp/sweep.x")
    argv = sweep.sandbox_argv(sweep.Plan("tests/a_test.py", "sandbox", ""), scratch, Path("/tree"), bwrap="bwrap")
    joined = " ".join(argv)
    check(all(part in joined for part in ("--ro-bind / /", "--tmpfs /run", "--tmpfs /tmp", "--unshare-net",
                                          "--unshare-pid", "--die-with-parent", "--bind /tree /tree", "env -i"))
          and "TICKET_BOARD" not in joined and argv[-2:] == ["-B", "tests/a_test.py"],
          f"the sandbox: read-only host, private /tmp and /run, no network, nothing of the pane: {argv}")


def test_a_sweep_reports_each_suite_and_retries_only_the_declared() -> None:
    root = _tree({
        "green_test": "print('green_test: ok')\n",
        "red_test": "raise SystemExit(1)\n",
        "flaky_test": "import os, pathlib\nmark = pathlib.Path(__file__).with_suffix('.seen')\n"
                      "first = not mark.exists(); mark.touch()\nraise SystemExit(1 if first else 0)\n",
        "flaky_undeclared_test": "import os, pathlib\nmark = pathlib.Path(__file__).with_suffix('.seen')\n"
                                 "first = not mark.exists(); mark.touch()\nraise SystemExit(1 if first else 0)\n",
        "env_test": "import os\nprint(sorted(k for k in os.environ if k.startswith(('TICKET_BOARD', 'PGU_'))))\n",
    })
    out = root / "logs"
    printed = io.StringIO()
    try:
        with mock.patch.object(sweep_requirements, "LOAD_SENSITIVE", ("flaky_test",)), \
                mock.patch.dict(os.environ, {"TICKET_BOARD_PANE_STATE_DIR": "/live/state"}), \
                contextlib.redirect_stdout(printed):
            code = sweep.main(["--root", str(root), "--out", str(out), "--jobs", "2", "green_test", "red_test",
                               "flaky_test", "flaky_undeclared_test", "env_test"])
        lines = {line.split()[0]: line.split()[1] for line in printed.getvalue().splitlines()
                 if not line.startswith("sweep:")}
        check(lines == {"green_test": "PASS", "red_test": "FAIL", "flaky_test": "PASS-ON-RETRY",
                        "flaky_undeclared_test": "FAIL", "env_test": "PASS"} and code == 1,
              f"each suite is reported, a declared load-sensitive one retried alone, and the sweep fails on a red: "
              f"{printed.getvalue()} exit {code}")
        check((out / "flaky_test.retry.log").exists() and "retried alone" in (out / "flaky_test.log").read_text(),
              "the retry keeps its own log and the first run's says it was retried")
        check("[]" in (out / "env_test.log").read_text(), "no pane variable reaches a suite that does not isolate itself")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_it_never_runs_as_root() -> None:
    with mock.patch.object(sweep.os, "geteuid", lambda: 0):
        try:
            sweep.main(["green_test"])
            outcome = "ran"
        except SystemExit as exc:
            outcome = str(exc)
    check("refusing to run as root" in outcome, f"the sweep refuses root: {outcome}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"sweep_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
