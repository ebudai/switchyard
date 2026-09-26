"""Seeding a role's provider credentials from its owner's, without following links.

A role runs as its own account, but a provider (agy, Hermes, ...) was
authenticated once by the project owner. This module covers:
- which credential artifacts each runtime reads (`ROLE_CREDENTIAL_ARTIFACTS`);
- what state they are in (`role_credential_manifest`);
- copying them into a role's home (`seed_role_credential`), with the owner's
  ownership and private modes, and never through a symlink;
- the `switchyard seed-role-credentials` verb.

The owner-safe filesystem primitives the copy stands on live here too: the
`openat` no-follow walk, the owner-traversal checks, and the fd copy.
`scripts/agy_credential.py` uses the same primitives for the owner's own agy
token.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-287). `team_launcher`
imports this module at its top and still exports the names callers reach
there. This module never imports `team_launcher` at its top. Launcher
facilities (`uid_for_user`, `home_dir_for_user`, `current_user_name` and the
rest) are read from `scripts.team_launcher` when a function runs, so the
suites' patches on the launcher still reach them. For the same reason the two
owner-traversal checks, although defined here, are called through the launcher:
tests patch `team_launcher._require_owner_home_traversable` and
`_require_owner_traversable`, and those patches must reach the copies here.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


AGY_CREDENTIAL_DIR_NAME = ".gemini/antigravity-cli"


AGY_CREDENTIAL_TOKEN_NAME = "antigravity-oauth-token"


def switchyard_seed_role_credentials_command(
    config: ProjectConfig,
    *,
    role_name: str = "",
    reseed: bool = False,
    home_base: Path = Path("/home"),
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Give each role its own private copy of the credentials its CLI needs.

    Idempotent: a role that already has a credential is left alone unless
    --reseed is passed, which is the deliberate repair/sync path. Nothing here
    changes how the board derives a role -- that stays SO_PEERCRED uid -- and
    nothing is shared between roles: each copy is owned by that role, 0600,
    under a 0700 directory (SYRD-39).
    """
    from scripts import team_launcher as launcher

    owner_user = config.run_as_user or launcher.current_user_name()
    roles = [role for role in config.roles if not role_name or role.role == role_name]
    if role_name and not roles:
        raise SystemExit(f"switchyard: {config.project} has no role {role_name}")
    seeded = 0
    for role in roles:
        # Seeding is preparation, and preparation happens BEFORE the active
        # configuration names the accounts -- naming them early is what breaks
        # the running roles. The pending plan is what says where to seed
        # (SYRD-45).
        identity = launcher.pending_identity_for(config, role)
        account = identity.get("account", "")
        if not account or account == owner_user:
            print_func(f"switchyard: {role.role} has no Unix account of its own; nothing to seed")
            continue
        role_home = Path(identity.get("home") or "") if identity.get("home") else None
        cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
        artifacts = role_credential_artifacts(role)
        if not artifacts:
            raise SystemExit(
                f"switchyard: no credential allowlist for {cli_name or 'unknown cli'} used by "
                f"{role.role}; refusing to guess what to copy"
            )
        seeded_for_role = 0
        for artifact in artifacts:
            if seed_role_credential(
                account=account,
                owner_user=owner_user,
                artifact=artifact,
                target=_role_credential_target(
                    config, role, artifact, home_base=home_base, account=account, role_home=role_home
                ),
                home_base=home_base,
                reseed=reseed,
                runner=runner,
                print_func=print_func,
            ):
                seeded += 1
                seeded_for_role += 1
        if not seeded_for_role and all(not artifact.required for artifact in artifacts):
            # Nothing was copied. That is fine when the role already holds one
            # of the alternatives -- this command is idempotent -- and a failure
            # when it holds none, because the owner is then not authenticated
            # for this CLI and the role must not start.
            already_held = any(
                not _credential_state(
                    *_role_credential_target(config, role, artifact, home_base=home_base),
                    account,
                    private=True,
                )
                for artifact in artifacts
            )
            if not already_held:
                raise SystemExit(
                    f"switchyard: {owner_user} has none of "
                    + ", ".join(artifact.relative_path for artifact in artifacts)
                    + f"; authenticate {cli_name} as {owner_user} first"
                )
    print_func(
        f"switchyard: seeded {seeded} credential file(s); every role now acts as the same "
        "model-provider account as the owner, and shares that account's quota, while keeping "
        "its own filesystem and its own board role"
    )
    return 0


