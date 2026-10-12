#!/usr/bin/env python3
"""The fresh-packet check tells retiring a publisher from installing one (SYRD-434).

`test_fresh_provisioning_grants_a_shared_account_project_nothing` required a
fresh `operator-commands.sh` never to mention `switchyard-publish-ref` or
`switchyard-integrate-main`. Since SYRD-123 the packet names both, in
`sudo rm -f` lines that take away what an older release staged. So the check
failed on every packet, and it would have passed one that kept a retired
publisher on disk by dropping those lines.

This runs the check as it was on main and as it is now, each from its own tree
in a child process, against the packet as shipped and against packets with one
fault planted:

* `dropped` -- the generator retires nothing (`RETIRED_STAGED_EXECUTABLES` is
  empty), so an upgraded tenant keeps its root-owned publisher;
* `staged` -- the publisher is installed where it was removed;
* `restaged` -- it is installed again on the removal's own line;
* `invoked` -- the packet runs the integrator;
* `granted` -- the packet grants the publisher NOPASSWD.

Main's check refuses the shipped packet and passes `dropped`. This one passes
the shipped packet and refuses every fault.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "0f6651c572c9f2b3a55dd781baca791131a585c0"  # main before SYRD-434
CHECK = "test_fresh_provisioning_grants_a_shared_account_project_nothing"
STAGED = "/usr/local/lib/switchyard/otto"
PUBLISHER = f"{STAGED}/switchyard-publish-ref"
INTEGRATOR = f"{STAGED}/switchyard-integrate-main"
REMOVAL = f"sudo rm -f '{PUBLISHER}'"
INSTALL = f"sudo install -m 0755 -o root -g root '/opt/switchyard/current/scripts/switchyard-publish-ref' '{PUBLISHER}'"
VARIANTS = ("shipped", "dropped", "staged", "restaged", "invoked", "granted")

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FAIL ") + message)
    if not condition:
        failures.append(message)


def plant(text: str, variant: str) -> str:
    if variant == "staged":
        return text.replace(REMOVAL, INSTALL)
    if variant == "restaged":
        return text.replace(REMOVAL, f"{REMOVAL} && {INSTALL}")
    if variant == "invoked":
        return text + f"sudo {INTEGRATOR} --project otto\n"
    if variant == "granted":
        return text + f"echo 'otto-agent ALL=(root) NOPASSWD: {PUBLISHER}' | sudo tee /etc/sudoers.d/49-otto-publish\n"
    return text


def run_variant(tree: Path, variant: str) -> dict:
    """Run `tree`'s fresh-packet check with `variant` planted; report what it did."""
    sys.path[:] = [str(tree / "tests"), str(tree)] + [p for p in sys.path if p != str(ROOT / "tests")]
    spec = importlib.util.spec_from_file_location(
        "role_control_sudoers_install_test", tree / "tests" / "role_control_sudoers_install_test.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provision = module.provision
    assert Path(provision.__file__).resolve().is_relative_to(tree.resolve()), provision.__file__

    packets: list[str] = []
    original = provision.write_artifacts

    def write_artifacts(plan, workdir, **kwargs):
        result = original(plan, workdir, **kwargs)
        packet = Path(workdir) / "operator-commands.sh"
        text = plant(packet.read_text(encoding="utf-8"), variant)
        packet.write_text(text, encoding="utf-8")
        # The packet names its source tree; the two trees differ only there.
        packets.append(text.replace(str(tree), "<tree>"))
        return result

    provision.write_artifacts = write_artifacts
    if variant == "dropped":
        provision.RETIRED_STAGED_EXECUTABLES = ()
    try:
        getattr(module, CHECK)()
    except AssertionError as error:
        frames = [f for f in traceback.extract_tb(error.__traceback__) if f.name == CHECK]
        line = frames[-1].line if frames else ""
        return {"passed": False, "line": line, "packets": packets}
    return {"passed": True, "line": "", "packets": packets}


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def child(tree: Path, variant: str) -> dict:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--variant", str(tree), variant],
        capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--variant":
        print(json.dumps(run_variant(Path(sys.argv[2]), sys.argv[3])))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd434.") as tmp:
        before_tree = tree_at(BEFORE, Path(tmp) / "before")
        before = {variant: child(before_tree, variant) for variant in VARIANTS}
        after = {variant: child(ROOT, variant) for variant in VARIANTS}

    # The plants arrived: each packet the checks read is the one described.
    for side, runs in (("before", before), ("after", after)):
        packets = {variant: run["packets"] for variant, run in runs.items()}
        check(all(len(p) == 1 for p in packets.values()),
              f"{side}: every check wrote exactly one packet: { {v: len(p) for v, p in packets.items()} }")
        shipped = packets["shipped"][0]
        check(shipped.count(REMOVAL) == 1 and shipped.count(f"sudo rm -f '{INTEGRATOR}'") == 1
              and INSTALL not in shipped and "NOPASSWD" not in shipped,
              f"{side}: the shipped packet removes both publishers and installs and grants neither")
        check("switchyard-publish-ref" not in packets["dropped"][0]
              and "switchyard-integrate-main" not in packets["dropped"][0],
              f"{side}: with nothing retired the packet never names a publisher")
        check(INSTALL in packets["staged"][0] and REMOVAL not in packets["staged"][0]
              and f"{REMOVAL} && {INSTALL}" in packets["restaged"][0]
              and packets["invoked"][0].endswith(f"sudo {INTEGRATOR} --project otto\n")
              and f"NOPASSWD: {PUBLISHER}" in packets["granted"][0],
              f"{side}: each planted fault is in the packet its check read")
    check(before["shipped"]["packets"] == after["shipped"]["packets"],
          "both trees generate the same shipped packet: the change is the check, not the generator")

    # Main: the shipped packet fails on its own cleanup, and a dropped retirement passes.
    check(not before["shipped"]["passed"]
          and before["shipped"]["line"] == 'assert "switchyard-publish-ref" not in commands, commands',
          f"before: the shipped packet fails on the publisher's name: {before['shipped']['line']!r}")
    check(before["dropped"]["passed"],
          f"before: a packet that retires nothing passes: {before['dropped']['line']!r}")
    check(all(not before[v]["passed"] for v in ("staged", "restaged", "invoked", "granted")),
          f"before: the other faults fail: { {v: before[v]['line'] for v in VARIANTS} }")

    # Now: the shipped packet passes, and every fault fails at the assertion about it.
    check(after["shipped"]["passed"], f"after: the shipped packet passes: {after['shipped']['line']!r}")
    check(not after["dropped"]["passed"] and after["dropped"]["line"] == "assert removals == [",
          f"after: a packet that retires nothing fails on the missing removals: {after['dropped']['line']!r}")
    check(all(not after[v]["passed"] and after[v]["line"] == "assert rest == [], rest"
              for v in ("staged", "restaged", "invoked")),
          f"after: an install, a re-install and an invocation fail as non-removals: "
          f"{ {v: after[v]['line'] for v in ('staged', 'restaged', 'invoked')} }")
    check(not after["granted"]["passed"]
          and after["granted"]["line"] == 'assert "NOPASSWD: /usr/local/lib/switchyard" not in commands, commands',
          f"after: a grant still fails on the grant check: {after['granted']['line']!r}")

    if failures:
        print(f"role_control_retired_cleanup_test: {len(failures)} failed")
        return 1
    print("role_control_retired_cleanup_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
