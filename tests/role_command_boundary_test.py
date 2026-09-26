#!/usr/bin/env python3
"""SYRD-290: the role-command and model-validation modules' boundary with the launcher.

A role's CLI command construction moved into `scripts/role_command.py`, and
checking a role's model into `scripts/model_validation.py`, unchanged. This
pins what makes that safe:

- Neither module imports the launcher at its top: the launcher imports them.
  `model_validation` stands on `role_command` (for `YOLO_ARGS_BY_CLI`), never
  the reverse.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it. Driven here with no agent CLI run
  and no model asked anything.

The bytes of what is built are pinned elsewhere: `codex_effort_config_key_test`
covers Codex's `model_reasoning_effort`, and the SYRD-290 packet records a
192-case argv matrix that is byte-identical to the baseline.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = {
    "scripts.role_command": (
        "DEFAULT_MODEL_ARG_BY_CLI", "DEFAULT_RESUME_FLAG_BY_CLI", "DEFAULT_RESUME_MODE_BY_CLI",
        "DEFAULT_RESUME_SUBCOMMAND_BY_CLI", "EFFORT_STYLE_BY_CLI", "STARTUP_ARGS_BY_CLI", "YOLO_ARGS_BY_CLI",
        "cli_command_for_role", "effort_args_for_role", "hermes_env_for_role", "startup_args_for_role",
        "yolo_args_for_role",
    ),
    "scripts.model_validation": (
        "MODEL_PROBE_EVIDENCE_CHARS", "MODEL_PROBE_FILENAME", "MODEL_PROBE_NO_TOOL_CALL_REASON",
        "MODEL_PROBE_TOOL_CALL_ATTEMPTS", "MODEL_VALIDATION_PROMPT", "ModelProbeAttempt", "ModelValidationFailure",
        "_effort_field", "_model_failure_suggestion", "_model_field", "_model_probe_called_a_tool",
        "_model_probe_evidence", "_model_validation_command", "_model_validation_passed", "_ModelProbeWorkspace",
        "confirm_unknown_models_with_owner", "record_role_model", "report_models_were_not_probed",
        "stop_before_launch_for_unknown_models", "validate_role_models",
    ),
}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_modules_import_without_the_launcher_and_in_one_direction() -> None:
    result = python(
        "import sys; import scripts.role_command; "
        "print('scripts.team_launcher' in sys.modules, 'scripts.model_validation' in sys.modules); "
        "import scripts.model_validation; print('scripts.team_launcher' in sys.modules)"
    )
    check(result.returncode == 0, f"both import on their own: {result.stderr[-600:]}")
    check(result.stdout.split() == ["False", "False", "False"],
          f"neither pulls the launcher in, and role_command does not pull model_validation in: {result.stdout!r}")


def test_every_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.model_validation", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.role_command", "scripts.model_validation"),
                  ("scripts.role_command", "scripts.team_launcher")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t; "
            f"table = {EXPORTED!r}; "
            "print(all(getattr(t, n) is getattr(importlib.import_module(m), n) for m, ns in table.items() for n in ns))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def _role(tmp: Path, cli: str, effort: str = "high"):
    import json

    from scripts import team_launcher

    path = tmp / f"{cli}.json"
    path.write_text(json.dumps({"project": "porter", "board_url": "http://127.0.0.1:1/", "roles": [
        {"role": "ops", "cli": [cli], "slot": 0, "workdir": str(tmp / "ops"), "effort": effort}]}), encoding="utf-8")
    return team_launcher.load_project_config("porter", path).roles[0]


def test_launcher_patches_reach_the_moved_code() -> None:
    from scripts import model_validation, role_command, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd290-seam.") as raw:
        tmp = Path(raw)
        role = _role(tmp, "codex")
        # The same role, whose CLI is now called something the adapters do not know.
        renamed = team_launcher.replace(role, cli=["renamed-cli"])
        try:
            role_command.effort_args_for_role(renamed)
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
        check("unsupported effort cli 'renamed-cli'" in refused,
              f"unpatched, an effort on a CLI the adapters do not know is refused: {refused!r}")
        asked: list[str] = []
        saved = (team_launcher._command_name, team_launcher._role_cli_name)
        team_launcher._command_name = lambda value: asked.append(f"name:{value}") or (
            "codex" if str(value) == "renamed-cli" else saved[0](value))
        team_launcher._role_cli_name = lambda r: asked.append("role-cli") or "claude"
        try:
            try:
                effort = role_command.effort_args_for_role(renamed)
            except SystemExit as exc:  # a refusal is the finding, reported below
                effort = [f"refused: {exc}"]
            command = model_validation._model_validation_command(team_launcher.replace(role, model="some-model"), tmp)
        finally:
            team_launcher._command_name, team_launcher._role_cli_name = saved
    check(effort == ["-c", 'model_reasoning_effort="high"'],
          f"the effort adapter asked the launcher's patched name lookup: {effort}")
    check("name:renamed-cli" in asked, f"through that lookup: {asked}")
    check("role-cli" in asked and command is not None
          and all(word in command for word in role_command.YOLO_ARGS_BY_CLI["claude"]),
          f"the probe command asked the launcher which CLI this is, and used that CLI's approval flags: {command}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"role_command_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
