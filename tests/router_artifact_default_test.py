#!/usr/bin/env python3
"""SYRD-787: `switchyard new --from` a design artifact that turns Router on without a model gets Router's default.

SYRD-779 made a new Router default to Haiku 5.5 at high effort on `switchyard new`'s questions, `team-launcher design`
and `switchyard router-role enable`. A design artifact written by hand, with `project.router = {"enabled": true,
"runtime": "claude"}` and no model or effort, still got nothing filled in, so Router started on Claude's own default,
Opus. Now `router_onboarding.artifact_router` fills in what the artifact leaves out from `router_launcher_settings`.

Through the real `new_project_command --from` (SYRD-599's provisioning fixture, a fake runner), on the projected
launcher entry:
* no model or effort: Haiku 5.5 at high, and the summary says each is Router's default;
* a model named: kept, with the default effort; a model and an effort named: both kept;
* another runtime: nothing recorded, so that runtime's own default.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import router_onboarding_test as r599  # noqa: E402 - SYRD-599's artifact writer and provisioning run
from scripts import router_onboarding, team_launcher  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def hand_written(tmp: Path, name: str, record: dict) -> Path:
    """A design artifact as an operator writes it: SYRD-599's, with `project.router` replaced by hand."""
    path = tmp / name / ".switchyard" / "cerulean.project.json"
    r599.write_artifact(path, tmp / name, role_models={"main": "gpt-5.5"})
    raw = json.loads(path.read_text())
    raw["project"]["router"] = record
    path.write_text(json.dumps(raw, indent=2))
    return path


def router_entry(tmp: Path, name: str, record: dict) -> tuple[dict, str]:
    artifact = hand_written(tmp, name, record)
    out = tmp / f"out-{name}"
    printed = io.StringIO()
    with redirect_stdout(printed):
        code, said = r599.provision(artifact, out)
    check(code == 0, f"{name}: provisioned from the artifact: {said[-600:]}")
    roles = json.loads((out / "cerulean.json").read_text())["roles"]
    return next((r for r in roles if r["role"] == "router"), {}), said + printed.getvalue()


def test_an_artifact_without_a_model_gets_routers_default() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd787.") as raw:
        tmp = Path(raw)
        entry, said = router_entry(tmp, "bare", {"enabled": True, "runtime": "claude"})
        check(entry.get("cli") == ["claude"] and (entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5", "high"),
              f"Router's projected launcher entry: Haiku 5.5 at high, not Claude's own default: {entry}")
        loaded = team_launcher.load_project_design_artifact(tmp / "bare" / ".switchyard" / "cerulean.project.json").router
        check(dict(loaded) == {"runtime": "claude", "model": "claude-haiku-5-5", "effort": "high"},
              f"the artifact reads back with them, so preview, apply and a retry agree: {loaded}")
        line = router_onboarding.summary_line(loaded, project="cerulean")
        check("Router: on (claude -> claude-haiku-5-5 (Router's default) -> effort high (Router's default))" in line,
              f"the summary says each is the default: {line}")

        entry, _said = router_entry(tmp, "model", {"enabled": True, "runtime": "claude", "model": "claude-opus-5-5"})
        check((entry.get("model"), entry.get("effort")) == ("claude-opus-5-5", "high"),
              f"a model the artifact names is kept; the effort it leaves out is the default: {entry}")
        line = router_onboarding.summary_line(
            team_launcher.load_project_design_artifact(tmp / "model" / ".switchyard" / "cerulean.project.json").router)
        check("claude -> claude-opus-5-5 -> effort high (Router's default)" in line,
              f"only the defaulted value is marked: {line}")

        entry, _said = router_entry(tmp, "both", {"enabled": True, "runtime": "claude", "model": "claude-opus-5",
                                                  "effort": "low"})
        check((entry.get("model"), entry.get("effort")) == ("claude-opus-5", "low"),
              f"a model and an effort the artifact names are both kept: {entry}")

        entry, _said = router_entry(tmp, "codex", {"enabled": True, "runtime": "codex"})
        check(entry.get("cli") == ["codex"] and "model" not in entry and "effort" not in entry,
              f"another runtime keeps its own default: nothing recorded: {entry}")


def test_artifact_router_fills_in_only_whats_missing() -> None:
    path = Path("/fixture/p.project.json")
    roles = ("main", "ops")
    check(router_onboarding.artifact_router({"enabled": True, "runtime": "claude"}, path=path, implementer_roles=roles)
          == (("runtime", "claude"), ("model", "claude-haiku-5-5"), ("effort", "high")), "nothing named: both filled")
    check(router_onboarding.artifact_router({"enabled": True, "runtime": "claude", "effort": "medium"}, path=path,
                                            implementer_roles=roles)
          == (("runtime", "claude"), ("model", "claude-haiku-5-5"), ("effort", "medium")), "an effort named: kept")
    check(router_onboarding.artifact_router({"enabled": False, "runtime": "claude"}, path=path, implementer_roles=roles)
          == (), "off stays off: nothing filled in")


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    for test in (test_artifact_router_fills_in_only_whats_missing, test_an_artifact_without_a_model_gets_routers_default):
        test()
    print(f"router_artifact_default_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
