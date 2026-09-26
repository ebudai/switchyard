"""The first-run authentication phase: what runs before any pane opens, and what it reports.

Before `switchyard new` or `switchyard <slug>` opens panes, every provider a
role uses has to be installed, signed in and trusting its workdir. This module
is the phase that sees to it, and the report it leaves:
- `run_first_run_auth_phase` builds the setup manifest, then runs its steps in
  order: each provider's own first run, then a single login per provider
  (re-reading the account first, so a setup that already signed in is not asked
  again), then folder trust. It returns a `FirstRunAuthReport`.
- `_run_owner_cli_until` runs one foreground step as the owner and ends it when
  it is finished; the `_*_instruction` helpers tell the person what the step
  is for.
- `NO_RUNNER_INJECTED` and `foreground_runner_for` keep "nobody injected a
  runner" apart from "somebody injected `subprocess.run`", which decides
  whether a person gets a watched setup window (SYRD-221).
- `report_first_run_auth_warnings` and
  `stop_before_launch_for_unauthenticated_providers` say what is still wrong,
  and stop a launch that cannot work; `_resumable_next_action` names how to
  resume it.

Running a step on a pty and owning the terminal is `scripts/provider_session.py`,
imported here directly. Probing auth state, trust and the manifest, the vendor
install table and its formatters, and the owner's environment stay in
`scripts/team_launcher.py`. This module reads them from there when a function
runs, so the suites' patches on the launcher reach it. The phase, the warning
report and `_run_owner_cli_until` are patched by the suites too, so this
module's own callers reach them through the launcher as well.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-300). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.provider_session import (
    FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
    SETUP_PURPOSE_FOLDER_TRUST,
    SETUP_PURPOSE_SIGN_IN,
)
from scripts.ticket_board import runtime_catalog

if TYPE_CHECKING:
    from scripts.team_launcher import (
        CodexHookTrustMismatch,
        GithubIdentityStatus,
        ModelValidationFailure,
        ProjectConfig,
    )


@dataclass(frozen=True)
class OwnerShellIssue:
    owner_user: str
    shell: str
    remedy: str


@dataclass(frozen=True)
class FirstRunAuthReport:
    unauthenticated_roles: dict[str, list[str]]
    untrusted_roles: list[tuple[str, str, str]]
    stale_codex_hook_trust: list[CodexHookTrustMismatch] = field(default_factory=list)
    missing_cli_roles: dict[str, list[str]] = field(default_factory=dict)
    #: Roles whose configured model is absent from their OWNER's own catalog,
    #: as (role, cli, model, available). Only ever populated from a list the
    #: owner's CLI actually produced; a recorded table never contradicts a
    #: configured value (SYRD-250).
    #:
    #: Deliberately absent from `has_warnings` and from
    #: `report_first_run_auth_warnings`: this one does not warn, it stops
    #: (`stop_before_launch_for_unknown_models`), and that gate says the whole
    #: thing where it is actionable. Counting it as a warning with no warning
    #: line to print would be a report claiming more than it shows.
    unknown_model_roles: list[tuple[str, str, str, tuple[str, ...]]] = field(default_factory=list)
    model_validation_failures: list[ModelValidationFailure] = field(default_factory=list)
    owner_user: str = ""
    owner_shell_issue: OwnerShellIssue | None = None
    github_identity: "GithubIdentityStatus | None" = None
    #: The CLIs this run logged in, and the roles configured to use each of
    #: them. A runtime that was already up when the login happened started
    #: before the provider state existed and cannot have picked it up: it is
    #: sitting on the provider's own sign-in screen, and presenting it is how
    #: five panes asked the User to authenticate again after two successful
    #: logins (SYRD-191).
    authenticated_now: dict[str, list[str]] = field(default_factory=dict)
    #: Providers whose account-wide first run was offered and is still not
    #: finished. Their roles will open it in the pane, so this is said rather
    #: than left to be discovered there.
    incomplete_provider_setup: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def roles_awaiting_restart(self) -> tuple[str, ...]:
        """Roles whose runtime predates the login this run performed."""
        names: list[str] = []
        for roles in self.authenticated_now.values():
            for role in roles:
                if role not in names:
                    names.append(role)
        return tuple(names)

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.unauthenticated_roles
            or self.incomplete_provider_setup
            or self.untrusted_roles
            or self.stale_codex_hook_trust
            or self.missing_cli_roles
            or self.model_validation_failures
            or self.owner_shell_issue
            or (self.github_identity is not None and not self.github_identity.ready)
        )


def _run_owner_cli_until(
    *,
    owner_user: str,
    owner_home: Path,
    cwd: Path,
    command: Sequence[str],
    is_complete: Callable[[], bool],
    watching: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    #: How the live step is started, resolved when it runs rather than bound
    #: when this was defined, so a caller can watch it. A session rather than a
    #: bare process, because a step that cannot be read cannot be ended by what
    #: is on its screen -- which is the whole of this step's completion.
    session_factory: Callable[..., Any] | None = None,
    #: The adjustment the desktop branch makes to every command it runs. The
    #: bounded path starts the process itself, so it has to make the same one
    #: or a tenant with a desktop policy would lose it (SYRD-191).
    transform: Callable[[list[str], dict[str, Any]], tuple[list[str], dict[str, Any]]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
    #: Which step this is, for the window's own name. These are the windows a
    #: fresh tenant meets most -- folder trust runs once per worktree -- and
    #: until SYRD-221 they were the unnamed, silent ones.
    purpose: str = SETUP_PURPOSE_FOLDER_TRUST,
    #: Whose prompts the person is being asked to answer. The caller knows it;
    #: falling back to the command keeps a caller that does not from having to
    #: invent one.
    cli: str = "",
    #: Where the window's name and the CLI's own output are written. None
    #: means the real terminal, which is the live path; a test passes its own
    #: to read back what the window claimed.
    output_write: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Run one interactive step and take the terminal back when it is done.

    The User answers the provider's own prompts and nothing else. Asking them to
    type `/exit` afterwards -- once for the account and again for every
    worktree -- is not a first run, it is a chore, so the step is bounded here
    instead: the state the step exists to record is watched, and the moment it
    appears the CLI is ended and the phase moves on.

    "Done" is three things, not two, and the third is the one that was
    missing. The state the step records is one; the deadline is another; and a
    provider sitting at its ordinary prompt with nothing left to ask is the
    third. Without it this step could only wait, and a fresh tenant's UAT
    reported exactly what that looks like: the person answered everything, the
    CLI went back to its prompt, and Switchyard held a standalone window open
    for ten more minutes (SYRD-221).

    A runner may be injected, in which case the step is simply run and its
    completion read back afterwards: that is the shape tests drive, and it is
    also the honest fallback for anything that cannot be watched.
    """
    from scripts import team_launcher as launcher

    args = launcher._owner_command_env_args(owner_user, owner_home, command)
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": launcher._pane_identity_scrubbed_env()}
    if transform is not None:
        args, kwargs = transform(args, kwargs)
    if runner is not None:
        runner(args, **kwargs)
        return is_complete()
    if is_complete():
        return True
    # Watched on a pty, exactly like the provider's own first run -- because
    # the failure this step kept producing is the one that runner was built to
    # end. A bare `Popen` inheriting stdio cannot see the screen, so the ONLY
    # way out was the state file or the deadline: when Claude answered the
    # question and went back to its ordinary prompt without recording anything
    # Switchyard reads, the step sat there for the full ten minutes with the
    # person watching a standalone CLI that was, as far as they could tell,
    # finished. Live UAT reported precisely that on a brand-new tenant
    # (SYRD-221). Reading the screen gives this step the same third answer the
    # first run has: nothing left to ask, so it is done.
    return launcher.run_provider_first_run_session(
        cli=cli or (Path(command[0]).name if command else "the provider"),
        args=args,
        kwargs=kwargs,
        is_complete=is_complete,
        watching=watching,
        session_factory=session_factory,
        input_fd=sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None,
        output_write=output_write,
        sleep=sleep,
        monotonic=monotonic,
        timeout_seconds=timeout_seconds,
        purpose=purpose,
        print_func=print_func,
    )


