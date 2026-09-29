"""The project owner's GitHub SSH identity, as provisioning renders and reads it.

The managed block in the owner's `~/.ssh/config` (between
`GITHUB_IDENTITY_BEGIN` and `GITHUB_IDENTITY_END`) that selects a named key for
the GitHub host; composing it into an existing config without touching anything
outside the markers; reading an existing block back and resolving the identity
to keep across an upgrade; the operator commands that write, select or remove
it; and whether a publication remote is on GitHub at all.

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-463).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote`, `_refuse_unnormalized` and `PathContainmentError` -- is read
through it when they run, so a patch there still reaches them. This module
imports `project_provision` only inside the functions that need it, when they
run, with the same fallback for direct script execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


#: The markers around the identity selection this project manages. Everything
#: between them is rewritten on every run; everything outside them is somebody
#: else's and is preserved (SYRD-74).
GITHUB_IDENTITY_BEGIN = "# BEGIN SWITCHYARD MANAGED GITHUB IDENTITY"
GITHUB_IDENTITY_END = "# END SWITCHYARD MANAGED GITHUB IDENTITY"
DEFAULT_GITHUB_HOST = "github.com"


def owner_github_key_path(owner_home: str, *, key_name: str = "") -> str:
    """The owner's GitHub key. Named, because a project account may hold several.

    The account this was found on had a perfectly good ED25519 key under a
    nonstandard filename and no configuration selecting it, so git offered
    nothing and the push failed with `Permission denied (publickey)`. A named
    key is the normal case here, not the exception (SYRD-74).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    name = (key_name or "").strip() or "id_ed25519"
    if "/" in name:
        raise provision.PathContainmentError(f"the GitHub key name {name} may not contain a path separator")
    return f"{owner_home.rstrip('/')}/.ssh/{name}"


def github_identity_block(
    owner_home: str, *, key_name: str = "", host: str = DEFAULT_GITHUB_HOST, host_alias: str = ""
) -> str:
    """The ssh_config stanza that makes git offer this project's key.

    `IdentitiesOnly yes` because an agent holding other keys will otherwise
    offer those first and GitHub will answer for whichever account it recognises
    -- which is how a push ends up rejected for the wrong identity rather than
    for a missing one.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    key = provision.owner_github_key_path(owner_home, key_name=key_name)
    lines = [provision.GITHUB_IDENTITY_BEGIN]
    for pattern in (host, *( [host_alias] if host_alias.strip() else [] )):
        lines.extend(
            [
                f"Host {pattern}",
                f"    HostName {host}",
                "    User git",
                f"    IdentityFile {key}",
                "    IdentitiesOnly yes",
                "",
            ]
        )
    lines[-1] = provision.GITHUB_IDENTITY_END
    return "\n".join(lines) + "\n"


def compose_ssh_config(existing: str, block: str) -> str:
    """The owner's ssh_config with exactly one managed block, at the front.

    First rather than appended: ssh takes the first value it obtains for each
    keyword, so a managed identity that lands after somebody else's `Host *`
    stanza is a managed identity that does not apply. Everything outside the
    markers is left exactly as it was, including a half-written file with a
    begin marker and no end (SYRD-74).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    kept: list[str] = []
    inside = False
    saw_begin = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped == provision.GITHUB_IDENTITY_BEGIN:
            inside = True
            saw_begin = True
            continue
        if inside:
            if stripped == provision.GITHUB_IDENTITY_END:
                inside = False
            continue
        kept.append(line)
    if inside and saw_begin:
        # A begin with no end: everything after it was this block's, so it is
        # replaced rather than kept as somebody else's configuration.
        pass
    remainder = "\n".join(kept).strip("\n")
    if not remainder:
        return block
    # The block already ends in a newline; the rest follows it directly, which
    # is what the rendered shell produces by concatenation.
    return f"{block}{remainder}\n"


@dataclass(frozen=True)
class OwnerGithubIdentity:
    """Which of the owner's keys this tenant publishes with, and how that is known.

    SYRD-74 made a named key the normal case. The upgrade path then called the
    renderer without one, so it defaulted to `id_ed25519`, generated that key,
    and rewrote the managed block away from the working deploy key the tenant
    had been using -- and the next push was refused for an identity GitHub had
    never seen (SYRD-100).
    """

    key_name: str
    host: str = DEFAULT_GITHUB_HOST
    host_alias: str = ""
    #: Where the selection came from, for the operator line that reports it.
    source: str = "default"
    #: Non-empty when the selection could not be established. The caller must
    #: then change nothing at all rather than fall back to a default.
    problems: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return not self.problems


