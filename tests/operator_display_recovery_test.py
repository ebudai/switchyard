#!/usr/bin/env python3
"""SYRD-239: the recovery instruction a disconnected Director slot can obey.

A live mefp Director display attachment was closed. The inert slot told the
desktop operator to run

    switchyard present mefp recover director

and that command refuses anyone who is not the Director pane -- the pane that
had just disconnected. The operator's escape hatch was to type the role in by
hand:

    env TICKET_BOARD_CALLER_ROLE=director TICKET_BOARD_PROJECT=mefp switchyard ...

which is teaching people to forge a role, and is the thing this ticket exists
to stop.

The slot now names `switchyard recover-display <project>`, which goes through
the tenant-control bridge: root-owned, reached by one NOPASSWD grant naming
that program, resolving the caller from SUDO_UID and checking it against the
tenant's root-owned grant. The tenant side re-reads that grant and requires the
two to agree.

Bounded on every axis, and each one has a case here: only the recovery action,
only the Director's slot, only that tenant's registered operator, only that
project. Ordinary presentation changes stay Director-only.
"""

from __future__ import annotations

import json
import signal
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as pc  # noqa: E402
import team_launcher as tl  # noqa: E402

CHECKS = 0
PROJECT = "mefp"
OPERATOR = "eric"
#: For cases that patch the grant lookup rather than pass a grant root: a name
#: no real grant on any host names. If a patch fails to reach the module that
#: is actually called, the real grant is read instead -- and with this name it
#: then REFUSES, so the mistake fails the test rather than passing it. With
#: "eric" it passed: this host's real mefp grant names eric (SYRD-239).
FAKE_OPERATOR = "syrd239-operator"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refused(call, expected: str = "") -> str:
    try:
        call()
    except SystemExit as exc:
        if expected:
            assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}, and it was allowed")


class grant:
    """A tenant's root-owned control grant, as provisioning writes it."""

    def __init__(self, *, project: str = PROJECT, authorized: str = OPERATOR) -> None:
        self.project = project
        self.authorized = authorized

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd239.")
        root = Path(self.tmp.__enter__())
        (root / self.project).mkdir()
        (root / self.project / "control-grant.json").write_text(
            json.dumps({
                "project": self.project,
                "owner": "stellaris-agent",
                "authorized_user": self.authorized,
                "launcher": "/opt/switchyard/current/switchyard",
            }),
            encoding="utf-8",
        )
        return root

    def __exit__(self, *exc):
        return self.tmp.__exit__(*exc)


def config(project: str = PROJECT, roles=()):
    """A stand-in for the authorization checks, which read only the project.

    `_require_director` and `operator_recovery_caller` look at `config.project`
    and nothing else, so this is the whole of what they need. The screen cases
    below use a REAL `ProjectConfig` instead, because those reach code that
    reads a tenant's roles, accounts and isolation settings -- and a namespace
    with just enough attributes to get past the first of them is how a test
    ends up asserting against a shape no tenant has.
    """
    return types.SimpleNamespace(project=project, roles=list(roles))


