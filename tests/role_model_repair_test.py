#!/usr/bin/env python3
"""SYRD-250 DAT: there has to be a supported way to change a bad model.

The launch gate stops a role whose model its owner's account does not list, and
tells the operator to repair it. That is only worth anything if the command it
names exists, runs, and changes the thing.

It did not. `set-role-runtime` computed a model ONLY when the runtime changed,
and `preflight.is_noop` looked at the runtime alone -- so for `test2`'s audit
role, already on `agy`, the command reported "no change" and kept the model the
account does not recognise. There was no supported way to fix it at all.

The message was wrong too: `switchyard set-role-runtime <role>` omits the
required project argument, so it could not even be run as printed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import role_runtime, team_launcher  # noqa: E402
from role_runtime_test import (  # noqa: E402
    DIRECTOR_ENV,
    FakeBoard,
    RuntimeRunner,
    _fake_start,
    _idle_state,
    _ready,
)

CHECKS = 0

#: test2's shape: the audit role on Antigravity, configured for a slug the
#: tenant owner's own `agy` does not list.
BAD_MODEL = "gemini-3.7-flash-high"
OWNER_LISTS = ["gemini-3.7-pro", "gemini-3.5-flash"]
#: Deliberately an account that does NOT exist on this host. `test2-agent`
#: does, and every owner-derived path -- the runtime journal among them --
#: would then resolve into a live tenant's real home. A non-existent owner
#: whose name is a component of the config path resolves inside the sandbox
#: instead (see `default_layout_output_path`).
OWNER = "syrd250-owner"


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class OwnerRunner(RuntimeRunner):
    """A tmux/agy runner whose `agy models` answers only for the owner."""

    def __init__(self) -> None:
        super().__init__()
        self.asked_as: list[str] = []

    def __call__(self, args, **kwargs):
        argv = list(args)
        if argv[-2:] == ["agy", "models"]:
            as_owner = argv[:2] == ["sudo", "-u"] and argv[2] == OWNER
            self.asked_as.append(OWNER if as_owner else "caller")
            listed = OWNER_LISTS if as_owner else [BAD_MODEL, *OWNER_LISTS]
            return subprocess.CompletedProcess(
                argv, 0, stdout="".join(f"{m} a description\n" for m in listed)
            )
        return super().__call__(args, **kwargs)


def _write_tenant(root: Path, *, model: str = BAD_MODEL, cli: str = "agy") -> Path:
    """A one-role tenant in test2's shape, wholly inside `root`.

    Nested under a directory named for the owner on purpose: that is what keeps
    the runtime journal and the layout projection inside the sandbox instead of
    in `/home/<owner>`.
    """
    root = root / OWNER
    provision = root / ".switchyard" / "provision"
    provision.mkdir(parents=True)
    layout = provision / "porter-konsole-layout.json"
    layout.write_text(
        json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    workdir = root / "audit"
    workdir.mkdir(parents=True)
    entry = {
        "role": "audit",
        "slot": 0,
        "detached": False,
        "target": "porter-audit:0.0",
        "tmux_session": "porter-audit",
        "workdir": str(workdir),
        "cli": [cli],
        "live_commands": [cli],
    }
    if model:
        entry["model"] = model
    config_path = provision / "porter.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "porter",
                "layout": str(layout),
                "session_dir": str(root / "sessions"),
                "board_url": "http://127.0.0.1:65535",
                "run_as_user": OWNER,
                "presentation": {"slot_count": 2},
                "roles": [entry],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _model_in(config_path: Path, role: str = "audit") -> str | None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return next(r.get("model") for r in raw["roles"] if r["role"] == role)


def _repair(config_path: Path, *, board: FakeBoard, runner: RuntimeRunner, **kwargs):
    """`switchyard set-role-runtime`, with a fake board and a fake worker.

    `switch_role_runtime` is called for real -- only the board and the process
    start are stand-ins -- so what these cases assert is the effect on the
    config and the session, not that an argument was forwarded.
    """
    real = role_runtime.switch_role_runtime

    def with_fakes(*args, **kw):
        kw.setdefault("client", board)
        kw.setdefault("workflow_reader", board.reader)
        kw.setdefault("start", _fake_start)
        kw.setdefault("busy_check", lambda *_a, **_k: False)
        kw.setdefault("environ", DIRECTOR_ENV)
        kw.setdefault("pane_state_dir", config_path.parent / "pane-state")
        return real(*args, **kw)

    role_runtime.switch_role_runtime = with_fakes
    try:
        return team_launcher.set_project_role_runtime_command(
            team_launcher.load_project_config("porter", config_path),
            config_path=config_path,
            role_name="audit",
            runner=runner,
            **kwargs,
        )
    finally:
        role_runtime.switch_role_runtime = real


def test_a_model_only_change_is_no_longer_mistaken_for_no_change() -> None:
    """The preflight defect, at its own level.

    `is_noop` compared runtimes and nothing else, so the one repair this ticket
    needs returned "nothing to do" and wrote nothing.
    """
    with tempfile.TemporaryDirectory(prefix="syrd250-noop.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        config = team_launcher.load_project_config("porter", config_path)
        board = FakeBoard()

        same_runtime_new_model, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=OWNER_LISTS[0],
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: False,
        )
        nothing_to_say, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=None,
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: False,
        )
        same_model, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=BAD_MODEL,
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: False,
        )

    check(same_runtime_new_model.is_noop is False,
          "the same runtime with a different model is a real change")
    check(nothing_to_say.is_noop is True,
          "a caller with no opinion about the model still gets the old answer")
    check(same_model.is_noop is True,
          "and asking for the model it already has is still nothing to do")
    check(same_runtime_new_model.current_model == BAD_MODEL,
          f"the preflight reports what it runs today: {same_runtime_new_model.current_model}")


def test_the_repair_actually_changes_the_model_and_restarts_the_role() -> None:
    """test2's shape, through the command the stop message names."""
    with tempfile.TemporaryDirectory(prefix="syrd250-repair.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        board, runner = FakeBoard(), OwnerRunner()

        rc = _repair(
            config_path, board=board, runner=runner,
            runtime="agy", model=OWNER_LISTS[0],
            interactive=False, print_func=lambda _l: None,
        )

        written = _model_in(config_path)
        killed, started = runner.sessions_killed(), runner.sessions_started()

    check(rc == 0, f"the command succeeds: {rc}")
    check(written == OWNER_LISTS[0], f"the config carries the new model: {written}")
    check("porter-audit" in killed and "porter-audit" in started,
          f"and the role was restarted so it takes effect: {killed} / {started}")


def test_a_model_the_owner_does_not_offer_is_refused_not_written() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd250-refuse.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        runner = OwnerRunner()
        refusal = ""
        try:
            _repair(
                config_path, board=FakeBoard(), runner=runner,
                runtime="agy", model="gemini-9-imaginary",
                interactive=False, print_func=lambda _l: None,
            )
        except SystemExit as exc:
            refusal = str(exc)

        check(_model_in(config_path) == BAD_MODEL,
              "the configured value is untouched by a refused change")

    check("does not offer" in refusal, f"and the refusal says why: {refusal}")
    check("gemini-9-imaginary" in refusal, f"naming what was asked for: {refusal}")
    check(OWNER in refusal, f"and whose account said so: {refusal}")
    check("gemini-3.7-pro" in refusal, f"and what it does offer: {refusal}")
    check(runner.asked_as == [OWNER],
          f"which it learned from the OWNER, not the caller: {runner.asked_as}")


def test_with_nobody_to_ask_the_refusal_names_a_command_that_runs() -> None:
    """The message the first candidate got wrong, checked against the parser."""
    with tempfile.TemporaryDirectory(prefix="syrd250-hint.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        said: list[str] = []
        refusal = ""
        try:
            _repair(
                config_path, board=FakeBoard(), runner=OwnerRunner(),
                runtime="agy", interactive=False, print_func=said.append,
            )
        except SystemExit as exc:
            refusal = str(exc)

        check(_model_in(config_path) == BAD_MODEL, "nothing was changed")

    told = "\n".join(said)
    check("does not offer" in told, f"the problem is stated: {told}")
    check(BAD_MODEL in told, f"naming the model: {told}")
    check("set-role-runtime porter audit --cli agy --model" in refusal,
          f"and the remedy is a real command: {refusal}")

    # Parsed by the real parser, so "it runs" is not a guess.
    # `switchyard set-role-runtime <project> <role> ...` -- the parser is
    # handed everything after the subcommand, exactly as the CLI does.
    suggested = refusal.split("`")[1].split()
    check(suggested[:2] == ["switchyard", "set-role-runtime"],
          f"the command names itself properly: {suggested}")
    parser = team_launcher._build_switchyard_set_role_runtime_parser()
    args = parser.parse_args(suggested[2:])
    check(args.project == "porter", f"project: {args.project}")
    check(args.role == "audit", f"role: {args.role}")
    check(args.cli == "agy", f"cli: {args.cli}")
    check(args.model in OWNER_LISTS, f"and a model the owner actually has: {args.model}")


def test_the_suggested_command_is_the_one_that_repairs_it() -> None:
    """End to end: run what the refusal printed, and check the result."""
    with tempfile.TemporaryDirectory(prefix="syrd250-loop.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        refusal = ""
        try:
            _repair(
                config_path, board=FakeBoard(), runner=OwnerRunner(),
                runtime="agy", interactive=False, print_func=lambda _l: None,
            )
        except SystemExit as exc:
            refusal = str(exc)

        args = team_launcher._build_switchyard_set_role_runtime_parser().parse_args(
            refusal.split("`")[1].split()[2:]
        )
        rc = _repair(
            config_path, board=FakeBoard(), runner=OwnerRunner(),
            runtime=args.cli, model=args.model,
            interactive=False, print_func=lambda _l: None,
        )
        written = _model_in(config_path)

    check(rc == 0, f"the printed command succeeds: {rc}")
    check(written == args.model, f"and repairs the role: {written}")
    check(written != BAD_MODEL, "which is the whole point")


def test_at_a_terminal_the_owners_list_is_offered_for_the_same_runtime() -> None:
    """No `--model`, a terminal, and a model the account does not list."""
    with tempfile.TemporaryDirectory(prefix="syrd250-ask.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        runner = OwnerRunner()
        shown: list[str] = []

        rc = _repair(
            config_path, board=FakeBoard(), runner=runner,
            runtime="agy", interactive=True,
            input_func=lambda _p: "",  # the default, whatever the list offers first
            print_func=shown.append,
        )
        written = _model_in(config_path)

    offered = "\n".join(shown)
    check(rc == 0, f"the repair completes: {rc}")
    check(written in OWNER_LISTS, f"on a model the owner has: {written}")
    check(BAD_MODEL not in offered.split("audit model:")[-1],
          f"and the refused slug was not offered back: {offered}")
    check(runner.asked_as and set(runner.asked_as) == {OWNER},
          f"the list came from the owner, never the caller: {runner.asked_as}")


def test_a_model_the_owner_does_offer_is_left_alone_and_nothing_is_asked() -> None:
    """The ordinary case stays ordinary: no prompt, no change, no restart."""
    with tempfile.TemporaryDirectory(prefix="syrd250-fine.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root, model=OWNER_LISTS[0])
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        runner = OwnerRunner()

        rc = _repair(
            config_path, board=FakeBoard(), runner=runner,
            runtime="agy", interactive=True,
            input_func=lambda _p: (_ for _ in ()).throw(
                AssertionError("nothing should have been asked")
            ),
            print_func=lambda _l: None,
        )

    check(rc == 0, f"the command is a no-op that succeeds: {rc}")
    check(runner.sessions_killed() == [], "and nothing was restarted")


def test_a_busy_role_is_not_restarted_for_a_model_change_either() -> None:
    """A model change restarts the role, so it earns the same protection.

    The readiness and busy checks used to be gated on "is the RUNTIME
    changing", which would have let a model-only repair kill a role mid-turn
    without the `--force`/`--reason` that exists to make that deliberate.
    """
    with tempfile.TemporaryDirectory(prefix="syrd250-busy.") as tmp:
        root = Path(tmp)
        config_path = _write_tenant(root)
        _idle_state(config_path, "porter-audit:0.0")
        role_runtime._readiness_blockers = _ready
        config = team_launcher.load_project_config("porter", config_path)
        board = FakeBoard()

        busy_model_change, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=OWNER_LISTS[0],
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: True,
        )
        forced, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=OWNER_LISTS[0], force=True,
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: True,
        )
        busy_but_nothing_asked, _doc = role_runtime.preflight(
            config, config_path=config_path, role_name="audit",
            runtime="agy", model=None,
            workflow_reader=board.reader, runner=OwnerRunner(),
            pane_state_dir=config_path.parent / "pane-state",
            busy_check=lambda *_a, **_k: True,
        )

    check(busy_model_change.blockers != (),
          "a busy role is refused a model change it did not consent to")
    check(any("busy" in blocker for blocker in busy_model_change.blockers),
          f"and told why: {busy_model_change.blockers}")
    check(any("--force" in blocker for blocker in busy_model_change.blockers),
          f"and how to mean it: {busy_model_change.blockers}")
    check(forced.blockers == (),
          f"--force is how somebody says they mean it: {forced.blockers}")
    check(busy_but_nothing_asked.blockers == (),
          f"and a caller changing nothing is not refused: {busy_but_nothing_asked.blockers}")



def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_model_repair_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