def parse_managed_github_identity(config_text: str) -> tuple[str, str, str, tuple[str, ...]]:
    """The key file, host and alias the managed block currently selects.

    Read from the block's own markers, so an operator's other stanzas are never
    mistaken for this tenant's selection. Returns empty strings when there is no
    managed block, which is a fresh tenant rather than a problem.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    identity_files: list[str] = []
    host_patterns: list[str] = []
    host_names: list[str] = []
    inside = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped == provision.GITHUB_IDENTITY_BEGIN:
            inside = True
            continue
        if not inside:
            continue
        if stripped == provision.GITHUB_IDENTITY_END:
            inside = False
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        keyword, value = parts[0].casefold(), parts[1].strip()
        if keyword == "identityfile":
            identity_files.append(value)
        elif keyword == "host":
            host_patterns.append(value)
        elif keyword == "hostname":
            host_names.append(value)
    if not host_patterns and not identity_files:
        return "", "", "", ()
    unique_files = sorted(set(identity_files))
    if len(unique_files) != 1:
        return "", "", "", (
            f"the managed GitHub identity block selects {len(unique_files)} different key files "
            f"({', '.join(unique_files) or 'none'}), so which one this tenant publishes with "
            "cannot be read from it",
        )
    key_file = unique_files[0]
    name = key_file.rsplit("/", 1)[-1]
    if not name or name != Path(key_file).name:
        return "", "", "", (
            f"the managed GitHub identity block names {key_file}, which is not a key file this "
            "tenant can select",
        )
    host = host_names[0] if host_names else provision.DEFAULT_GITHUB_HOST
    alias = host_patterns[1] if len(host_patterns) > 1 else ""
    return name, host, alias, ()


def existing_owner_ssh_key_names(owner_home: str) -> tuple[str, ...]:
    """The keys the owner already holds, by file name.

    A key with both halves present is one somebody set up. Offering to generate
    a competing default alongside it is how the working one stopped being used.
    """
    ssh_dir = Path(owner_home.rstrip("/")) / ".ssh"
    names: list[str] = []
    try:
        entries = sorted(ssh_dir.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if entry.suffix != ".pub":
            continue
        private = entry.with_name(entry.name[: -len(".pub")])
        if private.is_file():
            names.append(private.name)
    return tuple(names)


def resolve_owner_github_identity(
    owner_home: str,
    *,
    recorded_key_name: str = "",
    recorded_host: str = "",
    recorded_host_alias: str = "",
) -> OwnerGithubIdentity:
    """Which key this tenant publishes with, from what is recorded or installed.

    In order: what the tenant recorded when it was provisioned; then what its
    own managed block currently selects, which is the only record a tenant
    provisioned before this existed has; then, only for an owner holding no keys
    at all, the default a fresh provision would create.

    An owner who holds keys and has no readable selection is the case that must
    stop rather than guess: choosing one of several existing keys, or generating
    a new one beside them, is exactly the substitution this ticket is about.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if recorded_key_name.strip():
        return provision.OwnerGithubIdentity(
            key_name=recorded_key_name.strip(),
            host=(recorded_host.strip() or provision.DEFAULT_GITHUB_HOST),
            host_alias=recorded_host_alias.strip(),
            source="the tenant's recorded selection",
        )
    config = Path(owner_home.rstrip("/")) / ".ssh" / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    installed, host, alias, problems = provision.parse_managed_github_identity(text)
    if problems:
        return provision.OwnerGithubIdentity(key_name="", problems=problems)
    if installed:
        return provision.OwnerGithubIdentity(
            key_name=installed,
            host=host or provision.DEFAULT_GITHUB_HOST,
            host_alias=alias,
            source=f"the managed block in {config}",
        )
    existing = provision.existing_owner_ssh_key_names(owner_home)
    if existing:
        return provision.OwnerGithubIdentity(
            key_name="",
            problems=(
                f"{owner_home} holds {', '.join(existing)} and nothing records which of them this "
                f"tenant publishes with: {config} has no managed GitHub identity block and the "
                "tenant's plan predates recording one. Nothing was changed. Re-provision the "
                "tenant, or add the block naming the key it has been using, and run this again.",
            ),
        )
    return provision.OwnerGithubIdentity(
        key_name="",
        host=provision.DEFAULT_GITHUB_HOST,
        source="the default for an owner with no keys",
    )


