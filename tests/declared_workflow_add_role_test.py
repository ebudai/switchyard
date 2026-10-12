#!/usr/bin/env python3
"""SYRD-510: add-role on a declared-workflow tenant names the supported path, and every command in it runs.

MEFP's Director ran `switchyard add-role mefp luna-7 --cli codex --detached
--no-start`, as the director skill's add-role line says, and got only "use
ticket-board-workflow apply to update configured roles and stages": no role,
and no path. A declared-workflow tenant's roles are its workflow document's, so
the refusal itself is right; what it owed was the way through.

Each case is a disposable tenant -- a launcher config with a declared workflow
and a worker pool, in a temporary tree -- and the real `add_project_role_command`
with a runner that refuses everything. The refusal must leave every file as it
was and run nothing; each command it prints must parse with its own real
parser; and what it says a new role copies must be what the real pool expansion
and projection give one.
"""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import worker_pool_lifecycle_test as lifecycle  # noqa: E402
from scripts import team_launcher, worker_pool, workflow_launcher, workflow_manage  # noqa: E402
from scripts.launcher_parser import _build_parser as team_launcher_parser  # noqa: E402
from scripts.switchyard_parsers import _build_switchyard_add_role_parser, _build_switchyard_set_role_runtime_parser  # noqa: E402
from scripts.worker_pool_command import _build_switchyard_worker_pool_parser  # noqa: E402

CHECKS = 0
POOL = {"name": "luna", "runtime": "codex", "size": 6, "kind": "implementer"}


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def declared_tenant(tmp: Path, *, pool: dict | None = POOL) -> tuple[object, Path]:
    workers = [f"luna-{n}" for n in range(1, 7)] if pool else []
    config, path = lifecycle.config_with_pool(tmp, pool=pool or {}, workers=workers)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["workflow"] = lifecycle.base_document(lifecycle.PROJECT)
    if not pool:
        raw.pop("worker_pool", None)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(lifecycle.PROJECT, path), path


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def refuse(tmp: Path, **kwargs) -> tuple[str, list, dict, dict]:
    config, path = declared_tenant(tmp, **kwargs)
    before = snapshot(tmp)
    ran: list = []

    def runner(args, **_kw):
        ran.append(list(args))
        raise AssertionError(f"add-role ran something on a declared tenant: {args}")

    printed: list[str] = []
    try:
        team_launcher.add_project_role_command(
            config, config_path=path, role_name="luna-7", cli="codex", detached=True, start=False,
            runner=runner, print_func=printed.append, interactive=False)
        message = ""
    except SystemExit as exc:
        message = str(exc)
    return message, ran, before, snapshot(tmp)


def commands(message: str) -> list[list[str]]:
    return [shlex.split(line.strip()) for line in message.splitlines() if line.startswith("  ")]


def test_the_command_the_director_ran_is_the_one_refused() -> None:
    parser = _build_switchyard_add_role_parser()
    args = parser.parse_args(["mefp", "luna-7", "--cli", "codex", "--detached", "--no-start"])
    check((args.project, args.role, args.cli, args.detached, args.no_start) == ("mefp", "luna-7", "codex", True, True),
          f"MEFP's command parses as a detached, unstarted codex role: {args}")


def test_a_declared_tenant_is_refused_with_its_path_and_nothing_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd510-") as raw:
        message, ran, before, after = refuse(Path(raw))
    check(message.startswith(f"switchyard: {lifecycle.PROJECT} runs a declared workflow")
          and "Nothing was changed." in message.splitlines()[0], f"refused, and says nothing changed: {message[:200]}")
    check(ran == [] and before == after, f"nothing ran and every file is byte-identical: {ran}")
    check("ticket-board-workflow apply to update configured roles and stages" != message, "not the old dead end")
    lines = message.splitlines()
    check(any("raise worker_pool.size" in line and "luna pool (6 declared)" in line for line in lines),
          "the pool route, with the pool's name and size")
    check("default model and effort" in message and "set-role-runtime" in message,
          "and says a pool worker starts at the runtime's defaults, and how to set them")
    check(any("template_role" in line and "model and effort" in line for line in lines), "the document route")


def test_a_tenant_without_a_pool_is_given_the_document_route_only() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd510-") as raw:
        message, ran, before, after = refuse(Path(raw), pool=None)
    check(message and "worker_pool.size" not in message and "worker-pool" not in message and "template_role" in message
          and ran == [] and before == after, f"no pool, no pool route: {message[:300]}")


