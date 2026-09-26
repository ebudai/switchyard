"""Making an agent CLI that only the caller has into one every project account can run.

When a role's CLI is only on the caller's own PATH (see
`scripts/agent_cli_discovery.py`), a project's owner account cannot run it.
Switchyard can promote it: copy it to the host-wide bin directory, as root,
through the root-owned promoter, with the change journaled and rolled back on
failure. This module covers:
- **Policy and sources.** The policy a new tenant chooses (require host-wide,
  or promote a local copy), and the `--agent-cli-source` it may name.
- **Validating a source.** A promotable source must be self-contained: no
  launcher script whose interpreter or dependencies a stranger account cannot
  reach.
- **Promotion itself.** Checked afterwards by running the promoted CLI's
  version in the tenant's context.
- **The gates.** The offer before launch, and the requirement and the owner
  verification during `switchyard new`.
- **Upgrade.** Refreshing the CLIs recorded for registered tenants.

Vendor install commands are never run. The install table and the text built
from it stay in `scripts/team_launcher.py`, where
`team_launcher_missing_cli_install_hint_test` checks that nothing that reads
them can execute. This module reaches `host_wide_install_instruction` through
the launcher.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-295). It stands on
`scripts/agent_cli_discovery.py`, imported by name, and that module never
imports this one. `team_launcher` imports this module at its top and still
exports every name callers read there. This module never imports
`team_launcher` at its top. Launcher facilities (`_run_owner_cli_probe`,
`current_user_name`, `_repo_root`, `switchyard_registry_dir`,
`untrusted_root_executable_reasons`, ...) are read from
`scripts.team_launcher` when a function runs.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from scripts.agent_cli_discovery import (
    AGENT_CLI_SCOPE_CALLER_ONLY,
    AgentCliAvailability,
    AgentCliUnavailable,
    InvokingAccount,
    agent_cli_binary,
    agent_cli_scope_explanation,
    caller_aware_which,
    classify_agent_cli,
    classify_selected_agent_clis,
)


#: Predeclared answers for a run with nobody to ask. Required rather than
#: defaulted: every value here decides something a human would otherwise be
#: shown, and picking one silently is how an unattended run ends up having
#: installed software nobody asked for -- or having created half a tenant.
AGENT_CLI_POLICY_REQUIRE_HOST_WIDE = "require-host-wide"


AGENT_CLI_POLICY_PROMOTE_LOCAL = "promote-local"


AGENT_CLI_POLICIES = (AGENT_CLI_POLICY_REQUIRE_HOST_WIDE, AGENT_CLI_POLICY_PROMOTE_LOCAL)


def _agent_cli_blocking_report(
    blocked: dict[str, AgentCliAvailability], owner_user: str
) -> list[str]:
    lines = [
        f"switchyard: cannot provision {owner_user or 'this tenant'} with the selected CLIs; "
        "nothing has been created yet",
    ]
    for availability in blocked.values():
        lines.append(f"switchyard:   {agent_cli_scope_explanation(availability, owner_user)}")
    return lines


def _parse_agent_cli_sources(values: Sequence[str] | None) -> dict[str, str]:
    """`--agent-cli-source cli=path`, rejected early rather than half-read."""
    parsed: dict[str, str] = {}
    for raw in values or ():
        cli, separator, path = str(raw).partition("=")
        cli, path = cli.strip(), path.strip()
        if not separator or not cli or not path:
            raise AgentCliUnavailable(
                f"switchyard: --agent-cli-source expects <cli>=<path>, got {raw!r}"
            )
        parsed[cli] = path
    return parsed


def _promote_or_report(
    cli: str,
    source: str,
    *,
    promoter: Callable[..., AgentCliAvailability] | None,
    which: Callable[..., str | None],
    print_func: Callable[[str], None],
) -> AgentCliAvailability:
    """One promotion, through the seam tests and hosts can replace."""
    promote = promoter or promote_agent_cli_host_wide
    return promote(cli, source, which=which, print_func=print_func)


#: The privileged half of a promotion, in the shared release rather than in this
#: checkout: root should execute root-owned bytes it was installed with.
AGENT_CLI_PROMOTER_NAME = "switchyard-promote-agent-cli"


#: The journal label a promotion is recorded under.
AGENT_CLI_PROMOTION_LABEL = "agent-cli-promotion"


def _rollout_recorder_path_or_none() -> Path | None:
    """The recorder root should execute, without falling back to a checkout.

    `_rollout_recorder_path` accepts this checkout's copy so an operator packet
    can be rendered anywhere. A privileged mutation is a different question: the
    program root runs has to be one root was installed with.
    """
    from scripts import team_launcher as launcher

    installed = (
        launcher.switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    )
    return installed if installed.is_file() else None


def agent_cli_promoter_path() -> Path:
    """Where the privileged promoter lives once a release is installed."""
    from scripts import team_launcher as launcher

    installed = launcher.switchyard_shared_install_root() / "current" / "scripts" / AGENT_CLI_PROMOTER_NAME
    if installed.is_file():
        return installed
    return launcher._repo_root() / "scripts" / AGENT_CLI_PROMOTER_NAME


def _host_wide_agent_cli_state(
    cli: str, *, which: Callable[..., str | None]
) -> tuple[object, ...] | None:
    """What the host-wide copy of this CLI is right now, or None if there is none.

    Enough to tell "the same file" from "a different one" across a privileged
    step: the path it resolves to, the inode it lands on, its size and its
    mtime. Read rather than assumed, because the whole point is to stop
    describing a filesystem nobody looked at.
    """
    from scripts import team_launcher as launcher

    path = which(agent_cli_binary(cli), path=launcher.DEFAULT_PANE_BASE_PATH)
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def promote_agent_cli_through_sudo(
    cli: str,
    source: str | Path,
    *,
    project: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    which: Callable[..., str | None] = caller_aware_which,
    print_func: Callable[[str], None] = print,
    sudo_bin: str = "",
    helper: Path | None = None,
    recorder_path: Path | None = None,
    helper_owner_uid: int | None = None,
    helper_boundary: Path | None = None,
) -> AgentCliAvailability:
    """Promote from an UNPRIVILEGED process, across one explicit boundary.

    `promote_agent_cli_host_wide` chowns the staged file to root from inside
    this process. Its only caller was `switchyard new`, which already runs as
    root, so that worked there and nowhere else -- a resumed tenant is launched
    by an ordinary `switchyard <project>`, and accepting the promotion offer
    there failed with EPERM before anything could be installed (SYRD-211 DAT
    rejection).

    So the privileged half is a separate root-owned program and this is the
    crossing. Only the CLI name and the source path cross it; the destination,
    the ownership, the mode and the account the result is verified as are all
    decided on the far side, from root-owned bytes, which is what keeps this
    from being a way to have root copy an arbitrary file anywhere.

    The whole path to that program is pinned before root is asked to run it: a
    root-owned program in a directory somebody else can write is a program
    somebody else can replace, and sudo names a path (SYRD-62).
    """
    from scripts import team_launcher as launcher

    # Locally first, so an obvious mistake is a clear message rather than a
    # password prompt followed by one.
    resolved = resolve_agent_cli_source(cli, source)
    if not project:
        # The journal entry is opened against a project, and a privileged host
        # mutation that cannot be journalled must not happen at all.
        raise AgentCliUnavailable(
            f"switchyard: refusing to promote {cli} without naming the tenant it is for; "
            "the promotion is recorded against that tenant"
        )
    program = Path(helper) if helper is not None else agent_cli_promoter_path()
    recorder = Path(recorder_path) if recorder_path is not None else _rollout_recorder_path_or_none()
    if recorder is None or not recorder.is_file():
        raise AgentCliUnavailable(
            f"switchyard: refusing to promote {cli}: the rollout recorder is not installed on this "
            "host, and a privileged host mutation is not made without a journal"
        )
    pinned: dict[str, Path] = {}
    for candidate, what in ((recorder, "rollout recorder"), (program, "promoter")):
        if not candidate.is_file():
            raise AgentCliUnavailable(
                f"switchyard: cannot promote {cli}: {candidate} is not installed on this host"
            )
        # Where it really lands. A release is reached through `current`, which
        # is a symlink -- one that lives in a root-owned directory, so only root
        # can repoint it. Refusing every symlink outright would refuse the real
        # installed recorder; what has to be root's the whole way is the path it
        # resolves to, and that is what gets walked.
        candidate = Path(os.path.realpath(candidate))
        pinned[what] = candidate
        # Root, and never "whoever is asking". `helper_owner_uid` and
        # `helper_boundary` are a matched pair and exist only for a sandbox,
        # which can neither own a file as root nor place one under a chain of
        # root-owned directories. Production passes neither; a dedicated case
        # pins that default from an unprivileged caller.
        entitled = launcher.TENANT_CONTROL_OWNER_UID if helper_owner_uid is None else helper_owner_uid
        reasons = launcher.untrusted_root_executable_reasons(
            candidate, owner_uid=entitled, boundary=helper_boundary
        )
        if reasons:
            raise AgentCliUnavailable(
                f"switchyard: refusing to run the {what} {candidate} as root: {reasons[0]}"
            )
    recorder = pinned["rollout recorder"]
    program = pinned["promoter"]
    sudo = sudo_bin or os.environ.get("SWITCHYARD_SUDO_BIN", "") or "sudo"
    print_func(
        f"switchyard: promoting {cli} needs root, so this asks sudo to run {program} through "
        f"{recorder.name}; only the CLI name, that path and {project} cross"
    )
    # THROUGH the recorder, not beside it. The attempt is opened before the
    # promoter starts, and whatever it prints and returns is what the journal
    # keeps -- so there is no path where /usr/local/bin changed and nothing
    # recorded it, and no second interpretation of the recorder's own exit
    # status to get wrong (SYRD-211 DAT rejection 4).
    command = [
        sudo, str(recorder), project,
        "--label", AGENT_CLI_PROMOTION_LABEL,
        "--",
        str(program), cli, str(resolved), "--project", project,
    ]
    # Looked at before and after, because a nonzero exit from the WRAPPER does
    # not say where it failed. The recorder can start the promoter, the promoter
    # can replace the CLI, and the recorder can then fail closing its journal --
    # at which point the host has changed and a flat "nothing was installed"
    # is false (SYRD-211 DAT rejection 5).
    before = _host_wide_agent_cli_state(cli, which=which)
    result = runner(command)
    code = int(getattr(result, "returncode", 1) or 0)
    if code != 0:
        after = _host_wide_agent_cli_state(cli, which=which)
        if after == before:
            raise AgentCliUnavailable(
                f"switchyard: promoting {cli} failed (exit {code}); no host-wide copy was "
                "installed and any existing one is untouched. The attempt is in the rollout "
                "journal"
            )
        where = after[0] if after else "nowhere it resolves from"
        raise AgentCliUnavailable(
            f"switchyard: promoting {cli} failed (exit {code}), but the host-wide {cli} "
            f"CHANGED while it ran: it is now {where}. The promotion itself may have "
            "completed and the journal may not have, so check that copy and the rollout "
            "journal before relying on either"
        )
    verdict = classify_agent_cli(cli, which=which)
    if not verdict.serves_a_new_owner:
        raise AgentCliUnavailable(
            f"switchyard: {cli} reported promoted but still does not resolve on the base PATH "
            "every pane is given"
        )
    print_func(f"switchyard: {cli} is now host-wide at {verdict.host_wide_path}")
    return verdict


def registered_tenant_agent_clis(
    project: str, *, registry_dir: Path | None = None
) -> frozenset[str] | None:
    """Which agent CLIs this tenant's roles are configured with, or None.

    Read from the project's registry entry: root-owned, world-readable, one
    file per project, and already the record this unprivileged side consults
    before it crosses the tenant control boundary. The tenant's own
    configuration lives under the owner's home and stays unreadable from here,
    which is the boundary working -- so the selection it describes is recorded
    out here when root registers the project, rather than guessed at launch.

    `None` means no selection is recorded, which is not the same as an empty
    one: a tenant registered before this record existed says nothing about its
    CLIs, and a caller must not read silence as "uses none of them" any more
    than SYRD-211 could read it as "uses all of them" (SYRD-220).
    """
    from scripts import team_launcher as launcher

    entry_path = (registry_dir or launcher.switchyard_registry_dir()) / f"{project}.json"
    try:
        raw = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or str(raw.get("schema") or "") != launcher.SWITCHYARD_REGISTRY_SCHEMA:
        return None
    recorded = raw.get(launcher.SWITCHYARD_REGISTRY_AGENT_CLIS_KEY)
    if not isinstance(recorded, list):
        return None
    return frozenset(str(cli).strip() for cli in recorded if str(cli).strip())


def resolvable_agent_cli_promotions(
    *,
    which: Callable[..., str | None] = caller_aware_which,
    selected: Collection[str] | None = None,
) -> list[AgentCliAvailability]:
    """Agent CLIs this operator has privately and no tenant owner can reach.

    Deliberately asked of the OPERATOR's context and not of the tenant's, and
    deliberately before the tenant control boundary is crossed. Past it the
    launcher runs as the owner with a built PATH, so the operator's own copies
    are invisible there -- which is the whole of why a resumed tenant printed
    per-owner vendor instructions instead of offering the promotion SYRD-210
    exists to offer (SYRD-211 second kickback).

    A resumed tenant's selection lives in its configuration, under the owner's
    home, which this unprivileged account cannot read -- that is the boundary
    working. So the question asked here is the one that CAN be answered from
    outside: which agent CLIs exist for this operator alone. Anything already
    host-wide is not offered, because there is nothing to fix.
    """
    from scripts import team_launcher as launcher

    offers: list[AgentCliAvailability] = []
    for cli in sorted(launcher.FIRST_RUN_AUTH_STATUS_COMMANDS):
        if selected is not None and cli not in selected:
            # Not this tenant's. Promoting it would install something on the
            # host for a tenant that will never run it, and asking about it is
            # a question whose only useful answer is "no" -- which is what the
            # `test` tenant was asked, every launch (SYRD-220).
            continue
        verdict = classify_agent_cli(cli, which=which)
        if verdict.scope == AGENT_CLI_SCOPE_CALLER_ONLY and verdict.caller_path:
            offers.append(verdict)
    return offers


def offer_host_wide_promotion_before_launch(
    project: str,
    *,
    policy: str = "",
    sources: Mapping[str, str] | None = None,
    interactive: bool = True,
    which: Callable[..., str | None] = caller_aware_which,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    promoter: Callable[..., AgentCliAvailability] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    registry_dir: Path | None = None,
) -> list[str]:
    """Offer, before crossing the bridge. Never block the launch.

    The difference from `require_agent_clis_for_new_tenant` is the word
    "offer". There, nothing exists yet and provisioning a tenant that cannot run
    its own CLI is pointless, so an unsettled CLI stops the run. Here the tenant
    already exists, this is a resume, and declining has to leave the launch
    exactly as it was -- refusing to start somebody's tenant because they keep a
    private copy of a CLI it may not even use would be a worse bug than the one
    this fixes.

    Returns the CLIs promoted, for the caller to report.
    """
    from scripts import team_launcher as launcher

    selected = registered_tenant_agent_clis(project, registry_dir=registry_dir)
    #: Registered before the selection was recorded. Nothing out here can say
    #: which CLIs such a tenant uses, and SYRD-211's answer -- offer every
    #: caller-only CLI on the host -- is what asked the `test` tenant to promote
    #: a Hermes no role of its uses, at every launch. So nothing is offered and
    #: nothing is promoted; what a CLI cannot do is still diagnosed below, and a
    #: role that genuinely needs one is still named by the launch itself past
    #: the bridge, where the configuration can be read (SYRD-220).
    unrecorded_selection = selected is None
    offers = resolvable_agent_cli_promotions(which=which, selected=selected)
    if not offers:
        return []
    # A resume runs unprivileged, so the default here is the boundary-crossing
    # promoter and NOT `promote_agent_cli_host_wide`, which chowns to root from
    # inside this process and can only work where the caller is already root
    # (SYRD-211 DAT rejection).
    promote = promoter or promote_agent_cli_through_sudo
    declared = dict(sources or {})
    promoted: list[str] = []
    for verdict in offers:
        chosen = declared.get(verdict.cli, "").strip()
        if not chosen:
            if not interactive:
                # Unattended runs never guess and never prompt. A resume is not
                # the place to make a host-wide installation decision on
                # somebody's behalf, so this says what is available and goes on
                # to launch.
                unpromotable = agent_cli_detected_path_problems(verdict.cli, verdict.caller_path)
                if unpromotable:
                    for line in unpromotable:
                        print_func(line)
                    continue
                if unrecorded_selection:
                    # Unattended, so not even a line to read: this run makes no
                    # decision and leaves the launch as it was (SYRD-220).
                    continue
                if policy != AGENT_CLI_POLICY_PROMOTE_LOCAL:
                    print_func(
                        f"switchyard: {verdict.cli} is installed at {verdict.caller_path}, which "
                        f"only {launcher.current_user_name()} can reach; {project}'s owner cannot. Promote "
                        f"it with --agent-cli-policy promote-local "
                        f"--agent-cli-source {verdict.cli}={verdict.caller_path}"
                    )
                    continue
                chosen = verdict.caller_path
            else:
                print_func(
                    f"switchyard: {verdict.cli} is installed at {verdict.caller_path}, which only "
                    f"{launcher.current_user_name()} can reach. {project} runs as its own owner account, "
                    "which does not inherit it, and no later tenant would either"
                )
                unpromotable = agent_cli_detected_path_problems(verdict.cli, verdict.caller_path)
                if unpromotable:
                    # Not offered at all. There is nothing here an answer could
                    # fix: the artifact cannot serve a tenant whoever promotes
                    # it, so the honest move is to say why and leave the host
                    # and the running tenant alone (SYRD-210).
                    for line in unpromotable:
                        print_func(line)
                    print_func(
                        f"switchyard: install a self-contained {verdict.cli} host-wide, or give "
                        f"{project}'s roles a CLI that is already host-wide; this launch "
                        "continues unchanged"
                    )
                    continue
                if unrecorded_selection:
                    # Said rather than asked: starting a tenant is not the
                    # moment to make somebody guess whether it uses this
                    # (SYRD-220).
                    print_func(
                        f"switchyard: not offering to promote {verdict.cli} for {project}: which "
                        f"agent CLIs its roles use is not recorded. `sudo switchyard upgrade "
                        f"{project}` records it, and then a CLI this tenant needs is offered here"
                    )
                    continue
                print_func(
                    f"switchyard:   [p] promote {verdict.caller_path} to a root-owned host-wide "
                    "copy, reused by every later project; no credentials, config or session "
                    "travel with it"
                )
                print_func(
                    "switchyard:   [s] skip; launch without it, and leave this host unchanged"
                )
                answer = launcher._read_prompt(
                    f"Promote {verdict.cli} host-wide? [p/s] (p): ", input_func=input_func
                ).strip().casefold()
                if answer in {"s", "skip"}:
                    continue
                if answer not in {"", "p", "promote"}:
                    print_func(
                        f"switchyard: {answer!r} is not one of the choices for {verdict.cli}; "
                        "skipping it and launching unchanged"
                    )
                    continue
                chosen = verdict.caller_path
        try:
            result = promote(
                verdict.cli, chosen, which=which, print_func=print_func, runner=runner,
                project=project,
            )
        except AgentCliUnavailable as exc:
            # Offered, not required: a promotion that could not happen must not
            # stop a tenant that already exists from starting.
            print_func(str(exc))
            continue
        if result.serves_a_new_owner:
            promoted.append(verdict.cli)
    return promoted


def require_agent_clis_for_new_tenant(
    role_clis: Sequence[tuple[str, str]],
    *,
    owner_user: str,
    policy: str = "",
    interactive: bool = True,
    sources: Mapping[str, str] | None = None,
    which: Callable[..., str | None] = caller_aware_which,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    promoter: Callable[..., AgentCliAvailability] | None = None,
    account: InvokingAccount | None = None,
) -> Sequence[tuple[str, str]]:
    """Settle every selected CLI BEFORE anything is created.

    Returns the selection to provision with, which may differ from the one
    passed in if the operator chose a different CLI for the affected roles.

    The whole point is the ordering. This used to be discovered at the first-run
    manifest, after the owner account, the project directory and the board
    already existed, and the remedy printed there was a vendor installer that
    installs for whoever runs it -- so an operator who followed it exactly
    installed the CLI into their own account and the tenant still could not use
    it (SYRD-210).
    """
    from scripts import team_launcher as launcher

    selection = list(role_clis)
    availability = classify_selected_agent_clis(selection, which=which, account=account)
    blocked = {
        cli: verdict for cli, verdict in availability.items() if not verdict.serves_a_new_owner
    }
    if not blocked:
        for verdict in availability.values():
            print_func(
                f"switchyard: {verdict.cli} is host-wide at {verdict.host_wide_path}; "
                f"{owner_user} and later tenants use it without a per-owner install"
            )
        return tuple(selection)

    report = _agent_cli_blocking_report(blocked, owner_user)

    declared = dict(sources or {})

    if not interactive:
        # A policy alone is not an answer here. `promote-local` says WHAT may
        # happen; it cannot say which executable to promote, and guessing one on
        # an unattended host is exactly the kind of decision this refuses to make
        # for somebody. So the source is required too, and its absence is a
        # refusal before anything exists rather than a prompt nobody can answer.
        if policy != AGENT_CLI_POLICY_PROMOTE_LOCAL:
            if policy and policy != AGENT_CLI_POLICY_REQUIRE_HOST_WIDE:
                raise AgentCliUnavailable(
                    f"switchyard: unknown --agent-cli-policy {policy!r}; "
                    f"choose one of {', '.join(AGENT_CLI_POLICIES)}"
                )
            hint = (
                "switchyard: choose CLIs that are already host-wide with --role-cli, or re-run "
                "with --agent-cli-policy promote-local and --agent-cli-source <cli>=<path> for "
                "each one. Switchyard never fetches a vendor installer (PGU-904)"
            )
            raise AgentCliUnavailable("\n".join([*report, hint]))

        missing_sources = [cli for cli in blocked if not declared.get(cli)]
        if missing_sources:
            offer = ", ".join(
                f"--agent-cli-source {cli}={blocked[cli].caller_path or '<path>'}"
                for cli in missing_sources
            )
            raise AgentCliUnavailable(
                "\n".join(
                    [
                        *report,
                        f"switchyard: promote-local needs a source for {', '.join(missing_sources)}; "
                        f"add {offer}",
                    ]
                )
            )
        for line in report:
            print_func(line)
        for cli in list(blocked):
            _promote_or_report(
                cli, declared[cli], promoter=promoter, which=which, print_func=print_func
            )
        return tuple(selection)

    for line in report:
        print_func(line)

    for cli, verdict in list(blocked.items()):
        alternatives = [
            name
            for name in sorted(launcher.FIRST_RUN_AUTH_STATUS_COMMANDS)
            if name != cli
            and classify_agent_cli(name, which=which, account=account).serves_a_new_owner
        ]
        # The executable we already found is the obvious source, and offering it
        # by default is the difference between "install this somehow" and one
        # keystroke. Nothing is fetched: promoting a copy that is already on this
        # machine keeps PGU-904's boundary intact and leaves the operator owning
        # which version every tenant runs (SYRD-210).
        detected = verdict.caller_path
        # Checked before it is offered, not after root has been asked for.
        unpromotable = agent_cli_detected_path_problems(cli, detected)
        if unpromotable:
            for line in unpromotable:
                print_func(line)
            detected = ""
        while True:
            print_func(f"switchyard: choose how to proceed for {cli}:")
            if detected:
                print_func(
                    f"switchyard:   [p] promote {detected} to a root-owned host-wide copy, "
                    "reused by every later project; no credentials, config or session travel "
                    "with it"
                )
            print_func(
                "switchyard:   [l] promote a local executable you name instead"
            )
            if alternatives:
                print_func(
                    "switchyard:   [s] switch the affected roles to a CLI that is already "
                    f"host-wide ({', '.join(alternatives)})"
                )
            print_func("switchyard:   [a] abort; nothing has been created and nothing will be")
            default = "p" if detected else "l"
            answer = (
                input_func(f"Choice [{'p/' if detected else ''}l/s/a] ({default}): ") or default
            ).strip().casefold()

            if answer in ("a", "abort"):
                raise AgentCliUnavailable(
                    "switchyard: aborted before creating anything; no tenant residue to clean up"
                )
            if answer in ("p", "promote") and detected:
                _promote_or_report(
                    cli, detected, promoter=promoter, which=which, print_func=print_func
                )
                break
            if answer in ("l", "local"):
                # Prefilled with what was actually found, so choosing [l] over
                # [p] is still one keystroke and typing a path stays available
                # for an unusual alternate copy (SYRD-236).
                suggestion = f" [{detected}]" if detected else ""
                offered = (
                    input_func(f"Path to a {cli} executable{suggestion}: ") or detected
                ).strip()
                if not offered:
                    print_func("switchyard: no path given")
                    continue
                try:
                    _promote_or_report(
                        cli, offered, promoter=promoter, which=which, print_func=print_func
                    )
                except AgentCliSourceRejected as exc:
                    print_func(str(exc))
                    continue
                break
            if answer in ("s", "switch") and alternatives:
                replacement = (
                    input_func(f"Replacement CLI [{alternatives[0]}]: ") or alternatives[0]
                ).strip()
                if replacement not in alternatives:
                    print_func(
                        f"switchyard: {replacement or '(empty)'} is not host-wide; "
                        f"choose one of {', '.join(alternatives)}"
                    )
                    continue
                affected = [role for role, name in selection if name == cli]
                selection = [
                    (role, replacement if name == cli else name) for role, name in selection
                ]
                print_func(
                    f"switchyard: {', '.join(affected)} now use {replacement} instead of {cli}"
                )
                break
            print_func(
                "switchyard: answer " + ("p, " if detected else "") + "l, s or a"
            )

    return tuple(selection)


@dataclass(frozen=True)
class OwnerCliVerification:
    """What the owner account actually resolves and runs, after it exists."""

    cli: str
    owner_user: str
    path: str = ""
    version: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.path)


def verify_agent_clis_for_owner(
    role_clis: Sequence[tuple[str, str]],
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[OwnerCliVerification]:
    """Ask the generated owner what it will actually run, and say so.

    The precheck answers a question about the host before the owner exists;
    this answers the only question that finally matters -- what THIS account
    resolves -- and it can only be asked once the account is real. They can
    disagree: a host-wide CLI is what the precheck required, but the owner's own
    PATH is searched first, so an owner-local copy would win at pane launch and
    nothing else would ever mention it.

    Reported rather than merely checked, because "which binary is my tenant
    running" is the question a version mismatch makes someone ask days later
    (SYRD-210).
    """
    from scripts import team_launcher as launcher

    results: list[OwnerCliVerification] = []
    for cli in dict.fromkeys(cli for _role, cli in role_clis):
        binary = agent_cli_binary(cli)
        located = launcher._run_owner_cli_probe(
            owner_user=owner_user,
            owner_home=owner_home,
            command=["sh", "-c", f"command -v {shlex.quote(binary)}"],
            runner=runner,
        )
        path = (located.stdout or "").strip().splitlines()
        resolved = path[0] if path and located.returncode == 0 else ""
        version = ""
        if resolved:
            probe = launcher._run_owner_cli_probe(
                owner_user=owner_user,
                owner_home=owner_home,
                command=[binary, "--version"],
                runner=runner,
            )
            if probe.returncode == 0:
                lines = (probe.stdout or "").strip().splitlines()
                version = lines[0].strip() if lines else ""
        results.append(
            OwnerCliVerification(cli=cli, owner_user=owner_user, path=resolved, version=version)
        )
        if resolved:
            suffix = f" ({version})" if version else " (version not reported)"
            print_func(f"switchyard: {owner_user} runs {cli} from {resolved}{suffix}")
        else:
            print_func(
                f"switchyard: {owner_user} cannot resolve {cli}; its panes would fail to start. "
                f"{launcher.host_wide_install_instruction(cli)}"
            )
    return results


#: Where a promoted agent CLI lands. On DEFAULT_PANE_BASE_PATH, so every tenant
#: resolves it, and root-owned, so no tenant can rewrite what every tenant runs.
AGENT_CLI_HOST_WIDE_BIN = Path("/usr/local/bin")


class AgentCliSourceRejected(AgentCliUnavailable):
    """The offered local artifact is not something to put on every tenant's PATH."""


