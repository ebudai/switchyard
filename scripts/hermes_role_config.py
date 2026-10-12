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


def _ours(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().rstrip("\n") == HERMES_ROLE_CONFIG_MARK
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
    if not role.effort:
        if _ours(path):
            path.unlink()
        return False, ""
    if path.exists() and not path.is_symlink() and not _ours(path):
        return False, (
            f"{role.role}'s Hermes home has a config.yaml of its own ({path}); effort {role.effort!r} is not "
            "applied there -- set agent.reasoning_effort in it, or remove it to use the shared one"
        )
    shared = shared_home / HERMES_CONFIG_NAME
    try:
        import yaml
    except ImportError:
        return False, f"PyYAML is not available, so {role.role}'s effort {role.effort!r} cannot be written into its Hermes config"
    try:
        document = yaml.safe_load(shared.read_text(encoding="utf-8")) if shared.exists() else {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return False, f"cannot read {shared} to give {role.role} effort {role.effort!r}: {exc}"
    document = {} if document is None else document
    agent = document.get("agent", {}) if isinstance(document, dict) else None
    if not isinstance(agent, dict):
        return False, f"{shared} is not a YAML mapping with an `agent` mapping; {role.role}'s effort {role.effort!r} is not applied"
    document["agent"] = {**agent, "reasoning_effort": role.effort}
    rendered = HERMES_ROLE_CONFIG_MARK + "\n" + yaml.safe_dump(document, sort_keys=False)
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
