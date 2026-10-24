"""`switchyard status` and `switchyard release-status`: reading a tenant's state and reporting it.

- `switchyard_status_command` reports each project the caller can see: its
  registration, the release it runs, its board and panes. It also reports
  the host's installed Switchyard copies. A project the caller cannot read
  is diagnosed as such, and for root-owned tenants the answer falls back to
  root-owned records (`root_recorded_tenant_facts`).
- `switchyard_project_statuses` gathers `SwitchyardProjectStatus` records,
  within `STATUS_PROBE_TIMEOUT_SECONDS` per probe.
- `switchyard_runtime_copy_statuses` and its helpers report every
  `SwitchyardRuntimeCopyStatus`: the checkout, the shared release, and the
  installed wrapper and what it points at.
- `switchyard_release_status_command` compares a tenant's recorded release
  with what it runs (`format_release_alignment`). With `--close` it records
  the phase in the rollout journal (`close_release_phase`). A tenant whose
  configuration this account cannot use is reported from root-owned records
  instead (`release_status_from_root_records`, SYRD-659).

Loading project config, resolving users and releases, probing panes and
recording rollout phases stay in `scripts/team_launcher.py`. This module reads
them from there when a function runs, so the suites' patches on the launcher
reach it. Controlling the board and listener services is not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-301). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import LauncherCheckoutProbe, ProjectConfig, ReleaseAlignment


@dataclass(frozen=True)
class SwitchyardProjectStatus:
    name: str
    slug: str
    state: str
    panes_up: int | None
    panes_total: int | None
    config_path: Path
    viewer_session: str | None = None
    error: str = ""
    # SYRD-43: presentation windows running as root. A project with any of these
    # is not safely attached however many panes are up.
    root_windows: tuple[int, ...] = ()
    # SYRD-193: the three facts that tell a suspension from a closed window from
    # a half-stopped tenant. `None` is "not asked or not answered", which is a
    # third answer and must not read as False -- a listener whose manager did
    # not reply has not been shown to be down.
    board_active: bool | None = None
    listener_active: bool | None = None
    presentation_open: bool | None = None

    @property
    def runtime_state(self) -> str:
        """`state`, refined by what else is up. Falls back when nothing was asked."""
        if self.state in {"unknown", "unsafe-root-window"}:
            return self.state
        facts = (self.board_active, self.listener_active, self.presentation_open)
        if any(fact is None for fact in facts):
            return self.state
        panes = bool(self.panes_up)
        if self.board_active and self.listener_active and panes:
            return "running" if self.presentation_open else "presentation-closed"
        if not self.board_active and not self.listener_active and not panes and not self.presentation_open:
            return "suspended"
        return "partially-stopped"

    @property
    def panes_display(self) -> str:
        if self.panes_up is None or self.panes_total is None:
            return "?/?"
        return f"{self.panes_up}/{self.panes_total}"

    @property
    def viewer_display(self) -> str:
        return self.viewer_session or "-"


@dataclass(frozen=True)
class SwitchyardRuntimeCopyStatus:
    project: str
    copy: str
    path: Path
    status: str


def _format_checkout_probe_status(probe: LauncherCheckoutProbe) -> str:
    from scripts import team_launcher as launcher

    if probe.error:
        return f"unknown: {probe.error}"
    parts: list[str] = []
    if probe.behind:
        parts.append(f"behind {launcher._format_behind_count(probe.behind, exact=probe.behind_exact)}")
    if probe.ahead:
        parts.append(f"ahead {probe.ahead} commit(s)")
    return ", ".join(parts) if parts else "current"


def _runtime_checkout_copy_status(
    config: ProjectConfig,
    *,
    project: str,
    copy: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> SwitchyardRuntimeCopyStatus | None:
    from scripts import team_launcher as launcher

    if not path.exists():
        return None
    probe = launcher.probe_checkout_against_worktree_ref(config, path, runner=runner)
    return SwitchyardRuntimeCopyStatus(
        project=project,
        copy=copy,
        path=path,
        status=_format_checkout_probe_status(probe),
    )


def _runtime_release_copy_status(
    config: ProjectConfig,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> SwitchyardRuntimeCopyStatus | None:
    from scripts import team_launcher as launcher

    try:
        status = launcher.tenant_release_status(
            config, source_repo=source_repo, deploy_ref=launcher.worktree_ref(config), runner=runner
        )
    except (SystemExit, OSError) as exc:
        # One optional detail, not the command. A tenant whose release state
        # cannot be derived from here -- an owner_home this account may not
        # read, a plan it may not open -- is reported as unavailable rather
        # than ending a read-only status (SYRD-241).
        reason = " ".join(str(exc).split()).removeprefix("switchyard: ")
        return SwitchyardRuntimeCopyStatus(
            project=config.project,
            copy="tenant release",
            path=Path(str(launcher._tenant_board_root_from_config(config) or "-")),
            status=f"unavailable: {reason}",
        )
    if status is None:
        return None
    if status.resolve_error:
        summary = f"unknown: unresolved {status.deploy_ref}: {status.resolve_error}"
    elif not status.current_sha:
        summary = f"missing release; target {launcher._format_release_sha(status.target_sha)} from {status.deploy_ref}"
    elif status.unchanged:
        summary = f"current at {launcher._format_release_sha(status.current_sha)}"
    else:
        summary = f"stale: {launcher._format_release_sha(status.current_sha)} -> {launcher._format_release_sha(status.target_sha)}"
    return SwitchyardRuntimeCopyStatus(
        project=config.project,
        copy="tenant release",
        path=status.board_root,
        status=summary,
    )


def _runtime_shared_release_status(path: Path) -> SwitchyardRuntimeCopyStatus | None:
    from scripts import team_launcher as launcher

    release = launcher.shared_switchyard_release_for_path(path)
    if release is None:
        return None
    if release.marker_commit:
        summary = f"shared install at {release.marker_commit}"
    elif release.marker_error:
        summary = f"unknown shared install: {release.marker_error}"
    else:
        summary = "unknown shared install: missing release marker"
    return SwitchyardRuntimeCopyStatus(
        project="switchyard",
        copy="shared release",
        path=release.root,
        status=summary,
    )


def _parse_switchyard_wrapper_target(text: str) -> str:
    for prefix in ("readonly SWITCHYARD_DEFAULT_TARGET=", "readonly SWITCHYARD_TARGET="):
        target = _parse_switchyard_wrapper_target_line(text, prefix)
        if target:
            return target
    return ""


def _parse_switchyard_wrapper_target_line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        raw_value = line.split("=", 1)[1].strip()
        try:
            parts = shlex.split(raw_value)
        except ValueError:
            return raw_value
        return parts[0] if parts else raw_value
    return ""


def switchyard_installed_wrapper_status(install_path: Path | None = None) -> SwitchyardRuntimeCopyStatus | None:
    from scripts import team_launcher as launcher

    path = (
        install_path
        or Path(launcher._env_first("SWITCHYARD_INSTALL_PATH", "PGU_SWITCHYARD_INSTALL_PATH") or "/usr/local/bin/switchyard")
    ).expanduser()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SwitchyardRuntimeCopyStatus(
            project="switchyard",
            copy="installed wrapper",
            path=path,
            status=f"unknown: cannot read wrapper: {exc}",
        )
    target = _parse_switchyard_wrapper_target(text)
    if not target:
        return SwitchyardRuntimeCopyStatus(
            project="switchyard",
            copy="installed wrapper",
            path=path,
            status="unknown: not a switchyard trampoline",
        )
    target_path = Path(target).expanduser()
    target_status = "target reachable" if os.access(target_path, os.X_OK) else "target unreachable"
    if "--switchyard-wrapper-requires-root" not in text:
        summary = f"stale: embedded verb classification; {target_status}: {target_path}"
    else:
        summary = f"current: live verb classification; {target_status}: {target_path}"
    return SwitchyardRuntimeCopyStatus(
        project="switchyard",
        copy="installed wrapper",
        path=path,
        status=summary,
    )


def switchyard_runtime_copy_statuses(
    configs: Sequence[ProjectConfig],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_repo: Path | None = None,
    switchyard_install_path: Path | None = None,
) -> list[SwitchyardRuntimeCopyStatus]:
    from scripts import team_launcher as launcher

    source = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)
    statuses: list[SwitchyardRuntimeCopyStatus] = []
    seen: set[tuple[str, str, Path]] = set()

    def add(status: SwitchyardRuntimeCopyStatus | None) -> None:
        if status is None:
            return
        key = (status.project, status.copy, status.path.expanduser().resolve(strict=False))
        if key in seen:
            return
        seen.add(key)
        statuses.append(status)

    add(switchyard_installed_wrapper_status(switchyard_install_path))
    shared_status = _runtime_shared_release_status(source)
    if shared_status is not None:
        add(shared_status)
    for config in configs:
        if shared_status is None:
            add(_runtime_checkout_copy_status(config, project=config.project, copy="invoking checkout", path=source, runner=runner))
        if config.repository is not None:
            add(
                _runtime_checkout_copy_status(
                    config,
                    project=config.project,
                    copy="project checkout",
                    path=config.repository,
                    runner=runner,
                )
            )
        for role in config.roles:
            role_path = Path(role.workdir)
            if config.repository is not None and role_path.resolve(strict=False) == config.repository.resolve(strict=False):
                continue
            add(
                _runtime_checkout_copy_status(
                    config,
                    project=config.project,
                    copy=f"{role.role} worktree",
                    path=role_path,
                    runner=runner,
                )
            )
        if shared_status is None:
            add(_runtime_release_copy_status(config, source_repo=source, runner=runner))
    return statuses


#: How long one tenant's tmux server has to answer before its row is unknown.
#: A listing walks every registered project, so the cost of an unreachable one
#: has to be bounded rather than merely unlikely (SYRD-170).
STATUS_PROBE_TIMEOUT_SECONDS = 3.0


def switchyard_project_statuses(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    probe_timeout_seconds: float = STATUS_PROBE_TIMEOUT_SECONDS,
    # The two SOURCES a status rests on, injectable so a case can say what a
    # tenant's tmux server and board answer without saying what the verdict
    # should be. The verdict is still computed by the same `pane_liveness` the
    # privileged path uses (SYRD-170).
    owner_tmux_reader: "Callable[[ProjectConfig], tuple[set[str], str]] | None" = None,
    assignments_reader: "Callable[[ProjectConfig], tuple[dict[str, dict], str]] | None" = None,
) -> list[SwitchyardProjectStatus]:
    from scripts import team_launcher as launcher

    statuses: list[SwitchyardProjectStatus] = []
    for entry in launcher._switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        try:
            config = launcher.load_project_config(entry.slug, entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit) as exc:
            statuses.append(
                SwitchyardProjectStatus(
                    name=entry.name,
                    slug=entry.slug,
                    state="unknown",
                    panes_up=None,
                    panes_total=None,
                    config_path=entry.config_path,
                    viewer_session=None,
                    error=str(exc),
                )
            )
            continue
        # The same proof privileged recovery uses, not a second weaker one: the
        # owner's own tmux server, and the board's runtime assignments checked
        # against /proc by pid, start time and uid. The argv search this
        # replaced could not see a pane whose CLI had exec'd past its env
        # wrapper, so a healthy long-running project read as stopped (SYRD-170).
        #
        # Bounded and unprivileged: one tmux call and one board call per tenant,
        # both with a deadline, `sudo -n` so nothing waits for a password, and a
        # tenant that cannot answer becomes ONE unknown row rather than an
        # exception that ends the listing.
        tmux_targets, tmux_problem = (
            owner_tmux_reader(config)
            if owner_tmux_reader is not None
            else launcher.owner_tmux_targets(
                config, runner=runner, timeout_seconds=probe_timeout_seconds, interactive=False
            )
        )
        assignments, assignment_problem = (
            assignments_reader(config)
            if assignments_reader is not None
            else launcher.read_runtime_assignment_details(config)
        )
        if tmux_problem:
            statuses.append(
                SwitchyardProjectStatus(
                    name=entry.name,
                    slug=entry.slug,
                    state="unknown",
                    panes_up=None,
                    panes_total=len(config.roles),
                    config_path=entry.config_path,
                    viewer_session=launcher.viewer_session_for_project(config.project),
                    error=tmux_problem,
                )
            )
            continue
        owner_uid = launcher.uid_for_user(config.run_as_user)
        panes_up = sum(
            1
            for role in config.roles
            if launcher.pane_liveness(
                config, role,
                tmux_targets=tmux_targets,
                assignments=assignments,
                owner_uid=owner_uid,
            ).live
        )
        root_windows = launcher.unsafe_root_presentation_windows(config, config_path=entry.config_path)
        statuses.append(
            SwitchyardProjectStatus(
                name=entry.name,
                slug=entry.slug,
                state="unsafe-root-window" if root_windows else ("running" if panes_up else "stopped"),
                panes_up=panes_up,
                panes_total=len(config.roles),
                config_path=entry.config_path,
                viewer_session=launcher.viewer_session_for_project(config.project),
                root_windows=tuple(window.pid for window in root_windows),
                # The board being unreachable does not make the panes absent --
                # tmux still saw them -- but it does mean this row rests on one
                # source instead of two, and saying so is cheaper than a reader
                # guessing why the count looks the way it does.
                error=assignment_problem or "",
            )
        )
    return statuses


def _unit_is_active(unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool | None:
    try:
        result = runner(
            ["systemctl", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=STATUS_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    code = getattr(result, "returncode", None)
    return code == 0 if code in (0, 3) else None


def supervised_board_entries() -> dict[str, dict]:
    """What the host board supervisor last published, by tenant; empty on a host that has none (SYRD-586).

    The supervisor writes the whole record, world-readable, whenever a child
    starts, exits or changes state, so any account can say which process is
    each tenant's board without asking anything privileged.
    """
    from scripts import board_services
    from scripts.board_supervisor import read_status_file

    published = read_status_file(board_services.board_supervisor_paths()) or {}
    tenants = published.get("tenants") if isinstance(published, dict) else None
    return {str(tenant.get("project")): tenant for tenant in tenants or [] if isinstance(tenant, dict)}


def board_supervisor_lines(slugs: Sequence[str]) -> list[str]:
    """The board-services section: one line per tenant the supervisor runs, naming its child process."""
    from scripts.board_supervisor import describe_tenant
    from scripts.board_supervisor_spec import SUPERVISOR_UNIT

    supervised = supervised_board_entries()
    if not supervised:
        return []
    lines = [f"BOARD SERVICES (supervised by {SUPERVISOR_UNIT})"]
    lines.extend(f"  {describe_tenant(supervised[slug])}" for slug in sorted(supervised) if not slugs or slug in slugs)
    lines.extend(f"  {slug}: {slug}-ticket-board.service (not supervised)" for slug in slugs if slug not in supervised)
    return lines


def root_recorded_tenant_facts(
    slug: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    journal_root: Path | None = None,
) -> tuple[bool | None, str]:
    """What root's own records say about a tenant, readable by anybody.

    The registry, the installed system unit and the rollout journal are
    root-owned and world-readable by design, and none of them lives in the
    tenant's home. So a tenant whose configuration this account may not read
    can still be reported on -- which is what keeps a read-only status useful
    without asking an operator to cross a mutation boundary (SYRD-241).

    Returns (board unit active, the last rollout line). `None` is "not
    answered", which is not the same as inactive.
    """
    from scripts.ticket_board import rollout_journal

    # The same name provisioning renders and the deploy script derives, from
    # the slug alone: the tenant's own document is exactly what is unreadable.
    unit = f"{slug}-ticket-board.service"
    active: bool | None = None
    supervised = supervised_board_entries()
    if slug in supervised:
        # Its unit is disabled on purpose; the supervisor's own published record says (SYRD-586).
        active = supervised[slug].get("state") == "running"
    else:
        active = _unit_is_active(unit, runner=runner)
    last = ""
    index = rollout_journal.index_path(slug, root=journal_root)
    try:
        with index.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
    except OSError:
        return active, ""
    if not last:
        return active, ""
    try:
        record = json.loads(last)
    except json.JSONDecodeError:
        return active, ""
    if not isinstance(record, dict):
        return active, ""
    detail = " ".join(str(record.get("detail") or "").split())[:160]
    described = (
        f"last rollout {record.get('attempt') or 'unknown'} at {record.get('at') or 'an unknown time'}"
        f" ({record.get('status') or 'unknown'})"
    )
    return active, f"{described}: {detail}" if detail else described


def _switchyard_project_status_payload(status: SwitchyardProjectStatus) -> dict[str, Any]:
    return {
        "name": status.name,
        "slug": status.slug,
        "state": status.state,
        "runtime_state": status.runtime_state,
        "board_active": status.board_active,
        "listener_active": status.listener_active,
        "presentation_open": status.presentation_open,
        "panes_up": status.panes_up,
        "panes_total": status.panes_total,
        "panes": status.panes_display,
        "root_windows": list(status.root_windows),
        "viewer_session": status.viewer_session,
        "config_path": str(status.config_path),
        "error": status.error,
    }


def _switchyard_runtime_copy_status_payload(status: SwitchyardRuntimeCopyStatus) -> dict[str, str]:
    return {
        "project": status.project,
        "copy": status.copy,
        "path": str(status.path),
        "status": status.status,
    }


def switchyard_status_command(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    json_output: bool = False,
    project: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_repo: Path | None = None,
    switchyard_install_path: Path | None = None,
    owner_tmux_reader: "Callable[[ProjectConfig], tuple[set[str], str]] | None" = None,
    assignments_reader: "Callable[[ProjectConfig], tuple[dict[str, dict], str]] | None" = None,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    configs: list[ProjectConfig] = []
    statuses = switchyard_project_statuses(
        config_dir=config_dir,
        registry_dir=registry_dir,
        runner=runner,
        owner_tmux_reader=owner_tmux_reader,
        assignments_reader=assignments_reader,
    )
    if project:
        statuses = [status for status in statuses if status.slug == project]
    for status in statuses:
        if status.state == "unknown":
            continue
        try:
            configs.append(launcher.load_project_config(status.slug, status.config_path))
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
    runtime_statuses = switchyard_runtime_copy_statuses(
        configs,
        runner=runner,
        source_repo=source_repo,
        switchyard_install_path=switchyard_install_path,
    )
    supervised = supervised_board_entries()
    if json_output:
        payload: dict[str, Any] = {
            "projects": [_switchyard_project_status_payload(status) for status in statuses],
            "runtime_copies": [
                _switchyard_runtime_copy_status_payload(status) for status in runtime_statuses
            ],
        }
        if supervised:
            payload["supervised_boards"] = [
                supervised[slug] for slug in sorted(supervised) if not project or slug == project
            ]
        print_func(json.dumps(payload, indent=2))
        return 0
    rows = [("NAME", "SLUG", "STATE", "PANES", "VIEWER")]
    rows.extend(
        (status.name, status.slug, status.state, status.panes_display, status.viewer_display)
        for status in statuses
    )
    widths = [max(len(str(row[index])) for row in rows) for index in range(5)]
    for row in rows:
        print_func(
            f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  "
            f"{row[3]:<{widths[3]}}  {row[4]}"
        )
    # A tenant whose configuration this account cannot read still has a row --
    # its registration, release and journal state come from root-owned records
    # -- but its pane and viewer detail is its owner's to show. Naming that,
    # and how to see it, is the difference between a status an operator can use
    # unprivileged and one that used to demand root for the whole command
    # (SYRD-241).
    unreadable = [
        status for status in statuses
        if status.state == "unknown" and (project or status.error)
    ]
    if unreadable:
        print_func("")
        caller = launcher.current_user_name() or "this account"
        for status in unreadable:
            owner = launcher._project_config_path_owner_user(status.config_path)
            reason = " ".join(str(status.error or "").split()) or "it could not be read"
            print_func(
                f"switchyard: {status.slug}'s pane and viewer state is unavailable to {caller}: "
                f"{reason}. Everything above for {status.slug} comes from root-owned records and "
                "is complete."
            )
            active, journal = root_recorded_tenant_facts(status.slug, runner=runner)
            recorded = []
            if active is not None:
                recorded.append(f"board service {'active' if active else 'inactive'}")
            if journal:
                recorded.append(journal)
            if recorded:
                print_func(f"switchyard:   from root-owned records: {'; '.join(recorded)}")
            as_owner = f" as {owner}" if owner and owner != caller else ""
            print_func(
                f"switchyard:   to see the rest, run `switchyard status {status.slug}`{as_owner}, "
                f"or `sudo switchyard status {status.slug}`"
            )
    board_lines = board_supervisor_lines([status.slug for status in statuses])
    if board_lines:
        print_func("")
        for line in board_lines:
            print_func(line)
    unsafe = [status for status in statuses if status.root_windows]
    if unsafe:
        print_func("")
        for status in unsafe:
            pids = ", ".join(str(pid) for pid in status.root_windows)
            print_func(
                f"switchyard: {status.slug} is NOT safely attached: presentation window(s) "
                f"{pids} are running as root, so a detached pane falls back to a root shell. "
                f"An operator must run `sudo switchyard replace-window {status.slug}`, which "
                "leaves every worker session running."
            )
    if runtime_statuses:
        print_func("")
        runtime_rows = [("PROJECT", "COPY", "PATH", "STATUS")]
        runtime_rows.extend(
            (status.project, status.copy, str(status.path), status.status) for status in runtime_statuses
        )
        runtime_widths = [max(len(str(row[index])) for row in runtime_rows) for index in range(3)]
        print_func("RUNTIME COPIES")
        for row in runtime_rows:
            print_func(
                f"{row[0]:<{runtime_widths[0]}}  "
                f"{row[1]:<{runtime_widths[1]}}  "
                f"{row[2]:<{runtime_widths[2]}}  {row[3]}"
            )
    return 0


def format_release_alignment(alignment: ReleaseAlignment) -> list[str]:
    """One line per fact, so a disagreement is visible rather than summarised."""
    from scripts import team_launcher as launcher

    trusted = (
        (alignment.trusted_release_state or "(unrecorded)")
        if alignment.trusted_readable
        else "(not readable from this account)"
    )
    tenant = alignment.tenant_release_state or "(unrecorded)"
    if alignment.tenant_observation:
        tenant += f"; observed {alignment.tenant_observation} by an unprivileged command"
    lines = [
        f"switchyard: {alignment.project} release phase",
        f"  shared release   {launcher._format_release_sha(alignment.shared_release)}",
        f"  deployed release {launcher._format_release_sha(alignment.deployed_release)}",
        f"  live board build {launcher._format_release_sha(alignment.live_build)}",
        f"  pinned release   {launcher._format_release_sha(alignment.pinned_release)}",
        f"  trusted journal  {trusted}",
        f"  tenant journal   {tenant}",
        "  declared workflow "
        + (
            "installed"
            if alignment.board_runs_declared_workflow
            else "NONE -- the board is running no declared workflow"
        ),
    ]
    for error in alignment.errors:
        lines.append(f"  note             {error}")
    refusals = alignment.close_refusals()
    if refusals:
        lines.append("switchyard: the release phase cannot be recorded done:")
        lines.extend(f"  - {refusal}" for refusal in refusals)
    elif not alignment.trusted_readable:
        lines.append(
            f"switchyard: every check this account can make passes. Run "
            f"`pkexec switchyard release-status {alignment.project}` to compare root's journal."
        )
    elif alignment.trusted_release_state == "done":
        lines.append(
            "switchyard: the release phase is closed and the board is serving the release it names."
        )
    else:
        lines.append(
            f"switchyard: the board is serving {alignment.deployed_release} and every check "
            f"passes, but the release phase is recorded {trusted}. Close it with "
            f"`pkexec switchyard release-status {alignment.project} --close`."
        )
    return lines


def close_release_phase(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Record the release phase done, but only after re-proving the deployment.

    The one write, and it is root's. It happens only when the board is serving
    the release its own `current` link names and that release is the one this
    upgrade was pinned to. A failure records a non-done state carrying the
    reason, so the next reader learns what stopped it instead of finding the
    phase exactly where it was (SYRD-117).

    It deploys nothing, restarts nothing and rolls back nothing, which is what
    makes it usable on a tenant whose release is already deployed and whose
    phase was left `ready` by a release that predates this.
    """
    from scripts import team_launcher as launcher

    if os.geteuid() != 0:
        print_func(
            f"switchyard: closing {config.project}'s release phase writes root's journal, which "
            "this process cannot. Run it as an operator: "
            f"`pkexec switchyard release-status {config.project} --close`."
        )
        return 1
    alignment = launcher.release_alignment(config, config_path=config_path, opener=opener)
    for line in format_release_alignment(alignment):
        print_func(line)
    refusals = alignment.close_refusals()
    if refusals:
        launcher.record_upgrade_phase(
            config,
            config_path=config_path,
            phase="release",
            state="blocked",
            detail="; ".join(refusals),
        )
        print_func(
            f"switchyard: recorded {config.project}'s release phase blocked with that reason. "
            "Nothing was deployed, restarted or rolled back."
        )
        return 1
    launcher.record_upgrade_phase(
        config,
        config_path=config_path,
        phase="release",
        state="done",
        detail=(
            f"verified live board build {alignment.live_build} is the deployed release "
            f"{alignment.deployed_release}"
            + (f", pinned {alignment.pinned_release}" if alignment.pinned_release else "")
        ),
    )
    print_func(
        f"switchyard: recorded {config.project}'s release phase done against live build "
        f"{alignment.live_build}. Nothing was deployed, restarted or rolled back."
    )
    return 0