#: How much of a candidate executable is read to see what it depends on. A
#: launcher script is a few lines; anything longer is not one.
AGENT_CLI_SCRIPT_SAMPLE_BYTES = 65536


def _reachable_by_a_stranger(path: Path) -> str:
    """Why an account that owns nothing here could not use `path`, or ''.

    Read from the permission bits rather than by trying it. Trying it answers
    for whoever is asking, and the two accounts that do the asking are the two
    that cannot see the problem: the operator, who owns the home in question,
    and root, who bypasses the check entirely. A future tenant is neither, and
    it does not exist yet to be asked (SYRD-210).
    """
    for parent in reversed(path.parents):
        try:
            mode = parent.stat().st_mode
        except OSError as exc:
            return f"{parent} cannot be examined: {exc}"
        if not mode & stat.S_IXOTH:
            return f"{parent} is not searchable by other accounts"
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return f"{path} cannot be examined: {exc}"
    if not mode & stat.S_IROTH:
        return f"{path} is not readable by other accounts"
    return ""


def agent_cli_unreachable_dependencies(executable: Path) -> list[tuple[Path, str]]:
    """The files this artifact needs that a future tenant could not reach.

    A per-user install is often a launcher: a few lines whose interpreter is a
    virtualenv inside the operator's own home. Copying the launcher host-wide
    copies the pointer, not the runtime, and every tenant then execs a path
    under an account it cannot enter -- exit 126, at somebody's first pane.
    Live on a fresh host: promoting `hermes` produced a copy still running
    `/home/santiago/.hermes/hermes-agent/venv/bin/python` (SYRD-210).

    Only text is inspected. A compiled executable's private shared libraries
    are a real version of the same hazard and are NOT detected here; what
    catches those is the verification the promotion now runs before installing
    anything.
    """
    try:
        head = executable.open("rb").read(AGENT_CLI_SCRIPT_SAMPLE_BYTES)
    except OSError:
        return []
    if not head.startswith(b"#!"):
        return []
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = head.decode("utf-8", "replace")
    candidates: list[Path] = []
    shebang = text.splitlines()[0][2:].strip().split()
    if shebang:
        interpreter = Path(shebang[0])
        # `/usr/bin/env X` depends on the PATH it is run with rather than on a
        # path here; that is the verification's question, not this one.
        if interpreter.name != "env":
            candidates.append(interpreter)
    for match in re.findall(r"(?<![\w-])/[A-Za-z0-9_./+-]{3,}", text):
        candidate = Path(match)
        if candidate not in candidates and candidate.exists():
            candidates.append(candidate)
    unreachable: list[tuple[Path, str]] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        reason = _reachable_by_a_stranger(candidate)
        if reason:
            unreachable.append((candidate, reason))
    return unreachable