def _owner_can_traverse(info: os.stat_result, owner_user: str, owner_uid: int) -> bool:
    """Whether owner_user has execute permission on a directory, by the POSIX rule."""
    from scripts import team_launcher as launcher

    if info.st_uid == owner_uid:
        return bool(info.st_mode & stat.S_IXUSR)
    if info.st_gid in launcher._group_ids_for_user(owner_user):
        return bool(info.st_mode & stat.S_IXGRP)
    return bool(info.st_mode & stat.S_IXOTH)


def _require_owner_home_traversable(dir_fd: int, owner_user: str, home: Path) -> None:
    """Refuse an owner home the owner cannot enter.

    Only reachability is required here, not ownership: a home is not switchyard's to
    dictate, and one owned by root but world-executable is genuinely fine. A
    Director-approved pre-existing owner may have a home this function did not create, so
    it is proven rather than assumed -- a credential under a home the owner cannot
    traverse is exactly the unreadable-token failure this ticket exists to remove.
    """
    from scripts import team_launcher as launcher

    owner_uid = launcher._uid_for_user(owner_user)
    if owner_uid is None:
        raise SystemExit(
            f"switchyard: {owner_user!r} is not a user on this machine, so {home} cannot "
            "be shown to be reachable by it"
        )
    info = os.fstat(dir_fd)
    if not _owner_can_traverse(info, owner_user, owner_uid):
        raise SystemExit(
            f"switchyard: home {home} is owned by uid {info.st_uid} with mode "
            f"{stat.S_IMODE(info.st_mode):04o}, so {owner_user} (uid {owner_uid}) could "
            "not enter it and agy could not read a credential below it; fix the home's "
            "ownership or permissions and rerun"
        )


def _require_owner_traversable(dir_fd: int, owner_user: str, component: str, target_dir: Path) -> None:
    """Refuse a pre-existing component the pane owner cannot enter.

    A directory switchyard did not create carries no guarantee at all: it may be left
    over from a run whose ownership assignment failed, or simply be someone else's. A
    token written beneath one the owner cannot traverse is unreachable to agy, which is
    the very failure this ticket exists to fix, so it is a refusal rather than a warning.
    """
    from scripts import team_launcher as launcher

    owner_uid = launcher._uid_for_user(owner_user)
    if owner_uid is None:
        raise SystemExit(
            f"switchyard: {owner_user!r} is not a user on this machine, so {target_dir} "
            "cannot be shown to be reachable by it"
        )
    info = os.fstat(dir_fd)
    if info.st_uid != owner_uid or not info.st_mode & stat.S_IXUSR:
        raise SystemExit(
            f"switchyard: existing directory {component!r} of {target_dir} is owned by uid "
            f"{info.st_uid} with mode {stat.S_IMODE(info.st_mode):04o}, so {owner_user} "
            f"(uid {owner_uid}) could not enter it and agy could not read a credential "
            "below it; remove or fix that directory and rerun"
        )


# Hermes reads provider keys from its own home. The owner's authenticated state
# lives in ~/.hermes; a role reads the same two files from the per-role
# HERMES_HOME the launcher already gives it, so nothing has to be injected into
# the environment and no shell file is ever sourced (SYRD-39).
HERMES_OWNER_CREDENTIAL_DIR = ".hermes"