def owner_github_identity_commands(
    owner_user: str,
    owner_home: str,
    *,
    key_name: str = "",
    host: str = DEFAULT_GITHUB_HOST,
    host_alias: str = "",
    comment: str = "",
) -> list[str]:
    """Give the project owner a GitHub identity, and select it. Re-runnable.

    Nothing here reads, prints or copies private key material: the key is
    generated in place by the owner if it is missing, and everything after that
    is modes, ownership and one configuration stanza. The public half is the
    only thing an operator is ever shown (SYRD-74).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    key = provision.owner_github_key_path(owner_home, key_name=key_name)
    ssh_dir = f"{owner_home.rstrip('/')}/.ssh"
    config = f"{ssh_dir}/config"
    q_owner = provision.shell_quote(owner_user)
    q_key = provision.shell_quote(key)
    q_dir = provision.shell_quote(ssh_dir)
    q_config = provision.shell_quote(config)
    label = comment.strip() or f"{owner_user} switchyard"
    block = provision.github_identity_block(owner_home, key_name=key_name, host=host, host_alias=host_alias)
    return [
        f"sudo install -d -m 0700 -o {q_owner} -g {q_owner} {q_dir}",
        # Generated only when absent, and never printed. A second run finds it
        # and leaves it alone, which is what makes this safe to re-run.
        f"if [ ! -f {q_key} ]; then",
        f"    sudo -u {q_owner} ssh-keygen -t ed25519 -N '' -C {provision.shell_quote(label)} -f {q_key}",
        "fi",
        f"sudo chown {q_owner}:{q_owner} {q_key} {q_key}.pub",
        f"sudo chmod 0600 {q_key}",
        f"sudo chmod 0644 {q_key}.pub",
        # The managed block replaces itself and preserves everything else, so an
        # operator's own stanzas survive an upgrade.
        # The managed block replaces itself and everything else is preserved,
        # so an operator's own stanzas survive an upgrade. sed deletes the old
        # block inclusive of its markers -- and to end of file when a previous
        # run was interrupted between them -- and the new one is prepended,
        # because ssh takes the first value it obtains for a keyword.
        f"sudo -u {q_owner} sh -c "
        + provision.shell_quote(
            "set -e; "
            f'block={provision.shell_quote(block)}; '
            f'config={provision.shell_quote(config)}; '
            'tmp="$(mktemp)"; '
            'printf "%s" "$block" > "$tmp"; '
            'if [ -f "$config" ]; then '
            f"sed {provision.shell_quote(f'/^{provision.GITHUB_IDENTITY_BEGIN}$/,/^{provision.GITHUB_IDENTITY_END}$/d')} "
            '"$config" >> "$tmp"; fi; '
            'install -m 0600 "$tmp" "$config"; '
            'rm -f "$tmp"'
        ),
        f"sudo chown {q_owner}:{q_owner} {q_config}",
        f"sudo chmod 0600 {q_config}",
    ]


def owner_github_selection_commands(
    owner_user: str,
    owner_home: str,
    *,
    key_name: str,
    host: str = DEFAULT_GITHUB_HOST,
    host_alias: str = "",
) -> list[str]:
    """Select an existing key, and touch nothing else.

    Deliberately not `owner_github_identity_commands`. That one provisions: it
    creates the key when it is absent and then chowns and chmods both halves as
    root. Every one of those is wrong for a repair that only chooses between
    keys the owner already has, and each is a root write onto a path the tenant
    controls -- a role can point the named key at a root-owned file, or delete
    the key between the check and the command, and turn selection into creation
    or into root changing the owner of something it should not (SYRD-100 review).

    So this emits one thing: the managed block, rewritten by the owner as the
    owner. No key is created, no key is read, and no path under the owner's home
    is written by root. A symlink anywhere in it is the owner's own business,
    because the owner is who is writing.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    if not key_name.strip():
        raise provision.PathContainmentError("a key name is required to select an identity")
    # Raises when the name carries a path separator, which is the check that
    # keeps the selection inside the owner's own .ssh.
    provision.owner_github_key_path(owner_home, key_name=key_name)
    ssh_dir = f"{owner_home.rstrip('/')}/.ssh"
    config = f"{ssh_dir}/config"
    block = provision.github_identity_block(owner_home, key_name=key_name, host=host, host_alias=host_alias)
    q_owner = provision.shell_quote(owner_user)
    return [
        # As the owner, in the owner's own directory. The block replaces itself
        # between its markers and everything else is preserved, so an operator's
        # other stanzas survive.
        f"sudo -u {q_owner} sh -c "
        + provision.shell_quote(
            "set -e; "
            f'block={provision.shell_quote(block)}; '
            f'dir={provision.shell_quote(ssh_dir)}; '
            f'config={provision.shell_quote(config)}; '
            'mkdir -p "$dir"; chmod 0700 "$dir"; '
            'tmp="$(mktemp)"; '
            'printf "%s" "$block" > "$tmp"; '
            'if [ -f "$config" ]; then '
            f"sed {provision.shell_quote(f'/^{provision.GITHUB_IDENTITY_BEGIN}$/,/^{provision.GITHUB_IDENTITY_END}$/d')} "
            '"$config" >> "$tmp"; fi; '
            'install -m 0600 "$tmp" "$config"; '
            'rm -f "$tmp"'
        ),
    ]


