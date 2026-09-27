"""GitHub identity: reading an owner's publishing key from the host, and the
operator repairs that record or clear which key a tenant publishes with.

- `github_identity_status` reads the owner's ssh directory, key pair and ssh
  configuration without following links, never opens the private half,
  fingerprints the public half from the bytes it validated, and asks the forge
  once, bounded and non-interactive, whether the key authenticates (SYRD-74,
  SYRD-100).
- `set_owner_github_identity_command` records an existing key in both plan
  authorities and has the owner select it; `clear_owner_github_identity_command`
  undoes a GitHub identity recorded for a tenant that does not publish to GitHub
  (SYRD-229). Both read and validate both authorities before writing either, and
  put the first back if the second write fails.
- `write_plan_no_follow`, `selected_key_problems` and `_plan_with_selection` are
  their helpers; `GITHUB_IDENTITY_TIMEOUT_SECONDS` bounds the forge probe.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-345). The launcher
imports this module at its top and re-exports every name, so `upgrade` and the
CLI call the same objects. Everything else these functions use from the
launcher -- the owner lookups, the no-follow walk and plan reader, root's plan
paths, the identity status type and remedy -- is read from `team_launcher` when
they run, and so are `github_identity_status` and `write_plan_no_follow` where
the repairs call them, because suites patch them there. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import GithubIdentityStatus, PlanDocument, ProjectConfig

GITHUB_IDENTITY_TIMEOUT_SECONDS = 15.0


def github_identity_status(
    owner_user: str,
    owner_home: Path,
    *,
    key_name: str = "",
    host: str = "github.com",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> GithubIdentityStatus:
    """Read the owner's GitHub identity from the host, and try it once.

    Ownership and modes are read rather than assumed, because the failure this
    exists for looked like a missing key and was a key nothing selected. The
    authentication probe is non-interactive and bounded: a check that can ask
    for a passphrase or sit on a socket is a check that hangs a launch instead
    of reporting one (SYRD-74).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        GITHUB_IDENTITY_BEGIN,
        owner_github_key_path,
    )

    key = Path(owner_github_key_path(str(owner_home), key_name=key_name))
    public = key.with_name(key.name + ".pub")
    config = key.parent / "config"
    expected_uid = launcher.uid_for_user(owner_user)
    problems: list[str] = []

    def _read_no_follow(path: Path, mode: int, what: str) -> str | None:
        """Validate and read one file through a single descriptor.

        `lstat` then `read_text` is two lookups of one name, and between them a
        same-UID tenant can replace the name with a symlink. Root then reads,
        and `github_identity_remedy` prints, whatever it points at -- which for
        a root-run repair is any file root can read. One open with O_NOFOLLOW,
        fstat on that descriptor, and the bytes read from it, so what is
        reported is what was checked (SYRD-100 review).

        Returns the text, or None when it could not be safely read.
        """
        relative = Path(str(path).lstrip("/"))
        dir_fd, problem = launcher._walk_no_follow(Path(path.anchor or "/"), relative)
        if dir_fd < 0:
            problems.append(f"{what} {path} cannot be reached safely: {problem}")
            return None
        try:
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    problems.append(f"{what} {path} is not a regular file")
                elif exc.errno == errno.ENOENT:
                    problems.append(f"{what} {path} does not exist")
                else:
                    problems.append(f"{what} {path} cannot be read ({exc.strerror})")
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    problems.append(f"{what} {path} is not a regular file")
                    return None
                if expected_uid is not None and info.st_uid != expected_uid:
                    problems.append(
                        f"{what} {path} is owned by uid {info.st_uid} rather than by {owner_user}"
                    )
                if stat.S_IMODE(info.st_mode) != mode:
                    problems.append(
                        f"{what} {path} is mode {stat.S_IMODE(info.st_mode):04o} rather than "
                        f"{mode:04o}"
                    )
                raw = b""
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    raw += chunk
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{what} {path} is not text")
            return None

    def _check(path: Path, mode: int, what: str) -> bool:
        try:
            info = path.lstat()
        except OSError:
            problems.append(f"{what} {path} does not exist")
            return False
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) if what == "the owner's ssh directory" else stat.S_ISREG(info.st_mode)
        ):
            problems.append(f"{what} {path} is not a regular {'directory' if what.endswith('directory') else 'file'}")
            return False
        if expected_uid is not None and info.st_uid != expected_uid:
            problems.append(f"{what} {path} is owned by uid {info.st_uid} rather than by {owner_user}")
        if stat.S_IMODE(info.st_mode) != mode:
            problems.append(
                f"{what} {path} is mode {stat.S_IMODE(info.st_mode):04o} rather than {mode:04o}"
            )
        return True

    _check(key.parent, 0o700, "the owner's ssh directory")
    # The private half is checked and never read. Nothing here opens it.
    _check(key, 0o600, "the owner's GitHub key")
    public_key = ""
    fingerprint = ""
    public_text = _read_no_follow(public, 0o644, "the owner's GitHub public key")
    if public_text is not None:
        public_key = public_text.strip()
        # Fingerprinted from the bytes that were validated, not by handing the
        # path back to ssh-keygen to open a second time.
        proc = runner(
            ["ssh-keygen", "-l", "-f", "-"],
            input=public_key + "\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if getattr(proc, "returncode", 1) == 0:
            fingerprint = str(getattr(proc, "stdout", "") or "").strip()
    body = _read_no_follow(config, 0o600, "the owner's ssh configuration")
    if body is not None:
        if GITHUB_IDENTITY_BEGIN not in body:
            problems.append(
                f"{config} selects no managed identity for {host}; git offers no key and the "
                "push is refused as if there were none"
            )
        elif str(key) not in body:
            problems.append(f"{config} selects an identity other than {key} for {host}")

    authenticated = False
    detail = ""
    try:
        proc = runner(
            launcher._owner_command_env_args(
                owner_user,
                owner_home,
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=5", "-T", f"git@{host}",
                ],
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=GITHUB_IDENTITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        detail = f"{host} did not answer within {GITHUB_IDENTITY_TIMEOUT_SECONDS:g}s"
    except OSError as exc:
        detail = str(exc)
    else:
        # GitHub's shell server always exits non-zero; the greeting is the
        # answer, so the text decides rather than the status.
        detail = (str(getattr(proc, "stdout", "") or "").strip() or f"exit {proc.returncode}")[:300]
        authenticated = "successfully authenticated" in detail.casefold()
    return launcher.GithubIdentityStatus(
        owner_user=owner_user,
        key_path=key,
        problems=tuple(problems),
        authenticated=authenticated,
        detail=detail,
        public_key=public_key,
        fingerprint=fingerprint,
        checked=True,
    )


def write_plan_no_follow(document: PlanDocument, body: bytes) -> str:
    """Replace one plan authority atomically, inside its own directory.

    Staged beside it and renamed over, so a symlink at the destination is
    replaced rather than followed, and a reader never sees a half-written plan.
    Owner and mode are carried from the file that was there, so the tenant's copy
    stays the tenant's and root's stays root's.
    """
    from scripts import team_launcher as launcher

    relative = Path(str(document.path).lstrip("/"))
    dir_fd, problem = launcher._walk_no_follow(Path(document.path.anchor or "/"), relative)
    if dir_fd < 0:
        return f"{document.path}: {problem}"
    staged = f".{document.path.name}.switchyard-new"
    try:
        try:
            fd = os.open(
                staged,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                document.mode,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            os.unlink(staged, dir_fd=dir_fd)
            fd = os.open(
                staged,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                document.mode,
                dir_fd=dir_fd,
            )
        try:
            os.write(fd, body)
            os.fchmod(fd, document.mode)
            if os.geteuid() == 0:
                os.fchown(fd, document.uid, document.gid)
        finally:
            os.close(fd)
        os.rename(staged, document.path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        try:
            os.unlink(staged, dir_fd=dir_fd)
        except OSError:
            pass
        return f"{document.path} could not be replaced ({exc.strerror})"
    finally:
        os.close(dir_fd)
    return ""


def selected_key_problems(owner_home: Path, key_name: str, owner_uid: int) -> list[str]:
    """Whether both halves of the named key are the owner's own regular files.

    lstat rather than stat, on every component of the pair: a role can point the
    name at a root-owned file, and a check that follows it would report a
    perfectly good key that belongs to somebody else entirely. Nothing here
    opens, reads or changes either half -- selection is not provisioning
    (SYRD-100 review).
    """
    problems: list[str] = []
    for label, path in (
        ("private half", owner_home / ".ssh" / key_name),
        ("public half", owner_home / ".ssh" / f"{key_name}.pub"),
    ):
        try:
            info = os.lstat(path)
        except OSError:
            problems.append(f"the {label} {path} does not exist")
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"the {label} {path} is a symlink, so what it names is not this key")
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"the {label} {path} is not a regular file")
            continue
        if info.st_uid != owner_uid:
            problems.append(
                f"the {label} {path} is owned by uid {info.st_uid} rather than by the tenant owner"
            )
    return problems


def _plan_with_selection(document: PlanDocument, key_name: str, host_alias: str) -> bytes | None:
    """The plan's own bytes with the selection set, or None when already right.

    Rewritten field for field rather than regenerated: a plan carries decisions
    this command has no opinion about.
    """
    if (
        str(document.data.get("owner_github_key_name") or "") == key_name
        and str(document.data.get("owner_github_host_alias") or "") == host_alias
    ):
        return None
    data = dict(document.data)
    data["owner_github_key_name"] = key_name
    data["owner_github_host_alias"] = host_alias
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def clear_owner_github_identity_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    registration_root: Path | None = None,
) -> int:
    """Undo a GitHub identity recorded for a tenant that does not publish to GitHub.

    The repair for what SYRD-229 found on mefp: an upgrade told the operator to
    run set-owner-identity on a local-only tenant, and following it recorded a
    key in both plan authorities and wrote a github.com block into the owner's
    ssh config. This clears exactly those two plan fields, in both authorities
    or in neither, and removes only Switchyard's managed block -- as the owner,
    preserving every other stanza. The publication remote and commit_git_dir
    are not touched. Refused for a tenant that does publish to GitHub, whose
    identity is in use.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        GITHUB_IDENTITY_BEGIN,
        owner_github_block_removal_commands,
        publication_remote_host,
        publication_uses_github,
    )
    from scripts.ticket_board.publication_boundary import resolve_pinned_remote

    project = config.project
    remote, remote_problem = resolve_pinned_remote(
        project,
        registration_root=registration_root or launcher.switchyard_privileged_provision_root(),
        declared_remote="",
    )
    # The remote first: it is what decides whether there is anything to clear,
    # and it is root's alone, so it is judged before the tenant's documents.
    tenant_plan = config_path.parent / "plan.json"
    root_plan = launcher.privileged_baseline_plan_path(project)
    # Decided without reading anything the tenant controls. A local remote
    # needs no alias at all. For a hosted one, the only alias consulted is
    # root's own, opened the careful way -- never the tenant's plan, which this
    # privileged command must not follow a link into (SYRD-229 review).
    applies = publication_uses_github(remote)
    if applies is False and publication_remote_host(remote):
        root_document, _problem = launcher.read_plan_no_follow(root_plan, require_root_owned=True)
        if root_document is not None:
            applies = publication_uses_github(
                remote,
                recorded_host_alias=str(root_document.data.get("owner_github_host_alias") or ""),
            )
    if applies is not False:
        print_func(
            f"switchyard: refusing to clear {project}'s GitHub identity: "
            + (f"it publishes to {remote}, which is GitHub, so the identity is in use."
               if applies else f"its publication remote is not established ({remote_problem}).")
            + " Nothing was changed."
        )
        return 1
    print_func(f"switchyard: {project} publishes to {remote}, not GitHub")
    documents: list[PlanDocument] = []
    for path, require_root in ((root_plan, True), (tenant_plan, False)):
        document, problem = launcher.read_plan_no_follow(path, require_root_owned=require_root)
        if document is None:
            print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: {project}'s publication identity is recorded in two places and both "
                "have to be readable to clear it. Nothing was changed."
            )
            return 1
        documents.append(document)

    identity = launcher.trusted_owner_identity(project)
    if not identity.trusted:
        for problem in identity.problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: refusing to clear {project}'s GitHub identity: root cannot establish "
            "whose it is. Nothing was changed."
        )
        return 1
    owner, owner_home = identity.owner_user, identity.owner_home
    ssh_config = Path(owner_home) / ".ssh" / "config"
    # Asked as the owner: root does not read into the owner's home.
    probe = runner(
        ["sudo", "-u", owner, "sh", "-c",
         f"[ -f {shlex.quote(str(ssh_config))} ] && grep -qx {shlex.quote(GITHUB_IDENTITY_BEGIN)} "
         f"{shlex.quote(str(ssh_config))}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    has_block = getattr(probe, "returncode", 1) == 0
    pending = [(document, _plan_with_selection(document, "", "")) for document in documents]

    if dry_run:
        for document, body in pending:
            print_func(
                f"switchyard:   {document.path} "
                + ("would have owner_github_key_name and owner_github_host_alias cleared"
                   if body is not None else "records no GitHub identity")
            )
        print_func(
            f"switchyard:   {ssh_config}: "
            + ("would have Switchyard's managed GitHub block removed, as "
               f"{owner}, every other stanza kept" if has_block else "has no Switchyard GitHub block")
        )
        print_func("switchyard: nothing was written")
        return 0
    if os.geteuid() != 0:
        print_func(
            f"switchyard: clearing {project}'s GitHub identity writes root's own plan. Run: "
            f"sudo switchyard set-owner-identity {project} --clear"
        )
        return 1

    written: list[PlanDocument] = []
    for document, body in pending:
        if body is None:
            continue
        problem = launcher.write_plan_no_follow(document, body)
        if problem:
            print_func(f"switchyard: {problem}")
            for done in reversed(written):
                restored = launcher.write_plan_no_follow(done, done.raw)
                print_func(
                    f"switchyard: {done.path} "
                    + (f"could not be put back: {restored}" if restored else "was put back as it was")
                )
            print_func(
                f"switchyard: {project}'s GitHub identity was not cleared, and neither plan was "
                "left disagreeing with the other."
            )
            return 1
        written.append(document)
        print_func(f"switchyard: cleared the GitHub identity recorded in {document.path}")
    if has_block:
        script = "set -eu\n" + "\n".join(owner_github_block_removal_commands(owner, str(owner_home)))
        removed = runner(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if getattr(removed, "returncode", 1) != 0:
            print_func(
                f"switchyard: both plans are cleared, but Switchyard's GitHub block could not be "
                f"removed from {ssh_config} (exit {removed.returncode}): "
                f"{(str(getattr(removed, 'stderr', '') or '').strip() or 'no output')[:300]}. "
                "It is inert for a local remote; run this again to remove it."
            )
            return 1
        print_func(f"switchyard: removed Switchyard's GitHub block from {ssh_config}; nothing else in it changed")
    if not written and not has_block:
        print_func(f"switchyard: {project} records no GitHub identity; nothing to clear")
    return 0


def set_owner_github_identity_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    key_name: str,
    host_alias: str = "",
    host: str = "github.com",
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Record which existing key this tenant publishes with, and select it.

    The supported repair for a tenant whose managed block was pointed at the
    wrong key. An upgrade will not choose between an owner's keys and will not
    generate one beside them, so somebody has to say which key is the one, once,
    and have it recorded where both the tenant and root will read it.

    Everything about this runs as root on paths a tenant controls, so nothing
    the tenant writes is treated as authority and nothing under the owner's home
    is written by root. Who the owner is comes from root's own baseline plan and
    from passwd; the key is validated without following symlinks and is never
    opened, created or modified; the ssh_config rewrite is performed by the owner
    as the owner; and both plan authorities are read and validated before either
    is written (SYRD-100 review).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        owner_github_key_path,
        owner_github_selection_commands,
    )

    project = config.project
    selected = key_name.strip()
    alias = host_alias.strip()
    if not selected or "/" in selected:
        print_func(
            f"switchyard: {key_name!r} is not a key file name. Name one of the owner's keys, "
            "without a path."
        )
        return 1

    identity = launcher.trusted_owner_identity(project)
    if not identity.trusted:
        for problem in identity.problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: refusing to change {project}'s publication identity: root cannot "
            "establish whose it is. Nothing was changed."
        )
        return 1
    owner, owner_home = identity.owner_user, identity.owner_home

    key_problems = selected_key_problems(owner_home, selected, identity.owner_uid)
    if key_problems:
        for problem in key_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: {selected} is not a key pair {owner} owns, so there is nothing to "
            "select. Nothing was changed, and no key was created."
        )
        return 1

    # Both authorities, read and validated before either is written. Root's own
    # copy must be root's; the tenant's is the tenant's and is replaced rather
    # than followed.
    tenant_plan = config_path.parent / "plan.json"
    root_plan = launcher.privileged_baseline_plan_path(project)
    documents: list[PlanDocument] = []
    for path, require_root in ((root_plan, True), (tenant_plan, False)):
        document, problem = launcher.read_plan_no_follow(path, require_root_owned=require_root)
        if document is None:
            print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: {project}'s publication identity is recorded in two places and both "
                "have to be writable, or the two disagree afterwards. Nothing was changed."
            )
            return 1
        documents.append(document)

    key = Path(owner_github_key_path(str(owner_home), key_name=selected))
    if dry_run:
        print_func(
            f"switchyard: would record {project}'s publication identity as {selected}"
            + (f" with host alias {alias}" if alias else "")
        )
        for document in documents:
            pending = _plan_with_selection(document, selected, alias)
            print_func(
                f"switchyard:   {document.path} "
                + ("would be updated" if pending is not None else "already records it")
            )
        print_func(
            f"switchyard: would have {owner} select {key} in {owner_home}/.ssh/config, "
            "managed block only"
        )
        return 0

    if os.geteuid() != 0:
        print_func(
            f"switchyard: recording {project}'s publication identity writes root's own plan. Run: "
            f"sudo switchyard set-owner-identity {project} --key-name {selected}"
            + (f" --host-alias {alias}" if alias else "")
        )
        return 1

    # Written one after the other, with what was there kept so the first can be
    # put back if the second fails. Two authorities that disagree are worse than
    # two that are both stale: the next upgrade would read one of them.
    written: list[PlanDocument] = []
    for document in documents:
        body = _plan_with_selection(document, selected, alias)
        if body is None:
            print_func(f"switchyard: {document.path} already records {selected}")
            continue
        problem = launcher.write_plan_no_follow(document, body)
        if problem:
            print_func(f"switchyard: {problem}")
            for done in reversed(written):
                restored = launcher.write_plan_no_follow(done, done.raw)
                print_func(
                    f"switchyard: {done.path} "
                    + (f"could not be put back: {restored}" if restored else "was put back as it was")
                )
            print_func(
                f"switchyard: {project}'s publication identity was not changed, and neither plan "
                "was left disagreeing with the other."
            )
            return 1
        written.append(document)
        print_func(f"switchyard: recorded {selected} in {document.path}")

    script = "set -eu\n" + "\n".join(
        owner_github_selection_commands(
            owner, str(owner_home), key_name=selected, host=host, host_alias=alias
        )
    )
    applied = runner(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(applied, "returncode", 1) != 0:
        print_func(
            f"switchyard: could not select {selected} for {owner} (exit {applied.returncode}): "
            f"{(str(getattr(applied, 'stderr', '') or '').strip() or 'no output')[:300]}"
        )
        print_func(
            f"switchyard: both plans record {selected}; {owner_home}/.ssh/config does not yet. "
            f"Rerun this command, which is safe to repeat."
        )
        return 1
    print_func(f"switchyard: {owner_home}/.ssh/config now selects {key} for {host}")

    status = launcher.github_identity_status(owner, owner_home, key_name=selected, host=host, runner=runner)
    if status.fingerprint:
        print_func(f"switchyard: fingerprint: {status.fingerprint}")
    remedy = launcher.github_identity_remedy(status, project=project)
    if remedy:
        print_func(remedy)
        print_func(
            f"switchyard: {selected} is selected and recorded, but it did not authenticate. "
            "Register its public half with the forge, or select a different key."
        )
        return 1
    print_func(f"switchyard: {owner} can publish to GitHub as {selected}")
    return 0
