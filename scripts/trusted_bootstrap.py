"""The stale-launcher check, and the root-owned bootstrap it prints instead of running anything stale.

- `stale_launcher_problems` refuses an upgrade when this process is not the
  release it is about to install, and refuses root running the launcher out of
  a path root does not control -- naming the bootstrap in both cases.
- `trusted_bootstrap_commands` renders that bootstrap: the operator bundles the
  exact commit as themselves, and root builds its own repository from the
  bundle, demands the commit, and installs from it. On an upgrade the install
  is one attempt recorded by the root-owned recorder already installed
  (`installed_rollout_recorder`, `recorded_install_command`); on a first install
  it runs unrecorded and the boundary is recorded right after, by the recorder
  it installed (`install_boundary_command`). The two labels are the journal's.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-396), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's tooling phase that reads the check through it, and every suite that
calls or patches these there, reach the same objects. The launcher facilities
-- the shared install root and its default, and which installed release this
process runs from -- every name defined here that another definition here
reads when it runs, and the launcher's own file are read from `team_launcher`
when it runs, as they were: the file root is asked to trust is the launcher's,
resolved then, never this module's. The trust check's helper is still
imported inside the check when it runs. The label default is bound when the
function is defined, as it was. The standard-library names are this module's
own imports, the same objects. This module never imports `team_launcher` at
its top.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Sequence


#: The label an install attempt carries in the rollout journal.
INSTALL_ROLLOUT_LABEL = "install"
#: What a first install records once root-owned machinery exists to record with.
INSTALL_BOUNDARY_ROLLOUT_LABEL = "install-boundary"


def installed_rollout_recorder() -> Path | None:
    """The recorder root already has, or None when this host has none yet.

    Deliberately NOT `_rollout_recorder_path`, which falls back to the
    checkout's copy. That fallback is right for a step whose surrounding release
    is already installed and running; it is wrong for the step that installs
    one, because on a first install the checkout is precisely the unverified,
    tenant-writable tree this whole design exists to keep root out of. A host
    with no installed recorder has nothing trustworthy to record with, and
    saying so is better than executing an untrusted recorder and calling the
    result a root-owned record (SYRD-172).
    """
    from scripts import team_launcher as launcher

    installed = launcher.switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    return installed if installed.is_file() else None


def recorded_install_command(
    privileged_lines: "Sequence[str]",
    *,
    project: str,
    commit: str,
    recorder: Path,
    label: str = INSTALL_ROLLOUT_LABEL,
) -> str:
    """The whole bootstrap chain as ONE recorded attempt.

    The install is the step that puts new root-executed code on the host, and
    the only one whose input -- the bundle -- was produced by an unprivileged
    account. Everything the journal records afterwards runs FROM what this step
    installed, so a journal that begins after it cannot answer which release was
    installed, by whom, or from which bundle (SYRD-172).

    In whole, through one `bash -c`, for the reason `recorded_rollout_command`
    gives about the steps already wrapped: the chain carries its own `&&`, and
    leaving part of it outside the recorder would record the first command and
    run the rest unrecorded. `set -euo pipefail` in front so a failure anywhere
    ends the chain -- a half-installed release must be a FAILED attempt with no
    unrecorded suffix, not a successful one that stopped early.
    """
    for line in privileged_lines:
        if "switchyard-record-rollout" in line:
            raise ValueError(
                "refusing to wrap a chain that already records itself: "
                "nested attempts make the journal describe one install twice"
            )
    chain = "set -euo pipefail\n" + "\n".join(privileged_lines)
    prefix = ["sudo", str(recorder), project]
    if commit:
        prefix += ["--target-commit", commit]
    if label:
        prefix += ["--label", label]
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(chain)]
    )


def install_boundary_command(
    *, project: str, commit: str, recorder: Path, release_root: Path, pointer: Path
) -> str:
    """What a FIRST install can record, once there is something to record with.

    There is an irreducible gap on a host with no installed release: the bytes
    that would record the install do not exist until the install has put them
    there. The gap is not papered over by running the checkout's recorder --
    that would be root executing the tenant-writable tree this design refuses --
    so the boundary is recorded immediately AFTER, by the root-owned recorder the
    install itself just installed, and what it records is a verification rather
    than a claim: that `current` resolves to the release directory for this exact
    commit (SYRD-172).
    """
    from scripts import team_launcher as launcher

    verification = (
        f"test \"$(readlink -f {shlex.quote(str(pointer))})\" = {shlex.quote(str(release_root))}"
    )
    prefix = ["sudo", str(recorder), project, "--target-commit", commit,
              "--label", launcher.INSTALL_BOUNDARY_ROLLOUT_LABEL]
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(verification)]
    )


def trusted_bootstrap_commands(
    source_repo: Path,
    commit: str,
    *,
    project: str = "",
    publish_remote: str = "",
) -> list[str]:
    """How root reaches an exact release without reading a role-writable repository.

    The public wrapper dispatches privileged commands to whatever
    `<install root>/current` points at, so an operator who upgrades a checkout
    and runs `sudo switchyard upgrade` is still running whatever was installed
    last. The way out cannot be `sudo ./install`: that executes a script from the
    checkout, and under one shared account (SYRD-69) every role can write it.

    Nor can root simply read that checkout with an exact sha. A full sha does NOT
    make the content immutable: `git archive <sha>` honours `refs/replace/<sha>`,
    so whoever can write the repository can substitute the tree -- and the old
    installer executes the exported release before activating it, which turns
    that substitution into code execution as root. Git config in that repository
    can name programs to run as well. Root is given no access to it at all
    (SYRD-97 review).

    Instead the operator, unprivileged and in their own repository, produces a
    bundle; root builds its own repository from that bundle and demands the exact
    commit inside it. Object lookup is by content hash, so an object served under
    that sha hashes to it or git does not return it, and a bundle that does not
    carry it leaves root with nothing to check out. Replacement is disabled
    throughout, and only root's own configuration is ever in effect.
    """
    from scripts import team_launcher as launcher

    install_root = launcher.switchyard_shared_install_root()
    bootstrap = install_root / "bootstrap"
    src = bootstrap / "src"
    # Inside the checkout, because the operator writes it as themselves and the
    # root-owned bootstrap directory is not theirs to write. Root only reads it,
    # and reading it is safe for the same reason the whole design is: root
    # demands the exact commit afterwards, so anything else fails closed.
    bundle = source_repo / f".switchyard-bootstrap-{commit}.bundle"
    ref = f"refs/switchyard/bootstrap-{commit}"
    repo, sha = shlex.quote(str(source_repo)), shlex.quote(commit)
    q_src, q_bundle, q_ref = shlex.quote(str(src)), shlex.quote(str(bundle)), shlex.quote(ref)
    installer = shlex.quote(str(install_root / "current" / "scripts" / "install-switchyard"))
    release = shlex.quote(str(install_root / "releases" / commit))
    # Nothing root runs reads the checkout, so `no-replace` here is the
    # operator's own protection rather than the boundary.
    no_replace = "env GIT_NO_REPLACE_OBJECTS=1"
    unprivileged = [
        # Unprivileged, in the operator's own repository, as themselves.
        f"{no_replace} git -C {repo} update-ref {q_ref} {sha}",
        f"{no_replace} git -C {repo} bundle create {q_bundle} {q_ref}",
    ]
    privileged = [
        f"sudo install -d -m 0755 -o root -g root {shlex.quote(str(bootstrap))}",
        # Root, in a repository root creates, reading only that bundle.
        f"sudo {no_replace} git init -q {q_src}",
        f"sudo {no_replace} git -C {q_src} -c fetch.fsckObjects=true fetch --no-tags "
        f"{q_bundle} {q_ref}:{q_ref}",
        # The exact commit, or nothing: a bundle that does not carry it fails
        # here and no release is built.
        f"sudo {no_replace} git -C {q_src} checkout -q --detach {sha}",
        f"sudo {no_replace} SWITCHYARD_SOURCE_REPO={q_src} SWITCHYARD_SOURCE_REF={sha} "
        f"{installer} --apply",
    ]
    # Repointing `current` is not enough. The root-owned upgrade source record
    # still pins whatever was selected last, and the next upgrade recovers it
    # and goes straight back. This is what durably re-selects the reviewed
    # release, and it is the first command that runs the reviewed code.
    select = (
        f"sudo switchyard upgrade {shlex.quote(project or '<project>')} --source-repo {release} "
        f"--deploy-ref {sha} --publish-remote {shlex.quote(publish_remote or '<url>')}"
    )
    recorder = launcher.installed_rollout_recorder()
    if recorder is not None and project and commit:
        # AN UPGRADE. Root already holds a recorder it installed, so the step
        # that installs the next release leaves an attempt of its own: operator,
        # pkexec, target commit, exit status and output hashes, like every other
        # privileged step (SYRD-172).
        return [
            *unprivileged,
            launcher.recorded_install_command(privileged, project=project, commit=commit, recorder=recorder),
            select,
        ]
    if not project or not commit:
        # Rendered as guidance rather than for a particular host and release;
        # there is nothing to name in a record.
        return [*unprivileged, *privileged, select]
    # A FIRST INSTALL, and the gap is stated rather than hidden. There is no
    # root-owned recorder on this host yet, and the checkout's copy is the
    # tenant-writable tree root refuses to execute -- so these lines run
    # unrecorded, and the boundary is recorded immediately afterwards by the
    # recorder this install itself installs, against what actually landed.
    boundary = launcher.install_boundary_command(
        project=project, commit=commit,
        recorder=install_root / "current" / "scripts" / "switchyard-record-rollout",
        release_root=install_root / "releases" / commit,
        pointer=install_root / "current",
    )
    return [
        *unprivileged,
        f"# First install on this host: {install_root}/current does not exist yet, so there is no",
        "# root-owned switchyard-record-rollout to record the next four lines with. They run",
        "# unrecorded -- deliberately, rather than executing the recorder out of an unverified",
        "# checkout -- and the line after them records the boundary against what landed.",
        *privileged,
        boundary,
        select,
    ]


def stale_launcher_problems(
    release, *, source_repo: Path, project: str = "", publish_remote: str = ""
) -> list[str]:
    """Refuse when this process is not the release it is about to install.

    `/usr/local/bin/switchyard` sends privileged commands to whatever
    `<install root>/current` points at. An operator who pulls a checkout and runs
    `sudo switchyard upgrade` is therefore running the previously installed
    launcher, which does not contain this code at all -- so the upgrade quietly
    stages that older release's tools and reports success. It is the same trap as
    "pulling is not installing", one level up, and the only symptom is that the
    thing the operator was told exists is not there.

    Nothing can be done about it from inside the old launcher, which is why this
    is stated as a precondition of the new one: if this process is running from
    an installed release that is not the one selected, it stops and names the
    bootstrap (SYRD-97 review).
    """
    from scripts import team_launcher as launcher

    running = launcher.running_launcher_release()
    if os.geteuid() == 0 and launcher.switchyard_shared_install_root() == launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT:
        # Running privileged out of a path root does not control is the same
        # escalation SYRD-97 refused one level down. There, root was asked to
        # read a role-writable repository to build a release; here root is
        # already executing the launcher out of one -- so every privileged step
        # it is about to take, and every artifact it is about to render, comes
        # from bytes any role can rewrite. The dry run of an upgrade from a
        # worktree proposed exactly that and nothing stopped it (SYRD-93 live
        # acceptance).
        from scripts.ticket_board.project_provision import untrusted_root_executable_reasons

        # Against uid 0 by name: the identity entitled to have written what root
        # executes is root, whatever uid happens to be reading this. A
        # redirected install root is a sandbox by construction, which is why the
        # check above is scoped to the real one -- the same convention the
        # privileged provision root already uses.
        untrusted = untrusted_root_executable_reasons(
            Path(os.path.realpath(launcher.__file__)), owner_uid=0
        )
        if untrusted:
            return [
                "this command is running as root out of a path root does not control: "
                + untrusted[0],
                "nothing privileged was staged. Install the release you want with root-owned "
                "code only, and run the upgrade from that:",
                *(
                    f"  {line}"
                    for line in launcher.trusted_bootstrap_commands(
                        source_repo, release.commit, project=project, publish_remote=publish_remote
                    )
                ),
            ]
    if running is None:
        # A checkout, not an installed release. Ordinary for a developer run;
        # the privileged case was refused above, and the wrapper's dispatch is
        # what the rest of this is about.
        return []
    if running.marker_commit == release.commit:
        return []
    running_name = running.marker_commit or f"an unmarked release at {running.root}"
    return [
        f"this command is running from installed release {running_name}, but the upgrade "
        f"selected {release.commit}. The public `switchyard` wrapper dispatches privileged "
        "commands to whatever is installed, so it is not running the release you pinned and "
        "nothing privileged was staged.",
        "install that release first, with root-owned code only:",
        *(
            f"  {line}"
            for line in launcher.trusted_bootstrap_commands(
                source_repo, release.commit, project=project, publish_remote=publish_remote
            )
        ),
        "then re-run this command.",
    ]
