"""Which agent CLI an invoking account can actually run, and where it lives.

A person who runs `switchyard new` usually has their agent CLI in a
per-user directory (`~/.local/bin`, a version manager's shims, ...) that
the project's owner account will never see. This module covers:
- **Finding the CLI the caller would run.** It uses the caller's own PATH,
  recovered from the process tree when `sudo` has replaced it, plus the
  per-user bin directories people install into.
- **Execute permission.** It checks by the POSIX rule whether that account can
  execute the binary.
- **Classifying each role's CLI** as host-wide, caller-only or absent
  (`classify_agent_cli`), and explaining the difference.

`scripts/agent_cli_promotion.py` decides what to do about a caller-only CLI.
Nothing here installs, copies or runs a CLI.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-295). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top. The launcher's
`PROC_ROOT` (patched by the suites), `DEFAULT_PANE_BASE_PATH` and
`FIRST_RUN_AUTH_STATUS_COMMANDS` are read from `scripts.team_launcher` when a
function runs.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


#: What a selected CLI is, from the point of view of a tenant owner that does
#: not exist yet.
#:
#: The distinction is not cosmetic and it is not about accounts in general: it
#: is about WHEN. `switchyard new` chooses CLIs before it creates the owner, so
#: at decision time the owner has no home, no PATH and no files. Anything
#: reachable only through somebody's home directory therefore cannot serve the
#: tenant being planned, however well it works for the person typing the
#: command. Only a host-wide executable can, and that is also the only kind a
#: SECOND tenant gets for free (SYRD-210).
AGENT_CLI_SCOPE_HOST_WIDE = "host_wide"


AGENT_CLI_SCOPE_CALLER_ONLY = "caller_only"


AGENT_CLI_SCOPE_ABSENT = "absent"


#: Where a person's own tools land when they are not installed host-wide. Read
#: as directories, never by asking a shell: an alias or a function is not a file
#: and cannot be promoted, and sourcing somebody's startup files as root runs
#: their code with privilege this command does not need (SYRD-236).
CALLER_LOCAL_BIN_SUBDIRS = (
    ".local/bin",
    "bin",
    ".npm-global/bin",
    ".npm-packages/bin",
    ".yarn/bin",
    ".local/share/pnpm",
    ".bun/bin",
    ".deno/bin",
    ".cargo/bin",
    ".volta/bin",
    ".claude/local",
)


#: Version-managed toolchains, which put the executable behind a directory whose
#: name nobody can predict.
CALLER_LOCAL_BIN_GLOBS = (
    ".nvm/versions/node/*/bin",
    ".local/share/fnm/node-versions/*/installation/bin",
    ".asdf/shims",
)


#: How far up the process tree the invoking human's own process is looked for.
CALLER_PROCESS_TREE_HOPS = 12


@dataclass(frozen=True)
class InvokingAccount:
    """The human who typed the command, which under sudo is not this process."""

    user: str
    uid: int
    gid: int
    home: Path
    source: str

    @property
    def is_this_process(self) -> bool:
        return self.source == "self"


def invoking_account(
    *,
    environ: Mapping[str, str] | None = None,
    operator_resolver: Callable[..., Any] | None = None,
) -> InvokingAccount:
    """Who asked, taken from the mechanism that elevated this process.

    `switchyard new` runs as root through sudo, so "the account running this
    command" is root and its PATH is root's. The human whose CLI installation
    matters is the one sudo recorded, and the same PKEXEC_UID/SUDO_USER rule the
    rollout journal uses answers it without trusting a flag (SYRD-236).
    """
    from scripts.ticket_board.rollout_journal import resolve_operator

    env = dict(os.environ if environ is None else environ)
    resolver = operator_resolver or resolve_operator
    operator = resolver(env)
    name = str(getattr(operator, "name", "") or "")
    uid = getattr(operator, "uid", None)
    source = str(getattr(operator, "source", "") or "")
    if name and uid is not None and int(uid) != os.geteuid():
        try:
            record = pwd.getpwuid(int(uid))
        except KeyError:
            record = None
        if record is not None:
            return InvokingAccount(
                user=record.pw_name, uid=record.pw_uid, gid=record.pw_gid,
                home=Path(record.pw_dir), source=source or "sudo",
            )
    try:
        mine = pwd.getpwuid(os.geteuid())
    except KeyError:
        return InvokingAccount(user="", uid=os.geteuid(), gid=os.getegid(), home=Path(), source="self")
    return InvokingAccount(
        user=mine.pw_name, uid=mine.pw_uid, gid=mine.pw_gid,
        home=Path(mine.pw_dir), source="self",
    )


def _account_groups(account: InvokingAccount) -> set[int]:
    try:
        return set(os.getgrouplist(account.user, account.gid))
    except (KeyError, OSError, TypeError):
        return {account.gid}


def _account_has_bit(info: os.stat_result, account: InvokingAccount, bit: str) -> bool:
    """Read the permission bits for THIS account, rather than asking access().

    Root's access() says yes to everything, and the caller's own 0700 home says
    yes to the caller -- so the only honest answer comes from the bits plus who
    this account is (the SYRD-210 rule, asked about a named account).
    """
    mode = info.st_mode
    if info.st_uid == account.uid:
        return bool(mode & (stat.S_IXUSR if bit == "x" else stat.S_IRUSR))
    if info.st_gid in _account_groups(account):
        return bool(mode & (stat.S_IXGRP if bit == "x" else stat.S_IRGRP))
    return bool(mode & (stat.S_IXOTH if bit == "x" else stat.S_IROTH))


def account_can_execute(path: Path, account: InvokingAccount) -> bool:
    """A concrete regular file this account can reach and run, or nothing.

    Every directory on the way needs execute for this account and the file
    itself needs execute: a name on a PATH is not an executable, and a directory
    or a dangling symlink with the right name is not one either (SYRD-236).
    """
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        return False
    if not stat.S_ISREG(info.st_mode) or not _account_has_bit(info, account, "x"):
        return False
    for parent in resolved.parents:
        try:
            parent_info = parent.stat()
        except OSError:
            return False
        if not _account_has_bit(parent_info, account, "x"):
            return False
    return True


def _caller_path_from_process_tree(
    account: InvokingAccount, *, proc_root: Path | None = None
) -> str:
    """The PATH the invoking human's own process carries.

    Deliberate, not inferred: sudo replaces PATH with secure_path, so root's
    PATH is the wrong question and the right one is still on this host -- in the
    environment of the ancestor process that belongs to that account. Read, not
    executed, and everything found through it is checked before it is offered.
    """
    from scripts import team_launcher as launcher

    root = launcher.PROC_ROOT if proc_root is None else proc_root
    pid = os.getpid()
    for _hop in range(CALLER_PROCESS_TREE_HOPS):
        status = root / str(pid) / "status"
        try:
            lines = status.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        real_uid = None
        parent = None
        for line in lines:
            if line.startswith("Uid:"):
                fields = line.split()
                real_uid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
            elif line.startswith("PPid:"):
                fields = line.split()
                parent = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
        if real_uid == account.uid:
            try:
                raw = (root / str(pid) / "environ").read_bytes()
            except OSError:
                return ""
            for entry in raw.split(b"\0"):
                if entry.startswith(b"PATH="):
                    return entry[len("PATH="):].decode("utf-8", errors="replace")
            return ""
        if not parent or parent <= 1:
            return ""
        pid = parent
    return ""


def caller_command_search_path(
    account: InvokingAccount,
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: Path | None = None,
) -> str:
    """Where to look for this account's own executables, in order.

    Its own PATH when this process is that account, the PATH of its process when
    provisioning crossed sudo, and in both cases the standard per-user install
    directories -- so a fresh host where the operator's shell has the CLI but
    root's PATH does not still finds it (SYRD-236).
    """
    env = os.environ if environ is None else environ
    entries: list[str] = []
    if account.is_this_process:
        entries.extend(str(env.get("PATH", "")).split(os.pathsep))
    else:
        entries.extend(_caller_path_from_process_tree(account, proc_root=proc_root).split(os.pathsep))
    home = account.home
    if str(home) not in ("", "/"):
        entries.extend(str(home / sub) for sub in CALLER_LOCAL_BIN_SUBDIRS)
        for pattern in CALLER_LOCAL_BIN_GLOBS:
            entries.extend(sorted(str(match) for match in home.glob(pattern)))
    ordered: list[str] = []
    for entry in entries:
        candidate = entry.strip()
        if not candidate or not candidate.startswith("/") or candidate in ordered:
            continue
        ordered.append(candidate)
    return os.pathsep.join(ordered)


def caller_executable(
    binary: str,
    *,
    account: InvokingAccount | None = None,
    search_path: str | None = None,
) -> str:
    """The invoking human's own copy of `binary`, canonical and runnable by them."""
    who = account if account is not None else invoking_account()
    where = search_path if search_path is not None else caller_command_search_path(who)
    for directory in where.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / binary
        if account_can_execute(candidate, who):
            try:
                return str(candidate.resolve(strict=True))
            except OSError:
                continue
    return ""


