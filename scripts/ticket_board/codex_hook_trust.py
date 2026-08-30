"""Codex hook trust helpers shared by launcher checks and hook installation."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

CODEX_HOOK_EVENT_KEYS = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
CODEX_ADDITIONAL_CONTEXT_EVENTS = frozenset(
    {"pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit", "subagent_start"}
)
CODEX_DEFAULT_ADDITIONAL_CONTEXT_LIMIT = 2500


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_toml_object(path: Path) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def codex_hook_event_key(event: str) -> str:
    if event in CODEX_HOOK_EVENT_KEYS.values():
        return event
    mapped = CODEX_HOOK_EVENT_KEYS.get(event)
    if mapped:
        return mapped
    pieces = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
    return pieces.replace("-", "_")


def codex_hook_timeout(event_key: str, raw_timeout: Any) -> int:
    try:
        timeout = int(raw_timeout) if raw_timeout is not None else None
    except (TypeError, ValueError):
        timeout = None
    if event_key in {"session_end", "interrupt"}:
        return max(1, min(timeout if timeout is not None else 1, 3))
    return max(1, timeout if timeout is not None else 600)


def codex_hook_current_hash(event_key: str, group: dict[str, Any], handler: dict[str, Any]) -> str:
    command = str(handler.get("command") or "")
    normalized_handler: dict[str, Any] = {
        "async": bool(handler.get("async", False)),
        "command": command,
        "timeout": codex_hook_timeout(event_key, handler.get("timeout")),
        "type": "command",
    }
    status_message = handler.get("statusMessage")
    if isinstance(status_message, str):
        normalized_handler["statusMessage"] = status_message
    additional_context_limit = handler.get("additionalContextLimit")
    if (
        isinstance(additional_context_limit, int)
        and event_key in CODEX_ADDITIONAL_CONTEXT_EVENTS
        and additional_context_limit != CODEX_DEFAULT_ADDITIONAL_CONTEXT_LIMIT
    ):
        normalized_handler["additionalContextLimit"] = additional_context_limit

    identity: dict[str, Any] = {
        "event_name": event_key,
        "hooks": [normalized_handler],
    }
    matcher = group.get("matcher")
    if isinstance(matcher, str):
        identity["matcher"] = matcher
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def codex_command_hook_trust_entries(owner_home: Path) -> list[tuple[str, str, str]]:
    hooks_path = owner_home / ".codex" / "hooks.json"
    hooks_config = read_json_object(hooks_path)
    hooks = hooks_config.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries: list[tuple[str, str, str]] = []
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        event_key = codex_hook_event_key(str(event_name))
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = str(handler.get("command") or "").strip()
                if not command:
                    continue
                hook_key = f"{hooks_path}:{event_key}:{group_index}:{handler_index}"
                entries.append((event_key, hook_key, codex_hook_current_hash(event_key, group, handler)))
    return entries


def codex_trusted_hashes(owner_home: Path) -> dict[str, str]:
    config = read_toml_object(owner_home / ".codex" / "config.toml")
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    state = hooks.get("state")
    if not isinstance(state, dict):
        return {}
    trusted: dict[str, str] = {}
    for hook_key, value in state.items():
        if not isinstance(value, dict):
            continue
        trusted_hash = value.get("trusted_hash")
        if isinstance(trusted_hash, str):
            trusted[str(hook_key)] = trusted_hash
    return trusted


def render_codex_hook_trust_records(entries: list[tuple[str, str, str]], *, include_parent_header: bool) -> str:
    lines: list[str] = []
    if include_parent_header:
        lines.extend(["[hooks.state]", ""])
    for _event, hook_key, current_hash in entries:
        lines.extend(
            [
                f"[hooks.state.{json.dumps(hook_key)}]",
                f"trusted_hash = {json.dumps(current_hash)}",
                "",
            ]
        )
    return "\n".join(lines)