#: What an operator is told before a foreground step takes their terminal. Said
#: before it starts, because afterwards the CLI owns the screen (SYRD-191).
def _provider_setup_instruction(cli: str, owner_user: str) -> str:
    if cli == "claude":
        return (
            f"switchyard: {cli} will now run in this terminal as {owner_user}. Answer its own "
            "prompts to the end -- a theme, the sign-in it asks for even though credentials "
            "exist, because that flow does not consult them, and whether to trust this folder. "
            "The terminal comes back on its own once nothing is left to answer; you do not have "
            "to exit anything. It is asked once for the account, not once per role, and no pane "
            "will ask again."
        )
    return (
        f"switchyard: {cli} will now run in this terminal as {owner_user}. Complete what it asks; "
        "the terminal comes back on its own once it is recorded."
    )


def _provider_sign_in_instruction(cli: str, owner_user: str) -> str:
    """Said before a sign-in step, like the steps either side of it.

    It was the one step that started silently, so on test9 the only sign that
    one provider had finished and another had begun was the window title
    (SYRD-221 UAT).
    """
    return (
        f"switchyard: {cli} will now run in this terminal as {owner_user} to sign in. "
        "Complete what it asks -- a browser sign-in for some providers, a choice in the "
        "terminal for others; the terminal comes back on its own once the account is set up."
    )