def _build_switchyard_release_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard release-status",
        description=(
            "Compare a project's shared Switchyard release, its deployed board release, the "
            "build the running board reports, and both upgrade journals. Reads only. With "
            "--close, and only as root, record the release phase done -- after re-proving from "
            "the running board that the deployment happened. It deploys nothing, restarts "
            "nothing and rolls back nothing."
        ),
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument(
        "--close",
        action="store_true",
        help=(
            "record the authoritative release phase done after re-verifying the live build; "
            "refuses, and records the reason, when any check fails"
        ),
    )
    return parser


def release_status_from_root_records(
    slug: str,
    *,
    config_path: Path,
    problem: BaseException,
    close: bool = False,
    registry_dir: Path | None = None,
    opener: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """`release-status` for a tenant whose generated configuration this account cannot use (SYRD-659).

    The configuration is the tenant's own, in a tree only its owner may read,
    so a Director or operator asking about another tenant cannot load it -- and
    must not be given a way round that. What root-owned, world-readable records
    show is reported instead: the shared release, the build the running board
    reports at the port its root-owned unit names, and root's record of the
    board service and the last rollout. What only the owner or root can read is
    named as not read, with the command that compares everything. Nothing is
    changed; the answer is never success, because the comparison is not
    complete.
    """
    from scripts import team_launcher as launcher
    from scripts import board_supervisor_spec

    caller = launcher.current_user_name() or "this account"
    owner = launcher._project_config_path_owner_user(config_path)
    registry = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    code = getattr(problem, "errno", None)
    if code is None:
        # Root's reader refuses in words, not an errno. Which words is not the question here -- whether the file is
        # there is, and a look at it decides only how this is said; nothing is read or written from it.
        try:
            os.stat(config_path)
        except OSError as exc:
            code = exc.errno
    reason = " ".join(str(getattr(problem, "strerror", None) or problem).split()).removeprefix("switchyard: ")
    reason = reason.removesuffix(" Nothing was changed.").rstrip(".")
    if code in (errno.EACCES, errno.EPERM):
        print_func(f"switchyard: {slug}'s generated configuration {config_path} is not readable from {caller}: "
                   f"it is in {owner or 'its owner'}'s private tree.")
    elif code in (errno.ENOENT, errno.ENOTDIR):
        print_func(f"switchyard: {slug}'s registered configuration {config_path} does not exist, so the registry "
                   f"entry {registry} is stale. Restore the checkout there, or have an operator remove that entry "
                   f"and run `sudo switchyard register <the configuration's path>`.")
    else:
        if not isinstance(problem, (OSError, ValueError, SystemExit)):
            reason = f"{reason} ({type(problem).__name__})"
        print_func(f"switchyard: {slug}'s generated configuration {config_path} is not a usable launcher "
                   f"configuration: {reason}. It is {owner or 'its owner'}'s file to repair; nothing was changed.")

    unreadable = ("(not readable from this account)" if code in (errno.EACCES, errno.EPERM)
                  else "(unknown without a usable configuration)")
    marker = launcher._read_switchyard_release_marker(
        (launcher.switchyard_shared_install_root() / "current").resolve(strict=False)
    )
    shared = marker.marker_commit if marker is not None else ""
    notes: list[str] = [marker.marker_error] if marker is not None and marker.marker_error else []
    live = ""
    try:
        unit = board_supervisor_spec.read_unit_spec(slug, board_supervisor_spec.Paths())
    except (board_supervisor_spec.SpecError, OSError) as exc:
        notes.append(f"the board's root-owned unit could not be read: {exc}")
    else:
        if unit.port is None:
            notes.append(f"{unit.unit_path} names no --port")
        else:
            host = next((unit.argv[i + 1] for i, word in enumerate(unit.argv[:-1]) if word == "--host"), "127.0.0.1")
            board = SimpleNamespace(project=slug, board_url=f"http://{host}:{unit.port}")
            live, live_error = launcher._live_board_build_id(board, opener=opener)
            if live_error:
                notes.append(live_error)
    active, journal = root_recorded_tenant_facts(slug)
    service = "unknown" if active is None else ("active" if active else "inactive")
    lines = [
        f"switchyard: {slug} release phase, from root-owned records only",
        f"  shared release   {launcher._format_release_sha(shared)}",
        f"  live board build {launcher._format_release_sha(live)}",
        f"  deployed release {unreadable}",
        f"  pinned release   {unreadable}",
        f"  trusted journal  {unreadable}",
        f"  tenant journal   {unreadable}",
        f"  board service    {service}" + (f"; {journal}" if journal else ""),
        *(f"  note             {note}" for note in notes),
    ]
    for line in lines:
        print_func(line)
    if code in (errno.EACCES, errno.EPERM):
        as_owner = f", or `switchyard release-status {slug}` as {owner}" if owner and owner != caller else ""
        print_func(
            f"switchyard: to compare everything, run `pkexec switchyard release-status {slug}`{as_owner}."
            + (f" Closing the phase is root's: `pkexec switchyard release-status {slug} --close`." if close else "")
        )
    elif close:
        print_func(f"switchyard: {slug}'s release phase was not closed: it is closed only against a configuration "
                   "that loads.")
    return 1


def switchyard_release_status_command(
    project: str,
    *,
    close: bool = False,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    opener: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Show where a tenant's release phase really is, and optionally close it.

    The read is the point. An operator who had an exit-0 upgrade and a board
    that had not moved could not tell those two apart from any single record;
    this prints all four and marks the disagreement (SYRD-117).
    """
    from scripts import team_launcher as launcher

    entry = launcher._resolve_switchyard_project(project, config_dir=config_dir, registry_dir=registry_dir)
    try:
        config = launcher.load_project_config(entry.slug, entry.config_path)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see below
        # Another tenant's private tree, a checkout that moved, or a document that does not load: said, with what
        # root-owned records show, never a traceback (SYRD-659). Any failure, not a list of them: the document is the
        # tenant's to write, and its loader answers a malformed field with whatever the field breaks -- a TypeError
        # for a `slot` that is an object, an IndexError for an empty `cli` (Audit of a9e26d1d). Its class is named.
        return release_status_from_root_records(
            entry.slug, config_path=entry.config_path, problem=exc, close=close,
            registry_dir=registry_dir, opener=opener, print_func=print_func,
        )
    if close:
        return close_release_phase(
            config, config_path=entry.config_path, opener=opener, print_func=print_func
        )
    alignment = launcher.release_alignment(config, config_path=entry.config_path, opener=opener)
    for line in format_release_alignment(alignment):
        print_func(line)
    return 1 if alignment.close_refusals() or alignment.diverged else 0


def _build_switchyard_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard status", description="List registered Switchyard projects and pane liveness.")
    # Optional, and joined like `stop`'s, so a project whose name has spaces
    # selects the same way it does everywhere else. Omit it for every project.
    parser.add_argument("project", nargs="*", help="registered project name or slug (default: all)")
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable project status")
    return parser
