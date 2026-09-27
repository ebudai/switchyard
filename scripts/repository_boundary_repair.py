"""Repairing a running tenant's repository boundary: `switchyard repair-boundary`.

`switchyard_repair_boundary_command` applies the reviewed repository boundary to
a tenant that is already serving, without running the rest of its packet. As
root, and only for an operator authorized through pkexec, it reads root's own
installed plan without following a link, requires a trusted owner and a plan it
can rebuild without changing what it installs, lifts the boundary phase out of
root's own operator packet and checks every line against the shapes a boundary
phase is made of. A boundary that is already closed is left alone. Otherwise the
run is journalled: a dry run shows the repair, an applied run executes each
statement, stops at the first failure without rolling back, and reports success
only when the boundary reads as closed afterwards (SYRD-175).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-359). The launcher
imports this module at its top and re-exports the command, so the CLI and the
suites call the same object. Every launcher facility it uses -- the slug check,
root's provisioning record, plan reader and owner identity, the resume source
and plan, the root-controlled path walk and the boundary detector -- is read
from `team_launcher` when it runs; the provisioning and rollout-journal imports
stay inside the command. The standard-library names are this module's own
imports, the same objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable


def switchyard_repair_boundary_command(
    slug: str,
    *,
    apply: bool = False,
    euid_getter: Callable[[], int] = os.geteuid,
    operator_resolver: Callable[[], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    boundary_reader: Callable[["ProjectBoardProvision"], list[str]] | None = None,
    journal: Any | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Apply the reviewed repository boundary to a tenant that is already running.

    The generated packet carries this repair, and running the packet is not an
    option for a tenant that is serving: it deploys a release, replays the
    schema, seeds a workflow, installs and reloads units and starts sessions.
    A healthy tenant needs none of that and must not have it.

    So the phase is lifted out of root's own installed packet, between the
    markers the packet writes around it, and every line is checked against the
    shapes a boundary phase is made of before anything runs. Nothing is
    re-rendered here and nothing is read from the tenant: the commands are the
    bytes root installed, and the paths in them are root's (SYRD-175).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        repository_boundary_phase,
        repository_boundary_statements,
    )
    from scripts.ticket_board.rollout_journal import Attempt, resolve_operator

    slug = launcher._validate_project_slug(slug)
    said: list[str] = []

    def say(line: str) -> None:
        said.append(line)
        print_func(line)

    if euid_getter() != 0:
        print_func(
            f"switchyard: repairing {slug}'s repository boundary changes ACLs and modes on "
            f"root-owned surfaces. Run: pkexec switchyard repair-boundary {slug}"
            + (" --apply" if apply else "")
        )
        return 1
    operator = (operator_resolver or resolve_operator)()
    if getattr(operator, "source", "") != "pkexec" or not getattr(operator, "known", False):
        print_func(
            f"switchyard: this repair is an operator's decision and has to be authorized as "
            f"one. This run was elevated by "
            f"{getattr(operator, 'source', None) or 'nothing that names a person'}, so there "
            f"is nobody to record it against. Run: pkexec switchyard repair-boundary {slug}"
        )
        return 1

    baseline = launcher.privileged_baseline_plan_path(slug)
    if launcher.partial_provision_record(slug) is None:
        print_func(
            f"switchyard: root holds no provisioning record for {slug} at {baseline}, so there "
            "is no boundary of its to repair."
        )
        return 1
    document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
    if document is None:
        print_func(f"switchyard: {problem}")
        return 1
    identity = launcher.trusted_owner_identity(slug)
    if not identity.trusted:
        for objection in identity.problems:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to repair {slug}: root cannot establish whose installation "
            "this is. Nothing was changed."
        )
        return 1
    recorded_release = str(document.data.get("source_repo") or "").strip()
    selected = Path(recorded_release) if recorded_release else None
    if selected is None:
        selected, release_problem = launcher._resume_source_release(None)
        if release_problem:
            print_func(f"switchyard: {release_problem}")
            return 1
    plan, divergence = launcher._resume_plan_from_record(document, identity, source_repo=selected)
    if divergence:
        for objection in divergence:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to repair {slug}: root cannot rebuild its plan without "
            "changing what it installs. Nothing was changed."
        )
        return 1

    packet_path = baseline.with_name("operator-commands.sh")
    walk = launcher.root_controlled_problems_for(str(packet_path))
    if walk:
        for objection in walk:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to take a repair from a packet root does not control. "
            "Nothing was changed."
        )
        return 1
    try:
        packet = packet_path.read_text(encoding="utf-8")
    except OSError as exc:
        print_func(f"switchyard: {packet_path} could not be read: {exc}")
        return 1
    phase, phase_problem = repository_boundary_phase(packet)
    if phase_problem or not phase:
        print_func(f"switchyard: {phase_problem or 'that packet carries no boundary phase'}")
        print_func(
            f"switchyard: {packet_path} is not a packet this can repair from. Regenerate it "
            f"with `sudo switchyard upgrade {slug}` (or `resume-provision {slug}` for a tenant "
            "that never finished) and run this again. Nothing was changed."
        )
        return 1

    detect = boundary_reader or (lambda p: launcher.repository_boundary_problems(p, runner=runner))
    open_before = detect(plan)
    if not open_before:
        print_func(f"switchyard: {slug}'s repository boundary is already closed; nothing to do.")
        return 0

    attempt = journal or Attempt(
        slug,
        ["switchyard", "repair-boundary", slug, *(["--apply"] if apply else [])],
        operator=str(getattr(operator, "name", "") or ""),
    )
    attempt.operator = operator
    attempt.open()
    status, exit_status, detail = "failed", 1, ""
    try:
        say(f"switchyard: {slug}'s repository boundary is open:")
        for objection in open_before:
            say(f"    {objection}")
        say(f"switchyard: the repair, taken from {packet_path}:")
        for line in phase:
            say(f"    {line}")
        if not apply:
            say(
                f"switchyard: dry run; nothing was changed. Apply it with "
                f"`pkexec switchyard repair-boundary {slug} --apply`."
            )
            status, exit_status, detail = "completed", 0, "dry-run"
            return 0

        for statement in repository_boundary_statements(phase):
            script = "\n".join(statement)
            done = runner(["sh", "-c", script], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
            output = (str(getattr(done, "stdout", "") or "") + str(getattr(done, "stderr", "") or "")).strip()
            if output:
                say(f"    {output}")
            if getattr(done, "returncode", 1) != 0:
                say(f"switchyard: {statement[0].strip()} failed with exit {done.returncode}.")
                say(
                    "switchyard: the repair stopped there. What ran before it stands; running "
                    "this again resumes from what is still open."
                )
                detail = "failed mid-phase"
                return 1

        open_after = detect(plan)
        if open_after:
            for objection in open_after:
                say(f"switchyard: still open after the repair: {objection}")
            say(
                "switchyard: the repair ran and the boundary is not closed, so this does not "
                "report success."
            )
            detail = "still open"
            return 1
        say(
            f"switchyard: {slug}'s repository boundary is closed "
            f"(repaired by {operator.name} via {operator.source})"
        )
        say(
            "switchyard: nothing else was touched -- no deploy, no schema, no units, no "
            "sessions. The board service keeps its socket group, its commit store and its "
            "board release."
        )
        status, exit_status, detail = "completed", 0, "repaired"
        return 0
    finally:
        attempt.write("stdout", "\n".join(said) + "\n")
        attempt.close(status=status, exit_status=exit_status, detail=detail)
