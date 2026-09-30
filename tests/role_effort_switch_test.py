#!/usr/bin/env python3
"""`set-role-runtime --effort` changes an existing role's reasoning effort (SYRD-534).

MEFP moves its Luna workers from GPT-6 Luna high to gpt-5.6-luna xhigh. The
roles carry Codex's effort as a legacy argument -- `extra_args: ["-c",
"model_reasoning_effort=\\"high\\""]`, `effort: null` -- and the command had no
way to choose an effort at all. An explicit effort now goes through the same
switch as a runtime or model change: dry run, busy and readiness gates, the
exact workflow revision, one durable projection, the restart and the
byte-exact rollback. The level is checked against what Codex's own model cache
in the owner's account lists for the model, and every effort setting left in
the arguments is replaced, so exactly one effort reaches the command line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import role_runtime_consistency_test as c  # noqa: E402
import role_runtime_test as base  # noqa: E402
from scripts import project_role_runtime, role_runtime, runtime_arguments, team_launcher  # noqa: E402
from scripts.ticket_board import runtime_catalog  # noqa: E402

CHECKS = 0
LEGACY_HIGH = ["-c", 'model_reasoning_effort="high"']
#: The shape of Codex's own `models_cache.json`, as 0.159.0 writes it.
CODEX_CACHE = json.dumps({"models": [
    {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
    {"slug": "gpt-5.6-luna", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
    {"slug": "gpt-5.6-sol", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
]})


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _codex_role(tmp: Path, *, extra_args=LEGACY_HIGH, effort=None, model="gpt-6-luna", live=(), dies=frozenset()):
    config_path = c._tenant(tmp, cli="codex", live_commands=["codex"])
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in raw["roles"]:
        if entry["role"] == "audit":
            entry.update({"extra_args": list(extra_args), "yolo": True, "model": model})
            if effort is None:
                entry.pop("effort", None)
            else:
                entry["effort"] = effort
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    document = base._workflow_document()
    for role in document["roles"]:
        if role["name"] == "audit":
            role["runtime"] = "codex"
    board = c.StrictBoard(document)
    board.revision = 8
    return config_path, board, c.Host(tmp, config_path, live=tuple(live), dies=set(dies))


def _rendered(config_path: Path) -> list[str]:
    config = team_launcher.load_project_config("porter", config_path)
    role = team_launcher._role_by_name(config, "audit")
    command = team_launcher.cli_command_for_role(role, session_dir=config_path.parent / "sessions")
    return command[command.index(role.cli[0]):]


def _efforts_in(argv: list[str]) -> list[str]:
    return [arg for arg in argv if "model_reasoning_effort" in arg]


@c._sandbox
def test_legacy_high_to_explicit_xhigh_on_the_same_model(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, extra_args=["--search", *LEGACY_HIGH], live=("audit",))
    try:
        check(_efforts_in(_rendered(config_path)) == ['model_reasoning_effort="high"'], _rendered(config_path))
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "codex", effort="xhigh", dry_run=True)
        check(result is not None and result.configured_changed, f"an explicit effort is a change: {refused} {said}")
        check(any("replaced by the chosen effort" in line for line in said)
              and any("becomes `xhigh`" in line for line in said), f"the dry run says what changes: {said}")
        check(config_path.read_bytes() == before and board.revision == 8 and not c._journal(config_path).exists(),
              "and changes nothing")

        result, refused, said = c._switch(config_path, host, board, "codex", effort="xhigh")
        check(result is not None and result.live_session_changed, (refused, said))
        entry = c._entry(config_path)
        check(entry.get("effort") == "xhigh" and entry["extra_args"] == ["--search"] and entry["model"] == "gpt-6-luna",
              f"one canonical effort, other arguments and the model kept: {entry}")
        check(_efforts_in(_rendered(config_path)) == ['model_reasoning_effort="xhigh"'],
              f"exactly one effort reaches the command line: {_rendered(config_path)}")
        started = [call for call in host.calls if call[:2] == ["tmux", "new-session"]
                   and call[call.index("-s") + 1] == "porter-audit"]
        check(started and 'model_reasoning_effort="xhigh"' in started[-1][-1] and '"high"' not in started[-1][-1],
              f"the restarted worker runs at xhigh: {started[-1][-1] if started else None}")
        check(board.runtime_of("audit") == "codex" and board.revision == 9 and not c._journal(config_path).exists(),
              f"board agreement, one exact revision, journal retired: {board.revision}")
        check("still runs codex" in result.describe(), result.describe())
        result, refused, said = c._switch(config_path, host, board, "codex", effort="xhigh", dry_run=True)
        check(result is not None and not result.configured_changed, f"the same choice again is a no-op: {said}")
    finally:
        host.close()


@c._sandbox
def test_model_and_effort_change_together(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, live=("audit",))
    try:
        result, refused, said = c._switch(config_path, host, board, "codex", model="gpt-5.6-luna", effort="xhigh")
        check(result is not None and result.live_session_changed, (refused, said))
        check(any("becomes `xhigh`" in line for line in said) and not result.argument_repair
              and "now runs codex" in result.describe(),
              f"the effort change is listed, and a model change is described as the move it is: {said} {result.describe()}")
        entry = c._entry(config_path)
        check(entry["model"] == "gpt-5.6-luna" and entry.get("effort") == "xhigh" and entry["extra_args"] == [], entry)
        rendered = _rendered(config_path)
        check(rendered[rendered.index("--model") + 1] == "gpt-5.6-luna"
              and _efforts_in(rendered) == ['model_reasoning_effort="xhigh"'], rendered)
    finally:
        host.close()


@c._sandbox
def test_omitted_effort_keeps_the_role_s_own(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, live=("audit",))
    try:
        result, refused, said = c._switch(config_path, host, board, "codex", model="gpt-5.6-luna")
        check(result is not None, (refused, said))
        entry = c._entry(config_path)
        check(entry["extra_args"] == LEGACY_HIGH and "effort" not in entry,
              f"no --effort decides nothing about the effort: {entry}")
    finally:
        host.close()


@c._sandbox
def test_ambiguity_is_refused_until_an_explicit_choice_resolves_it(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, extra_args=["-c", 'model_reasoning_effort="low"'], effort="high",
                                          live=("audit",))
    try:
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "codex", model="gpt-5.6-luna")
        check(result is None and "pass --effort" in refused and "extra_args[0]" in refused, refused)
        check(config_path.read_bytes() == before and board.revision == 8, "nothing changed")
        result, refused, said = c._switch(config_path, host, board, "codex", model="gpt-5.6-luna", effort="high")
        check(result is not None and c._entry(config_path).get("effort") == "high"
              and c._entry(config_path)["extra_args"] == [], (refused, c._entry(config_path)))
    finally:
        host.close()


@c._sandbox
def test_a_busy_role_is_refused(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, live=("audit",))
    try:
        before = config_path.read_bytes()
        config = team_launcher.load_project_config("porter", config_path)
        readiness = role_runtime._readiness_blockers
        role_runtime._readiness_blockers = base._ready
        try:
            role_runtime.switch_role_runtime(
                config, config_path=config_path, role_name="audit", runtime="codex", effort="xhigh",
                environ=base.DIRECTOR_ENV, runner=host, client=board, workflow_reader=board.reader,
                pane_state_dir=config_path.parent / "pane-state", print_func=lambda _l: None,
                busy_check=lambda *a, **k: True,
            )
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
        finally:
            role_runtime._readiness_blockers = readiness
        check("is busy" in refused, f"an effort change waits for an idle role like any switch: {refused!r}")
        check(config_path.read_bytes() == before and board.revision == 8, "and nothing changed")
    finally:
        host.close()


@c._sandbox
def test_a_failed_start_restores_the_role_byte_for_byte(tmp: Path) -> None:
    config_path, board, host = _codex_role(tmp, live=("audit",))
    try:
        # Armed after the live session is up: the restart is the start that dies.
        check(host._alive("porter-audit"), "the role is running before the switch")
        host.dies = {"codex"}
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "codex", effort="xhigh")
        check(result is None, f"a worker that dies on start fails the switch: {said}")
        check(config_path.read_bytes() == before, "the legacy effort and everything else are restored exactly")
        check(board.runtime_of("audit") == "codex", (board.document, board.revision))
    finally:
        host.close()


@c._sandbox
def test_the_command_checks_the_level_against_the_owner_s_codex_cache(tmp: Path) -> None:
    config_path = c._tenant(tmp, cli="codex", live_commands=["codex"])
    config = team_launcher.load_project_config("porter", config_path)
    reads: list[list[str]] = []

    def runner(args, **_kwargs):
        import subprocess
        if list(args[-3:]) == list(runtime_catalog.CODEX_MODEL_CACHE_READ):
            reads.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=CODEX_CACHE, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    switched: list[dict] = []
    original = role_runtime.switch_role_runtime

    def record(config, **kwargs):
        switched.append(kwargs)
        return role_runtime.RuntimeSwitchResult(project="porter", role="audit", previous_runtime="codex", runtime="codex",
                                                configured_changed=True, live_session_changed=False, reconnected_slots=())

    role_runtime.switch_role_runtime = record
    # The owner's account answers, not the caller's: a prefix no host has.
    owner_prefix = ("sudo", "-n", "-u", "syrd534-owner", "--")
    owner_catalog_args = team_launcher._owner_catalog_args
    team_launcher._owner_catalog_args = lambda _config: ("syrd534-owner", owner_prefix)
    said: list[str] = []
    try:
        code = project_role_runtime.set_project_role_runtime_command(
            config, config_path=config_path, role_name="audit", runtime="codex", model="gpt-5.6-luna",
            effort="ultra", runner=runner, interactive=False, print_func=said.append)
        check(code == 1 and not switched and "does not accept effort 'ultra' for gpt-5.6-luna" in " ".join(said),
              f"an effort the model does not list is refused before anything is written: {code} {said}")
        check(reads and all(tuple(read[:len(owner_prefix)]) == owner_prefix for read in reads),
              f"and the answer came from Codex's own cache, read in the owner's account: {reads}")
        said.clear()
        code = project_role_runtime.set_project_role_runtime_command(
            config, config_path=config_path, role_name="audit", runtime="codex", model="gpt-5.6-sol",
            effort="ultra", runner=runner, interactive=False, print_func=said.append)
        check(code == 0 and switched and switched[-1].get("effort") == "ultra",
              f"the same level is accepted for a model that lists it: {code} {said} {switched}")
        switched.clear()
        code = project_role_runtime.set_project_role_runtime_command(
            config, config_path=config_path, role_name="audit", runtime="codex", model="gpt-5.6-luna",
            effort="xhigh", runner=runner, interactive=False, print_func=said.append)
        check(code == 0 and switched and switched[-1].get("effort") == "xhigh" and switched[-1].get("model") == "gpt-5.6-luna",
              f"MEFP's choice passes: {switched}")
        switched.clear()
        code = project_role_runtime.set_project_role_runtime_command(
            config, config_path=config_path, role_name="audit", runtime="agy", model="",
            effort="high", runner=runner, interactive=False, print_func=said.append)
        check(code == 1 and not switched and "does not take an effort level" in " ".join(said), said)
        said.clear()
        code = project_role_runtime.set_project_role_runtime_command(
            config, config_path=config_path, role_name="audit", runtime="codex", model="gpt-9-unlisted",
            effort="ultra", runner=runner, interactive=False, print_func=said.append)
        check(code == 0 and switched and switched[-1].get("effort") == "ultra" and "could not be checked" in " ".join(said),
              f"a model the cache does not describe is not refused on a table's strength, and says so: {code} {said}")
    finally:
        team_launcher._owner_catalog_args = owner_catalog_args
        role_runtime.switch_role_runtime = original
    from scripts import switchyard_parsers
    parsed = switchyard_parsers._build_switchyard_set_role_runtime_parser().parse_args(
        ["mefp", "luna-6", "--cli", "codex", "--model", "gpt-5.6-luna", "--effort", "xhigh", "--dry-run"])
    check((parsed.cli, parsed.model, parsed.effort, parsed.dry_run) == ("codex", "gpt-5.6-luna", "xhigh", True), parsed)
    check(switchyard_parsers._build_switchyard_set_role_runtime_parser().parse_args(["mefp", "luna-6"]).effort is None,
          "omitted means keep")


@c._sandbox
def test_conflicting_carriers_are_refused_until_an_explicit_choice(tmp: Path) -> None:
    """Audit, ae107642: with no recorded effort, duplicates were kept and the last one won."""
    duplicates = ["-c", 'model_reasoning_effort="low"', '--config=model_reasoning_effort="high"']
    config_path, board, host = _codex_role(tmp, extra_args=["--search", *duplicates], live=("audit",))
    try:
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "codex", model="gpt-5.6-luna")
        check(result is None and "conflicting effort levels" in refused and "extra_args[1]" in refused
              and "extra_args[3]" in refused and "pass --effort" in refused, f"Codex: both carriers named: {refused!r}")
        check(config_path.read_bytes() == before and board.revision == 8 and not c._journal(config_path).exists(),
              "and nothing changed")
        result, refused, said = c._switch(config_path, host, board, "codex", effort="xhigh")
        check(result is not None and c._entry(config_path)["extra_args"] == ["--search"]
              and _efforts_in(_rendered(config_path)) == ['model_reasoning_effort="xhigh"'],
              f"an explicit choice resolves it, one effort rendered: {refused} {_rendered(config_path)}")
    finally:
        host.close()
    for runtime, carriers in (("claude", ["--effort", "low", "--effort=high"]),
                              ("hermes", ["--reasoning", "low", "--reasoning=high"])):
        entry = {"cli": [runtime], "extra_args": ["--verbose", *carriers]}
        untouched = json.dumps(entry, sort_keys=True)
        try:
            runtime_arguments.reconcile(entry, runtime=runtime, previous_runtime=runtime)
            refused = ""
        except runtime_arguments.ArgumentRefusal as exc:
            refused = str(exc)
        check("conflicting effort levels" in refused and "'low'" in refused and "'high'" in refused
              and json.dumps(entry, sort_keys=True) == untouched, f"{runtime}: refused, untouched: {refused!r} {entry}")
        runtime_arguments.reconcile(entry, runtime=runtime, previous_runtime=runtime, effort="medium")
        check(entry["extra_args"] == ["--verbose"] and entry["effort"] == "medium", f"{runtime}: the choice resolves it: {entry}")
    # The role's own carrier against one translated from another runtime: the
    # translation would record high while `--effort low` still rendered last.
    entry = {"cli": ["claude"], "extra_args": ["--effort", "low", "-c", 'model_reasoning_effort="high"']}
    untouched = json.dumps(entry, sort_keys=True)
    try:
        runtime_arguments.reconcile(entry, runtime="claude", previous_runtime="codex")
        refused = ""
    except runtime_arguments.ArgumentRefusal as exc:
        refused = str(exc)
    check("conflicting effort levels" in refused and "`--effort low`" in refused and "model_reasoning_effort" in refused
          and json.dumps(entry, sort_keys=True) == untouched, f"own against translated: refused, untouched: {refused!r} {entry}")
    entry = {"cli": ["claude"], "extra_args": ["--effort", "high", "--effort=high"]}
    check(runtime_arguments.reconcile(entry, runtime="claude", previous_runtime="claude") == []
          and entry["extra_args"] == ["--effort", "high", "--effort=high"], f"carriers that agree are not a conflict: {entry}")


def test_a_valueless_effort_flag_never_takes_the_next_option() -> None:
    """Audit, ae107642: `--effort --verbose` with a choice lost --verbose."""
    for runtime, flag in (("claude", "--effort"), ("hermes", "--reasoning")):
        for tail in (["--verbose"], []):
            entry = {"cli": [runtime], "extra_args": [flag, *tail, "--keep"]}
            try:
                notes = runtime_arguments.reconcile(entry, runtime=runtime, previous_runtime=runtime, effort="high")
            except runtime_arguments.ArgumentRefusal as exc:
                notes = [f"REFUSED: {exc}"]
            check(entry["extra_args"] == [*tail, "--keep"] and entry["effort"] == "high"
                  and any(f"`{flag}`, which names no level" in note for note in notes),
                  f"{runtime}: only the valueless flag is replaced; {tail + ['--keep']} stay: {entry} {notes}")
        entry = {"cli": [runtime], "extra_args": [flag, "--verbose"]}
        untouched = json.dumps(entry, sort_keys=True)
        try:
            runtime_arguments.reconcile(entry, runtime=runtime, previous_runtime=runtime)
            refused = ""
        except runtime_arguments.ArgumentRefusal as exc:
            refused = str(exc)
        check(f"`{flag}` with no level after it" in refused and "`--verbose` is another option" in refused
              and json.dumps(entry, sort_keys=True) == untouched, f"{runtime}: without a choice, refused: {refused!r}")
        entry = {"cli": [runtime], "extra_args": [f"{flag}=", "--verbose"]}
        try:
            runtime_arguments.reconcile(entry, runtime=runtime, previous_runtime=runtime)
            refused = ""
        except runtime_arguments.ArgumentRefusal as exc:
            refused = str(exc)
        check(f"`{flag}=` with no level after it" in refused, f"{runtime}: an empty `=` level is refused too: {refused!r}")


@c._sandbox
def test_the_catalog_reads_the_owner_s_cache_whatever_the_caller_s_codex_home(tmp: Path) -> None:
    """Audit, ae107642: the owner prefix set HOME but passed the caller's CODEX_HOME through."""
    import os
    import subprocess
    owner_home, caller_home = tmp / "owner-home", tmp / "caller-codex"
    (owner_home / ".codex").mkdir(parents=True)
    caller_home.mkdir()
    (owner_home / ".codex" / "models_cache.json").write_text(CODEX_CACHE, encoding="utf-8")
    (caller_home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-5.6-luna", "supported_reasoning_levels": [{"effort": "ultra"}]}]}), encoding="utf-8")
    # The real prefix, for an owner who is the caller: no sudo, `env HOME=<owner home> ...`.
    owner_args = tuple(team_launcher._owner_command_env_args(team_launcher.current_user_name(), owner_home, []))
    check(owner_args[:1] == ("env",) and f"HOME={owner_home}" in owner_args, f"the same-user prefix: {owner_args}")
    saved = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(caller_home)
    try:
        # Positive control: through this prefix the old read really did reach the caller's cache.
        old = subprocess.run([*owner_args, "sh", "-c", 'cat "${CODEX_HOME:-$HOME/.codex}/models_cache.json"'],
                             capture_output=True, text=True, check=False)
        check('"ultra"' in old.stdout, f"the caller's CODEX_HOME passes through the prefix: {old.stdout[:120]} {old.stderr[-200:]}")
        levels = runtime_catalog.owner_effort_catalog("codex", "gpt-5.6-luna", runner=subprocess.run, owner_args=owner_args)
        check([choice.value for choice in levels.choices] == ["low", "medium", "high", "xhigh", "max"] and levels.enumerable,
              f"the owner's own cache answers: {levels}")
    finally:
        if saved is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = saved


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_effort_switch_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
