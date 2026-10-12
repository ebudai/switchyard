#!/usr/bin/env python3
"""SYRD-547: the model probe runs the argv the role's pane runs, and a Hermes refusal is shown.

`switchyard validate-models otto` passed `main` (Hermes, glm-5.3, effort high)
while its pane exited 2 at launch (OTTO-1, SYRD-546): the probe,
`hermes -m <model> --yolo -z <prompt>`, carried none of the effort or startup
arguments the pane is started with. And `hermes -z` sends its output to
/dev/null and exits 0 when the provider refuses, so an invalid model id showed
only as "did not answer"; the refusal is in `$HERMES_HOME/sessions/request_dump_*.json`.

Each scenario runs the real `validate_role_models` on a disposable tenant's
Hermes role, in a child against a tree -- main `BEFORE` from a `git archive`,
and this one -- as the owner it already is, with the owner's home in a
temporary directory. The `hermes` on that home's PATH is the installed Hermes
itself, run offline in its own user and network namespace (`unshare -rn`): its
real `main()` parses the probe's argv, and only the one-shot it would send is
stood in -- answering as a model that read the probe file, or refusing as a
provider does, by writing the request dump Hermes's own
`dump_api_request_debug` writes and printing nothing. No provider is reached.

The pane's effort adapter is put back as release 2951c5c9 had it (`--reasoning
<effort>` for Hermes) by rebinding `role_command.effort_args_for_role`, the
function the pane command calls: the probe that runs that pane's argv must now
fail the role, as the pane did.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "4c441951a174c13c543a72b4ad99e2bdda5decde"
HERMES_PYTHON = Path("/opt/hermes/venv/bin/python3")
CHECKS = 0

#: The installed Hermes's own `main()`, with only its one-shot stood in.
STUB = r'''
import json, os, sys, time
from pathlib import Path
import hermes_cli.oneshot as oneshot

def run_oneshot(prompt, model=None, provider=None, toolsets=None):
    mode = os.environ.get("SYRD547_HERMES", "answer")
    if mode == "answer":
        token = (Path.cwd() / "switchyard-model-probe.txt").read_text(encoding="utf-8").strip()
        print(f"model-ok {token}", flush=True)
        return 0
    token = (Path.cwd() / "switchyard-model-probe.txt").read_text(encoding="utf-8").strip()
    home = Path(os.environ.get("HERMES_HOME") or Path(os.environ["HOME"]) / ".hermes")
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    # One dump per failed API call, as Hermes writes them: a retried one first, then the refusal.
    now = time.time()
    for offset, (status, message) in enumerate((
            (503, "upstream connect error, retrying"),
            (400, f"Error code: 400 - {model.replace('.', '-')} is not a valid model ID (echoed {token})"))):
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{offset:06d}"
        payload = {"timestamp": stamp, "session_id": "probe", "reason": "non_retryable_client_error",
                   "request": {"method": "POST", "url": "https://openrouter.ai/api/v1/chat/completions",
                               "headers": {"Authorization": "Bearer sk-or-...abcd"}, "body": {"model": model}},
                   "error": {"type": "APIStatusError", "status_code": status, "message": message}}
        dump = home / "sessions" / f"request_dump_probe_{stamp}.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(dump, (now + offset, now + offset))
    return 0

oneshot.run_oneshot = run_oneshot
import hermes_cli.main as main
sys.argv = ["hermes", *sys.argv[1:]]
main.main()
'''

#: Runs in a child: argv is the tree, the scratch root, and a JSON scenario.
CHILD = r'''
import json, os, subprocess, sys, time
from pathlib import Path
tree, root, scenario = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3])
for extra in (str(tree), str(tree / "tests")):
    sys.path.insert(0, extra)
import team_launcher_presentation_test as fixture
from scripts import role_command, team_launcher

(root / "tenant").mkdir(parents=True)
config_path = fixture._write_presentation_config(root / "tenant")
raw = json.loads(config_path.read_text(encoding="utf-8"))
home = root / "home"
raw["session_dir"] = str(home / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
raw["desktop_access"] = {"mode": "headless"}
for role in raw["roles"]:
    if role["role"] == "app":
        role.update(cli=["hermes"], live_commands=["hermes"], model="glm-5.3", effort="high", yolo=True)
config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if scenario.get("stale_dump"):
    (home / ".hermes" / "sessions").mkdir(parents=True, exist_ok=True)
    stale = home / ".hermes" / "sessions" / "request_dump_old_20260101_000000_000000.json"
    stale.write_text(json.dumps({"error": {"message": "an older refusal that is not this probe's", "status_code": 401}}), encoding="utf-8")
    # Newer by its clock than anything the probe writes: only "was it there before" can pass it over.
    os.utime(stale, (time.time() + 3600, time.time() + 3600))
if scenario.get("old_adapter"):
    current = role_command.effort_args_for_role
    def effort_args_for_role(role):
        # Release 2951c5c9's Hermes style, "reasoning".
        if role.effort and team_launcher._command_name(role.cli[0]) == "hermes":
            return ["--reasoning", role.effort]
        return current(role)
    role_command.effort_args_for_role = effort_args_for_role
config = team_launcher.load_project_config("porter", config_path)
roles = [r for r in config.roles if r.role == "app"]
pane = team_launcher.cli_command_for_role(roles[0], session_dir=team_launcher.role_session_dir(config, roles[0]))
at = max(i for i, word in enumerate(pane) if word == "hermes")
from scripts import model_validation
probe = model_validation._model_validation_command(roles[0], root)
lines = []
failures = team_launcher.validate_role_models(roles, owner_user=scenario["owner"], owner_home=home, runner=subprocess.run, print_func=lines.append)
print(json.dumps({"pane": pane[at + 1:], "probe": probe[1:], "failures": [
    {"role": f.role, "reason": f.reason, "evidence": list(f.evidence)} for f in failures], "printed": lines}))
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def run(tree: Path, **scenario) -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="syrd547-"))
    try:
        bin_dir = scratch / "home" / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        stub = scratch / "hermes_stub.py"
        stub.write_text(STUB, encoding="utf-8")
        hermes = bin_dir / "hermes"
        hermes.write_text(f"#!/bin/sh\nexec unshare -rn {HERMES_PYTHON} -I {stub} \"$@\"\n", encoding="utf-8")
        hermes.chmod(0o755)
        me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(scratch / "home"), "USER": me, "LOGNAME": me,
               "PYTHONDONTWRITEBYTECODE": "1", "SYRD547_HERMES": scenario.pop("hermes", "answer")}
        done = subprocess.run([sys.executable, "-B", "-c", CHILD, str(tree), str(scratch), json.dumps({**scenario, "owner": me})],
                              cwd=tree, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        check(done.returncode == 0, f"the scenario ran in {tree}: {done.stderr[-2000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(scratch)


def archive(commit: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix="syrd547-before-"))
    tarball = subprocess.run(["git", "-C", str(ROOT), "archive", commit], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(base)], input=tarball.stdout, check=True)
    return base


def hermes_present() -> bool:
    if HERMES_PYTHON.exists():
        return True
    print(f"hermes_model_probe_test: {HERMES_PYTHON} is not installed here; the Hermes cases are SKIPPED")
    return False


def test_main_passed_a_role_whose_pane_could_not_start() -> None:
    if os.environ.get("SYRD547_MUTATION_SKIP_BEFORE") or not hermes_present():
        return
    base = archive(BEFORE)
    try:
        got = run(base, old_adapter=True)
        check("--reasoning" in got["pane"] and "--reasoning" not in got["probe"], f"main {BEFORE[:8]}: the pane carries the flag, the probe not: {got}")
        check(got["failures"] == [], f"main {BEFORE[:8]}: validation passed the role whose pane exits 2: {got['failures']}")
        refused = run(base, hermes="refuse")
        evidence = " ".join(refused["failures"][0]["evidence"]) if refused["failures"] else ""
        check("did not answer" in evidence and "not a valid model ID" not in evidence,
              f"main {BEFORE[:8]}: a provider refusal shows only as 'did not answer': {evidence}")
    finally:
        shutil.rmtree(base)


def test_the_probe_runs_the_panes_argv() -> None:
    if not hermes_present():
        return
    got = run(ROOT)
    check(got["probe"][:-2] == got["pane"] and got["probe"][-2] == "-z",
          f"the probe is the pane's argv, then the one-shot prompt: pane {got['pane']} probe {got['probe']}")
    check(got["pane"] == ["-m", "glm-5.3", "--yolo", "--accept-hooks", "--pass-session-id"], f"as SYRD-546 left it: {got['pane']}")
    check(got["failures"] == [], f"and a model that reads the file passes: {got['failures']}")


def test_a_pane_argv_hermes_rejects_now_fails_the_probe() -> None:
    if not hermes_present():
        return
    got = run(ROOT, old_adapter=True)
    check("--reasoning" in got["pane"] and got["probe"][:-2] == got["pane"], f"pane and probe both carry it: {got['probe']}")
    check(len(got["failures"]) == 1 and got["failures"][0]["role"] == "app", f"the role fails validation: {got['failures']}")
    failure = got["failures"][0]
    check("hermes: error: argument command: invalid choice: 'high'" in failure["reason"]
          and failure["evidence"] and failure["evidence"][0].startswith("attempt 1 (exit 2) did not answer"),
          f"with Hermes's own refusal of the argv, exit 2, as otto's pane had: {failure}")


def test_a_provider_refusal_is_shown_from_the_request_dump() -> None:
    if not hermes_present():
        return
    got = run(ROOT, hermes="refuse", stale_dump=True)
    check(len(got["failures"]) == 1 and got["failures"][0]["reason"] == "model probe did not confirm model-ok", got["failures"])
    evidence = got["failures"][0]["evidence"]
    check(evidence and all("provider error (HTTP 400) in request_dump_probe_" in line and "glm-5-3 is not a valid model ID" in line for line in evidence),
          f"the provider's own error, from the dump this attempt left: {evidence}")
    check(not any("older refusal" in line for line in evidence), f"never a dump that was there before, however new: {evidence}")
    check(not any("retrying" in line for line in evidence), f"and of this attempt's dumps, the last: {evidence}")
    check(all("(echoed <token>)" in line for line in evidence), f"and the probe's token is removed from it: {evidence}")


def test_every_runtime_probes_with_its_panes_effort() -> None:
    sys.path.insert(0, str(ROOT))
    from types import SimpleNamespace

    from scripts import model_validation, role_command
    for cli, model_arg, expected in (("claude", "--model", ["--effort", "high"]),
                                     ("codex", "-m", ["-c", 'model_reasoning_effort="high"'])):
        role = SimpleNamespace(role="r", cli=[cli], model="m1", model_arg=model_arg, effort="high", yolo=True, extra_args=[])
        probe = model_validation._model_validation_command(role, Path("/ws"))
        effort = role_command.effort_args_for_role(role)
        check(effort == expected and any(probe[i:i + len(effort)] == effort for i in range(len(probe))),
              f"{cli}: the probe carries the pane's effort {effort}: {probe}")


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print(f"hermes_model_probe_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
