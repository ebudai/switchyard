#!/usr/bin/env python3
"""SYRD-779: a new Router defaults to Haiku 5.5 at high effort, on both paths that create one.

The User, 2026-11-15: "default router model should be haiku 5.5 high, not opus 5". syrd's own Router was set to it by
hand. A Router the product created got Claude's default (Opus 5) at low effort from `switchyard new`, or its template
role's model and effort from `switchyard router-role enable`.

* `switchyard new`: the real Router prompt (`router_onboarding.prompt_router`, then `prompt_router_runtime`) offers
  `claude-haiku-5-5` and `high` in brackets on claude, and Enter records them in the design artifact the real writer
  writes and on Router's projected launcher entry (`record_router_runtime`). On another runtime it keeps that
  runtime's own first model, at low effort, as before.
* `switchyard router-role enable`, through the real `switchyard_main` against a real board (SYRD-599's
  `router_role_command_test.Board`) and a tenant projection whose template role runs another model: Haiku 5.5 at high
  on claude unless `--model` or `--effort` names others; on another runtime, nothing recorded, so the runtime's own
  default. The template role's model and effort are never Router's.
* The default is one constant, checked against the runtime catalog, so a model the catalog does not know is refused.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import router_role_command_test as r599  # noqa: E402 - SYRD-599's real board and tenant fixture
from scripts import router_onboarding, team_launcher  # noqa: E402
from scripts.ticket_board import router_setup, runtime_catalog  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
TEMPLATE_MODEL, TEMPLATE_EFFORT = "the-template-model", "medium"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def operator(*answers: str):
    queue, asked, said = list(answers), [], []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"FAILED CHECK: asked more than was answered: {prompt!r}")
        return queue.pop(0)

    return ask, asked, said


def router(*answers: str) -> tuple[object, list[str], list[str]]:
    ask, asked, said = operator(*answers)
    try:
        selection = router_onboarding.prompt_router(["main", "ops"], runner=None, owner_args=(), unverified_because="",
                                                    input_func=ask, print_func=said.append)
    except (ValueError, SystemExit) as exc:  # a refusal is a finding, not a crash
        raise AssertionError(f"FAILED CHECK: Router's questions refused ({answers}): {exc}") from None
    return selection, asked, said


# -- switchyard new -------------------------------------------------------------------------------------------------
def test_new_offers_haiku_high_and_enter_takes_them() -> None:
    selection, asked, said = router("on", "", "", "")
    check((selection.cli, selection.model, selection.effort) == ("claude", "claude-haiku-5-5", "high"),
          f"Enter at each Router question takes claude, Haiku 5.5, high: {selection}")
    check(asked == ["Router (optional) [off]: ", "router runtime [Claude Code]: ", "router model [Haiku 5.5]: ",
                    "router effort [high]: "], f"the defaults are shown in brackets: {asked}")
    check(any("on claude it starts on Haiku 5.5 at high effort" in line
              and "on another runtime on that runtime's own default model" in line for line in said),
          f"and the menu says so, for claude and for every other runtime: {said[:4]}")
    check(any("Opus 5.5 --" in line for line in said) and any("Opus 5 --" in line for line in said),
          f"the other Claude models are still offered: {said}")
    chosen, _asked, _said = router("on", "", "2", "low")
    check((chosen.model, chosen.effort) == ("claude-opus-5", "low"), f"and chosen by number or name: {chosen}")


def test_new_records_them_in_the_design_record_and_the_launcher_entry() -> None:
    selection, _asked, _said = router("on", "", "", "")
    pairs = router_onboarding.router_pairs(selection)
    check(pairs == (("runtime", "claude"), ("model", "claude-haiku-5-5"), ("effort", "high")),
          f"the design record's pairs: {pairs}")
    with tempfile.TemporaryDirectory(prefix="syrd779.") as raw:
        tmp = Path(raw)
        artifact_path = tmp / "repo" / ".switchyard" / "p779.project.json"
        artifact_path.parent.mkdir(parents=True)
        team_launcher._write_initial_switchyard_project_artifact(
            project_name="P779", slug="p779", owner_user="p779-agent", project_dir=tmp / "repo",
            artifact_path=artifact_path, design_document=tmp / "repo" / "design.md", owner_shell="/bin/sh",
            implementer_roles=("main", "ops"), role_clis=(("director", "claude"), ("main", "codex"), ("ops", "codex")),
            include_designer=False, include_audit=False, router=pairs)
        recorded = json.loads(artifact_path.read_text())["project"].get("router")
        check(recorded == {"enabled": True, "runtime": "claude", "model": "claude-haiku-5-5", "effort": "high"},
              f"the design artifact records Router's model and effort: {recorded}")
        loaded = team_launcher.load_project_design_artifact(artifact_path).router
        check(dict(loaded) == {"runtime": "claude", "model": "claude-haiku-5-5", "effort": "high"},
              f"and reads them back for --from and resume: {loaded}")
        config_path = tmp / "p779.json"
        config_path.write_text(json.dumps({"project": "p779", "roles": [
            {"role": "main", "cli": ["codex"], "model": TEMPLATE_MODEL, "effort": TEMPLATE_EFFORT},
            {"role": "router", "cli": ["claude"], "model": TEMPLATE_MODEL, "effort": TEMPLATE_EFFORT}]}))
        router_onboarding.record_router_runtime(config_path, None, router=loaded)
        entry = next(r for r in json.loads(config_path.read_text())["roles"] if r["role"] == "router")
        check((entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5", "high"),
              f"Router's projected launcher entry carries them, not its template's: {entry}")


def test_new_on_another_runtime_keeps_its_own_default() -> None:
    selection, asked, _said = router("on", "codex", "", "")
    first = runtime_catalog.RECORDED_MODELS["codex"][0]
    check((selection.cli, selection.model, selection.effort) == ("codex", first.value, "low"),
          f"on codex: codex's own first model, at low effort, as before: {selection}")
    check(f"router model [{first.label}]: " in asked and "router effort [low]: " in asked,
          f"shown in brackets: {asked}")
    selection, asked, _said = router("on", "agy", "")
    check(selection.cli == "agy" and selection.model == runtime_catalog.RECORDED_MODELS["agy"][0].value
          and selection.effort == "" and not any("effort" in prompt for prompt in asked),
          f"on agy: its own first model, and no effort question for a runtime that takes none: {selection} {asked}")


def test_the_design_command_records_the_defaults_too() -> None:
    from scripts.ticket_board.router_setup import router_launcher_settings

    check(router_launcher_settings("claude") == {"model": "claude-haiku-5-5", "effort": "high"}
          and router_launcher_settings("claude", model="claude-opus-5-5") == {"model": "claude-opus-5-5",
                                                                               "effort": "high"}
          and router_launcher_settings("codex") == {"model": "", "effort": ""},
          "`team-launcher design --router-runtime` and router-role enable share one rule")
    source = (ROOT / "scripts" / "project_design_command.py").read_text(encoding="utf-8")
    check("router_onboarding.router_launcher_settings(router_runtime, model=router_model" in source,
          "the design command records Router through it")


# -- the constant, against the catalog ------------------------------------------------------------------------------
def test_the_default_is_one_constant_the_catalog_knows() -> None:
    check((router_setup.ROUTER_DEFAULT_MODEL, router_setup.ROUTER_DEFAULT_EFFORT) == ("claude-haiku-5-5", "high")
          and router_setup.router_defaults("claude") == ("claude-haiku-5-5", "high")
          and router_setup.router_defaults("codex") == ("", ""),
          "Haiku 5.5 at high on claude, nothing on another runtime")
    check("claude-haiku-5-5" in [c.value for c in runtime_catalog.RECORDED_MODELS["claude"]],
          "the runtime catalog offers it")
    with patch.object(router_setup, "ROUTER_DEFAULT_MODEL", "claude-unknown-9"):
        try:
            router_setup.router_defaults("claude")
            refused = ""
        except ValueError as exc:
            refused = str(exc)
    check("claude-unknown-9" in refused and "runtime catalog" in refused,
          f"a model the catalog does not know fails loudly: {refused!r}")


# -- switchyard router-role enable ----------------------------------------------------------------------------------
def enable_cases(cluster: object, board: object) -> None:
    launcher_dir = cluster.root / "launcher"
    launcher_dir.mkdir()
    project_dir = cluster.root / "project"
    (project_dir / "docs" / "onboarding").mkdir(parents=True)
    (cluster.root / "cerulean.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
    config_path = launcher_dir / "cerulean.json"
    config_path.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "board_url": board.base,
        "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3, "model": TEMPLATE_MODEL,
                   "effort": TEMPLATE_EFFORT}],
        "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
    }))
    r599.t.TEST_WRITE_TOKEN = board.server.write_token
    r599.t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                     {"document": validate(copy.deepcopy(r599.SHIPPED)), "expected_revision": 0, "dry_run": False},
                     caller="director")
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    importlib.reload(importlib.import_module("scripts.workflow_manage"))
    launcher = importlib.import_module("scripts.team_launcher")
    previous = launcher.DEFAULT_CONFIG_DIR
    launcher.DEFAULT_CONFIG_DIR = launcher_dir

    def run(*extra: str) -> tuple[object, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = launcher.switchyard_main(["router-role", "cerulean", *extra])
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:  # noqa: BLE001 - a fault in the command is a failed check, not a crash
                code = f"{type(exc).__name__}: {exc}"
        entry = None
        if code == 0 and "--dry-run" in extra:
            projected = json.loads(out.getvalue())["projection"][str(config_path)]
            entry = next((r for r in projected["roles"] if r["role"] == "router"), None)
        return code, entry, err.getvalue()

    try:
        before = board.document()
        code, entry, err = run("enable", "--runtime", "claude", "--dry-run")
        check(code == 0 and entry is not None and (entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5",
                                                                                               "high"),
              f"enable on claude without --model: Haiku 5.5 at high, never the template's: {code} {entry} {err[-300:]}")
        code, entry, err = run("enable", "--runtime", "claude", "--model", "claude-opus-5-5", "--dry-run")
        check(code == 0 and (entry.get("model"), entry.get("effort")) == ("claude-opus-5-5", "high"),
              f"--model names Router's model, the effort still its default: {code} {entry} {err[-300:]}")
        code, entry, err = run("enable", "--runtime", "claude", "--effort", "low", "--dry-run")
        check(code == 0 and (entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5", "low"),
              f"--effort names Router's effort: {code} {entry} {err[-300:]}")
        code, entry, err = run("enable", "--runtime", "codex", "--template-role", "main", "--dry-run")
        check(code == 0 and "model" not in entry and "effort" not in entry and entry.get("cli") == ["codex"],
              f"another runtime: nothing recorded, so the runtime's own default -- not the template's "
              f"{TEMPLATE_MODEL}: {code} {entry} {err[-300:]}")
        code, entry, err = run("enable", "--runtime", "codex", "--model", "gpt-5.5", "--effort", "medium", "--dry-run")
        check(code == 0 and (entry.get("model"), entry.get("effort")) == ("gpt-5.5", "medium"),
              f"and named ones on any runtime: {code} {entry} {err[-300:]}")
        code, _entry, err = run("enable", "--runtime", "agy", "--effort", "high", "--dry-run")
        check(code not in (0, None) and "takes no effort level" in err,
              f"an effort for a runtime that takes none is refused: {code} {err[-300:]}")
        check(board.document() == before, "the dry runs changed nothing")
        code, _entry, err = run("enable", "--runtime", "claude")
        launched = json.loads(config_path.read_text())
        entry = next((r for r in launched["roles"] if r["role"] == "router"), {})
        check(code == 0 and (entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5", "high"),
              f"applied, the launcher's Router runs Haiku 5.5 at high: {code} {entry} {err[-300:]}")
        main_entry = next((r for r in launched["roles"] if r["role"] == "main"), {})
        check((main_entry.get("model"), main_entry.get("effort")) == (TEMPLATE_MODEL, TEMPLATE_EFFORT),
              f"and the template role keeps its own: {main_entry}")
        code, _entry, err = run("disable", "--model", "x")
        check(code not in (0, None) and "--model and --effort" in err, f"disable takes no model: {code} {err[-200:]}")
        # An existing Router's recorded model and effort are left alone by a later projection.
        from scripts.workflow_launcher import projection_files

        again = json.loads(projection_files(config_path, board.document())[config_path])
        entry = next((r for r in again["roles"] if r["role"] == "router"), {})
        check((entry.get("model"), entry.get("effort")) == ("claude-haiku-5-5", "high"),
              f"a later projection keeps Router's recorded model and effort: {entry}")
    finally:
        launcher.DEFAULT_CONFIG_DIR = previous


def test_router_role_enable() -> None:
    saved = {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith(("TICKET_BOARD_",
                                                                                   "PGU_TICKET_BOARD_",
                                                                                   "DIRECTORCTL"))]}
    try:
        with temporary_cluster(prefix="syrd779-", shutdown="immediate") as cluster:
            board = r599.Board(cluster, "command", migrate=True)
            try:
                enable_cases(cluster, board)
            finally:
                board.close()
    finally:
        for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_"))]:
            os.environ.pop(key)
        os.environ.update(saved)


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"router_default_model_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