def test_every_printed_command_parses_with_its_own_parser() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd510-") as raw:
        message, *_ = refuse(Path(raw))
    printed = commands(message)
    check(len(printed) == 9, f"nine commands: {printed}")
    parsed = []
    for argv in printed:
        filled = [{"<worker>": "luna-8", "<document.json>": "/tmp/doc.json", "<revision>": "3", "<before.json>": "/tmp/b.json",
                   "<runtime>": "codex", "<model>": "gpt-5.5", "<effort>": "high"}.get(word, word) for word in argv]
        program = Path(filled[0]).name
        if program == "switchyard" and filled[1] == "worker-pool":
            args = _build_switchyard_worker_pool_parser().parse_args(filled[2:])
            parsed.append(("worker-pool", args.action, args.member, args.apply_changes))
        elif program == "switchyard" and filled[1] == "set-role-runtime":
            args = _build_switchyard_set_role_runtime_parser().parse_args(filled[2:])
            parsed.append(("set-role-runtime", args.role, args.cli, args.model, args.effort))
        elif program == "ticket-board-workflow":
            check(Path(filled[0]).resolve() == ROOT / "scripts" / "ticket-board-workflow", f"this release's own: {filled[0]}")
            if filled[1] == "prepare-role":
                with mock.patch.object(workflow_manage, "prepare_role") as prepare:
                    workflow_manage.main(filled[1:])
                parsed.append(("prepare-role", *prepare.call_args.args))
            else:
                # Parsed by the real CLI, which then asks the board for the active workflow: stopped there.
                with mock.patch.object(workflow_manage.urllib.request, "urlopen", side_effect=RuntimeError("parsed")):
                    try:
                        workflow_manage.main(filled[1:])
                    except RuntimeError as exc:
                        parsed.append((filled[1], str(exc), "--dry-run" in filled))
        elif program == "team-launcher":
            check(Path(filled[0]).resolve() == ROOT / "scripts" / "team-launcher", f"this release's own: {filled[0]}")
            args = team_launcher_parser().parse_args(filled[1:])
            parsed.append(("team-launcher", args.project, args.command, args.pane_mode, args.role, Path(args.config).name if args.config else None))
        else:
            check(False, f"an unexpected command: {argv}")
    config_path = f"{lifecycle.PROJECT}.json"
    check(parsed[:2] == [("worker-pool", "apply", "", False), ("worker-pool", "apply", "", True)]
          and parsed[2][0] == "prepare-role" and Path(parsed[2][1]).name == config_path and parsed[2][2] == "luna-8"
          and parsed[3] == ("worker-pool", "start", "luna-8", False), f"the pool route, on the tenant's own config: {parsed[:4]}")
    check(parsed[4:6] == [("apply", "parsed", True), ("apply", "parsed", False)], f"the document route: {parsed[4:6]}")
    check(parsed[6][0] == "prepare-role" and Path(parsed[6][1]).name == config_path and parsed[6][2] == "luna-7"
          and parsed[7] == ("team-launcher", lifecycle.PROJECT, "pane", "attach-or-start", "luna-7", config_path)
          and parsed[8] == ("set-role-runtime", "luna-7", "codex", "gpt-5.5", "high"), f"and the rest: {parsed[6:]}")


def test_what_it_says_a_new_role_copies_is_what_the_projection_gives() -> None:
    document = lifecycle.base_document(lifecycle.PROJECT)
    template = {"role": "main", "slot": 1, "target": f"{lifecycle.PROJECT}-main:0.0", "cli": ["codex", "--search"],
                "model": "gpt-5.5", "effort": "high"}
    raw = {"project": lifecycle.PROJECT, "roles": [template], "worktree_base": "/srv/wt"}
    # The pool route: the next member, the template's stages and actors, the runtime's defaults.
    pool = team_launcher.parse_worker_pool({**POOL, "size": 1})
    declared, _changes = worker_pool.expand_pool(document, pool, project=lifecycle.PROJECT)
    worker = next(r for r in declared["roles"] if r["name"] == "luna-1")
    owners = [s["name"] for s in declared["stages"] if "luna-1" in (s.get("owners") or [])]
    projected = workflow_launcher.project_roles(raw, declared)
    entry = next(r for r in projected["roles"] if r["role"] == "luna-1")
    check(owners == ["in_progress"] and "template_role" not in worker and "model" not in entry and "effort" not in entry
          and entry["cli"] == ["codex"], f"a pool worker owns the template's stage, at the runtime's defaults: {entry}")
    # The document route: template_role copies the launcher entry's model and effort, and its arguments on the same runtime.
    own = copy.deepcopy(document)
    own["roles"].append({**copy.deepcopy(next(r for r in own["roles"] if r["name"] == "main")), "name": "luna-7",
                         "label": "Luna 7", "target": f"{lifecycle.PROJECT}-luna-7:0.0", "slot": None,
                         "ephemeral": True, "template_role": "main"})
    for stage in own["stages"]:
        if "main" in (stage.get("owners") or []):
            stage["owners"] = [*stage["owners"], "luna-7"]
    for transition in own["transitions"]:
        if "main" in transition["actors"]:
            transition["actors"] = [*transition["actors"], "luna-7"]
    entry = next(r for r in workflow_launcher.project_roles(raw, own)["roles"] if r["role"] == "luna-7")
    check((entry["model"], entry["effort"], entry["cli"], entry.get("detached"), entry.get("ephemeral"))
          == ("gpt-5.5", "high", ["codex", "--search"], True, True),
          f"template_role gives model, effort and arguments; slot null a detached session; ephemeral a fresh one: {entry}")


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print(f"declared_workflow_add_role_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
