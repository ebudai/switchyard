"""A Hermes role's reasoning effort, given through its own HERMES_HOME (SYRD-546).

Hermes has no reasoning flag. The `--reasoning <effort>` Switchyard passed made
Hermes 0.15.2 read the level as a subcommand and exit 2 ("argument command:
invalid choice: 'high'"), so a Hermes role with an effort never started (otto's
main, OTTO-1). Hermes reads its main agent's effort from `agent.reasoning_effort`
in `$HERMES_HOME/config.yaml`, with no flag or environment override.

A role's home links `config.yaml` to the owner's shared one, so the effort
cannot go there without changing every Hermes role's effort. A role with an
effort gets a private `config.yaml` instead: the shared document with only
`agent.reasoning_effort` set, written beside the link it replaces, never
through it, and rewritten at every start, so a change to the shared document
reaches the role on its next start. A role without one keeps, or gets back,
the link. A `config.yaml` of the role's own that Switchyard did not write is
never replaced; nor is anything when the shared document cannot be read as a
YAML mapping. Both say why and start the role at Hermes's configured effort.

A role that starts every ticket fresh gets the private document too, with its
ticket boundary closed (`close_ticket_boundary`, SYRD-693): Hermes's `/clear`
empties the conversation, but its memory, user profile and session search
would otherwise carry the last ticket into the next.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig

HERMES_CONFIG_NAME = "config.yaml"
#: The first line of a `config.yaml` Switchyard wrote for a role, and so may rewrite or remove.
HERMES_ROLE_CONFIG_MARK = (
    "# Written by Switchyard for this role (SYRD-546): the shared config.yaml with agent.reasoning_effort "
    "set to the role's effort. It is rewritten at every start; edit the shared one."
)


#: Hermes toolsets that reach past the current conversation: its persistent memory, and search over earlier sessions.
TICKET_BOUNDARY_TOOLSETS = ("memory", "session_search")


def starts_each_ticket_fresh(role: RoleConfig) -> bool:
    """Whether the board clears this role into each ticket: the workflow's `ephemeral`, or the launcher's own
    `fresh_session_per_ticket` (SYRD-135; Audit, on 8c31c512: the two are separate fields)."""
    return bool(getattr(role, "ephemeral", False) or getattr(role, "fresh_session_per_ticket", False))


def boundary_cutover(role: RoleConfig, *, session_dir: Path) -> bool:
    """Whether this start is the one that first closes a Hermes role's ticket boundary (SYRD-693).

    Its recorded conversation was then held by a Hermes with memory and session search open, so it may
    carry anything from before; such a start never resumes it. Every later start of the role resumes a
    conversation that began closed, which a same-parent continuation may carry on (SYRD-626).
    """
    from scripts.provider_resume import _uses_hermes, hermes_home_for_role
    from scripts.ticket_board.hermes_ticket_boundary import MARK_LINES, TICKET_BOUNDARY_MARK

    if not _uses_hermes(role) or not starts_each_ticket_fresh(role):
        return False
    path = hermes_home_for_role(role, session_dir=session_dir) / HERMES_CONFIG_NAME
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            head = [stream.readline().rstrip("\n") for _ in range(MARK_LINES)]
    except OSError:
        return True  # no config of its own yet: nothing it held was closed
    return TICKET_BOUNDARY_MARK not in head  # the shared config, linked, is never marked


def close_ticket_boundary(document: dict) -> dict:
    """`document` for a role that starts every ticket fresh: nothing reaches it from an earlier conversation.

    Hermes keeps `memories/MEMORY.md` and `USER.md` in the role's home and puts them in every new session's
    prompt, and `session_search` reads every earlier session; a `/clear` touches neither (SYRD-693 canary).
    """
    memory = document.get("memory") if isinstance(document.get("memory"), dict) else {}
    agent = document.get("agent") if isinstance(document.get("agent"), dict) else {}
    disabled = [name for name in agent.get("disabled_toolsets") or [] if isinstance(name, str)]
    return {**document,
            "memory": {**memory, "memory_enabled": False, "user_profile_enabled": False, "provider": ""},
            "agent": {**agent, "disabled_toolsets": [*disabled, *(t for t in TICKET_BOUNDARY_TOOLSETS if t not in disabled)]}}


def _ours(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    from scripts.hermes_local_profile import CONFIG_MARK as LOCAL_PROFILE_MARK

    try:
        with path.open(encoding="utf-8") as handle:
            # A local profile's config (SYRD-692) is Switchyard's too: a role leaving its profile gets it replaced.
            return handle.readline().rstrip("\n") in (HERMES_ROLE_CONFIG_MARK, LOCAL_PROFILE_MARK)
    except (OSError, UnicodeDecodeError):
        return False


def prepare_hermes_role_config(role: RoleConfig, *, hermes_home: Path, shared_home: Path) -> tuple[bool, str]:
    """Give `role`'s Hermes home its effort; whether its config.yaml is now private, and any notice.

    With no effort, a config.yaml Switchyard wrote is removed so the shared
    one is linked back in. With one, the shared document is written privately
    with `agent.reasoning_effort` set. Returns `(False, why)` when the effort
    cannot be given, leaving whatever was there.
    """
    path = hermes_home / HERMES_CONFIG_NAME
    profile = getattr(role, "hermes_local", None)
    if profile is not None:
        # The local model service's profile (SYRD-692): always private, effort included.
        from scripts.hermes_local_profile import write_role_home

        notice = write_role_home(hermes_home, profile, shared_home=shared_home, effort=role.effort,
                                 fresh_per_ticket=starts_each_ticket_fresh(role))
        return not notice, notice
    fresh = starts_each_ticket_fresh(role)
    what = " and ".join(([f"effort {role.effort!r}"] if role.effort else []) + (["its ticket boundary"] if fresh else []))
    if not role.effort and not fresh:
        if _ours(path):
            path.unlink()
        return False, ""
    if path.exists() and not path.is_symlink() and not _ours(path):
        return False, (
            f"{role.role}'s Hermes home has a config.yaml of its own ({path}); {what} is not "
            "applied there -- remove it to use the shared one"
        )
    shared = shared_home / HERMES_CONFIG_NAME
    try:
        import yaml
    except ImportError:
        return False, f"PyYAML is not available, so {role.role}'s {what} cannot be written into its Hermes config"
    try:
        document = yaml.safe_load(shared.read_text(encoding="utf-8")) if shared.exists() else {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return False, f"cannot read {shared} to give {role.role} {what}: {exc}"
    document = {} if document is None else document
    agent = document.get("agent", {}) if isinstance(document, dict) else None
    if not isinstance(agent, dict):
        return False, f"{shared} is not a YAML mapping with an `agent` mapping; {role.role}'s {what} is not applied"
    if role.effort:
        document["agent"] = {**agent, "reasoning_effort": role.effort}
    if fresh:
        document = close_ticket_boundary(document)
    from scripts.ticket_board.hermes_ticket_boundary import TICKET_BOUNDARY_MARK

    # The board clears a Hermes pane into a ticket only when the config it started from carries the mark.
    rendered = "\n".join([HERMES_ROLE_CONFIG_MARK, *([TICKET_BOUNDARY_MARK] if fresh else []),
                          yaml.safe_dump(document, sort_keys=False)])
    # A new file renamed over the link: the shared document is never written through it.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=hermes_home, prefix=f".{HERMES_CONFIG_NAME}.", suffix=".tmp",
                                     delete=False) as handle:
        handle.write(rendered)
        temp = Path(handle.name)
    try:
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True, ""
