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
