"""`switchyard publication-status`: where a project's publication-key cutover stands.

- `_build_switchyard_publication_status_parser` is the command's parser: a
  project and `--verify`.
- `publication_status_command` reports the cutover from the root-owned remote
  record, the owner's identity and the recorded, non-secret evidence -- and,
  only under `--verify`, asks the forge whether the shared credential may still
  write, the one thing it can record (SYRD-116).
- `_public_key_fingerprint` reads a public key's SHA256 fingerprint, or nothing.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-410), in their original
order. The launcher imports this module and re-exports all three names;
`switchyard_main` still dispatches `publication-status` through its own
globals. Everything the command calls -- the current user, the owner's home,
the plan data, the provision root and the fingerprint reader -- is read through
the launcher at call time, so a suite that rebinds one there still intercepts
it. The provision and publication-boundary helpers are imported inside the
command, when it runs, as they were. The `subprocess.run` and `print` defaults
are bound at definition, as they were, from this module's own imports: the
same objects. `ProjectConfig` is an annotation only. This module imports
`team_launcher` only inside the command, when it runs.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def _build_switchyard_publication_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard publication-status",
        description=(
            "Report where a project's publication-key cutover stands, and optionally check the "
            "one thing no successful push can establish."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "ask the forge whether the shared project credential may still write. The check is a "
            "dry-run push of the remote's own tip onto its own ref: it proposes no change, moves "
            "no ref, and leaves read access alone"
        ),
    )
    return parser


def publication_status_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    verify: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Report the cutover, and nothing else at all.

    Deliberately not a flag on `upgrade`. Asking whether one credential may
    write should not run a tenant upgrade: that path resolves releases, stages
    root-owned tooling, rewrites grants and advances recorded phases, and none of
    that is what was asked for. The only thing this can write is the root-owned,
    non-secret evidence file, and only when --verify is given (SYRD-116 review).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        owner_github_key_path,
        publish_identity_path,
        resolve_owner_github_identity,
    )
    from scripts.ticket_board.publication_boundary import (
        CUTOVER_READY,
        cutover_state,
        read_cutover_evidence,
        resolve_pinned_remote,
        shared_credential_check_command,
        verify_shared_credential,
    )

    project = config.project
    owner_user = config.run_as_user or launcher.current_user_name()
    owner_home = launcher.home_dir_for_user(owner_user)
    plan_data = launcher._plan_data_from_config(config, config_path)
    selected = resolve_owner_github_identity(
        str(owner_home),
        recorded_key_name=str(plan_data.get("owner_github_key_name") or ""),
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    shared_identity = owner_github_key_path(
        str(owner_home), key_name=selected.key_name if selected.resolved else ""
    )

    remote, remote_problem = resolve_pinned_remote(
        project, registration_root=launcher.switchyard_privileged_provision_root(), declared_remote=""
    )
    if not remote:
        print_func(f"switchyard: {project} has no root-owned publication remote: {remote_problem}")
        return 1

    publication_fingerprint = launcher._public_key_fingerprint(
        f"{publish_identity_path(project)}.pub", runner=runner
    )
    shared_fingerprint = launcher._public_key_fingerprint(f"{shared_identity}.pub", runner=runner)
    if not publication_fingerprint:
        print_func(
            f"switchyard: {project}'s publication public key could not be read, so the key in use "
            "cannot be identified and no recorded verdict describes it."
        )
    if not shared_fingerprint:
        print_func(
            f"switchyard: {project}'s shared credential could not be identified at "
            f"{shared_identity}.pub, so any recorded verdict about it no longer describes what is "
            "in use."
        )

    if verify:
        finding = verify_shared_credential(
            project,
            remote=remote,
            owner_user=owner_user,
            identity_file=shared_identity,
            publication_fingerprint=publication_fingerprint,
            shared_fingerprint=shared_fingerprint,
            runner=runner,
        )
        print_func(
            f"switchyard: {project} shared credential write authority: {finding.state}"
            + (f" -- {finding.detail}" if finding.detail else "")
        )

    evidence = read_cutover_evidence(
        project,
        remote=remote,
        publication_fingerprint=publication_fingerprint,
        shared_fingerprint=shared_fingerprint,
    )
    for reason in evidence.stale:
        print_func(f"switchyard: {project}'s recorded cutover state no longer applies: {reason}")
    state = cutover_state(evidence)
    print_func(f"switchyard: {project} publication cutover: {state}")
    print_func(
        f"switchyard:   publication key ({publication_fingerprint or 'unidentified'}): "
        f"{evidence.publication.state}"
        + (f" -- {evidence.publication.detail}" if evidence.publication.detail else "")
    )
    print_func(
        f"switchyard:   shared credential ({shared_fingerprint or 'unidentified'}): "
        f"{evidence.shared.state}"
        + (f" -- {evidence.shared.detail}" if evidence.shared.detail else "")
    )
    if state != CUTOVER_READY and not verify:
        command = " ".join(
            shlex.quote(part)
            for part in shared_credential_check_command(
                remote, owner_user=owner_user, identity_file=shared_identity
            )
        )
        print_func(
            f"switchyard: check the shared credential with `switchyard publication-status "
            f"{project} --verify`, which runs: {command}"
        )
    return 0


def _public_key_fingerprint(path: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    """The SHA256 fingerprint of a public key, or nothing if it cannot be read."""
    shown = runner(["ssh-keygen", "-l", "-f", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(shown, "returncode", 1) != 0:
        return ""
    for token in str(getattr(shown, "stdout", "") or "").split():
        if token.startswith("SHA256:"):
            return token
    return ""