def publication_remote_host(remote: str) -> str:
    """The host a git remote names, or "" for a local repository.

    `/data/git/fixpatch`, `file:///srv/x.git` and `../x.git` are local; so is a
    path that merely contains a colon after a slash. `git@host:path`,
    `ssh://git@host/path` and `https://host/path` name `host`.
    """
    value = remote.strip()
    if not value:
        return ""
    if "://" in value:
        scheme, _, rest = value.partition("://")
        if scheme.lower() == "file":
            return ""
        netloc = rest.split("/", 1)[0]
        return netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if value.startswith(("/", "./", "../", "~")):
        return ""
    head, colon, _tail = value.partition(":")
    if not colon or "/" in head:
        return ""
    return head.rsplit("@", 1)[-1].lower()


def publication_uses_github(remote: str, *, recorded_host_alias: str = "") -> bool | None:
    """Whether a tenant's publication remote is GitHub, so its owner identity applies.

    None when there is no remote to judge -- the caller keeps its GitHub
    behaviour then, because skipping a real GitHub tenant's identity is the worse
    mistake. A local repository is never GitHub. A host is GitHub when it is
    github.com, one of its subdomains, the host alias the tenant's identity
    records, or a `github-...` alias of the kind Switchyard's managed block
    answers to (SYRD-229).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not remote.strip():
        return None
    host = provision.publication_remote_host(remote)
    if not host:
        return False
    alias = recorded_host_alias.strip().lower()
    return (
        host == provision.DEFAULT_GITHUB_HOST
        or host.endswith("." + provision.DEFAULT_GITHUB_HOST)
        or (bool(alias) and host == alias)
        or host.startswith("github-")
    )


def owner_github_block_removal_commands(owner_user: str, owner_home: str) -> list[str]:
    """Remove Switchyard's managed GitHub block, and nothing else, as the owner.

    The inverse of `owner_github_selection_commands`, with the same care: run as
    the owner in the owner's own directory, so no root write lands on a path the
    tenant controls. Everything outside the markers is preserved byte for byte,
    and a config without the block is not rewritten at all (SYRD-229).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    config = f"{owner_home.rstrip('/')}/.ssh/config"
    q_owner = provision.shell_quote(owner_user)
    return [
        f"sudo -u {q_owner} sh -c "
        + provision.shell_quote(
            "set -e; "
            f"config={provision.shell_quote(config)}; "
            '[ -f "$config" ] || exit 0; '
            f"grep -qx {provision.shell_quote(provision.GITHUB_IDENTITY_BEGIN)} \"$config\" || exit 0; "
            'tmp="$(mktemp)"; '
            f"sed {provision.shell_quote(f'/^{provision.GITHUB_IDENTITY_BEGIN}$/,/^{provision.GITHUB_IDENTITY_END}$/d')} "
            '"$config" > "$tmp"; '
            'install -m 0600 "$tmp" "$config"; '
            'rm -f "$tmp"'
        ),
    ]