# Hermes keeps model-provider keys in the SAME .env as SUDO_PASSWORD, messaging
# bot tokens, GitHub tokens, tool credentials and terminal SSH keys -- its own
# terminal tool consumes SUDO_PASSWORD. Copying that file into every role would
# hand each one the owner's login password and external identities, which is the
# opposite of what per-role accounts are for. Only these keys cross, and a new
# file is constructed rather than the source copied (SYRD-39).
HERMES_PROVIDER_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "ARCEEAI_API_KEY",
        "ARCEE_BASE_URL",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_BASE_URL",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        "GLM_API_KEY",
        "GLM_BASE_URL",
        "GMI_API_KEY",
        "GMI_BASE_URL",
        "GOOGLE_API_KEY",
        "HERMES_GEMINI_CLIENT_ID",
        "HERMES_GEMINI_CLIENT_SECRET",
        "HERMES_GEMINI_PROJECT_ID",
        "HERMES_QWEN_BASE_URL",
        "HF_BASE_URL",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "KIMI_CN_API_KEY",
        "LM_API_KEY",
        "LM_BASE_URL",
        "MINIMAX_API_KEY",
        "MINIMAX_BASE_URL",
        "MINIMAX_CN_API_KEY",
        "MINIMAX_CN_BASE_URL",
        "MISTRAL_API_KEY",
        "NOUS_BASE_URL",
        "NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_BASE_URL",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_ZEN_BASE_URL",
        "OPENROUTER_API_KEY",
        "STEPFUN_API_KEY",
        "STEPFUN_BASE_URL",
        "XAI_API_KEY",
        "XAI_BASE_URL",
        "XIAOMI_API_KEY",
        "XIAOMI_BASE_URL",
        "ZAI_API_KEY",
        "Z_AI_API_KEY",
    }
)


@dataclass(frozen=True)
class RoleCredentialArtifact:
    """One file a CLI needs in a role's own home to start authenticated.

    An allowlist, deliberately narrow. Whole CLI homes are never copied: they
    carry conversations, histories, caches, hooks and general settings, and
    sharing those between roles would hand every role the others' work as well
    as their tokens (SYRD-39).
    """

    cli: str
    relative_path: str
    required: bool = True


# What each supported CLI needs, and nothing else. Anything not listed here is
# never copied into a role home.
ROLE_CREDENTIAL_ARTIFACTS: dict[str, tuple[RoleCredentialArtifact, ...]] = {
    "claude": (RoleCredentialArtifact("claude", ".claude/.credentials.json"),),
    "codex": (RoleCredentialArtifact("codex", ".codex/auth.json"),),
    "agy": (
        RoleCredentialArtifact(
            "agy", f"{AGY_CREDENTIAL_DIR_NAME}/{AGY_CREDENTIAL_TOKEN_NAME}"
        ),
    ),
    # Hermes resolves provider keys from the environment, not a token file, and
    # an ambient environment does not survive sudo -u into a separate account.
    # The owner therefore keeps its key in one private file, which is seeded
    # like any other credential and sourced by the role's own shell at start.
    # Targets are inside the role's own HERMES_HOME rather than its home root,
    # so they are resolved per role rather than listed here.
    # Only the filtered .env. auth.json is deliberately NOT seeded: its schema
    # is not something this code can verify is provider-only, and an artifact
    # whose contents cannot be constrained must not cross the boundary.
    "hermes": (RoleCredentialArtifact("hermes", f"{HERMES_OWNER_CREDENTIAL_DIR}/.env"),),
}


def hermes_credential_target(config: ProjectConfig, role: RoleConfig, artifact: RoleCredentialArtifact) -> Path:
    """Where a hermes role reads one credential from.

    Its own HERMES_HOME, which the launcher already points it at, so the file is
    read by hermes itself out of a directory the role owns. Nothing is injected
    into the environment and no shell file is sourced.
    """
    base, relative = _role_credential_target(config, role, artifact)
    return base / relative


