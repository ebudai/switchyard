"""Install the publication boundary on a tenant that already exists.

SYRD-93 put the push credential where no role can reach it, and SYRD-96 then
found that only fresh provisioning ever installs it: `publish_grant_commands()`
is reachable from the operator command packet and from nowhere else, and the
shared-project-account compatibility path skips the old role-account migration
that used to carry it. An existing tenant therefore upgrades its board, its
staged role tooling and its schema, and still has no publisher rule, no
`/etc/switchyard/publish`, and no key. There was no supported way to get one.

Two things make this different from rendering more shell for an operator to run.

Root does the work itself, here, in this process. The fresh-provisioning packet
is a file the operator saves and runs; under one shared account (SYRD-69) that
file is writable by every role, and asking an operator to run it as root hands
those roles a root shell. Nothing here is written to disk for root to execute.

What root installs comes out of the object store, not out of a working tree.
`git archive <commit>` reads the commit, so a role that can write files in the
checkout cannot change what is materialized -- only the commit selects that, and
the commit comes from the pinned upgrade source root recorded. The materialized
release is then verified as root-owned along its whole path before a single
privileged artifact is installed, because a path is only as pinned as the
directories leading to it (SYRD-50).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .project_provision import (
    PUBLISH_GRANT_SCHEMA,
    publish_grant_path,
    publish_grant_root,
    publish_identity_path,
    publish_staging_root,
)

SWITCHYARD_RELEASE_MARKER_NAME = ".switchyard-release.json"
MAX_LINK_DEPTH = 8
Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def _run(args: Sequence[str], *, runner: Runner, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return runner(list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)


def _output(proc: subprocess.CompletedProcess[Any]) -> str:
    return str(getattr(proc, "stdout", "") or "").strip()


def _failure(proc: subprocess.CompletedProcess[Any]) -> str:
    detail = str(getattr(proc, "stderr", "") or "").strip() or _output(proc) or "no output"
    return detail[:400]


def root_controlled_problems(
    declared: str, *, expect_uid: int | None = None, base: str = "/"
) -> list[str]:
    """Every component of one absolute path, proven to belong to root.

    Checking the final file is not enough: if any directory or symlink on the
    way to it can be replaced by somebody else, then somebody else chooses what
    root ends up reading. The string is validated before pathlib touches it,
    because constructing a Path silently drops `.` components and what is walked
    must be what was written rather than a tidied version of it (SYRD-50).
    """
    owner_uid = os.getuid() if expect_uid is None else expect_uid
    if not declared.startswith("/"):
        return [f"{declared} is not an absolute path"]
    if not base.startswith("/"):
        return [f"{base} is not an absolute path"]
    if not declared.startswith(base.rstrip("/") + "/") and declared != base.rstrip("/"):
        return [f"{declared} is not under {base}"]
    segments = declared.split("/")[1:]
    if any(segment in {"", ".", ".."} for segment in segments):
        return [f"{declared} contains empty, . or .. components"]
    # Where the walk starts. On a host that is "/", and every directory from
    # there down has to belong to root. It moves only for the shared install
    # root's documented test override, because a fixture cannot own "/" and
    # a fixture path under a world-writable temp directory is one this would
    # be right to refuse.
    base_segments = base.rstrip("/").split("/")[1:]
    segments = segments[len(base_segments):]

    problems: list[str] = []
    seen_links = 0

    def check(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError as exc:
            problems.append(f"cannot inspect {path}: {exc}")
            return False
        if info.st_uid != owner_uid:
            problems.append(f"{path} is owned by uid {info.st_uid} rather than by uid {owner_uid}")
            return False
        # Mode bits on a symlink mean nothing on Linux -- they are always
        # lrwxrwxrwx -- so the check that matters for one is where it points.
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
            problems.append(f"{path} is group- or world-writable")
            return False
        return True

    current = Path(base.rstrip("/") or "/")
    if not check(current):
        return problems
    for segment in segments:
        current = current / segment
        if not check(current):
            return problems
        while current.is_symlink():
            seen_links += 1
            if seen_links > MAX_LINK_DEPTH:
                problems.append(f"{declared} resolves through too many symlinks")
                return problems
            target = Path(os.readlink(current))
            if not target.is_absolute():
                target = current.parent / target
            nested = root_controlled_problems(str(target), expect_uid=owner_uid)
            if nested:
                return nested
            current = target
    return problems


@dataclass(frozen=True)
class TrustedRelease:
    """An immutable, root-owned tree holding one exact commit."""

    root: Path
    commit: str
    materialized: bool = False


def materialize_trusted_release(
    commit: str,
    *,
    source_repo: Path,
    install_root: Path,
    trust_base: str = "/",
    runner: Runner = subprocess.run,
    dry_run: bool = False,
) -> tuple[TrustedRelease | None, list[str]]:
    """Consume the root-owned release for `commit`, or create it from the object store.

    The globally installed release is whatever was last installed, which on the
    tenant that found this was several weeks behind the board it was serving.
    Waiting for an operator to re-run the host installer first would make this
    upgrade depend on a step nobody is prompted to take, so the exact release is
    materialized here when it is missing.
    """
    commit = commit.strip()
    if not commit:
        return None, ["no commit was selected, so no release can be verified"]
    releases = install_root / "releases"
    target = releases / commit

    # Anything already on this path is refused rather than repaired or replaced.
    # A releases directory somebody else can write is one they can race a tree
    # into between the check and the rename, and a release tree they own is one
    # whose contents root would then install as its own. Neither is something to
    # fix quietly under an operator who asked for an upgrade.
    for existing_path in (install_root, releases, target):
        if not (existing_path.exists() or existing_path.is_symlink()):
            continue
        problems = root_controlled_problems(str(existing_path), base=trust_base)
        if problems:
            return None, [
                f"{existing_path} already exists and is not root-controlled, so it "
                "will not be used and will not be replaced"
            ] + problems

    if target.exists():
        marker = read_release_marker(target)
        if marker.get("commit") == commit:
            return TrustedRelease(root=target, commit=commit), []
        return None, [
            f"{target} exists but its release marker names "
            f"{marker.get('commit') or 'nothing'} rather than {commit}"
        ]

    if dry_run:
        return TrustedRelease(root=target, commit=commit, materialized=True), []

    probe = _run(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{commit}^{{commit}}"],
        runner=runner,
    )
    if probe.returncode != 0 or _output(probe) != commit:
        return None, [
            f"{source_repo} does not contain commit {commit}, so no release can be "
            f"materialized from it: {_failure(probe)}"
        ]

    # Done here rather than shelled out. Root already is this process, so
    # `install`, `chown` and `mv` are system calls it can make directly -- and
    # routing them through an injected runner made materialization depend on
    # what that runner chose to model, which is not something a privileged step
    # should rest on.
    try:
        for directory in (install_root, releases):
            # No chown: everything here is created by this process, so it is
            # already owned by whoever is entitled to own it -- root on a host,
            # and the reader's own uid where a suite exercises this branch
            # without one. `root_controlled_problems` checks it against that
            # same identity rather than against a literal 0.
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o755)
    except OSError as exc:
        return None, [f"could not create {releases}: {exc}"]

    staging = Path(f"{target}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        return None, [f"could not create {staging}: {exc}"]
    # From the object store, so nothing a role wrote in the working tree can
    # reach the release. The commit is what selects the content.
    extract = subprocess.run(
        ["sh", "-c", f"git -C {_quote(str(source_repo))} archive {_quote(commit)} "
                     f"| tar -C {_quote(str(staging))} -x"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if extract.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        return None, [f"could not extract {commit} into {staging}: {_failure(extract)}"]
    try:
        (staging / SWITCHYARD_RELEASE_MARKER_NAME).write_text(
            json.dumps(
                {"commit": commit, "source_repo": str(source_repo), "source_ref": commit},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        for path in (staging, *staging.rglob("*")):
            if path.is_symlink():
                continue
            os.chmod(path, 0o755 if path.is_dir() else (path.stat().st_mode | 0o444) & ~0o022)
        staging.rename(target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return None, [f"could not seal {target}: {exc}"]

    # Verified after it is in place, not before: what was checked and what is
    # installed have to be the same tree.
    problems = root_controlled_problems(str(target), base=trust_base)
    if problems:
        return None, [f"the materialized release at {target} is not root-controlled"] + problems
    return TrustedRelease(root=target, commit=commit, materialized=True), []


def read_release_marker(root: Path) -> dict[str, str]:
    try:
        payload = json.loads((root / SWITCHYARD_RELEASE_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(value) for key, value in payload.items() if isinstance(value, str)}


def _quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


PUBLISH_REMOTE_REGISTRATION = "publish-remote"


def publish_remote_registration_path(project: str, registration_root: Path) -> Path:
    """Where root records the destination it is willing to push to.

    Under the tenant's own directories this would be pointless: the shared
    project account owns them. It lives beside the other privileged artifacts
    root installs from, which no tenant can reach.
    """
    return Path(registration_root) / project / PUBLISH_REMOTE_REGISTRATION


def resolve_pinned_remote(
    project: str,
    *,
    registration_root: Path,
    declared_remote: str = "",
) -> tuple[str, str]:
    """The destination, from somewhere the project account cannot rewrite.

    Three sources, all root's, in the order that keeps an already-correct tenant
    working without an operator having to re-supply anything:

    * the grant root already installed, so a re-run pins what root pinned;
    * a remote root recorded on a previous run, so an operator states it once;
    * one an operator states now, which is then recorded.

    The shared account's git config is not among them, and neither is the
    tenant's provisioning plan; on the tenant this was found on both are
    writable by the account every role runs as.
    """
    declared = (declared_remote or "").strip()
    registration = publish_remote_registration_path(project, registration_root)
    if declared:
        return declared, ""
    installed = _read_marker_like(Path(publish_grant_path(project)))
    pinned = str(installed.get("remote") or "").strip()
    if pinned:
        return pinned, ""
    try:
        recorded = registration.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    if recorded:
        return recorded, ""
    return "", (
        f"no remote is pinned for {project} by root. The project account's git config is "
        "not trusted for this, because every role runs as that account and could aim the "
        "push at a server of its own. Supply it once with "
        f"`switchyard upgrade {project} --publish-remote <url>`; it is recorded at "
        f"{registration} and reused from then on."
    )


def _read_marker_like(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(value) for key, value in payload.items() if isinstance(value, str)}


@dataclass(frozen=True)
class PublicationArtifact:
    """One privileged thing this installs, named so a dry run can report it."""

    path: str
    mode: str
    description: str


@dataclass
class PublicationOutcome:
    artifacts: list[PublicationArtifact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    key_created: bool = False
    public_key_restored: bool = False
    #: The public half that was there did not belong to the private key.
    public_key_mismatched: bool = False
    public_key: str = ""
    fingerprint: str = ""
    registration_required: bool = False
    #: Named artifacts this run could not install. The boundary is then partial,
    #: and must be reported as partial rather than as done: an operator who is
    #: told known_hosts is installed when it is absent has been told the push
    #: will be verified when it will be refused (SYRD-97 review).
    pending: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems and not self.pending


def publication_artifacts(project: str, sudoers_path: str) -> list[PublicationArtifact]:
    """Everything privileged this installs, in the order it installs it."""
    key = publish_identity_path(project)
    return [
        PublicationArtifact(publish_grant_root(), "0755 root:root", "publication grant directory"),
        PublicationArtifact(publish_staging_root(), "0755 root:root", "publication staging directory"),
        PublicationArtifact(key, "0600 root:root", "publication private key (never regenerated)"),
        PublicationArtifact(f"{key}.pub", "0644 root:root", "publication public key"),
        PublicationArtifact(f"{publish_grant_root()}/known_hosts", "0644 root:root", "pinned forge host keys"),
        PublicationArtifact(publish_grant_path(project), "0640 root:root", "publication grant document"),
        PublicationArtifact(sudoers_path, "0440 root:root", "narrow NOPASSWD publisher rule"),
    ]


def _current_user_name() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


def _installed_public_key(project: str, *, runner: Runner) -> tuple[str, str]:
    """The public key and its fingerprint, read back from what is installed."""
    key = publish_identity_path(project)
    shown = _run(["cat", f"{key}.pub"], runner=runner)
    if shown.returncode != 0:
        return "", ""
    public = _output(shown)
    printed = _run(["ssh-keygen", "-l", "-f", f"{key}.pub"], runner=runner)
    return public, (_output(printed) if printed.returncode == 0 else "")


def install_publication_boundary(
    *,
    project: str,
    release: TrustedRelease,
    registration_root: Path,
    sudoers_path: str,
    sudoers_document: str,
    declared_remote: str = "",
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> PublicationOutcome:
    """Install the publication boundary, idempotently, as root and in-process.

    Ordered so nothing is ever live without what constrains it: the directories
    first, then the key, then the pinned host keys and the grant that names them,
    and only then the sudoers rule that lets the project account reach the
    publisher at all. A retry after an interruption re-enters at whichever of
    those is missing and rewrites only what is derived.
    """
    outcome = PublicationOutcome(artifacts=publication_artifacts(project, sudoers_path))
    key = publish_identity_path(project)
    grant = publish_grant_path(project)
    known_hosts = f"{publish_grant_root()}/known_hosts"

    if dry_run:
        print_func(
            f"switchyard: would install {project}'s publication boundary from "
            f"{release.root} ({release.commit})"
        )
        # Resolved during the preview as well, because "the real run would refuse
        # for want of a pinned remote" is exactly what an operator asks a dry run
        # in order to find out (SYRD-97 review).
        previewed_remote, previewed_problem = resolve_pinned_remote(
            project, registration_root=registration_root, declared_remote=declared_remote
        )
        if previewed_remote:
            print_func(f"switchyard: would pin {project} publication at {previewed_remote}")
        else:
            outcome.problems.append(previewed_problem)
        if release.materialized:
            print_func(
                f"switchyard: would materialize that release first; {release.root} is not present"
            )
        for artifact in outcome.artifacts:
            existing = "present" if Path(artifact.path).exists() else "absent"
            note = ", left as it is" if artifact.path.startswith(key) and existing == "present" else ""
            print_func(f"switchyard:   {artifact.path} ({artifact.mode}) -- {artifact.description} [{existing}{note}]")
        return outcome

    for directory in (publish_grant_root(), publish_staging_root()):
        made = _run(["install", "-d", "-m", "0755", "-o", "root", "-g", "root", directory], runner=runner)
        if made.returncode != 0:
            outcome.problems.append(f"could not create {directory}: {_failure(made)}")
            return outcome

    present = _run(["test", "-f", key], runner=runner)
    if present.returncode == 0:
        # The private key is kept whatever state the rest of it is in. What can
        # be repaired without replacing it is repaired, and the modes go first:
        # `ssh-keygen -y` refuses to read a key others could read, so deriving a
        # lost public half from a key an interrupted run left at 0644 fails
        # until the key is secured again. An interrupted upgrade otherwise
        # leaves a key with no public half, and every later run reports success
        # while the operator has nothing to register (SYRD-97 review).
        for args in (
            ["chown", "root:root", key],
            ["chmod", "0600", key],
        ):
            step = _run(args, runner=runner)
            if step.returncode != 0:
                outcome.problems.append(f"could not re-secure {key}: {_failure(step)}")
                return outcome
        # Derived every time, and compared. A public half that is merely present
        # is not necessarily this key's: an interrupted run can leave one from a
        # previous key, and an operator would then register something that
        # cannot sign. The private key is never replaced either way (SYRD-97
        # review).
        derived = _run(["ssh-keygen", "-y", "-f", key], runner=runner)
        if derived.returncode != 0 or not _output(derived):
            outcome.problems.append(
                f"{key} exists but no public half can be derived from it: "
                f"{_failure(derived)}. It is not replaced; publication stays refused."
            )
            return outcome
        expected = _output(derived).split()
        installed = _run(["cat", f"{key}.pub"], runner=runner)
        present = _output(installed).split() if installed.returncode == 0 else []
        if present[:2] != expected[:2]:
            # Rewritten from the private key, with its comment preserved so the
            # fingerprint an operator registered still reads the same.
            comment = " ".join(present[2:]) if len(present) > 2 else f"switchyard {project} publication"
            restored = " ".join([*expected[:2], comment]).strip()
            written_pub = runner(
                ["sh", "-c", f"printf '%s\\n' {_quote(restored)} | "
                             f"install -m 0644 -o root -g root /dev/stdin {_quote(f'{key}.pub')}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if getattr(written_pub, "returncode", 1) != 0:
                outcome.problems.append(f"could not restore {key}.pub: {_failure(written_pub)}")
                return outcome
            outcome.public_key_restored = True
            outcome.public_key_mismatched = bool(present)
        for args in (["chown", "root:root", f"{key}.pub"], ["chmod", "0644", f"{key}.pub"]):
            step = _run(args, runner=runner)
            if step.returncode != 0:
                outcome.problems.append(f"could not re-secure {key}.pub: {_failure(step)}")
                return outcome
    else:
        # Never regenerated. A new key silently breaks publication until its
        # public half is registered with the forge, and the failure appears as a
        # rejected push long after the upgrade that caused it.
        generated = _run(
            [
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                "-C", f"switchyard {project} publication", "-f", key,
            ],
            runner=runner,
        )
        if generated.returncode != 0:
            outcome.problems.append(f"could not create the publication key: {_failure(generated)}")
            return outcome
        for args in (["chmod", "0600", key], ["chmod", "0644", f"{key}.pub"]):
            step = _run(args, runner=runner)
            if step.returncode != 0:
                outcome.problems.append(f"could not secure {key}: {_failure(step)}")
                return outcome
        outcome.key_created = True

    outcome.problems.extend(enforce_artifact_permissions(outcome.artifacts, runner=runner))
    if outcome.problems:
        return outcome

    outcome.public_key, outcome.fingerprint = _installed_public_key(project, runner=runner)

    remote, remote_problem = resolve_pinned_remote(
        project, registration_root=registration_root, declared_remote=declared_remote
    )
    if remote and declared_remote.strip():
        registration = publish_remote_registration_path(project, registration_root)
        made = _run(
            ["install", "-d", "-m", "0755", "-o", "root", "-g", "root", str(registration.parent)],
            runner=runner,
        )
        if made.returncode != 0:
            outcome.problems.append(f"could not create {registration.parent}: {_failure(made)}")
            return outcome
        recorded = runner(
            ["sh", "-c", f"printf '%s\n' {_quote(remote)} | install -m 0644 -o root -g root "
                         f"/dev/stdin {_quote(str(registration))}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if getattr(recorded, "returncode", 1) != 0:
            outcome.problems.append(f"could not record the pinned remote at {registration}: {_failure(recorded)}")
            return outcome
    if not remote:
        # Never from the shared account's git config. On the tenant this was
        # found on, both the trusted cache's config and the provisioning plan are
        # owned and writable by the shared project UID, so a role could set the
        # remote root was about to pin and aim every future push at a server of
        # its own. Root pins this or nobody does (SYRD-97 review).
        outcome.problems.append(remote_problem)
        return outcome

    host = remote.split("@", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    if host:
        known = _run(["grep", "-qs", host, known_hosts], runner=runner)
        if known.returncode != 0:
            scanned = _run(["ssh-keyscan", "-H", host], runner=runner)
            if scanned.returncode == 0 and _output(scanned):
                appended = runner(
                    ["sh", "-c", f"printf '%s\\n' {_quote(_output(scanned))} >> {_quote(known_hosts)}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if getattr(appended, "returncode", 1) == 0:
                    _run(["chmod", "0644", known_hosts], runner=runner)
                else:
                    outcome.problems.append(f"could not pin {host} in {known_hosts}: {_failure(appended)}")
            else:
                # Not fatal, and not done either. Publication fails closed without
                # a pinned host, so the boundary is still safe -- but it is
                # incomplete, and saying otherwise tells an operator the push will
                # be verified when it will be refused. The next run pins it.
                outcome.pending.append(known_hosts)
                print_func(
                    f"warning: switchyard: could not read host keys for {host}; {known_hosts} is "
                    "unchanged and publication stays refused until it can be pinned"
                )

    document = json.dumps(
        {
            "schema": PUBLISH_GRANT_SCHEMA,
            "project": project,
            "identity_file": key,
            "known_hosts": known_hosts,
            "remote": remote,
        },
        indent=2,
        sort_keys=True,
    )
    written = runner(
        ["sh", "-c", f"printf '%s\\n' {_quote(document)} | install -m 0640 -o root -g root /dev/stdin {_quote(grant)}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if getattr(written, "returncode", 1) != 0:
        outcome.problems.append(f"could not write {grant}: {_failure(written)}")
        return outcome

    # Last, and validated before it is live: a malformed sudoers file can lock
    # the host out of sudo entirely.
    staged = f"{sudoers_path}.staged"
    installed = runner(
        ["sh", "-c", f"printf '%s' {_quote(sudoers_document)} | install -m 0440 -o root -g root /dev/stdin {_quote(staged)}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if getattr(installed, "returncode", 1) != 0:
        outcome.problems.append(f"could not stage {staged}: {_failure(installed)}")
        return outcome
    checked = _run(["visudo", "-c", "-f", staged], runner=runner)
    if checked.returncode != 0:
        _run(["rm", "-f", staged], runner=runner)
        outcome.problems.append(f"the publisher rule is not valid sudoers, so it was not installed: {_failure(checked)}")
        return outcome
    moved = _run(["mv", staged, sudoers_path], runner=runner)
    if moved.returncode != 0:
        outcome.problems.append(f"could not install {sudoers_path}: {_failure(moved)}")
        return outcome

    outcome.registration_required = outcome.key_created
    return outcome


def _actual_state(path: str) -> tuple[str, bool]:
    """The mode and ownership a path really has, for reporting and repair."""
    try:
        info = os.stat(path)
    except OSError:
        return "", False
    return f"{oct(info.st_mode & 0o777)[2:].zfill(4)} uid {info.st_uid}", info.st_uid == os.getuid()


def enforce_artifact_permissions(
    artifacts: Sequence[PublicationArtifact], *, runner: Runner
) -> list[str]:
    """Re-assert ownership and modes on everything already installed.

    A retry used to skip an artifact that merely existed, so a known_hosts left
    world-writable by an interrupted run stayed that way while the report called
    it 0644 root:root. Existence is not correctness, and for a file the publisher
    reads it is the difference between a pinned host and one another account can
    rewrite (SYRD-97 review).
    """
    problems: list[str] = []
    for artifact in artifacts:
        if not Path(artifact.path).exists():
            continue
        mode = artifact.mode.split()[0]
        for args in (["chown", "root:root", artifact.path], ["chmod", mode, artifact.path]):
            step = _run(args, runner=runner)
            if step.returncode != 0:
                problems.append(f"could not re-secure {artifact.path}: {_failure(step)}")
                break
    return problems


def report_publication_outcome(
    outcome: PublicationOutcome,
    *,
    project: str,
    print_func: Callable[[str], None] = print,
) -> None:
    """Say what is actually there, what is not, and what only a human can finish.

    Every line is decided by looking at the path, not by whether this run meant
    to write it. A hard failure part-way through used to print the artifacts it
    never reached as installed, which is the one thing a report of a security
    boundary must not do (SYRD-97 review).
    """
    pending = set(outcome.pending)
    missing: list[str] = []
    for artifact in outcome.artifacts:
        actual, owned = _actual_state(artifact.path)
        # Pending beats existence. A known_hosts holding some other host's keys
        # is a file, and reporting it as the pinned-hosts artifact would tell an
        # operator the push will be verified when it will be refused.
        if not actual or artifact.path in pending:
            missing.append(artifact.path)
            print_func(
                f"switchyard: {project} publication {artifact.description}: {artifact.path} "
                + ("NOT INSTALLED" if not actual else "INCOMPLETE -- present but not finished")
            )
            continue
        # The mode it really has, not the one it was meant to get.
        note = "" if owned else " -- NOT owned by this installer"
        print_func(
            f"switchyard: {project} publication {artifact.description}: "
            f"{artifact.path} ({actual}){note}"
        )
    if missing:
        print_func(
            f"switchyard: {project}'s publication boundary is INCOMPLETE: "
            + ", ".join(missing)
            + " is missing, so publication stays refused. Rerun the upgrade once the "
            "reason above is resolved."
        )
    if not outcome.public_key or outcome.problems:
        return
    if outcome.public_key_mismatched:
        print_func(
            f"switchyard: {project}'s public half did not match its private key and was "
            "rewritten from it. The private key is unchanged; if the forge holds the "
            "mismatched key, register the one below."
        )
    if outcome.public_key_restored:
        print_func(
            f"switchyard: {project}'s public key was missing and was derived back from the "
            "private key it already had. The key itself is unchanged, so nothing needs "
            "re-registering at the forge."
        )
    if outcome.key_created:
        print_func(f"switchyard: {project} has a new publication key. It is root's; no role can read it.")
    else:
        print_func(f"switchyard: {project} already had a publication key and it was left alone.")
    print_func(f"switchyard: public key:  {outcome.public_key}")
    if outcome.fingerprint:
        print_func(f"switchyard: fingerprint: {outcome.fingerprint}")
    print_func(
        f"switchyard: register that key with the forge as a WRITE key for {project}, then remove "
        "write authority from the shared project-account credential."
    )
    print_func(
        "switchyard: until you do both, the shared project credential still has write authority "
        "and every role under that account can still push. This upgrade cannot change that for you."
    )