def _folder_trust_instruction(cli: str, workdir: Path, roles: Sequence[str]) -> str:
    covered = ", ".join(roles)
    return (
        f"switchyard: {cli} will now run in {workdir} as this project's owner so it can be "
        f"trusted once for {covered}. Answer the trust prompt; the terminal comes back on its "
        "own once the answer is recorded."
    )


class _NoRunnerInjected:
    """The difference between "nobody injected a runner" and "somebody injected
    `subprocess.run`".

    It is a real difference and it decides whether a person gets a watched
    setup window or an unwatched one, but `subprocess.run` is also the obvious
    default for a command that shells out for a dozen other things. Both live
    entry points therefore declared `runner = subprocess.run`, passed it on,
    and the phase -- correctly, by its own contract -- read that as "the caller
    is driving the interactive steps itself" and fired every one of them
    unwatched. Three candidates' worth of watching, narrating and bounding were
    live-path code that the live path never reached (SYRD-221).

    A sentinel keeps the two apart where the two matter.
    """


NO_RUNNER_INJECTED = _NoRunnerInjected()


def foreground_runner_for(
    runner: Callable[..., subprocess.CompletedProcess[Any]] | _NoRunnerInjected,
) -> Callable[..., subprocess.CompletedProcess[Any]] | None:
    """Who drives the windows a person sits in front of, given what was injected.

    Only for a command whose `runner` does a dozen other jobs as well, so the
    two questions cannot be asked separately at its boundary. A caller that CAN
    ask them separately says so outright instead -- see
    `run_switchyard_launch_first_run_auth`, where `subprocess.run` for probes
    is ordinary and correct and must not cost anybody a watched window.
    """
    return None if isinstance(runner, _NoRunnerInjected) else runner


