#!/usr/bin/env python3
"""SYRD-886: a new Claude role nobody chose a model for is configured with Opus 5.5, not Opus 5.

The User, 2026-11-26: "I would like the default version for opus to be 5.5 instead of 5". SYRD-778 had already put
`claude-opus-5-5` first in the runtime catalog, so `switchyard new` offered it. Two places still said Opus 5: the
bundled `config/team-launcher/pgu.json` (director, research and audit), and the catalog's description of Opus 5
("configured for director, research and audit"). `switchyard add-role` configured no model at all, so its Claude role
ran whatever the installed claude picked.

* **One default.** `runtime_catalog.default_model("claude")` is the catalog's first Claude model, `claude-opus-5-5`.
  Every other runtime keeps its own default (""). Opus 5 is still offered, as an explicit choice only.
* **`switchyard new`.** Its real role question (`_prompt_role_runtime_plan`) records `claude-opus-5-5` for a Claude
  role, both with Enter at a terminal and with nobody to ask. The real config writer then writes that model.
* **`switchyard add-role`.** The real command, against a real generated config in a temporary directory, writes
  `claude-opus-5-5` for a Claude role, and still no model for a Codex one.
* **The bundled config.** The real loader reads `config/team-launcher/pgu.json`, and every Claude role in it (director,
  research, audit) runs `claude-opus-5-5`. Nothing in it names Opus 5.

Guard-only (SYRD-808): `host_state_guard` refuses any read or exec of this host's Switchyard state and privileged
tools, and the last check fails on any reach. Files are written only under temporary directories; every command
add-role would run goes to a recording fake. Run it as an ordinary account, never as root.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import host_state_guard  # noqa: E402  - before any production module: no host state (SYRD-808)

host_state_guard.install()

import json  # noqa: E402
import tempfile  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from io import StringIO  # noqa: E402
from pathlib import Path  # noqa: E402

from team_launcher_test_helpers import KeywordRecordingFakeRunner, load_project_config, team_launcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPUS_5_5, OPUS_5 = "claude-opus-5-5", "claude-opus-5"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def test_the_catalog_names_one_claude_default() -> None:
    catalog = team_launcher.runtime_catalog
    check(catalog.default_model("claude") == OPUS_5_5, f"Claude's default is Opus 5.5: {catalog.default_model('claude')!r}")
    offered = catalog.model_catalog("claude").choices
    check(offered[0].value == catalog.default_model("claude"),
          f"and it is the model a Claude role is offered first: {[c.value for c in offered]}")
    opus_5 = [c for c in offered if c.value == OPUS_5]
    check(len(opus_5) == 1 and "configured for" not in opus_5[0].description,
          f"Opus 5 is still offered, described as no role's configuration: {opus_5}")
    others = {runtime: catalog.default_model(runtime) for runtime in ("codex", "agy", "hermes", "copilot", "")}
    check(set(others.values()) == {""}, f"every other runtime keeps its own default: {others}")


def test_new_configures_a_claude_role_with_opus_5_5() -> None:
    chosen = {}
    for role, interactive in (("director", True), ("audit", True), ("designer", False)):
        asked: list[str] = []
        plan = team_launcher._prompt_role_runtime_plan(
            role, default_cli="claude", interactive=interactive,
            input_func=lambda prompt: asked.append(prompt) or "", print_func=lambda _line: None)
        how = "Enter at a terminal" if interactive else "nobody to ask"
        check(plan.cli == "claude" and plan.model == OPUS_5_5,
              f"new, {role}, {how}: the role question records {OPUS_5_5}: {plan} {asked}")
        chosen[role] = plan.model
    with tempfile.TemporaryDirectory(prefix="syrd886-new.") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "source").mkdir()
        plan = team_launcher.build_plan(project="o886", owner_user=team_launcher.current_user_name(), port=18886,
                                        source_repo=tmp_path / "source", implementer_roles=("main",))
        payload = team_launcher._new_project_launcher_config_payload(
            plan, repository=tmp_path / "repo", implementer_roles=("main",),
            role_clis=(("designer", "claude"), ("director", "claude"), ("audit", "claude"), ("main", "codex")),
            role_models=chosen)
        config_path = tmp_path / "o886.json"
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        loaded = {role.role: role.model for role in load_project_config("o886", config_path).roles}
    check(loaded == {"designer": OPUS_5_5, "director": OPUS_5_5, "audit": OPUS_5_5, "main": ""},
          f"new: the written config runs each Claude role on {OPUS_5_5}: {loaded}")


def added_role(cli: str) -> tuple[dict, str]:
    """`switchyard add-role o886 ops --cli <cli>`, the real command against a real generated config."""
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="syrd886-add-role.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        (tmp_path / "source").mkdir()
        (tmp_path / "repo").mkdir()
        plan = team_launcher.build_plan(project="o886", owner_user=current_user, port=18886,
                                        source_repo=tmp_path / "source", implementer_roles=("app", "main"))
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
        config_path = provision_dir / "o886.json"
        config_path.write_text(json.dumps(team_launcher._new_project_launcher_config_payload(
            plan, repository=tmp_path / "repo", implementer_roles=("app", "main")), indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (provision_dir / "o886-konsole-layout.json").write_text(
            json.dumps(team_launcher._new_project_layout_payload(5), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = team_launcher.add_project_role_command(
                load_project_config("o886", config_path), account_exists=lambda _account: True,
                config_path=config_path, role_name="ops", cli=cli, script_path=ROOT / "scripts" / "team-launcher",
                runner=KeywordRecordingFakeRunner(existing_sessions={"o886-director", "o886-main"}), interactive=False)
        check(code == 0, f"add-role --cli {cli} succeeds: {code} {stdout.getvalue()[-600:]}")
        entry = json.loads(config_path.read_text(encoding="utf-8"))["roles"][-1]
        model = next(role.model for role in load_project_config("o886", config_path).roles if role.role == "ops")
    return entry, model


def test_add_role_configures_a_claude_role_with_opus_5_5() -> None:
    entry, model = added_role("claude")
    check(entry["role"] == "ops" and entry["cli"] == ["claude"] and entry.get("model") == OPUS_5_5 and model == OPUS_5_5,
          f"add-role --cli claude, no model named: the new role is configured with {OPUS_5_5}: {entry} {model!r}")
    entry, model = added_role("codex")
    check(entry["role"] == "ops" and entry["cli"] == ["codex"] and "model" not in entry and model == "",
          f"add-role --cli codex: still no model, so Codex's own default: {entry} {model!r}")


def test_the_bundled_config_runs_its_claude_roles_on_opus_5_5() -> None:
    path = ROOT / "config" / "team-launcher" / "pgu.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    claude = {role.role: role.model for role in load_project_config("pgu", path).roles if role.cli[:1] == ["claude"]}
    check(claude == {"director": OPUS_5_5, "research": OPUS_5_5, "audit": OPUS_5_5},
          f"the bundled pgu.json: director, research and audit run {OPUS_5_5}: {claude}")
    named = [role["role"] for role in raw["roles"] if role.get("model") == OPUS_5]
    check(not named, f"and no role in it names {OPUS_5}: {named}")


def main() -> int:
    try:
        for name, case in list(globals().items()):
            if name.startswith("test_") and callable(case):
                case()
    finally:  # a reach that also broke a case is reported as the reach
        host_state_guard.assert_untouched("opus_default_model_test")
    print(f"opus_default_model_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