def caller_aware_which(binary: str, path: str | None = None) -> str | None:
    """`shutil.which` for an explicit PATH; the invoking human's tools otherwise.

    The seam every caller of these classifiers already had, with the half that
    was wrong replaced. A named PATH -- the base every pane is given -- is still
    a plain lookup; an unnamed one used to mean "this process's PATH", which
    under sudo is root's and is how a working `claude` was reported missing.
    """
    if path is not None:
        return shutil.which(binary, path=path)
    return caller_executable(binary) or None


@dataclass(frozen=True)
class AgentCliAvailability:
    """Where one selected CLI can be reached from, and by whom."""

    cli: str
    scope: str
    host_wide_path: str = ""
    caller_path: str = ""
    #: Whose context the caller lookup asked about, and where it looked. Under
    #: sudo that account is not this process, so saying "the account running
    #: this command" was both wrong and unactionable (SYRD-236).
    caller_user: str = ""
    caller_search_path: str = ""

    @property
    def serves_a_new_owner(self) -> bool:
        return self.scope == AGENT_CLI_SCOPE_HOST_WIDE


def agent_cli_binary(cli: str) -> str:
    """The executable a CLI selection actually runs.

    Read from the probe command rather than assumed equal to the CLI name, so
    one table stays the source of truth for both.
    """
    from scripts import team_launcher as launcher

    command = launcher.FIRST_RUN_AUTH_STATUS_COMMANDS.get(cli) or []
    return command[0] if command else cli