def run_first_run_auth_phase(
    config: ProjectConfig,
    *,
    owner_user: str | None = None,
    owner_home: Path | None = None,
    validate_models: bool = False,
    #: A caller driving these steps itself passes its own runner -- that is how
    #: the suites exercise them. None means the live path: probes run through
    #: `subprocess.run`, and the foreground steps are bounded rather than fired
    #: and forgotten. A sentinel rather than a default of `subprocess.run`,
    #: because the desktop branch below rebinds this name and comparing against
    #: it afterwards said "injected" on every tenant with a pane (SYRD-191).
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    #: Who drives the interactive steps. This used to be `runner` itself, which
    #: meant a caller could not ask for "probe with this, but WATCH the windows
    #: a person sits in front of" -- and every live caller wanted exactly that
    #: and got the opposite (SYRD-221). `None` is the watched path; a runner
    #: here is a suite driving the steps; the sentinel means "same as `runner`",
    #: which is what every existing caller expects.
    foreground_runner: Callable[..., subprocess.CompletedProcess[Any]] | None | _NoRunnerInjected = (
        NO_RUNNER_INJECTED
    ),
    print_func: Callable[[str], None] = print,
) -> FirstRunAuthReport:
    from scripts import team_launcher as launcher

    injected_runner = runner if isinstance(foreground_runner, _NoRunnerInjected) else foreground_runner
    runner = runner or subprocess.run
    foreground_transform: Callable[..., Any] | None = None
    if config.desktop_access is not None:
        config = launcher.prepare_project_desktop(config, runner=runner)
        desktop_env = {k:v for k,v in config.roles[0].env.items() if k in launcher.DESKTOP_ENV_KEYS}
        base_runner = runner

        def desktop_transform(args, kwargs):
            args = list(args)
            if "env" in args:
                position = args.index("env") + 1
                args[position:position] = [*launcher._env_unset_prefix(launcher.DESKTOP_ENV_KEYS), *launcher._env_prefix(desktop_env)]
            environment = dict(kwargs.get("env", os.environ))
            for key in launcher.DESKTOP_ENV_KEYS:
                environment.pop(key, None)
            environment.update(desktop_env)
            kwargs = {**kwargs, "env": environment}
            return args, kwargs

        foreground_transform = desktop_transform

        def runner(args, **kwargs):
            args, kwargs = desktop_transform(args, kwargs)
            return base_runner(args, **kwargs)
    effective_owner = (owner_user or config.run_as_user or launcher.current_user_name()).strip()
    if not effective_owner:
        return FirstRunAuthReport({}, [])
    effective_home = owner_home or launcher._owner_home_for_auth(effective_owner)
    unauthenticated: dict[str, list[str]] = {}
    missing_cli_roles: dict[str, list[str]] = {}

    manifest = launcher.build_first_run_setup_manifest(
        config,
        owner_user=effective_owner,
        owner_home=effective_home,
        runner=runner,
    )
    launcher.print_first_run_setup_manifest(manifest, print_func=print_func)
    missing_cli_roles.update(manifest.missing_cli_roles)

    # The provider's own first run, once per provider, before any role is
    # launched or presented. Switchyard asks the CLI to run its setup and then
    # looks at the account state again; it never writes that state itself and
    # never answers a security prompt on the owner's behalf (SYRD-191).
    incomplete_setup: list[tuple[str, list[str]]] = []
    for step in manifest.provider_setup_steps:
        print_func(_provider_setup_instruction(step.cli, effective_owner))
        completed = launcher._run_provider_first_run(
            cli=step.cli,
            owner_user=effective_owner,
            owner_home=effective_home,
            command=list(step.command),
            is_complete=lambda cli=step.cli: launcher._provider_account_setup_complete(
                cli, owner_home=effective_home
            ),
            watching=f"{step.cli} to record its own first run for {effective_owner}",
            runner=injected_runner,
            transform=foreground_transform,
            print_func=print_func,
        )
        if not completed:
            incomplete_setup.append((step.cli, list(step.roles)))
    #: Providers whose own first run did not finish in this pass. A model probe
    #: for such a provider starts the CLI itself, which shows that unfinished
    #: first run rather than answering the probe -- so it asks the operator the
    #: same sign-in again and then reports the role's MODEL as broken, which is
    #: a problem the account does not have. Live on sbs one owner account was
    #: asked to authenticate Claude three times -- login prompts, confirmed by
    #: the User as not per-worktree trust prompts -- and the run still ended
    #: saying its first-run setup had not completed (SYRD-221).
    #:
    #: Two steps are deliberately NOT gated on this. The provider's own login
    #: is a different command with its own flow, and an account whose welcome
    #: flow signs nobody in would be stranded without it (SYRD-191). Folder
    #: trust is a separate question per worktree, and the User confirmed those
    #: prompts were not what repeated.
    unfinished_first_run = {cli for cli, _roles in incomplete_setup}

    authenticated_now: dict[str, list[str]] = {}
    #: What the completion check last read, so the phase does not ask twice for
    #: the same answer: the bounded step below already probes to know when it
    #: is finished, and that probe is the phase's answer too.
    observed_auth: dict[str, str] = {}

    def _signed_in(cli: str) -> bool:
        observed_auth[cli] = launcher._cli_auth_status(
            cli, owner_user=effective_owner, owner_home=effective_home, runner=runner
        )
        return observed_auth[cli] == "authenticated"

    for step in manifest.login_steps:
        # Asked again first. That provider's own first run may have signed in
        # as part of itself, and live Zorin UAT showed the cost of not
        # checking: `claude login`, `codex login`, and then Claude asking to
        # sign in a second time inside its welcome flow (SYRD-211 live UAT).
        # Running setup before this is what makes the second question
        # avoidable; re-reading here is what makes it actually avoided.
        if launcher._cli_auth_status(
            step.cli,
            owner_user=effective_owner,
            owner_home=effective_home,
            runner=runner,
        ) == "authenticated":
            authenticated_now.setdefault(step.cli, list(step.roles))
            continue
        login_command = list(step.command)
        print_func(_provider_sign_in_instruction(step.cli, effective_owner))
        # Bounded, like the setup and folder-trust steps beside it. This was a
        # bare `subprocess.run`, with no watcher, no timeout and nothing to
        # say when it was done -- so on a fresh tenant `claude auth login`
        # printed "Paste code here if prompted >" to a session with no browser
        # to complete the flow, and held the entire launch there. Measured: it
        # does not exit on its own. That is the standalone window a User was
        # left in, and the reason no presentation ever opened (SYRD-221).
        #
        # What finishes it is the state it exists to produce, read back the
        # same way the phase reads it below.
        launcher._run_owner_cli_until(
            owner_user=effective_owner,
            owner_home=effective_home,
            cwd=effective_home,
            command=login_command,
            is_complete=lambda cli=step.cli: _signed_in(cli),
            watching=f"{step.cli} to record a signed-in account for {effective_owner}",
            purpose=SETUP_PURPOSE_SIGN_IN,
            cli=step.cli,
            runner=injected_runner,
            transform=foreground_transform,
            print_func=print_func,
        )
        # What the completion check already read, rather than asking again.
        # `is None` rather than a falsy test: every status this returns is a
        # non-empty word, so `or` here would be a test that never fires and
        # would quietly re-probe if one ever were empty.
        auth_status = observed_auth.get(step.cli)
        if auth_status is None:
            auth_status = launcher._cli_auth_status(
                step.cli, owner_user=effective_owner, owner_home=effective_home, runner=runner
            )
        if auth_status == "not_installed":
            missing_cli_roles[step.cli] = list(step.roles)
        elif auth_status != "authenticated":
            unauthenticated[step.cli] = list(step.roles)
        else:
            # This run is what made that provider usable. Every role configured
            # for it that is already running started before it (SYRD-191).
            authenticated_now[step.cli] = list(step.roles)

    untrusted: list[tuple[str, str, str]] = []
    for step in manifest.folder_trust_steps:
        # Re-read before announcing it. The manifest was built before the
        # provider's own first run, and that run records trust for whatever
        # directory it was answered in -- so by the time this loop reaches that
        # worktree the step can already be done. Telling somebody to answer a
        # trust prompt and then not showing one is how a step that is simply
        # finished reads as a stall (SYRD-245).
        if launcher._workdir_is_trusted(step.cli, owner_home=effective_home, workdir=step.workdir):
            print_func(
                f"switchyard: {step.cli} already trusts {step.workdir} for this account, "
                "so that step is done; nothing to answer."
            )
            continue
        print_func(_folder_trust_instruction(step.cli, step.workdir, step.roles or (step.role,)))
        trusted = launcher._run_owner_cli_until(
            owner_user=effective_owner,
            owner_home=effective_home,
            cwd=step.workdir,
            command=list(step.command),
            is_complete=lambda cli=step.cli, workdir=step.workdir: launcher._workdir_is_trusted(
                cli, owner_home=effective_home, workdir=workdir
            ),
            watching=f"{step.cli} to record trust for {step.workdir}",
            purpose=SETUP_PURPOSE_FOLDER_TRUST,
            cli=step.cli,
            runner=injected_runner,
            transform=foreground_transform,
            print_func=print_func,
        )
        if not trusted:
            for role in step.roles or (step.role,):
                untrusted.append((step.cli, role, str(step.workdir)))

    # Free, and not a probe: one `agy models` in the OWNER's context, compared
    # against what each role is configured to run. SYRD-246 removed the probe
    # that asked a model to prove itself, which cost tokens and time and could
    # reject a working model; this asks the CLI for its own list and checks
    # membership, which is neither. Without it `test2` started its audit pane on
    # a slug the owner does not recognise and the provider quietly ran something
    # else (SYRD-250).
    #
    # Asked once per CLI rather than once per role, because the answer is a
    # property of the account and the account does not change between two
    # roles. This is the second `agy models` of the phase and deliberately so:
    # the first is `agy`'s auth probe, which ran before this phase could log
    # anybody in, so its answer may describe an account that was not signed in
    # yet.
    unknown_model_roles: list[tuple[str, str, str, tuple[str, ...]]] = []
    owner_prefix = launcher._owner_command_env_args(effective_owner, effective_home, [])
    owner_catalogs: dict[str, runtime_catalog.Catalog | None] = {}
    for role in config.roles:
        cli = launcher._role_cli_name(role)
        if not role.model or cli in unauthenticated or cli in missing_cli_roles:
            # An account that cannot answer at all has nothing to say about a
            # model, and those roles are already reported. A role with no model
            # configured takes the runtime's own default and has nothing to
            # contradict.
            continue
        if cli not in owner_catalogs:
            owner_catalogs[cli] = runtime_catalog.owner_model_catalog(
                cli, runner=runner, owner_args=owner_prefix
            )
        mismatch = runtime_catalog.model_absent_from(owner_catalogs[cli], role.model)
        if mismatch is not None:
            unknown_model_roles.append(
                (role.role, cli, role.model, tuple(c.value for c in mismatch.choices))
            )

    model_validation_failures: list[ModelValidationFailure] = []
    if validate_models:
        # A provider whose first run is unfinished cannot answer a model probe
        # either; running one asks the same unfinished question a third time
        # and reports a model failure for an account that is simply not signed
        # in yet (SYRD-221).
        skipped_clis = set(unauthenticated) | set(missing_cli_roles) | unfinished_first_run
        model_roles = [role for role in config.roles if launcher._role_cli_name(role) not in skipped_clis]
        model_validation_failures = launcher.validate_role_models(
            model_roles,
            owner_user=effective_owner,
            owner_home=effective_home,
            runner=runner,
            print_func=print_func,
        )

    return FirstRunAuthReport(
        unauthenticated,
        untrusted,
        manifest.stale_codex_hook_trust,
        missing_cli_roles,
        unknown_model_roles,
        model_validation_failures,
        # Named whenever the report will tell somebody to run something as
        # that account. An incomplete provider setup now carries a resumable
        # command, and "run this as the owner account" without saying which
        # account is not a way out of anything (SYRD-221).
        owner_user=effective_owner
        if (
            missing_cli_roles
            or manifest.stale_codex_hook_trust
            or manifest.owner_shell_issue
            or incomplete_setup
            # An unfinished sign-in tells somebody to run the provider's own
            # login as the owner, which is the same kind of instruction as the
            # four above and was simply missing from the list. Without it the
            # real unauthenticated-only path produced owner_user='' and the
            # gate said "as the owner account" -- naming nobody, in the one
            # message whose whole job is to say whose account to finish it on
            # (SYRD-221 DAT).
            or unauthenticated
            # Same reason again. The unknown-model gate's entire claim is that
            # THIS account does not list the model, and the operator has to
            # know which account was asked before they can agree or disagree
            # with it -- their own `agy` may well list the slug (SYRD-250).
            or unknown_model_roles
        )
        else "",
        owner_shell_issue=manifest.owner_shell_issue,
        authenticated_now=authenticated_now,
        incomplete_provider_setup=incomplete_setup,
    )