def agent_cli_detected_path_problems(cli: str, caller_path: str | Path | None) -> list[str]:
    """Why the executable we found cannot be promoted, or nothing.

    Asked before it is offered. Offering a launcher as the one-keystroke
    default and discovering at verification that no tenant can run it costs an
    operator a sudo prompt and a failed provisioning to learn what its first
    two lines already said (SYRD-210).
    """
    if not caller_path:
        return []
    candidate = Path(caller_path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return []
    return agent_cli_source_is_self_contained(cli, resolved)


def agent_cli_source_is_self_contained(cli: str, resolved: Path) -> list[str]:
    """Empty when this artifact can serve a tenant, or why it cannot."""
    unreachable = agent_cli_unreachable_dependencies(resolved)
    if not unreachable:
        return []
    lines = [
        f"switchyard: {resolved} is a launcher, not a self-contained {cli}: it runs files "
        "that belong to the account it was installed for, and a tenant is a different "
        "account that cannot read them:",
    ]
    for path, reason in unreachable[:4]:
        lines.append(f"switchyard:   {path} -- {reason}")
    lines.append(
        f"switchyard: copying it host-wide would copy the pointer and not the runtime, and "
        f"every tenant would fail to exec it. Nothing has been created and nothing was "
        f"installed."
    )
    return lines


def resolve_agent_cli_source(cli: str, source: str | Path) -> Path:
    """The real executable behind an offered path, or a refusal saying why.

    Symlinks are followed to the file that will actually be copied. That matters
    here more than usual: the common source IS a link -- a per-user install in
    the operator's home -- and copying the link rather than its target would put
    a pointer into somebody's home directory on every tenant's PATH, which is
    the arrangement this ticket exists to end (SYRD-210).
    """
    offered = Path(source).expanduser()
    if not offered.exists():
        raise AgentCliSourceRejected(
            f"switchyard: no {cli} executable at {offered}; nothing has been created"
        )
    resolved = offered.resolve(strict=False)
    if resolved.is_dir():
        raise AgentCliSourceRejected(
            f"switchyard: {offered} is a directory; give the {cli} executable itself"
        )
    if not resolved.is_file():
        raise AgentCliSourceRejected(f"switchyard: {offered} is not a regular file")
    if not os.access(resolved, os.X_OK):
        raise AgentCliSourceRejected(f"switchyard: {resolved} is not executable")
    # Before sudo, deliberately. `promote_agent_cli_through_sudo` resolves here
    # first precisely so an answerable mistake is a sentence rather than a
    # password prompt followed by one -- and an artifact that cannot serve a
    # tenant is answerable now, by choosing another source or another CLI.
    not_self_contained = agent_cli_source_is_self_contained(cli, resolved)
    if not_self_contained:
        raise AgentCliSourceRejected("\n".join(not_self_contained))
    return resolved


def promote_agent_cli_host_wide(
    cli: str,
    source: str | Path,
    *,
    bin_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    which: Callable[..., str | None] = caller_aware_which,
    print_func: Callable[[str], None] = print,
    chown: Callable[[Any, int, int], None] = os.chown,
) -> AgentCliAvailability:
    """Copy one local executable to where every tenant can run it.

    A copy, never a link. The source is usually a per-user install in the
    operator's home, and a symlink would leave every tenant's PATH pointing into
    an account they cannot read and whose owner can replace the target.

    Atomic: written beside the destination and renamed onto it, so a tenant
    launching mid-promotion sees the old file or the new one and never a
    half-written one.

    Only the executable moves. No configuration, token, trust record or session
    file is read or copied -- those stay in the account that authenticates,
    which is created later and per tenant (SYRD-210).
    """
    destination_dir = Path(bin_dir) if bin_dir is not None else AGENT_CLI_HOST_WIDE_BIN
    resolved = resolve_agent_cli_source(cli, source)
    destination = destination_dir / cli
    destination_dir.mkdir(parents=True, exist_ok=True)

    staged = destination_dir / f".{cli}.switchyard-promote.{os.getpid()}"
    try:
        shutil.copyfile(resolved, staged)
        os.chmod(staged, 0o755)
        try:
            chown(staged, 0, 0)
        except PermissionError as exc:
            raise AgentCliUnavailable(
                f"switchyard: promoting {cli} needs root so the result is root-owned: {exc}"
            ) from exc
        # Run it BEFORE it is installed, not after. The copy was verified where
        # it had already replaced whatever was there, so a tenant that had a
        # working host-wide CLI lost it to a promotion that then failed -- the
        # failure and the damage were the same step. Verified while it is still
        # a staging file, an artifact that cannot run in a tenant's context
        # never becomes the one every tenant runs (SYRD-210).
        version = _agent_cli_version_in_tenant_context(staged, runner=runner)
        os.replace(staged, destination)
    except AgentCliUnavailable:
        _unlink_quietly(staged)
        raise
    except OSError as exc:
        _unlink_quietly(staged)
        raise AgentCliUnavailable(
            f"switchyard: could not install {cli} at {destination}: {exc}"
        ) from exc

    print_func(f"switchyard: promoted {resolved} to {destination}, owned by root")
    verdict = classify_agent_cli(cli, which=which)
    if not verdict.serves_a_new_owner:
        raise AgentCliUnavailable(
            f"switchyard: {cli} is installed at {destination} but does not resolve on the base "
            "PATH every pane is given; nothing has been created"
        )
    print_func(
        f"switchyard: {cli} verified in a tenant-owner context: {verdict.host_wide_path}"
        + (f" ({version})" if version else " (version not reported)")
    )
    return verdict


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _agent_cli_version_in_tenant_context(
    executable: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """Run it the way a tenant will, before any tenant exists.

    The owner account is created later, so it cannot be asked yet. What CAN be
    reproduced now is the context it will run in: an emptied environment, the
    base PATH every pane is given, and a HOME that is not the operator's -- so a
    binary that only works because of something in the promoting user's home
    fails here rather than at somebody's first pane launch.
    """
    from scripts import team_launcher as launcher

    home = Path(tempfile.mkdtemp(prefix="switchyard-cli-verify."))
    try:
        proc = runner(
            [str(executable), "--version"],
            cwd=str(home),
            env={"PATH": launcher.DEFAULT_PANE_BASE_PATH, "HOME": str(home)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise AgentCliUnavailable(
            f"switchyard: {executable} could not be executed in a tenant context: {exc}"
        ) from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip().splitlines()
        raise AgentCliUnavailable(
            f"switchyard: {executable} failed to run in a tenant context "
            f"(exit {proc.returncode}): {detail[-1] if detail else 'no output'}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def refresh_registered_agent_clis(
    config: "ProjectConfig",
    *,
    registry_dir: Path | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Record this tenant's CLI selection where a launch can read it.

    Registration writes it, but every tenant registered before it existed has a
    record that says nothing -- and a launch that cannot tell which CLIs a
    tenant uses cannot offer a promotion for one without guessing. There is no
    other moment: a launch runs unprivileged and this file is root's.

    Narrow on purpose. Only this one key is written, only when it differs from
    the configuration, and a record that is missing or is not a registry
    document is left exactly as found -- repairing one is not this command's
    decision (SYRD-220).
    """
    from scripts import team_launcher as launcher

    slug = str(config.project or "").strip()
    if not slug:
        return ["a project with no slug cannot have its CLI selection recorded"]
    registry_path = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{registry_path} cannot be read to record {slug}'s CLI selection: {exc}"]
    if not isinstance(raw, dict) or str(raw.get("schema") or "") != launcher.SWITCHYARD_REGISTRY_SCHEMA:
        return [f"{registry_path} is not a registry record; {slug}'s CLI selection was not recorded"]
    configured = _configured_agent_clis(config)
    if raw.get(launcher.SWITCHYARD_REGISTRY_AGENT_CLIS_KEY) == configured:
        return []
    if dry_run:
        print_func(
            f"switchyard: would record {slug}'s agent CLIs ({', '.join(configured) or 'none'}) "
            f"in {registry_path}"
        )
        return []
    raw[launcher.SWITCHYARD_REGISTRY_AGENT_CLIS_KEY] = configured
    try:
        registry_path.write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        registry_path.chmod(0o644)
    except OSError as exc:
        return [f"{slug}'s CLI selection could not be recorded in {registry_path}: {exc}"]
    print_func(
        f"switchyard: recorded {slug}'s agent CLIs ({', '.join(configured) or 'none'}) in "
        f"{registry_path}; a launch now offers a promotion only for those"
    )
    return []


def _configured_agent_clis(config: "ProjectConfig") -> list[str]:
    """The distinct agent CLIs this project's roles are configured with."""
    from scripts import team_launcher as launcher

    return sorted({
        name for role in config.roles if (name := launcher._role_cli_name(role))
    })