def select_hermes_provider_env(text: str) -> tuple[str, list[str], str]:
    """Build a role .env holding only inference-provider authentication.

    Returns (content, omitted_keys, problem). The source is parsed, not copied:
    plain KEY=value grammar stops shell execution but says nothing about WHICH
    secrets cross, and hermes stores SUDO_PASSWORD, messaging tokens, GitHub and
    tool credentials in the same file. Everything outside the provider allowlist
    is dropped, and the caller reports what was left behind so the omission is
    visible rather than silent (SYRD-39).
    """
    kept: list[str] = []
    omitted: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator:
            return "", [], f"line {number} is not KEY=value"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return "", [], f"line {number} does not name a valid environment variable"
        if name in HERMES_PROVIDER_ENV_KEYS:
            kept.append(f"{name}={value.strip()}")
        else:
            omitted.append(name)
    if not kept:
        return "", omitted, "it holds no inference-provider credentials"
    header = (
        "# Generated by switchyard: inference-provider authentication only.\n"
        "# Other entries in the owner's .env are deliberately not shared with roles.\n"
    )
    return header + "\n".join(kept) + "\n", omitted, ""


def role_credential_artifacts(role: RoleConfig) -> tuple[RoleCredentialArtifact, ...]:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return ROLE_CREDENTIAL_ARTIFACTS.get(cli_name, ())