def classify_agent_cli(
    cli: str,
    *,
    which: Callable[..., str | None] = caller_aware_which,
    account: InvokingAccount | None = None,
) -> AgentCliAvailability:
    """Host-wide, reachable only by the invoking human, or absent.

    Host-wide is decided against DEFAULT_PANE_BASE_PATH -- the base every pane
    is given -- and not against this process's PATH, which carries the invoking
    human's home directories and would call their private install host-wide.

    The other half is that human's own context. `switchyard new` runs as root
    through sudo, so asking without a PATH used to ask root, and a fresh host
    where `which claude` worked for the operator was told claude was installed
    nowhere at all (SYRD-236).
    """
    from scripts import team_launcher as launcher

    binary = agent_cli_binary(cli)
    host_wide = which(binary, path=launcher.DEFAULT_PANE_BASE_PATH) or ""
    if host_wide:
        return AgentCliAvailability(
            cli=cli, scope=AGENT_CLI_SCOPE_HOST_WIDE, host_wide_path=str(host_wide)
        )
    who = account if account is not None else invoking_account()
    searched = caller_command_search_path(who)
    # The shipped lookup is given the account and the search path it belongs to;
    # a test seam still means what it always meant, "name -> path".
    caller = (
        caller_executable(binary, account=who, search_path=searched)
        if which is caller_aware_which
        else (which(binary) or "")
    )
    if caller:
        return AgentCliAvailability(
            cli=cli, scope=AGENT_CLI_SCOPE_CALLER_ONLY, caller_path=str(caller),
            caller_user=who.user, caller_search_path=searched,
        )
    return AgentCliAvailability(
        cli=cli, scope=AGENT_CLI_SCOPE_ABSENT,
        caller_user=who.user, caller_search_path=searched,
    )


def classify_selected_agent_clis(
    role_clis: Sequence[tuple[str, str]],
    *,
    which: Callable[..., str | None] = caller_aware_which,
    account: InvokingAccount | None = None,
) -> dict[str, AgentCliAvailability]:
    """One verdict per distinct CLI in a selection, in selection order."""
    seen: dict[str, AgentCliAvailability] = {}
    for _role, cli in role_clis:
        if cli in seen:
            continue
        seen[cli] = classify_agent_cli(cli, which=which, account=account)
    return seen


def agent_cli_scope_explanation(availability: AgentCliAvailability, owner_user: str = "") -> str:
    """Why this verdict blocks the tenant, in the reader's terms.

    Never prints a bare vendor installer for a caller-only CLI: run as printed,
    those install into the account of whoever runs them, which is the desktop
    operator and not the owner this tenant is being created for (SYRD-210).
    """
    owner = (owner_user or "").strip() or "the new owner user"
    if availability.scope == AGENT_CLI_SCOPE_HOST_WIDE:
        return f"{availability.cli} is installed host-wide at {availability.host_wide_path}"
    caller_account = (availability.caller_user or "").strip()
    if availability.scope == AGENT_CLI_SCOPE_CALLER_ONLY:
        # Named, because under sudo the account running this command is root
        # and the account that can reach this copy is the human who typed it.
        whose = (
            f"only by {caller_account}, the account this command was run from"
            if caller_account
            else "only by the account running this command"
        )
        return (
            f"{availability.cli} is installed at {availability.caller_path}, which is reachable "
            f"{whose}. {owner} does not exist yet and will not "
            f"inherit it, and no later tenant would either"
        )
    # Which account and which directories, so the answer is checkable rather
    # than a flat denial of something the operator can see working (SYRD-236).
    checked = ""
    if caller_account:
        checked = f"; checked {caller_account}'s own tools"
        if availability.caller_search_path:
            checked += f" on {availability.caller_search_path}"
    return (
        f"{availability.cli} is not installed anywhere this host can reach: not host-wide, and "
        f"not for the account running this command{checked}"
    )


class AgentCliUnavailable(SystemExit):
    """Refused before the first mutation, with everything needed to fix it.

    A SystemExit so it ends the command, and its own type so a caller that
    wants to distinguish "we chose not to provision" from "provisioning broke"
    can. Raised only from the precheck, which runs before the owner account,
    the project directory or any provisioning artifact exists -- declining an
    installation must never leave a half-built tenant behind (SYRD-210).
    """