def _resumable_next_action(cli: str, *, owner_user: str) -> str:
    """What to do next, as a command, and what to do after doing it.

    A step that did not finish has to leave somebody able to finish it. The
    report used to say only which step was outstanding, which is a description
    of a problem rather than a way out of it -- and on a fresh tenant the one
    thing a person needed to know was that the launch could simply be run
    again once the provider's own question was answered (SYRD-221).
    """
    from scripts import team_launcher as launcher

    command = " ".join(launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS.get(cli, [cli])[:1])
    owner = owner_user or "the owner account"
    return (
        f"To resume: run `{command}` as {owner}, answer its own prompts to the end, "
        "then run the same switchyard launch again -- it picks up from whatever is "
        "already recorded and does not repeat the steps that are done."
    )


def report_first_run_auth_warnings(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    from scripts import team_launcher as launcher

    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    for cli, roles in report.missing_cli_roles.items():
        print_func(
            f"warning: switchyard: {cli} is not installed{owner_detail} "
            f"(affected roles: {', '.join(roles)}); "
            f"{launcher._missing_cli_install_clause(cli)}"
        )
    if report.owner_shell_issue:
        issue = report.owner_shell_issue
        print_func(
            f"warning: switchyard: owner shell for {issue.owner_user} is not executable: "
            f"{issue.shell}; repair with `{issue.remedy}`"
        )
    for cli, roles in report.unauthenticated_roles.items():
        print_func(
            f"warning: switchyard: {cli} is still unauthenticated; affected roles: {', '.join(roles)}"
        )
    for cli, roles in report.incomplete_provider_setup:
        print_func(
            f"warning: switchyard: {cli}'s own first run is still not complete{owner_detail}; "
            f"affected roles: {', '.join(roles)}; each of their panes will open it instead of a prompt. "
            + _resumable_next_action(cli, owner_user=report.owner_user)
        )
    for cli, role, workdir in report.untrusted_roles:
        print_func(f"warning: switchyard: {cli} workspace still untrusted for {role}: {workdir}")
    if report.stale_codex_hook_trust:
        print_func(
            "warning: switchyard: "
            + launcher._format_codex_hook_trust_report(report.stale_codex_hook_trust, owner_user=report.owner_user)
        )
    for failure in report.model_validation_failures:
        print_func(
            f"warning: switchyard: model validation failed for role {failure.role} "
            f"(cli {failure.cli}, model {failure.model}): {failure.reason}; {failure.suggestion}"
        )
        # What actually came back, per attempt. Without it the reader is told a
        # conclusion and given nothing to check it against -- which is how a
        # fresh Zorin run ended with a model change nobody could confirm was
        # the right remedy (SYRD-244). Token-free by construction.
        for line in failure.evidence:
            print_func(f"warning: switchyard:   probe {line}")
    if report.github_identity is not None:
        remedy = launcher.github_identity_remedy(report.github_identity)
        if remedy:
            print_func(remedy)


def stop_before_launch_for_unauthenticated_providers(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> bool:
    """A role whose provider is not ready must not be started.

    The ticket asks for each unique provider to be authenticated once, before
    any pane. Until now an unfinished sign-in was a warning printed AFTER the
    launch: test7 started three Codex roles and then said "codex is still
    unauthenticated; affected roles: main, app, ops", which is a report of the
    thing that was supposed to be prevented. A role started without
    credentials cannot do its work, and it cannot say why in a way anybody
    reads (SYRD-221).
    """
    # Both halves block, because both mean the same thing to a role that is
    # about to start: its provider is not ready. A required first run that did
    # not finish leaves no credentials either, so checking only
    # `unauthenticated_roles` let a report with
    # `incomplete_provider_setup=[("claude", ["designer"])]` open panes anyway --
    # which is the launch this ticket exists to prevent (SYRD-221 DAT).
    if not report.unauthenticated_roles and not report.incomplete_provider_setup:
        return False
    lines = [
        "switchyard: not starting any role yet -- a provider's setup did not "
        "finish, so those roles would come up unable to do anything:"
    ]
    for cli, roles in sorted(report.unauthenticated_roles.items()):
        lines.append(f"  {cli}: {', '.join(roles)} (sign-in not finished)")
    for entry in report.incomplete_provider_setup:
        cli, roles = entry[0], entry[1]
        lines.append(f"  {cli}: {', '.join(roles)} (first-run setup did not complete)")
    owner = report.owner_user or "the owner account"
    lines.append(
        f"Finish it by running the provider's own sign-in as {owner}, then run the "
        "same switchyard command again -- it picks up from whatever is already "
        "recorded and does not repeat the steps that are done."
    )
    print_func("\n".join(lines))
    return True
