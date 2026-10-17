"""Which human accounts may use the board UI's Director operations, decided and recorded by root (SYRD-598).

The board lends a Director operation to its page only for a connection the kernel says an approved board
UI user owns. Who is approved is root's decision, recorded in the project's root-owned registry record
(/etc/switchyard/projects/<slug>.json, key `board_ui`) as account names and the numeric uids root resolved
them to. The board reads that record from the path on its unit's command line, never from anything the
tenant can write.

The approved user is the host's approved desktop owner -- the person whose desktop this project was
granted, taken from the host's root-owned desktop approval, never from the tenant's own configuration --
or, on a headless host, whoever the operator names with --board-ui-user. Root, the board service, the
tenant's project account and its role accounts are never board UI users; naming one is refused rather
than recorded, and the record is left as it was. The record also carries those exclusions, so the board need not take them from an
environment the tenant can override. Each decision is written to the rollout journal.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

RECORD_KEY = "board_ui"
#: team_launcher.SWITCHYARD_REGISTRY_SCHEMA: a registry record, the only document this writes into.
REGISTRY_SCHEMA = "switchyard.project-registry.v1"
#: The board service's account (project_provision.DEFAULT_SERVICE_USER).
SERVICE_USER = "boardsvc"
NONE_APPROVED = ("no approved desktop owner and no --board-ui-user: the board UI lends no Director operation "
                 "until an operator names one")


def exclusions(config: Any) -> dict[str, Any]:
    """The accounts root records as never board UI users, for the board to refuse by."""
    from scripts.ticket_board.provision_role_accounts import roles_group_name

    return {
        "tenant_user": str(config.run_as_user or ""),
        "roles_group": roles_group_name(config.project),
        "role_accounts": sorted({str(getattr(role, "run_as_user", "") or "") for role in config.roles} - {""}),
        "service_user": SERVICE_USER,
    }


def chosen_users(config: Any, *, operator_users: Sequence[str] = (), approval_path: Path | None = None) -> tuple[list[str], str, str]:
    """(accounts, source, note): the operator's --board-ui-user, else the host's approved desktop owner."""
    named = [name.strip() for name in operator_users if name.strip()]
    if named:
        return sorted(set(named)), "operator (--board-ui-user)", ""
    from scripts.desktop_policy import approved_desktop_policy

    plan = SimpleNamespace(project=config.project, owner_user=config.run_as_user)
    policy, problems = approved_desktop_policy(plan, config, approval_path=approval_path)
    if policy and policy.get("mode") == "wayland" and policy.get("gui_user"):
        return [str(policy["gui_user"])], "the host's approved desktop owner", ""
    return [], "", "; ".join(problems) or NONE_APPROVED


def resolved(names: Sequence[str], excluded: dict[str, Any], *,
             getpwnam: Callable[[str], Any] = pwd.getpwnam,
             getgrnam: Callable[[str], Any] = grp.getgrnam) -> list[dict[str, Any]]:
    """Each account with the uid root resolves it to. ValueError, saying why, for an account that is never a
    board UI user or is not one on this host."""
    never: dict[int, str] = {0: "root"}

    def add(name: str, why: str) -> None:
        try:
            never.setdefault(getpwnam(name).pw_uid, why)
        except KeyError:
            pass

    for name, why in ((excluded.get("service_user") or "", "the board service"),
                      (excluded.get("tenant_user") or "", "the project account")):
        if name:
            add(name, why)
    for name in excluded.get("role_accounts") or []:
        add(name, "a role account")
    try:
        for name in getgrnam(excluded["roles_group"]).gr_mem if excluded.get("roles_group") else []:
            add(name, "a role account")
    except KeyError:
        pass
    users = []
    for name in names:
        try:
            uid = getpwnam(name).pw_uid
        except KeyError:
            raise ValueError(f"board UI user {name!r} is not an account on this host") from None
        if uid in never:
            raise ValueError(f"{name} (uid {uid}) is {never[uid]}, which is never a board UI user")
        users.append({"account": name, "uid": uid})
    return users


def _write_atomically(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def record_board_ui_users(
    config: Any,
    *,
    registry_dir: Path | None = None,
    operator_users: Sequence[str] = (),
    approval_path: Path | None = None,
    dry_run: bool = False,
    journal_root: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Record the project's approved board UI users in its root-owned registry record, and journal it.

    Returns the problems worth showing (none of which stop the caller): no approved user means the board
    UI lends no Director operation, which is the safe state, not a failure.
    """
    from scripts.project_identity import switchyard_registry_dir

    path = Path(registry_dir or switchyard_registry_dir()) / f"{config.project}.json"
    excluded = exclusions(config)
    names, source, note = chosen_users(config, operator_users=operator_users, approval_path=approval_path)
    try:
        users = resolved(names, excluded)
    except ValueError as exc:
        # Refused, not broadened: the record keeps what it had, and the rest of new or upgrade goes on.
        return [f"{exc}; {config.project}'s board UI users were left as they were"]
    who = ", ".join(f"{u['account']} (uid {u['uid']})" for u in users) or "nobody"
    if dry_run:
        print_func(f"switchyard: would record {config.project}'s board UI users: {who}")
        return [note] if note else []
    unrecorded = f"{config.project}'s board UI users were not recorded"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{unrecorded}: {path} cannot be read ({exc})"]
    if not isinstance(payload, dict) or payload.get("schema") != REGISTRY_SCHEMA:
        return [f"{unrecorded}: {path} is not a registry record"]
    decision = {"users": users, "source": source, **excluded}
    held = payload.get(RECORD_KEY)
    if isinstance(held, dict) and {k: v for k, v in held.items() if k not in ("recorded_at", "recorded_by")} == decision:
        print_func(f"switchyard: {config.project}'s board UI users are already recorded: {who}")
        return [note] if note else []
    from scripts.ticket_board.rollout_journal import JOURNAL_ROOT_ENV, Attempt, resolve_operator

    operator = resolve_operator()
    payload[RECORD_KEY] = {**decision, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "recorded_by": str(getattr(operator, "name", "") or "")}
    # Root keeps the rollout journal, as only root's record is one the board trusts. An unprivileged run (a
    # test's) journals only where it is told to, and never reaches for root's.
    attempt = None
    if journal_root is not None or os.environ.get(JOURNAL_ROOT_ENV, "").strip() or os.geteuid() == 0:
        attempt = Attempt(config.project, ["switchyard", "board-ui-users", *[u["account"] for u in users]],
                          root=journal_root, operator=str(getattr(operator, "name", "") or ""))
        attempt.operator = operator
        try:
            attempt.open()
        except OSError as exc:  # no decision goes unjournalled
            return [f"{unrecorded}: the rollout journal cannot be opened ({exc})"]
    status, exit_status, detail = "failed", 1, ""
    try:
        _write_atomically(path, payload)
        status, exit_status, detail = "recorded", 0, f"board UI users: {who}; source: {source or 'none'}"
    except OSError as exc:
        detail = f"{unrecorded}: {path} cannot be written ({exc})"
        return [detail]
    finally:
        if attempt is not None:
            attempt.close(status=status, exit_status=exit_status, detail=detail)
    print_func(f"switchyard: {config.project}'s board UI users: {who}" + (f" ({source})" if source else "")
               + ("" if attempt is not None else "; not journalled: only root keeps the rollout journal"))
    return [note] if note else []
