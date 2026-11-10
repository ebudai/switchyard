"""The environment a launch of a role's pane starts from, when another pane asks for it (SYRD-752).

`switchyard restart-role` runs in the Director's pane, and everything it starts inherited that pane's environment: the
Director's own pane identity -- which the launcher rightly refuses to start another role under ("ambient pane session
id ... does not match recorded session") -- and the variables the Director's agent CLI sets for the commands it runs.
A Claude Code pane hands its children CLAUDECODE and CLAUDE_CODE_CHILD_SESSION, CLAUDE_CODE_SESSION_ID,
CLAUDE_CODE_MESSAGING_SOCKET and the rest, so a `claude` started under them is that session's child, not a role of its
own: on sapi every Claude role it restarted ended and did not come back. The documented workaround was for the caller
to `env -u` the right set first; nobody should have to know it.

So the launch starts from an environment built here: the caller's, without

- the caller's pane identity: who it is and which pane it is (its provider session, pane target, board role, tmux
  pane) and the presentation bridge's one-shot handoff -- `TMUX` itself stays, the address of the server the role
  lives on, and so do the pane-store directories, which name this account's own stores and which the launcher already
  honours only for the caller's own account (SYRD-627);
- every runtime's nesting variables: what each supported agent CLI sets for the commands it runs, below.

Nothing is taken from the caller that the target needs: the role's own values -- its board variables, its runtime's
settings, any `CLAUDE_CODE_*` a role is configured with -- are set by the pane command itself, after it clears the
inherited ones, from the project's configuration.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping

#: Per supported runtime, the variables it sets for the commands it runs: exact names, then name prefixes. Measured
#: (SYRD-752): Claude Code from a pane's tool shell; Codex, Hermes, Copilot and agy from their installed builds.
#: Configuration a runtime READS -- CODEX_HOME, HERMES_HOME, COPILOT_HOME, credentials -- is not here: a role's own
#: is set by its pane command, and an operator's is not the caller's identity.
RUNTIME_NESTING_ENV: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "claude": (("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"), ("CLAUDE_CODE_",)),
    "codex": (("CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"), ()),
    "hermes": (("HERMES_SESSION_ID", "HERMES_SESSION_SOURCE", "HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS",
                "HERMES_KANBAN_BOARD"), ("HERMES_TUI_",)),
    "copilot": (("COPILOT_SEA_EXTRACT_ONLY", "COPILOT_PACKAGE_IDENTITY"), ("COPILOT_CLI_",)),
    "agy": (("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_SIDECAR_WEB_PORT"), ()),
}


def caller_identity_keys() -> tuple[str, ...]:
    """Who the calling pane is: never carried into a launch it asks for."""
    from scripts import team_launcher as launcher

    stores = {"TICKET_BOARD_PANE_SESSION_DIR", "TICKET_BOARD_PANE_STATE_DIR",
              "PGU_TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR"}
    return (*(key for key in launcher.PANE_TARGET_ENV_KEYS if key not in stores), "TMUX_PANE",
            *launcher.PRESENTATION_HANDOFF_ENV_KEYS)


def is_runtime_nesting(name: str) -> bool:
    return any(name in names or name.startswith(prefixes) for names, prefixes in RUNTIME_NESTING_ENV.values())


def launch_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`environ` (default: this process's) as a launch for another pane starts from."""
    source = os.environ if environ is None else environ
    drop = set(caller_identity_keys())
    return {key: value for key, value in source.items() if key not in drop and not is_runtime_nesting(key)}


@contextlib.contextmanager
def applied() -> Iterator[dict[str, str]]:
    """This process's environment, for the length of a launch, is the launch environment: the launcher's own checks
    and every command it runs -- tmux, the CLI's probes, a tmux server the launch itself starts -- see only it. Put
    back exactly as it was afterwards, however the launch ends."""
    saved = dict(os.environ)
    clean = launch_environment(saved)
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield clean
    finally:
        os.environ.clear()
        os.environ.update(saved)