class real_config:
    """A loaded ProjectConfig, built from a configuration on disk."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd239-cfg.")
        root = Path(self.tmp.__enter__())
        for name in ("d", "a"):
            (root / name).mkdir()
        (root / f"{PROJECT}.json").write_text(json.dumps({
            "project": PROJECT,
            "board_url": "http://127.0.0.1:1/",
            "roles": [
                {"role": "director", "cli": "claude", "slot": 0, "workdir": str(root / "d")},
                {"role": "app", "cli": "claude", "slot": 1, "workdir": str(root / "a")},
            ],
        }), encoding="utf-8")
        return tl.load_project_config(PROJECT, root / f"{PROJECT}.json")

    def __exit__(self, *exc):
        return self.tmp.__exit__(*exc)


# -- the catch-22, and what replaced it --------------------------------------


def test_the_slot_no_longer_tells_the_operator_to_run_what_refuses_them() -> None:
    """The regression in one line: what the screen says, and who can obey it."""
    with real_config() as cfg:
        instruction = pc._recovery_instruction(cfg, "director")
        check(
            instruction == f"switchyard recover-display {PROJECT}",
            f"a disconnected Director slot names the operator command: {instruction}",
        )
        check(
            "present" not in instruction and "recover director" not in instruction,
            f"and not the one gated to the pane that just disconnected: {instruction}",
        )
        check(
            "TICKET_BOARD_CALLER_ROLE" not in instruction,
            f"and never teaches the role variable: {instruction}",
        )

        # Every other slot keeps the Director's own command: for those the
        # Director is still attached, and it is still their call.
        other = pc._recovery_instruction(cfg, "app")
        check(
            other == f"switchyard present {PROJECT} recover app",
            f"an app slot is unchanged: {other}",
        )


def test_the_disconnected_slot_screens_carry_that_instruction() -> None:
    """Not just the helper -- the text a person actually reads off the slot."""
    with real_config() as cfg:
        _workdir, command = pc._proxy_command(cfg, "director", {"live": True})
        check(f"switchyard recover-display {PROJECT}" in command, f"live-but-disconnected: {command}")
        check("present mefp recover director" not in command, f"{command}")

        _workdir, dead = pc._proxy_command(cfg, "director", {"live": False, "state": "stopped"})
        check(f"switchyard recover-display {PROJECT}" in dead, f"not running: {dead}")
        check("TICKET_BOARD_CALLER_ROLE" not in dead, f"{dead}")


# -- the authorization, on every axis the ticket names -----------------------


def test_the_registered_operator_may_recover_the_director_slot() -> None:
    with grant() as root:
        who = pc._require_director(
            config(),
            {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
            operator_recovery=True,
            grant_root=root,
        )
        check(who == f"operator:{OPERATOR}", f"authorized, and recorded as themselves: {who}")
        check(
            not who.endswith("director") and who != "director",
            "an operator's recovery is not written into history as the Director's move",
        )


def test_ordinary_presentation_changes_stay_director_only() -> None:
    """The authority this must not widen.

    The same operator, the same grant, the same environment -- and a show, a
    swap or a hide is still refused. Only the recovery opens.
    """
    with grant() as root:
        message = refused(
            lambda: pc._require_director(
                config(),
                {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                operator_recovery=False,
                grant_root=root,
            ),
            "TICKET_BOARD_CALLER_ROLE=director",
        )
        check("recover-display" not in message, f"and it is not offered the recovery: {message}")


def test_only_the_directors_slot_opens_the_operator_path() -> None:
    """Driven through the dispatcher's own decision, not a restatement of it.

    `operator_recovery` is computed from the action and the role, so the rule
    is asserted where it is made.
    """
    def opens(action: str, role_name: str) -> bool:
        return action == "recover" and (role_name or "").strip().lower() == pc.DIRECTOR_ROLE

    check(opens("recover", "director"), "the Director's slot, being recovered")
    check(opens("recover", "Director"), "spelled any way")
    for action, role_name in (
        ("recover", "app"), ("recover", "ops"), ("recover", ""),
        ("show", "director"), ("swap", "director"), ("hide", "director"),
        ("restore", "director"), ("bootstrap", "director"),
    ):
        check(not opens(action, role_name), f"{action} {role_name!r} does not open it")

    # And the same rule as the module computes it, so this cannot drift from
    # the line that actually runs.
    source = (ROOT / "scripts/presentation_controller.py").read_text()
    check(
        'operator_recovery = action == "recover" and (role_name or "").strip().lower() == DIRECTOR_ROLE'
        in source,
        "the dispatcher computes it exactly this way",
    )


def test_an_unregistered_caller_authorizes_nothing() -> None:
    with grant() as root:
        for pretender in ("mallory", "root", "stellaris-agent", ""):
            message = refused(
                lambda name=pretender: pc._require_director(
                    config(),
                    {tl.TENANT_CONTROL_CALLER_ENV: name},
                    operator_recovery=True,
                    grant_root=root,
                ),
                "registered operator",
            )
            check(
                f"switchyard recover-display {PROJECT}" in message,
                f"{pretender!r} is told the command that would work for the right person",
            )
            check(
                "TICKET_BOARD_CALLER_ROLE" not in message,
                f"and never told to forge a role: {message}",
            )


def test_cross_project_recovery_is_refused() -> None:
    with grant() as root:
        refused(
            lambda: pc._require_director(
                config(),
                {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR, "TICKET_BOARD_PROJECT": "syrd"},
                operator_recovery=True,
                grant_root=root,
            ),
            "refusing cross-project presentation change",
        )
    # And a grant that names another tenant is not a grant for this one, even
    # when it is found under this one's directory.
    with grant(project=PROJECT) as root:
        (root / PROJECT / "control-grant.json").write_text(
            json.dumps({
                "project": "syrd", "owner": "o", "authorized_user": OPERATOR, "launcher": "/x",
            }),
            encoding="utf-8",
        )
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=root) == "",
            "a grant for another project authorizes nothing here",
        )


def test_a_missing_or_unreadable_grant_authorizes_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=Path(raw)) == "",
            "no grant, no authority",
        )
        (Path(raw) / PROJECT).mkdir()
        (Path(raw) / PROJECT / "control-grant.json").write_text("{ not json", encoding="utf-8")
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=Path(raw)) == "",
            "an unreadable grant fails closed rather than open",
        )


def test_the_directors_own_pane_is_unchanged() -> None:
    with grant() as root:
        for recovery in (True, False):
            who = pc._require_director(
                config(), {"TICKET_BOARD_CALLER_ROLE": "director"},
                operator_recovery=recovery, grant_root=root,
            )
            check(who == "director", f"still the Director's own authority: {who}")
        refused(
            lambda: pc._require_director(
                config(),
                {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "syrd"},
                operator_recovery=True, grant_root=root,
            ),
            "refusing cross-project presentation change",
        )


# -- the operator entry point ------------------------------------------------


def test_the_bridge_carries_one_more_fixed_verb_and_no_role() -> None:
    """The role is written into the bridge, never taken from the caller."""
    source = (ROOT / "scripts/switchyard-tenant-control").read_text()
    check(
        '"recover-display": ("present", PROJECT_ARGUMENT, "recover", "director")' in source,
        "the verb names the Director's slot itself",
    )
    check("recover-display" in tl.TENANT_CONTROL_OPERATIONS, "and the launcher knows it")
    check("recover-display" in tl.SWITCHYARD_COMMANDS, "and it is a real command")

    # Two words, this project, exactly as stop and status are routed.
    check(
        tl._tenant_control_operation(["recover-display", PROJECT], PROJECT) == "recover-display",
        "the operator's invocation routes to the bridge",
    )
    for argv in (
        ["recover-display", "syrd"],          # another tenant
        ["recover-display"],                  # no project
        ["recover-display", PROJECT, "app"],  # a role the caller chose
    ):
        check(
            tl._tenant_control_operation(argv, PROJECT) == "",
            f"{argv} does not reach the bridge",
        )


# -- SYRD-239 live UAT: the command existed everywhere except the dispatcher ---


class patched:
    """Replace module attributes for one block, and always put them back.

    `team_launcher` is loaded twice in this process: as `team_launcher`, which
    this file imports, and as `scripts.team_launcher`, which
    presentation_controller imports. They are different module objects, so a
    patch on one does not reach code running in the other. Passing `tl`
    patches both.
    """

    def __init__(self, module, **replacements) -> None:
        self.modules = [module]
        if module is tl and pc.team_launcher is not tl:
            self.modules.append(pc.team_launcher)
        self.replacements = replacements

    def __enter__(self):
        self.saved = [
            (module, {name: getattr(module, name) for name in self.replacements})
            for module in self.modules
        ]
        for module in self.modules:
            for name, value in self.replacements.items():
                setattr(module, name, value)
        return self

    def __exit__(self, *exc):
        for module, saved in self.saved:
            for name, value in saved.items():
                setattr(module, name, value)
        return False


def test_the_public_dispatcher_reaches_the_command() -> None:
    """What the membership checks above never did: run it.

    The verb was in SWITCHYARD_COMMANDS, in TENANT_CONTROL_OPERATIONS and in the
    bridge, and `switchyard_main` had no branch for it -- so the live UAT got
    `unknown project 'recover-display mefp'`. The end-to-end proof, from the
    operator's own `scripts/switchyard` through sudo and the bridge, is in
    tenant_control_bridge_e2e_test; this is the same seam without a namespace.
    """
    reached: list[list[str]] = []
    with patched(tl, switchyard_recover_display_command=lambda argv: reached.append(list(argv)) or 0):
        code = tl.switchyard_main(["recover-display", PROJECT])
    check(code == 0, f"the dispatcher returned the command's result: {code}")
    check(reached == [["recover-display", PROJECT]], f"and reached the command itself: {reached}")


def _bridge_run(operation: str, *, desktop: bool):
    """`_switchyard_exec_through_tenant_control` with the bridge answering 0."""
    completions: list[str] = []
    said: list[str] = []

    def complete(project, *, caller, runner):
        completions.append(project)
        return 1  # what it answers on a desktop tenant with no handoff

    with patched(
        tl,
        ensure_staged_role_bundle_before_crossing=lambda *a, **k: "",
        _tenant_has_desktop_access=lambda *a, **k: desktop,
        complete_desktop_presentation=complete,
    ):
        try:
            tl._switchyard_exec_through_tenant_control(
                PROJECT, operation,
                grant={"authorized_user": tl.current_user_name()},
                runner=lambda argv, **kw: types.SimpleNamespace(returncode=0),
                ensure_helper=lambda *a, **k: None,
                print_func=said.append,
            )
        except SystemExit as exc:
            return exc.code, completions, said
    raise AssertionError("the bridge exec always ends in SystemExit")


def test_a_recovery_is_not_reported_as_a_window_that_never_came() -> None:
    """The second defect behind the first.

    After the bridge answers, every verb but `stop` used to go on to open the
    window the owner half handed back. A recovery hands nothing back -- it
    reattaches the Director inside the window that is already open -- and on a
    tenant with desktop access "nothing handed back" is reported as a failure:
    "no presentation window was handed back ... run it again". So with the
    dispatch fixed and nothing else, a SUCCESSFUL recovery would still have
    exited 1 and told the operator it had not worked.
    """
    code, completions, said = _bridge_run("recover-display", desktop=True)
    check(code == 0, f"a recovery the bridge completed exits 0: {code}")
    check(completions == [], f"and asks for no window to open: {completions}")
    check(any("Director display was recovered" in line for line in said), f"and says so: {said}")

    # The control, so the seam above is known to be live: a start on the same
    # tenant does complete its window, and reports its absence.
    code, completions, _said = _bridge_run("start", desktop=True)
    check(completions == [PROJECT], f"a start still completes its window: {completions}")
    check(code == 1, f"and still reports a window that never came: {code}")


def test_without_the_bridge_the_refusal_does_not_send_them_back_to_it() -> None:
    """Reached only when no crossing happened: the owner, or root.

    Neither is the registered operator the bridge authenticates, so nothing new
    is authorized here. What changes is the message: the presentation
    controller's own refusal tells its reader to run `switchyard
    recover-display`, which is the command this caller just ran.
    """
    loaded: list[list[str]] = []
    presented: list[list[str]] = []
    entry = tl.SwitchyardProjectEntry(slug=PROJECT, name="MEFP Project", config_path=Path("/nonexistent"))

    with real_config() as cfg:
        def load(entry_, argv):
            loaded.append(list(argv))
            return cfg

        def present(config_, *, config_path, args):
            presented.append([args.project, args.action, args.role])
            return 0

        with patched(
            tl,
            _resolve_switchyard_project=lambda selection: entry,
            _load_switchyard_project_config_for_command=load,
            _tenant_control_grant=lambda project, **k: {"authorized_user": FAKE_OPERATOR},
            current_user_name=lambda: "stellaris-agent",
            switchyard_present_command=present,
        ):
            message = refused(
                lambda: tl.switchyard_recover_display_command(
                    ["recover-display", "MEFP", "Project"], environ={}
                ),
                "did not arrive over that bridge",
            )
            check(f"registered to {FAKE_OPERATOR}" in message, f"it says who can: {message}")
            check("Run `switchyard recover-display" not in message,
                  f"and does not send them back to the command they ran: {message}")
            check(presented == [], "nothing was recovered")
            # The slug crosses, not what was typed: the bridge serves only
            # `<verb> <slug>`, so a display name would silently fall to sudo.
            check(loaded == [["recover-display", PROJECT]], f"the crossing argv: {loaded}")

            # The two identities that ARE authorized still get through, to the
            # same fixed recovery the bridge would have asked for.
            code = tl.switchyard_recover_display_command(
                ["recover-display", PROJECT], environ={"TICKET_BOARD_CALLER_ROLE": "director"}
            )
            check(code == 0 and presented[-1] == [PROJECT, "recover", "director"],
                  f"the Director's own pane: {presented}")
            code = tl.switchyard_recover_display_command(
                ["recover-display", PROJECT],
                environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
            )
            check(code == 0 and presented[-1] == [PROJECT, "recover", "director"],
                  f"and the operator the bridge named: {presented}")
            refused(
                lambda: tl.switchyard_recover_display_command(
                    ["recover-display", PROJECT],
                    environ={tl.TENANT_CONTROL_CALLER_ENV: "intruder"},
                ),
                "did not arrive over that bridge",
            )