def _credential_state(
    base: Path,
    relative: Path | str,
    expected_user: str,
    *,
    private: bool,
) -> str:
    """Empty when the credential is safe to rely on, otherwise why it is not.

    Every component from the home downwards is opened with O_NOFOLLOW, so a
    symlinked ancestor is refused rather than silently followed, and the leaf is
    inspected through that anchored descriptor rather than by path.
    """
    from scripts import team_launcher as launcher

    relative_path = Path(relative)
    expected_uid = launcher.uid_for_user(expected_user)
    if expected_uid is None:
        return f"cannot be checked: {expected_user} is not a local account"
    dir_fd, problem = launcher._walk_no_follow(base, relative_path)
    if problem:
        return problem
    try:
        try:
            info = os.stat(relative_path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            return f"unreadable ({exc.strerror})"
        if stat.S_ISLNK(info.st_mode):
            return "is a symlink; credentials must be private regular files"
        if not stat.S_ISREG(info.st_mode):
            return "is not a regular file"
        if info.st_uid != expected_uid:
            return f"is owned by uid {info.st_uid}, not {expected_user}"
        if info.st_mode & 0o077:
            return f"is readable beyond its owner (mode {oct(stat.S_IMODE(info.st_mode))})"
        if private:
            parent = os.fstat(dir_fd)
            if parent.st_uid != expected_uid:
                return (
                    f"its directory is owned by uid {parent.st_uid}, not {expected_user}"
                )
            if parent.st_mode & 0o077:
                return (
                    "its directory is reachable beyond its owner "
                    f"(mode {oct(stat.S_IMODE(parent.st_mode))})"
                )
    finally:
        os.close(dir_fd)
    return ""


def _artifact_present(home: Path, artifact: RoleCredentialArtifact) -> bool:
    candidate = home / artifact.relative_path
    try:
        return candidate.is_file()
    except OSError:
        return False


def _role_credential_target(
    config: ProjectConfig,
    role: RoleConfig,
    artifact: RoleCredentialArtifact,
    *,
    home_base: Path | None = None,
    account: str = "",
    role_home: Path | None = None,
) -> tuple[Path, Path]:
    """(base, relative) for where a role reads one credential.

    Most CLIs read from a fixed place under the role's home. Hermes reads from
    the per-role HERMES_HOME the launcher already points it at, so its target is
    resolved per role rather than assumed to mirror the owner's layout.
    """
    from scripts import team_launcher as launcher

    account = account or role.run_as_user or launcher.role_run_as_user(config, role)
    if role_home is None or home_base is not None:
        role_home = (
            home_base / account
            if home_base is not None
            else launcher.home_dir_for_user(account) or Path("/home") / account
        )
    if artifact.cli == "hermes":
        # Always expressed relative to the role's home so every component is
        # created and anchored under it, whatever base is in use.
        session_relative = Path(".local/state") / f"{config.project}-ticket-board"
        home_name = launcher.session_file_name(role.target).removesuffix(".json")
        return role_home, (
            session_relative
            / "hermes-homes"
            / home_name
            / Path(artifact.relative_path).name
        )
    return role_home, Path(artifact.relative_path)


def role_credential_manifest(config: ProjectConfig) -> list[str]:
    """What still has to be true before each role can start authenticated.

    Fails closed by being explicit: every line names the role, the CLI, and the
    exact artifact that is missing from the role's own home or absent from the
    owner's, rather than letting a role launch and fail at the provider.
    """
    from scripts import team_launcher as launcher

    owner_user = config.run_as_user or launcher.current_user_name()
    owner_home = launcher.home_dir_for_user(owner_user)
    lines: list[str] = []
    for role in config.roles:
        account = role.run_as_user or launcher.role_run_as_user(config, role)
        cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
        if not account or account == config.run_as_user:
            continue
        role_home = launcher.home_dir_for_user(account)
        artifacts = role_credential_artifacts(role)
        if not artifacts:
            lines.append(f"{role.role} ({cli_name or 'unknown cli'}): no known credential artifact")
            continue
        optional_group = all(not artifact.required for artifact in artifacts)
        satisfied = False
        pending: list[str] = []
        for artifact in artifacts:
            if role_home is not None:
                target_base, target_relative = _role_credential_target(config, role, artifact)
                target_problem = _credential_state(
                    target_base, target_relative, account, private=True
                )
                if not target_problem:
                    satisfied = True
                    continue
                if target_problem != "missing":
                    # Present but unsafe is worse than absent: say so instead of
                    # treating it as ready.
                    # Present but unsafe is worse than absent, for an
                    # alternative as much as a required artifact.
                    lines.append(
                        f"{role.role} ({cli_name}): {artifact.relative_path} in /home/{account} "
                        f"{target_problem}; reseed it with "
                        f"`switchyard seed-role-credentials {config.project} --role {role.role} --reseed`"
                    )
                    satisfied = True
                    continue
            owner_problem = (
                "missing"
                if owner_home is None
                else _credential_state(owner_home, artifact.relative_path, owner_user, private=False)
            )
            if owner_problem:
                if owner_problem == "missing" and optional_group:
                    # One of several alternatives; only report if none exist.
                    continue
                pending.append(
                    f"{role.role} ({cli_name}): the owner's {artifact.relative_path} "
                    f"{owner_problem}, so it cannot be seeded; authenticate {cli_name} as "
                    f"{owner_user} first"
                )
                continue
            pending.append(
                f"{role.role} ({cli_name}): {artifact.relative_path} not yet seeded into "
                f"/home/{account}"
            )
        if optional_group and satisfied:
            continue
        if optional_group and not pending:
            lines.append(
                f"{role.role} ({cli_name}): the owner has none of "
                + ", ".join(artifact.relative_path for artifact in artifacts)
                + f"; authenticate {cli_name} as {owner_user} first"
            )
            continue
        lines.extend(pending)
    return lines


def _open_role_credential_parent(
    account: str,
    base: Path,
    relative_path: Path | str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """Open the role-owned directory that will hold one credential.

    Extends the SYRD-28 model: every component is created with mkdirat 0700 and
    opened with openat/O_NOFOLLOW relative to the descriptor above it, so no
    privileged step is ever handed a whole path that a symlink could redirect,
    and a component that already existed is proven to belong to the role rather
    than assumed to (SYRD-39).
    """
    from scripts import team_launcher as launcher

    relative = Path(relative_path)
    target_dir = base / relative.parent
    fd = os.open(base.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = _openat_no_follow(fd, base.name, target_dir)
        launcher._require_owner_home_traversable(fd, account, base)
        for component in relative.parent.parts:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise SystemExit(
                    f"switchyard: failed to create {target_dir} for {account} ({exc.strerror})"
                ) from exc
            parent_fd = fd
            child_fd = _openat_no_follow_keep_parent(parent_fd, component, target_dir)
            try:
                if created:
                    os.fchmod(child_fd, 0o700)
                    own = runner(["chown", f"{account}:{account}", f"/proc/{os.getpid()}/fd/{child_fd}"])
                    if own.returncode != 0:
                        raise SystemExit(f"switchyard: failed to assign {target_dir} to {account}")
                else:
                    launcher._require_owner_traversable(child_fd, account, component, target_dir)
            except BaseException:
                os.close(child_fd)
                if created:
                    try:
                        os.rmdir(component, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            os.close(parent_fd)
            fd = child_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _try_open_credential_source(base: Path, relative: Path | str) -> tuple[int, str]:
    """Open a credential for reading with every component anchored.

    Returns (fd, problem); fd is -1 when problem is set. Unlike the raising
    variant this lets an optional artifact be absent without aborting.
    """
    from scripts import team_launcher as launcher

    relative_path = Path(relative)
    dir_fd, problem = launcher._walk_no_follow(base, relative_path)
    if problem:
        return -1, problem
    try:
        try:
            return os.open(relative_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd), ""
        except FileNotFoundError:
            return -1, "missing"
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return -1, "is a symlink; credentials must be private regular files"
            return -1, f"cannot be opened ({exc.strerror})"
    finally:
        os.close(dir_fd)


def _fd_credential_state(fd: int, expected_user: str) -> str:
    """Validate the OPEN FILE, not a path that may have changed since.

    Validating by path and then reading by path leaves a window in which the
    name can be repointed at another file, and the privileged seeding command
    would read outside the approved artifact. Everything here is decided on the
    descriptor the bytes are actually read from (SYRD-39).
    """
    from scripts import team_launcher as launcher

    expected_uid = launcher.uid_for_user(expected_user)
    if expected_uid is None:
        return f"cannot be checked: {expected_user} is not a local account"
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_uid != expected_uid:
        return f"is owned by uid {info.st_uid}, not {expected_user}"
    if info.st_mode & 0o077:
        return f"is readable beyond its owner (mode {oct(stat.S_IMODE(info.st_mode))})"
    return ""


def _read_fd_bytes(fd: int) -> bytes:
    """Read a whole file from an already-open descriptor."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte; os.write may write fewer than requested."""
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])


def seed_role_credential(
    *,
    account: str,
    owner_user: str,
    artifact: RoleCredentialArtifact,
    target: tuple[Path, Path] | None = None,
    home_base: Path = Path("/home"),
    reseed: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Give one role its own private copy of one credential.

    A copy, never a symlink and never a shared directory: the role owns the
    file, refreshes rotate its own copy, and revoking one role's filesystem
    access does not touch another's. Returns False when the role already has it
    and reseed was not requested, which is what makes repair idempotent.
    """
    role_home = home_base / account
    owner_home = home_base / owner_user
    target_base, target_relative = target or (role_home, Path(artifact.relative_path))
    target_name = target_relative.name
    # Open the source ONCE, through anchored components, and decide everything
    # from that descriptor. Validating a path and then reading the same path
    # leaves a window in which the name can be repointed at another file
    # (SYRD-39).
    source_fd, source_problem = _try_open_credential_source(owner_home, artifact.relative_path)
    if source_problem == "missing" and not artifact.required:
        return False
    if source_problem == "missing":
        raise SystemExit(
            f"switchyard: {owner_user} has no {artifact.relative_path} to seed for {account}; "
            f"authenticate {artifact.cli} as {owner_user} first"
        )
    if source_problem:
        raise SystemExit(
            f"switchyard: {owner_user}'s {artifact.relative_path} {source_problem}; refusing to "
            "seed it"
        )
    constructed: bytes | None = None
    try:
        state_problem = _fd_credential_state(source_fd, owner_user)
        if state_problem:
            # Refuse to propagate a credential that is already unsafe, rather
            # than copying it into every role home.
            raise SystemExit(
                f"switchyard: {owner_user}'s {artifact.relative_path} {state_problem}; refusing "
                "to seed it"
            )
        if artifact.cli == "hermes" and target_relative.name == ".env":
            # Filtered from the bytes of the descriptor that was just validated,
            # never from a fresh read of the name.
            content, omitted, problem = select_hermes_provider_env(
                _read_fd_bytes(source_fd).decode("utf-8", errors="replace")
            )
            if problem:
                raise SystemExit(
                    f"switchyard: {owner_user}'s {artifact.relative_path} cannot be seeded "
                    f"({problem})"
                )
            constructed = content.encode("utf-8")
            if omitted:
                # Named, so the omission is visible rather than silent: these are
                # the owner's other secrets and settings, which roles must not
                # receive.
                print_func(
                    f"switchyard: not sharing {len(omitted)} non-provider entr"
                    + ("y" if len(omitted) == 1 else "ies")
                    + f" from {owner_user}'s {artifact.relative_path} with {account}: "
                    + ", ".join(sorted(omitted))
                )
    except BaseException:
        os.close(source_fd)
        raise
    dir_fd = _open_role_credential_parent(
        account, target_base, target_relative, runner=runner
    )
    created_target = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if reseed else os.O_EXCL)
        try:
            target_fd = os.open(target_name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            print_func(
                f"switchyard: {account} already has {artifact.relative_path}; leaving it alone "
                "(pass --reseed to replace it)"
            )
            return False
        except OSError as exc:
            raise SystemExit(
                f"switchyard: failed to seed {artifact.relative_path} for {account} "
                f"({exc.strerror})"
            ) from exc
        created_target = not reseed
        try:
            os.fchmod(target_fd, 0o600)
            if constructed is None:
                os.lseek(source_fd, 0, os.SEEK_SET)
                _copy_fd_contents(source_fd, target_fd)
            else:
                # A newly built file, never the source bytes, written in full.
                _write_all(target_fd, constructed)
            # Ownership is assigned to the open description rather than the
            # path, so nothing can be substituted between copy and chown.
            own = runner(["chown", f"{account}:{account}", f"/proc/{os.getpid()}/fd/{target_fd}"])
            if own.returncode != 0:
                raise SystemExit(
                    f"switchyard: failed to assign {artifact.relative_path} to {account}"
                )
        finally:
            os.close(target_fd)
    except BaseException:
        if created_target:
            try:
                os.unlink(target_name, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)
        os.close(dir_fd)
    print_func(f"switchyard: seeded {artifact.relative_path} for {account} from {owner_user}")
    return True


def _openat_no_follow_keep_parent(parent_fd: int, component: str, target_dir: Path) -> int:
    """Like _openat_no_follow, but the caller keeps ownership of parent_fd."""
    try:
        return os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SystemExit(
                f"switchyard: path component {component!r} of {target_dir} is a symlink "
                "or not a directory; refusing to write a credential through a replaced "
                "path component"
            ) from exc
        raise SystemExit(
            f"switchyard: failed to open {component!r} of {target_dir} ({exc.strerror})"
        ) from exc


def _openat_no_follow(parent_fd: int, component: str, target_dir: Path) -> int:
    """Open one path component relative to parent_fd, refusing to traverse a symlink."""
    try:
        nested = os.open(
            component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SystemExit(
                f"switchyard: path component {component!r} of {target_dir} is a symlink "
                "or not a directory; refusing to write a credential through a replaced "
                "path component"
            ) from exc
        raise SystemExit(
            f"switchyard: failed to open {component!r} of {target_dir} ({exc.strerror})"
        ) from exc
    os.close(parent_fd)
    return nested


def _copy_fd_contents(source_fd: int, target_fd: int) -> None:
    """Copy fd to fd in the kernel, so the token's bytes never enter this process."""
    offset = 0
    while True:
        sent = os.sendfile(target_fd, source_fd, offset, 1 << 20)
        if not sent:
            return
        offset += sent
