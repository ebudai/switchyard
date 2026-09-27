"""Release alignment: what is installed, deployed, running and recorded, compared one fact at a time.

- `_read_deploy_sha_marker` and `_current_tenant_release` read which commit a
  tenant board's `current` link names -- its marker, else the resolved release,
  else the release's own name.
- `_live_board_build_id` asks the running board for its build id, through an
  injected opener or the launcher's `_open_board_url`.
- `release_alignment` reads the shared release, the deployed board, the live
  build, root's pinned release (only a full commit counts), root's journal when
  it is readable, the tenant's copy and its observation, and the board's
  declared workflow, into a frozen `ReleaseAlignment`. It reads only.
- `ReleaseAlignment` holds those facts separately and says, in order, why the
  release phase must not be closed (`close_refusals`).
- `director_release_divergence_report` is what the unprivileged director says
  about the release phase: what it sees, that it closed nothing, and the one
  operator command that does.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-377), in their original
order. The launcher imports this module and re-exports every name, so
`release-status`, `finish-upgrade`, the tenant release target and every suite
that reaches these through the launcher reach the same objects, the class
included. Every launcher facility these use -- including the upgrade records it
re-exports -- and every name defined here that another definition here reads
when it runs, is read from `team_launcher` when it runs, as it was, so a patch
on the launcher still intercepts. `ReleaseAlignment`'s `dataclass` decorator is
bound when this module loads, as it was when the launcher loaded it. The
standard-library names are this module's own imports, the same objects. This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def _read_deploy_sha_marker(path: Path) -> str:
    try:
        return (path / ".pgu-deploy-sha").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_tenant_release(board_root: Path) -> tuple[Path | None, str]:
    from scripts import team_launcher as launcher

    current_link = board_root / "current"
    if not current_link.exists() and not current_link.is_symlink():
        return None, ""
    current_release = current_link.resolve(strict=False)
    current_sha = launcher._read_deploy_sha_marker(current_link)
    if not current_sha:
        current_sha = launcher._read_deploy_sha_marker(current_release)
    if not current_sha and current_release.name:
        current_sha = current_release.name
    return current_release, current_sha


# --------------------------------------------------------------------------
# The release phase: proved from the host, never from a claim (SYRD-117)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseAlignment:
    """The four facts an operator has to compare, read one at a time.

    Separate fields rather than a verdict, because the interesting states are
    the disagreements: a board serving a build its own `current` link does not
    name, a trusted journal saying `ready` over a deployment that happened, a
    tenant copy claiming a completion nothing else has. One boolean would hide
    every one of them (SYRD-117).
    """

    project: str
    #: The shared Switchyard release this host has installed.
    shared_release: str = ""
    #: The commit the tenant's board `current` link resolves to.
    deployed_release: str = ""
    #: What the running board reports as its own build.
    live_build: str = ""
    #: The commit this upgrade was pinned to deploy, when one was recorded.
    pinned_release: str = ""
    #: Root's journal, and whether it could be read at all from here.
    trusted_release_state: str = ""
    trusted_readable: bool = True
    #: The tenant's readable copy, and any unprivileged note beside it.
    tenant_release_state: str = ""
    tenant_observation: str = ""
    #: Whether the running board is enforcing a declared workflow, and -- when
    #: it is not -- whether that is this tenant's legacy state rather than its
    #: design. A release phase closed over a board running no workflow is how a
    #: tenant ends up upgraded, "done", and still serving its Director the
    #: provisioning scaffold (SYRD-240).
    board_runs_declared_workflow: bool = True
    legacy_without_workflow: bool = False
    #: Why a reading is missing, per fact.
    errors: tuple[str, ...] = ()

    @property
    def board_is_serving_its_release(self) -> bool:
        """The check the deploy itself makes: the live build IS the deployed tree."""
        return bool(self.deployed_release) and self.live_build == self.deployed_release

    @property
    def deployed_matches_pin(self) -> bool:
        """Whether what is deployed is what this upgrade was asked to deploy."""
        return not self.pinned_release or self.deployed_release == self.pinned_release

    @property
    def diverged(self) -> bool:
        """Whether the records disagree with each other or with the machine."""
        if self.tenant_release_state and self.trusted_readable:
            if self.tenant_release_state != self.trusted_release_state:
                return True
        if not self.trusted_readable:
            return False
        proved = self.board_is_serving_its_release and self.deployed_matches_pin
        if self.trusted_release_state == "done":
            return not proved
        return proved

    def close_refusals(self) -> list[str]:
        """Why the release phase must not be recorded done, in order.

        Read from the host every time. The deploy's exit status is deliberately
        not on this list: a phase closed because a previous command returned 0
        is a phase closed on a claim, and a claim is what this ticket is about.
        Re-reading costs one HTTP request and one readlink.
        """
        refusals: list[str] = []
        if not self.deployed_release:
            refusals.append(
                f"{self.project}'s board root names no deployed release, so there is no "
                "deployment to close the phase over"
            )
        elif not self.live_build:
            detail = f" ({'; '.join(self.errors)})" if self.errors else ""
            refusals.append(f"the running board did not report a build id{detail}")
        elif not self.board_is_serving_its_release:
            refusals.append(
                f"the running board reports build {self.live_build}, but {self.project}'s "
                f"deployed release is {self.deployed_release}; the board is not serving what "
                "was deployed"
            )
        if self.deployed_release and not self.deployed_matches_pin:
            refusals.append(
                f"the deployed release is {self.deployed_release}, but this upgrade was pinned "
                f"to {self.pinned_release}; closing the phase would claim a deploy that did not "
                "happen"
            )
        if self.legacy_without_workflow:
            # The release can be perfectly deployed and this still be wrong.
            # Every check above compares bytes on disk with what the board
            # reports running; none of them asks whether the board is
            # enforcing a workflow at all, so a legacy tenant closed cleanly
            # while its Director kept provisioning-scaffold onboarding and
            # `/api/workflow` kept answering null (SYRD-240).
            refusals.append(
                f"{self.project}'s board is running no declared workflow, so its director is "
                "still on provisioning-scaffold onboarding and /api/workflow answers null. "
                f"Closing the release phase would record a migration that has not happened. "
                f"Run `switchyard migrate-workflow {self.project}` to see what would be "
                "installed, and `pkexec switchyard migrate-workflow "
                f"{self.project} --apply` to install it"
            )
        return refusals


def _live_board_build_id(
    config: ProjectConfig, *, opener: Callable[[str], Any] | None = None
) -> tuple[str, str]:
    """What the running board says it is, or why it could not be asked."""
    from scripts import team_launcher as launcher

    board_root = (
        str(config.board_url or "").rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    )
    if not board_root:
        return "", f"{config.project} declares no board url"
    open_url = opener or launcher._open_board_url
    try:
        with open_url(board_root + "/api/board") as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "not proven"
        return "", f"the board at {board_root} could not be read ({exc})"
    build = str(payload.get("build_id") or "").strip() if isinstance(payload, Mapping) else ""
    return build, "" if build else f"the board at {board_root} reported no build id"


def release_alignment(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
) -> ReleaseAlignment:
    """Compare the shared release, the deployed board, the live build and both journals.

    Reads only. Every value comes from the host or the running board rather
    than from a record somebody wrote about them, which is the point: the
    defect this answers is a journal that disagreed with the machine and won
    (SYRD-117).
    """
    from scripts import team_launcher as launcher

    errors: list[str] = []
    shared = ""
    marker = launcher._read_switchyard_release_marker(
        (launcher.switchyard_shared_install_root() / "current").resolve(strict=False)
    )
    if marker is not None:
        shared = marker.marker_commit
        if marker.marker_error:
            errors.append(marker.marker_error)

    deployed = ""
    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        errors.append(f"{config.project} serves no tenant board release")
    else:
        _release, deployed = launcher._current_tenant_release(board_root)

    live, live_error = launcher._live_board_build_id(config, opener=opener)
    if live_error:
        errors.append(live_error)

    # `read_upgrade_source` already refuses anything that is not root's own
    # record and answers `{}` instead, so there is nothing to guard here.
    pinned = str(launcher.read_upgrade_source(config).get("deploy_ref") or "").strip()
    # Only a resolved commit can be compared with a deployed one. A branch name
    # says which branch, not which commit, so it is not a pin for this purpose
    # and treating it as one would fail every close.
    if not re.fullmatch(r"[0-9a-f]{40}", pinned):
        pinned = ""

    trusted_readable = os.geteuid() == 0 or os.access(
        launcher.privileged_upgrade_journal_path(config), os.R_OK
    )
    trusted_state = ""
    if trusted_readable:
        trusted_state = launcher.upgrade_phase_state(
            launcher.read_upgrade_journal(config, config_path=config_path, trusted=True), "release"
        )
    else:
        errors.append(
            "root's journal is not readable from this account; run this as an operator to "
            "compare it"
        )
    tenant_journal = launcher.read_upgrade_journal(config, config_path=config_path)
    observation = launcher.upgrade_phase_observation(tenant_journal, "release")
    # Read from the running board like every other fact here, rather than from
    # anything written about it.
    presence = launcher.declared_workflow_presence(config, config_path=config_path)
    if presence.board_problem and not presence.board_document:
        errors.append(presence.board_problem)
    return launcher.ReleaseAlignment(
        project=config.project,
        shared_release=shared,
        deployed_release=deployed,
        live_build=live,
        pinned_release=pinned,
        trusted_release_state=trusted_state,
        trusted_readable=trusted_readable,
        tenant_release_state=launcher.upgrade_phase_state(tenant_journal, "release"),
        tenant_observation=str(observation.get("state") or ""),
        board_runs_declared_workflow=presence.board_document,
        legacy_without_workflow=presence.legacy_without_workflow,
        errors=tuple(errors),
    )


def director_release_divergence_report(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
) -> list[str]:
    """What the director should say about the release phase, and never claim.

    The director can read the board and the tenant copy; it cannot read or write
    root's journal. So it reports what it can see, says plainly that it did not
    close anything, and names the one supported action that does (SYRD-117).
    """
    from scripts import team_launcher as launcher

    alignment = launcher.release_alignment(config, config_path=config_path, opener=opener)
    lines = [
        f"switchyard: finish-upgrade does not close {config.project}'s release phase. That phase "
        "is root's record and this command is unprivileged by design, so what it wrote is an "
        "observation beside the phases, not a phase.",
    ]
    if alignment.trusted_readable and alignment.trusted_release_state == "done":
        return lines + [
            f"switchyard: root's journal already records the release phase done for "
            f"{config.project}; nothing is outstanding."
        ]
    if alignment.close_refusals():
        return lines + [
            f"switchyard: {config.project}'s board is not serving a deployment that could close "
            "the phase yet:",
            *(f"  - {refusal}" for refusal in alignment.close_refusals()),
        ]
    return lines + [
        f"switchyard: {config.project}'s board is serving {alignment.deployed_release} and every "
        "check passes, but the authoritative release phase is "
        + (
            f"recorded {alignment.trusted_release_state or 'pending'}"
            if alignment.trusted_readable
            else "not readable from this account"
        )
        + f". An operator closes it with `pkexec switchyard release-status {config.project} "
        "--close`, which re-verifies the live build and deploys, restarts and rolls back nothing.",
    ]
