#!/usr/bin/env python3
"""Director-settable onboarding prompts per role (SYRD-36)."""

from __future__ import annotations

import argparse
import copy
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher, workflow_manage  # noqa: E402
from scripts.ticket_board.workflow_config import ONBOARDING_PROMPT_MAX_CHARS, validate  # noqa: E402
from scripts.workflow_launcher import project_roles  # noqa: E402

PROJECT = "cerulean"
MULTILINE_PROMPT = "You own deployment.\n\n- Verify staging first.\n- Never touch prod on Friday.\n"


def _load_hook_module() -> Any:
    loader = importlib.machinery.SourceFileLoader(
        "role_prompt_idle_hook_for_test", str(ROOT / "scripts" / "ticket-board-pane-idle-hook")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _minimal_document(**role_extra: Any) -> dict[str, Any]:
    """A real validated workflow, with the ops role carrying whatever is under test.

    The shipped example is used rather than a hand-rolled document so these tests cannot
    quietly diverge from the schema the product actually accepts.
    """
    document = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
    for role in document["roles"]:
        if role["name"] == "ops":
            role.update(role_extra)
    return document


def _validated(**role_extra: Any) -> dict[str, Any] | str:
    try:
        return validate(_minimal_document(**role_extra))
    except Exception as exc:  # validation failures surface as the message under test
        return str(exc)


# --- schema -----------------------------------------------------------------


def test_schema_accepts_a_multiline_prompt() -> None:
    result = _validated(onboarding_prompt=MULTILINE_PROMPT)
    assert isinstance(result, dict), result
    ops = next(r for r in result["roles"] if r["name"] == "ops")
    assert ops["onboarding_prompt"] == MULTILINE_PROMPT
    assert "\n" in ops["onboarding_prompt"], "multiline content must survive validation"


def test_schema_still_requires_onboarding_to_be_a_single_line_path() -> None:
    """The prompt is new; the path field keeps its old, stricter rule."""
    assert isinstance(_validated(onboarding="/etc/remit.md"), dict)
    assert "absolute file path" in str(_validated(onboarding="not/absolute"))
    assert "absolute file path" in str(_validated(onboarding="/etc/a\nb"))


def test_schema_rejects_an_empty_or_blank_prompt() -> None:
    """Clearing removes the key; an empty string would blur set and clear."""
    for blank in ("", "   ", "\n\n"):
        assert "non-empty text" in str(_validated(onboarding_prompt=blank)), repr(blank)


def test_schema_bounds_prompt_length_and_rejects_nul() -> None:
    assert isinstance(_validated(onboarding_prompt="x" * ONBOARDING_PROMPT_MAX_CHARS), dict)
    too_long = _validated(onboarding_prompt="x" * (ONBOARDING_PROMPT_MAX_CHARS + 1))
    assert "at most" in str(too_long)
    assert "NUL" in str(_validated(onboarding_prompt="bad\x00prompt"))


def test_schema_still_rejects_unknown_role_fields() -> None:
    assert "unknown role field" in str(_validated(onboarding_prompts="typo"))


# --- projection into the pane environment ------------------------------------


def _projected_env(**role_extra: Any) -> dict[str, str]:
    document = validate(_minimal_document(**role_extra))
    raw = {"project": PROJECT, "roles": [{"role": "ops", "env": {}}], "layout": "layout.json"}
    projected = project_roles(raw, document)
    ops = next(r for r in projected["roles"] if r["role"] == "ops")
    return ops.get("env", {})


def test_prompt_is_projected_into_the_pane_environment() -> None:
    env = _projected_env(onboarding_prompt=MULTILINE_PROMPT)
    assert env["TICKET_BOARD_ROLE_ONBOARDING_PROMPT"] == MULTILINE_PROMPT


def test_clearing_a_prompt_removes_it_from_the_projected_environment() -> None:
    """A stale prompt left in the environment would outlive the clear that removed it."""
    document = validate(_minimal_document())
    raw = {
        "project": PROJECT,
        "roles": [{"role": "ops", "env": {"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": "stale"}}],
        "layout": "layout.json",
    }
    ops = next(r for r in project_roles(raw, document)["roles"] if r["role"] == "ops")
    assert "TICKET_BOARD_ROLE_ONBOARDING_PROMPT" not in ops.get("env", {})


# --- runtime behaviour -------------------------------------------------------


def _session_start(environ: dict[str, str], *, resume_session_id: str = "") -> Any:
    hook = _load_hook_module()
    return hook._session_start_additional_context_output(
        target=f"{PROJECT}-ops:0.0",
        payload={},
        environ={"TICKET_BOARD_PROJECT": PROJECT, **environ},
        resume_session_id=resume_session_id,
    )


def _context_text(result: Any) -> str:
    return str(result["hookSpecificOutput"]["additionalContext"])


def test_fresh_session_receives_the_stored_prompt() -> None:
    result = _session_start({"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": MULTILINE_PROMPT})
    text = _context_text(result)
    assert "Never touch prod on Friday." in text
    assert "Your ops session started." in text


def test_a_resumed_session_is_not_given_the_prompt() -> None:
    """Changing configuration must not interrupt or rewrite a running conversation.

    The prompt is gated on a fresh start, so a resumed session keeps the context it
    already had and picks the new prompt up only on its next fresh conversation.
    """
    environ = {"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": MULTILINE_PROMPT}
    # Asserted as a difference between the two starts, so this cannot pass merely
    # because the resumed call returned nothing for some unrelated reason.
    fresh = _session_start(environ)
    resumed = _session_start(environ, resume_session_id="abc123")

    assert "Never touch prod on Friday." in _context_text(fresh)
    assert resumed is None or "Never touch prod on Friday." not in _context_text(resumed)


def test_the_stored_prompt_wins_over_a_configured_path(tmp_path: Path | None = None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        remit = Path(tmp) / "remit.md"
        remit.write_text("PACKAGED REMIT BODY\n", encoding="utf-8")
        result = _session_start(
            {
                "TICKET_BOARD_ROLE_ONBOARDING_PROMPT": MULTILINE_PROMPT,
                "TICKET_BOARD_ROLE_ONBOARDING": str(remit),
            }
        )
        text = _context_text(result)

    assert "Never touch prod on Friday." in text
    assert "PACKAGED REMIT BODY" not in text


def test_the_configured_path_still_works_when_no_prompt_is_set() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        remit = Path(tmp) / "remit.md"
        remit.write_text("PACKAGED REMIT BODY\n", encoding="utf-8")
        text = _context_text(_session_start({"TICKET_BOARD_ROLE_ONBOARDING": str(remit)}))

    assert "PACKAGED REMIT BODY" in text


def test_every_runtime_start_shape_receives_the_prompt() -> None:
    """All four CLIs share this session-start path; only the payload label differs.

    Runtimes that label a fresh start send "startup"; Hermes's on_session_start carries
    a session id and no label at all. Both are plain starts and both must get the remit.
    A label meaning "this session already has context" must not.
    """
    hook = _load_hook_module()
    environ = {"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": MULTILINE_PROMPT}

    for payload in ({}, {"source": hook.SESSION_START_FRESH_SOURCE}, {"session_id": "s-1"}):
        result = hook._session_start_additional_context_output(
            target=f"{PROJECT}-ops:0.0",
            payload=payload,
            environ={"TICKET_BOARD_PROJECT": PROJECT, **environ},
        )
        assert result is not None, payload
        assert "Never touch prod on Friday." in _context_text(result), payload

    for continued in sorted(hook.SESSION_START_ACTIVE_WORK_SOURCES):
        result = hook._session_start_additional_context_output(
            target=f"{PROJECT}-ops:0.0",
            payload={"source": continued},
            environ={"TICKET_BOARD_PROJECT": PROJECT, **environ},
        )
        text = _context_text(result) if result else ""
        assert "Never touch prod on Friday." not in text, continued


def test_the_board_skill_instruction_is_present_exactly_once() -> None:
    """It is prepended by the shared wrapper, so the prompt must not carry its own copy."""
    hook = _load_hook_module()
    text = _context_text(
        _session_start({"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": MULTILINE_PROMPT})
    )
    assert text.count(hook.BOARD_SKILL_INSTRUCTION) == 1


# --- the director is no longer a special case --------------------------------


def _director_start(environ: dict[str, str]) -> Any:
    hook = _load_hook_module()
    return hook._session_start_additional_context_output(
        target=f"{PROJECT}-director:0.0",
        payload={"source": "startup"},
        environ={"TICKET_BOARD_PROJECT": PROJECT, **environ},
    )


def test_director_receives_a_stored_prompt_through_the_generic_path() -> None:
    seeded = team_launcher.director_onboarding_seed_text(Path("/srv/porter"))
    text = _context_text(_director_start({"TICKET_BOARD_ROLE_ONBOARDING_PROMPT": seeded}))
    assert "Read the onboarding packet first: /srv/porter/docs/onboarding." in text
    assert "/srv/porter/docs/onboarding/switchyard-director-guide.md" in text


def test_director_without_stored_onboarding_gets_no_special_context() -> None:
    """The removed special case, pinned as removed.

    A director with neither a prompt nor a remit path now behaves like any other role in
    the same state. This is also where a cleared director prompt lands, which is what
    makes clearing generic rather than a return to a private code path.
    """
    text = _context_text(_director_start({}))
    assert "onboarding packet" not in text
    assert "just started fresh" not in text


def test_the_removed_director_branch_is_gone_from_the_hook() -> None:
    """Asserted against the source, so it cannot be reintroduced quietly."""
    source = (ROOT / "scripts" / "ticket-board-pane-idle-hook").read_text(encoding="utf-8")
    assert 'role == "director"' not in source
    for helper in (
        "_director_startup_onboarding_context",
        "_onboarding_project_root",
        "_onboarding_packet_exists",
        "ONBOARDING_PACKET_DIR",
    ):
        assert helper not in source, helper


def test_provisioning_seeds_the_director_and_leaves_other_roles_alone() -> None:
    document = {"roles": [{"name": "director"}, {"name": "ops"}]}
    seeded, changed = team_launcher.seed_director_onboarding(document, Path("/srv/porter"))
    director = next(r for r in seeded["roles"] if r["name"] == "director")
    ops = next(r for r in seeded["roles"] if r["name"] == "ops")
    assert changed is True, "seeding must report the change it made"
    assert "docs/onboarding" in director["onboarding_prompt"]
    assert "onboarding_prompt" not in ops

    # Idempotent: a second pass changes nothing and reports nothing.
    again, changed_again = team_launcher.seed_director_onboarding(seeded, Path("/srv/porter"))
    assert changed_again is False
    assert again == seeded


def test_provisioning_never_overwrites_onboarding_an_operator_already_set() -> None:
    for existing in ({"onboarding_prompt": "mine"}, {"onboarding": "/etc/remit.md"}):
        document = {"roles": [{"name": "director", **existing}]}
        seeded, changed = team_launcher.seed_director_onboarding(document, Path("/srv/porter"))
        director = seeded["roles"][0]
        assert changed is False, "an operator's own onboarding must not be reported as seeded"
        assert director == {"name": "director", **existing}, director


def test_seeding_tolerates_a_project_with_no_workflow_document() -> None:
    assert team_launcher.seed_director_onboarding(None, Path("/srv/porter")) == (None, False)


class _Plan:
    project = "porter"
    board_current = "/opt/switchyard/current"


def test_a_project_with_no_workflow_document_still_seeds_the_director() -> None:
    """The regression this must not reintroduce.

    A project generated without a declarative workflow has no document to carry the
    prompt, and the hook no longer has a director-only branch to fall back to. The
    launcher therefore seeds the generic field directly into the director's pane
    environment, so removing the special case cannot strip an existing director's remit.
    """
    env = team_launcher._new_project_role_env(
        "director",
        _Plan(),
        design_document=Path("/srv/porter/PROJECT_DESIGN.md"),
        director_onboarding=Path("/srv/porter/.switchyard/DIRECTOR_ONBOARDING.md"),
    )
    assert "docs/onboarding" in env["TICKET_BOARD_ROLE_ONBOARDING_PROMPT"]


def test_the_director_seed_survives_a_missing_design_document() -> None:
    env = team_launcher._new_project_role_env(
        "director",
        _Plan(),
        director_onboarding=Path("/srv/porter/.switchyard/DIRECTOR_ONBOARDING.md"),
    )
    assert "/srv/porter/docs/onboarding" in env["TICKET_BOARD_ROLE_ONBOARDING_PROMPT"]


def test_other_roles_get_no_seeded_prompt_from_the_launcher() -> None:
    env = team_launcher._new_project_role_env(
        "ops", _Plan(), design_document=Path("/srv/porter/PROJECT_DESIGN.md")
    )
    assert "TICKET_BOARD_ROLE_ONBOARDING_PROMPT" not in env


# --- lifecycle wiring --------------------------------------------------------


class _Sentinel(Exception):
    """Raised from a stubbed migration to prove the call site was reached."""


def _declarative_config(tmp: str) -> tuple[Any, Path]:
    launcher_dir = Path(tmp) / "launcher"
    launcher_dir.mkdir()
    config_path = launcher_dir / "porter.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "porter",
                "layout": "layout.json",
                "roles": [
                    {
                        "role": "ops",
                        "cli": ["claude"],
                        "target": "porter-ops:0.0",
                        "tmux_session": "porter-ops",
                        "detached": True,
                    }
                ],
                "workflow": {
                    "schema": "switchyard.workflow.v1",
                    "roles": [],
                    "stages": [],
                    "transitions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return team_launcher.load_project_config("porter", config_path), config_path


def test_reload_runs_the_migration_and_attach_does_not() -> None:
    """Reload re-projects the stored document, so an unmigrated tenant needs the backfill.

    Proved by making the migration raise: reaching it aborts the launch, which is a
    stronger signal than asserting on state the rest of launch_project would touch.
    """
    import tempfile

    original = team_launcher.migrate_declarative_director_onboarding

    def _boom(*_args, **_kwargs):
        raise _Sentinel()

    team_launcher.migrate_declarative_director_onboarding = _boom
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config, config_path = _declarative_config(tmp)
            reached = False
            try:
                team_launcher.launch_project(
                    config,
                    config_path=config_path,
                    mode="reload",
                    script_path=Path("/nonexistent/team-launcher"),
                    runner=lambda *a, **k: None,
                )
            except _Sentinel:
                reached = True
            except Exception:
                reached = False
            assert reached, "reload must run the director onboarding migration"

            # An attach is not a re-projection, so it must not migrate.
            attached_without_migration = False
            try:
                team_launcher.launch_project(
                    config,
                    config_path=config_path,
                    mode="attach",
                    script_path=Path("/nonexistent/team-launcher"),
                    runner=lambda *a, **k: None,
                )
                attached_without_migration = True
            except _Sentinel:
                attached_without_migration = False
            except Exception:
                attached_without_migration = True
            assert attached_without_migration, "attach must not run the migration"
    finally:
        team_launcher.migrate_declarative_director_onboarding = original


def test_a_failed_migration_stops_the_upgrade_rather_than_warning() -> None:
    """Fail-safe, not fail-open.

    The deployed hook carries no legacy director source, so an upgrade that proceeds
    over an unmigrated document would strip that director's onboarding. Failing before
    the release is reported is recoverable; succeeding is not.
    """
    import tempfile

    from scripts import workflow_manage as wm

    original = wm.main
    wm.main = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("board rejected the document"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config, config_path = _declarative_config(tmp)
            try:
                team_launcher.migrate_declarative_director_onboarding(
                    config, config_path=config_path, print_func=lambda _m: None
                )
                raise AssertionError("expected a failed migration to stop the upgrade")
            except SystemExit as exc:
                message = str(exc)
    finally:
        wm.main = original

    assert "director onboarding migration failed" in message
    assert "board rejected the document" in message
    assert "role-prompt clear director" in message, "the message should name the deliberate way out"


def test_the_write_path_hands_the_projection_to_the_tenant_when_privileged() -> None:
    """upgrade re-executes as root, so the projection must not be left root-owned."""
    import os
    import tempfile

    from scripts import workflow_launcher, workflow_manage as wm

    handed: list[Path] = []
    original_euid = os.geteuid
    original_assign = workflow_launcher.assign_projection_owner
    original_load = team_launcher.load_project_config
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _config, config_path = _declarative_config(tmp)

            # Unprivileged: nothing to hand over, the files are already the tenant's.
            wm._hand_projection_to_tenant(config_path)
            assert handed == []

            os.geteuid = lambda: 0
            workflow_launcher.assign_projection_owner = (
                lambda config, path, *, runner: handed.append(Path(path))
            )
            team_launcher.load_project_config = lambda project, path: _OwnerConfig()
            wm._hand_projection_to_tenant(config_path)
            assert handed == [config_path], handed
    finally:
        os.geteuid = original_euid
        workflow_launcher.assign_projection_owner = original_assign
        team_launcher.load_project_config = original_load


# --- add-role leaves the document alone --------------------------------------


def test_add_role_refuses_outright_for_a_declarative_tenant() -> None:
    """So it cannot seed or clear onboarding on any role, related or not.

    add-role builds the launcher role dict only; for a project with a workflow document
    it refuses and directs the operator to the apply path, which is where role data
    actually lives. The document is therefore preserved by construction rather than by
    add-role being careful.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        launcher_dir = Path(tmp) / "launcher"
        launcher_dir.mkdir()
        config_path = launcher_dir / "porter.json"
        document = {
            "schema": "switchyard.workflow.v1",
            "roles": [],
            "stages": [],
            "transitions": [],
        }
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": "layout.json",
                    "roles": [
                        {
                            "role": "ops",
                            "cli": ["claude"],
                            "target": "porter-ops:0.0",
                            "tmux_session": "porter-ops",
                            "detached": True,
                        }
                    ],
                    "workflow": document,
                }
            ),
            encoding="utf-8",
        )
        config = team_launcher.load_project_config("porter", config_path)
        try:
            team_launcher._project_plan_for_added_role(
                config, config_path=config_path, role_name="perf"
            )
            raise AssertionError("expected add-role to refuse for a declarative tenant")
        except SystemExit as exc:
            message = str(exc)
        unchanged = json.loads(config_path.read_text())["workflow"]

    assert "ticket-board-workflow apply" in message
    assert unchanged == document, "add-role must leave the workflow document untouched"


# --- tenant ownership of what the write path produces ------------------------


class _OwnerConfig:
    """Just enough of a project config for the ownership helpers."""

    run_as_user = "porter-agent"


def test_the_intended_ownership_operation_names_the_tenant_uid_and_gid() -> None:
    """Non-root coverage of the operation itself.

    ensure_owner_file no-ops unless euid is 0, so asserting its effect as an ordinary
    user would prove nothing. What can be asserted without root is the command it would
    run: the tenant as both user and group, on that exact path.
    """
    args = team_launcher.chown_owner_file_args(_OwnerConfig(), Path("/srv/porter/workflow.json"))
    assert args == ["chown", "porter-agent:porter-agent", "/srv/porter/workflow.json"]


def test_ownership_is_refused_when_no_tenant_is_configured() -> None:
    try:
        team_launcher.chown_owner_file_args(type("C", (), {"run_as_user": ""})(), Path("/srv/x"))
        raise AssertionError("expected a refusal with no run_as_user")
    except ValueError as exc:
        assert "requires run_as_user" in str(exc)


def test_projection_paths_are_handed_to_the_tenant_and_nothing_else() -> None:
    """The handover covers the projection and its directory, and is not recursive."""
    import tempfile

    from scripts import workflow_launcher

    handed: list[Path] = []
    original = team_launcher.ensure_owner_file
    team_launcher.ensure_owner_file = lambda config, path, *, runner: handed.append(Path(path))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher_dir = root / "launcher"
            launcher_dir.mkdir()
            config_path = launcher_dir / "porter.json"
            config_path.write_text(
                json.dumps({"project": "porter", "layout": "layout.json"}), encoding="utf-8"
            )
            (launcher_dir / "layout.json").write_text("{}", encoding="utf-8")
            (launcher_dir / "workflow.json").write_text("{}", encoding="utf-8")
            unrelated = launcher_dir / "service-receipt.json"
            unrelated.write_text("{}", encoding="utf-8")

            workflow_launcher.assign_projection_owner(
                _OwnerConfig(), config_path, runner=lambda *a, **k: None
            )
            names = sorted(p.name for p in handed)
    finally:
        team_launcher.ensure_owner_file = original

    assert names == ["launcher", "layout.json", "porter.json", "workflow.json"], names
    assert "service-receipt.json" not in names, "the handover must not sweep unrelated files"


def test_real_ownership_change_when_running_privileged() -> None:
    """Exercised only as root; skipped otherwise rather than faked."""
    import os
    import tempfile

    if os.geteuid() != 0:
        return
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "workflow.json"
        target.write_text("{}", encoding="utf-8")
        import subprocess

        subprocess.run(
            team_launcher.chown_owner_file_args(_OwnerConfig(), target), check=True
        )
        import pwd as _pwd

        assert target.stat().st_uid == _pwd.getpwnam(_OwnerConfig.run_as_user).pw_uid


# --- the mutation the command performs ---------------------------------------


class _Parser(argparse.ArgumentParser):
    """Turns argparse's exit-on-error into something a test can assert on."""

    def error(self, message: str):  # type: ignore[override]
        raise ValueError(message)


class _Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _current(document: dict[str, Any]) -> dict[str, Any]:
    return {"revision": 7, "document": document}


def test_set_stores_multiline_text_without_touching_other_roles() -> None:
    current = _current(_minimal_document())
    result = workflow_manage._document_with_role_prompt(
        current,
        _Args(role="ops", operation="set-role-prompt", prompt=MULTILINE_PROMPT, prompt_file=None),
        _Parser(),
    )
    ops = next(r for r in result["roles"] if r["name"] == "ops")
    director = next(r for r in result["roles"] if r["name"] == "director")
    assert ops["onboarding_prompt"] == MULTILINE_PROMPT
    assert "onboarding_prompt" not in director
    # The fetched document must not be mutated in place; concurrency control compares it.
    assert not any("onboarding_prompt" in r for r in current["document"]["roles"])


def test_clear_removes_the_key_rather_than_emptying_it() -> None:
    current = _current(_minimal_document(onboarding_prompt=MULTILINE_PROMPT))
    result = workflow_manage._document_with_role_prompt(
        current,
        _Args(role="ops", operation="clear-role-prompt", prompt=None, prompt_file=None),
        _Parser(),
    )
    ops = next(r for r in result["roles"] if r["name"] == "ops")
    assert "onboarding_prompt" not in ops


def test_an_unknown_role_is_refused_and_names_the_configured_roles() -> None:
    current = _current(_minimal_document())
    try:
        workflow_manage._document_with_role_prompt(
            current,
            _Args(role="nope", operation="set-role-prompt", prompt="x", prompt_file=None),
            _Parser(),
        )
        raise AssertionError("expected an unknown role to be refused")
    except ValueError as exc:
        assert "unknown role: nope" in str(exc)
        assert "ops" in str(exc), "the refusal should say which roles exist"


def test_set_requires_prompt_content_and_refuses_two_sources() -> None:
    parser = _Parser()
    for args, expected in (
        (_Args(role="ops", operation="set-role-prompt", prompt=None, prompt_file=None),
         "requires --prompt or --prompt-file"),
        (_Args(role="ops", operation="set-role-prompt", prompt="a", prompt_file=Path("b")),
         "not both"),
        (_Args(role="ops", operation="set-role-prompt", prompt="   ", prompt_file=None),
         "use clear-role-prompt"),
    ):
        try:
            workflow_manage._requested_prompt(args, parser)
            raise AssertionError(f"expected refusal: {expected}")
        except ValueError as exc:
            assert expected in str(exc), f"{expected} not in {exc}"


def test_mutating_requires_an_active_configuration() -> None:
    try:
        workflow_manage._document_with_role_prompt(
            {"revision": 0, "document": None},
            _Args(role="ops", operation="set-role-prompt", prompt="x", prompt_file=None),
            _Parser(),
        )
        raise AssertionError("expected a refusal with no active configuration")
    except ValueError as exc:
        assert "no workflow configuration is active" in str(exc)


# --- authorization surface ---------------------------------------------------


def test_role_prompt_is_a_registered_unprivileged_command() -> None:
    """The director runs this after startup, so it must not demand root."""
    assert "role-prompt" in team_launcher.SWITCHYARD_COMMANDS
    assert "role-prompt" in team_launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS
    assert "role-prompt" not in team_launcher.SWITCHYARD_PRIVILEGED_COMMANDS


def test_role_prompt_is_documented_in_help_and_readme() -> None:
    help_text = team_launcher.switchyard_help_text()
    assert "role-prompt" in help_text
    assert "role-prompt" in (ROOT / "README.md").read_text(encoding="utf-8")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("role_onboarding_prompt_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