# -- SYRD-239 live UAT, second failure: a live worker the board will not name --


class DivergedBoard:
    """mefp as measured: the board lists no assignment for director or app,
    and its declared workflow says both run claude."""

    def __init__(self, declared: dict[str, tuple[str, str]]) -> None:
        self.declared = declared

    def __call__(self, url: str):
        import io

        if url.endswith("/api/runtime-assignments"):
            body = {"project": PROJECT, "authority_mode": "process", "assignments": {}}
        elif url.endswith("/api/workflow"):
            body = {"revision": 3, "document": {"roles": [
                {"name": name, "active": True, "runtime": runtime, "target": target}
                for name, (runtime, target) in self.declared.items()
            ]}}
        else:
            raise AssertionError(f"unexpected board read: {url}")
        return io.BytesIO(json.dumps(body).encode())


class LivePanes:
    """tmux as the owner sees it: which declared target has a live pane, and
    what process is in it. Records every call so nothing else can happen."""

    def __init__(self, panes: dict[str, int]) -> None:
        self.panes = panes
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["tmux", "display-message"]:
            target = args[args.index("-t") + 1].lstrip("=")
            if target in self.panes:
                return types.SimpleNamespace(returncode=0, stdout=f"{self.panes[target]}\n", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="can't find pane")
        raise AssertionError(f"only a pane probe may run here: {args}")


def _diverged_recovery(*, environ, declared, panes, processes):
    """Run the REAL presentation_action's recover against a diverged board."""
    with tempfile.TemporaryDirectory(prefix="syrd239-proc.") as proc, real_config() as cfg:
        for pid, argv in processes.items():
            (Path(proc) / str(pid)).mkdir()
            (Path(proc) / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        isolated = tl.replace(cfg, role_state_isolation=True, run_as_user=tl.current_user_name())
        tmux = LivePanes(panes)
        with patched(tl, _tenant_control_grant=lambda project, **k: {"authorized_user": FAKE_OPERATOR}):
            message = refused(lambda: pc.presentation_action(
                isolated, config_path=Path("/nonexistent/mefp.json"), action="recover",
                role_name="director", environ=environ, runner=tmux,
                proc_root=Path(proc), assignment_opener=DivergedBoard(declared),
            ))
    return message, tmux.calls


MEFP_DECLARED = {"director": ("claude", f"{PROJECT}-director:0.0"), "app": ("claude", f"{PROJECT}-app:0.0")}


def test_a_live_worker_the_declaration_disowns_is_named_not_reattached() -> None:
    """What the operator is told instead of the bare "no live runtime assignment"."""
    message, calls = _diverged_recovery(
        environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
        declared=MEFP_DECLARED,
        panes={f"{PROJECT}-director:0.0": 294308},
        processes={294308: ["codex", "resume", "019f9418"]},
    )
    check(
        f"director: declared claude at {PROJECT}-director:0.0, but the live worker in that pane "
        "runs codex (pid 294308)" in message,
        f"the divergence, with both runtimes and the pane: {message}",
    )
    check("Nothing was changed" in message, f"and that nothing was: {message}")
    check("Director has to correct the declared runtime" in message, f"and who can fix it: {message}")
    # app's declared pane is not live, so there is nothing verified to say about it.
    check("app:" not in message, f"an unverifiable role is not guessed at: {message}")
    # Exactly the two declared targets were probed -- exact-match, nothing else run.
    check(sorted(c[-2] for c in calls) == [f"={PROJECT}-app:0.0", f"={PROJECT}-director:0.0"],
          f"only the declared targets, exactly: {calls}")
    check(all(c[:2] == ["tmux", "display-message"] for c in calls), f"and only probes: {calls}")


def test_a_caller_who_could_not_act_gets_the_old_refusal_and_no_probe() -> None:
    message, calls = _diverged_recovery(
        environ={},
        declared=MEFP_DECLARED,
        panes={f"{PROJECT}-director:0.0": 294308},
        processes={294308: ["codex"]},
    )
    check("no live runtime assignment for configured role(s): app, director" in message,
          f"the refusal is unchanged: {message}")
    check("declared claude" not in message, f"and discloses nothing more: {message}")
    check(calls == [], f"and nothing was probed on their behalf: {calls}")


def test_no_provable_divergence_leaves_the_ordinary_refusal() -> None:
    # The pane runs what is declared: the reason is something else, and this
    # does not pretend to know it.
    message, _calls = _diverged_recovery(
        environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
        declared=MEFP_DECLARED,
        panes={f"{PROJECT}-director:0.0": 7},
        processes={7: ["claude"]},
    )
    check("no live runtime assignment for configured role(s)" in message, message)
    check("declared claude" not in message, message)

    # A pane whose process is not a runtime proves nothing about the runtime.
    message, _calls = _diverged_recovery(
        environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
        declared=MEFP_DECLARED,
        panes={f"{PROJECT}-director:0.0": 11},
        processes={11: ["/bin/bash", "-c", "codex"]},
    )
    check("runs bash" not in message and "declared claude" not in message,
          f"a shell in the pane is not reported as the worker: {message}")

    # A declared target outside this project is never probed.
    message, calls = _diverged_recovery(
        environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
        declared={"director": ("claude", "otherproject-director:0.0"), "app": ("claude", f"{PROJECT}-app:0.0")},
        panes={"otherproject-director:0.0": 294308},
        processes={294308: ["codex"]},
    )
    check(all("otherproject" not in c[-2] for c in calls), f"a foreign target is not probed: {calls}")
    check("declared claude" not in message, message)


def test_the_declared_target_is_probed_never_the_conventional_name() -> None:
    """A replacement pane may live at a recovery target (SYRD-69), so the
    conventional `<project>-<role>:0.0` is not evidence of anything. Here the
    declared Director lives at a recovery target, and a decoy sits at the
    conventional name: only the declared one may be read."""
    recovery = f"{PROJECT}-director-r2:0.0"
    message, calls = _diverged_recovery(
        environ={tl.TENANT_CONTROL_CALLER_ENV: FAKE_OPERATOR},
        declared={"director": ("claude", recovery), "app": ("claude", f"{PROJECT}-app:0.0")},
        panes={recovery: 294308, f"{PROJECT}-director:0.0": 999},
        processes={294308: ["codex", "resume"], 999: ["codex"]},
    )
    check(f"declared claude at {recovery}, but the live worker in that pane runs codex (pid 294308)"
          in message, f"the declared pane is the one described: {message}")
    check("pid 999" not in message, f"the decoy at the conventional name is not: {message}")
    check(f"={PROJECT}-director:0.0" not in [c[-2] for c in calls],
          f"and the conventional name was never probed: {calls}")


def test_a_dead_slot_is_not_attached_even_if_its_old_tty_is_reused() -> None:
    """Why `_proxy_attached` checks the pane is alive, not only its terminal.

    A dead pane still reports the terminal it had, and Linux hands a freed pty
    number to the next terminal that asks. If that terminal is attached to the
    Director -- someone's own `switchyard attach` -- the dead slot's old tty is
    in the Director's client list, and the tty test alone would call it
    connected.
    """
    def tmux_state(dead: str):
        def run(args, **kwargs):
            if args[:2] == ["tmux", "display-message"] and args[-1] == "#{pane_dead}":
                return types.SimpleNamespace(returncode=0, stdout=f"{dead}\n")
            if args[:2] == ["tmux", "display-message"] and args[-1] == "#{pane_tty}":
                return types.SimpleNamespace(returncode=0, stdout="/dev/pts/5\n")
            if args[:2] == ["tmux", "list-clients"]:
                return types.SimpleNamespace(returncode=0, stdout="/dev/pts/5\n")
            raise AssertionError(f"unexpected tmux call: {args}")
        return run

    with real_config() as cfg:
        director = pc._role_by_name(cfg, "director")
        check(pc._proxy_attached(cfg, 0, director, runner=tmux_state("0")) is True,
              "a live pane whose tty is a Director client is attached")
        check(pc._proxy_attached(cfg, 0, director, runner=tmux_state("1")) is False,
              "a dead pane is not, whoever now holds its old tty")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("operator_display_recovery_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"operator_display_recovery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
