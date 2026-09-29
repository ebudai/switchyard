#!/usr/bin/env python3
"""SYRD-359: `switchyard repair-boundary`, against the launcher it came out of.

`switchyard_repair_boundary_command` moved into
`scripts/repository_boundary_repair.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top. The
  provisioning and rollout-journal imports are still made inside the command, so
  their functions are read from those modules when it runs.
- **One object.** The launcher re-exports the command, the very same object
  whichever module is imported first. Its `euid_getter`, `runner` and
  `print_func` defaults are `os.geteuid`, `subprocess.run` and `print` themselves.
- **Seams (rule 24).** Every launcher facility it uses is read from the launcher
  when it runs, the boundary detector inside its lambda included. Nothing it
  binds -- the `say` closure, the lambda's parameter, the `except` binding -- is
  read through the launcher (rule 27).
- **The behaviour is unchanged:**
  - refused without root, or without an operator known through pkexec;
  - refused without root's provisioning record, a root-owned plan, a trusted
    owner, a usable release or a plan root can rebuild unchanged;
  - refused for a packet root does not control, cannot be read, or carries no
    valid boundary phase;
  - an already-closed boundary left alone, before any journal is opened;
  - otherwise journalled: opened before anything is tried, a dry run shown, each
    statement run in order and stopped at the first failure, the boundary read
    again, and success reported only when it is closed;
  - the journal is always written and closed, even on an exception.

Every facility is this test's own recording fake, installed before the command
runs. The effective uid is an injected answer, and the packet is a file in a
temporary directory this test creates. No statement is executed, and no ACL,
mode, journal, board, tmux session or display is touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
OWN = ("os", "subprocess", "Path", "Any", "Callable")
SEAMS = ("_validate_project_slug", "privileged_baseline_plan_path", "partial_provision_record", "read_plan_no_follow",
         "trusted_owner_identity", "_resume_source_release", "_resume_plan_from_record", "root_controlled_problems_for",
         "repository_boundary_problems")
SLUG = "p359"
OPERATOR = SimpleNamespace(name="syrd359-operator", source="pkexec", known=True)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one object for one block, as the suites do."""

    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.repository_boundary_repair as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_object_and_the_defaults() -> None:
    for order in (("scripts.repository_boundary_repair", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.repository_boundary_repair")):
        result = python("import importlib, os, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.repository_boundary_repair as m; "
                        "k = m.switchyard_repair_boundary_command.__kwdefaults__; "
                        "print(t.switchyard_repair_boundary_command is m.switchyard_repair_boundary_command, "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}), "
                        "k['euid_getter'] is os.geteuid and k['runner'] is subprocess.run and k['print_func'] is builtins.print)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_imports_and_the_bindings() -> None:
    module = ast.parse((ROOT / "scripts" / "repository_boundary_repair.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    command = next(n for n in module.body if isinstance(n, ast.FunctionDef))
    through = sorted(n.attr for n in ast.walk(command) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                     and n.value.id == "launcher")
    bare = sorted({n.id for n in ast.walk(command) if isinstance(n, ast.Name) and n.id in SEAMS})
    check(through == sorted(SEAMS) and not bare, f"each of the nine read once through the launcher, none bare: {through} {bare}")
    imports = [ast.unparse(n) for n in command.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(imports == ["from scripts import team_launcher as launcher",
                      "from scripts.ticket_board.project_provision import repository_boundary_phase, repository_boundary_statements",
                      "from scripts.ticket_board.rollout_journal import Attempt, resolve_operator"],
          f"the call-time import, then both of the command's own imports: {imports}")
    say = [n for n in ast.walk(command) if isinstance(n, ast.FunctionDef) and n.name == "say"]
    lam = [ast.unparse(n) for n in ast.walk(command) if isinstance(n, ast.Lambda)]
    check(len(say) == 1 and not [n for n in ast.walk(say[0]) if isinstance(n, ast.Attribute) and ast.unparse(n.value) == "launcher"]
          and lam == ["lambda p: launcher.repository_boundary_problems(p, runner=runner)"],
          f"the closure reads nothing of the launcher's, and the lambda only the detector: {lam}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    check(not [n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "switchyard_repair_boundary_command"]
          and len([n for n in ast.walk(ast.Module(body=launcher_body(ROOT, launcher), type_ignores=[])) if isinstance(n, ast.Call) and ast.unparse(n.func) == "switchyard_repair_boundary_command"]) == 1,
          "the launcher no longer defines it and dispatches by its own name")


# --- the command ----------------------------------------------------------------------------------------------------


class Journal:
    def __init__(self, log: list) -> None:
        self.log, self.operator = log, None

    def open(self) -> None:
        self.log.append(("journal-open",))

    def write(self, stream: str, text: str) -> None:
        self.log.append(("journal-write", stream, text))

    def close(self, *, status: str, exit_status: int, detail: str) -> None:
        self.log.append(("journal-close", status, exit_status, detail))


class Repair:
    """The command's facilities, answering from this test's objects, into one log."""

    def __init__(self, raw: str, *, record: bool = True, plan_problem: str = "", trusted: bool = True, release: str = "/rel",
                 release_problem: str = "", divergence: tuple = (), walk: tuple = (), packet: str | None = "PACKET",
                 phase: tuple = ("sudo setfacl a", "sudo setfacl b"), phase_problem: str = "", open_before: tuple = ("x is open",),
                 open_after: tuple = (), results: tuple = (0, 0), operator=OPERATOR, detector_error: Exception | None = None) -> None:
        self.dir = Path(raw) / "etc" / SLUG
        self.dir.mkdir(parents=True)
        self.baseline = self.dir / "plan.json"
        if packet is not None:
            (self.dir / "operator-commands.sh").write_text(packet, encoding="utf-8")
        self.record, self.plan_problem, self.trusted, self.release = record, plan_problem, trusted, release
        self.release_problem, self.divergence, self.walk = release_problem, list(divergence), list(walk)
        self.phase, self.phase_problem = list(phase), phase_problem
        self.opens = [list(open_before), list(open_after)]
        self.results, self.operator, self.detector_error = list(results), operator, detector_error
        self.plan = SimpleNamespace(name="rebuilt-plan")
        self.log: list[tuple] = []
        self.said: list[str] = []
        self.journal = Journal(self.log)

    def names(self) -> dict:
        L = self.log
        return dict(
            privileged_baseline_plan_path=lambda slug: L.append(("baseline?", slug)) or self.baseline,
            partial_provision_record=lambda slug: L.append(("record?", slug)) or ({"slug": slug} if self.record else None),
            read_plan_no_follow=lambda path, *, require_root_owned: L.append(("plan", path, require_root_owned))
                or ((None, self.plan_problem) if self.plan_problem else (SimpleNamespace(data={"source_repo": self.release}), "")),
            trusted_owner_identity=lambda slug: L.append(("owner?", slug))
                or SimpleNamespace(trusted=self.trusted, problems=[] if self.trusted else ["syrd359: owner unknown"]),
            _resume_source_release=lambda source: L.append(("fallback-release", source)) or (Path("/fallback"), self.release_problem),
            _resume_plan_from_record=lambda document, identity, *, source_repo: L.append(("rebuild", source_repo)) or (self.plan, list(self.divergence)),
            root_controlled_problems_for=lambda path: L.append(("walk", path)) or list(self.walk),
            repository_boundary_problems=lambda plan, *, runner: self.detect(plan, runner),
        )

    def detect(self, plan, runner):
        self.log.append(("detect", plan, runner))
        if self.detector_error:
            raise self.detector_error
        return self.opens.pop(0)

    def runner(self, argv, **kwargs):
        self.log.append(("run", list(argv), kwargs))
        code = self.results.pop(0)
        return SimpleNamespace(returncode=code, stdout=f"out-{len(self.results)}", stderr="")

    def run(self, **kwargs):
        from scripts import repository_boundary_repair as m, team_launcher
        from scripts.ticket_board import project_provision

        kwargs.setdefault("euid_getter", lambda: 0)
        kwargs.setdefault("operator_resolver", lambda: self.operator)
        kwargs.setdefault("journal", self.journal)
        with patched(team_launcher, **self.names()), \
                patched(project_provision, repository_boundary_phase=lambda packet: self.log.append(("phase", packet)) or (list(self.phase), self.phase_problem),
                        repository_boundary_statements=lambda phase: [[line] for line in phase]):
            return m.switchyard_repair_boundary_command(SLUG, runner=self.runner, print_func=self.said.append, **kwargs)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def test_authority_is_root_and_a_known_pkexec_operator() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd359-auth.") as raw:
        fx = Repair(raw)
        check(fx.run(euid_getter=lambda: 1000, apply=True) == 1 and fx.kinds() == [] and fx.said == [
            f"switchyard: repairing {SLUG}'s repository boundary changes ACLs and modes on root-owned surfaces. Run: "
            f"pkexec switchyard repair-boundary {SLUG} --apply"], f"not root: told how, nothing read: {fx.said}")
    for operator, named in ((SimpleNamespace(name="x", source="sudo", known=True), "sudo"),
                            (SimpleNamespace(name="x", source="pkexec", known=False), "pkexec"),
                            (SimpleNamespace(), "nothing that names a person")):
        with tempfile.TemporaryDirectory(prefix="syrd359-auth.") as raw:
            fx = Repair(raw, operator=operator)
            check(fx.run() == 1 and fx.kinds() == [] and f"This run was elevated by {named}, so there is nobody to record it against." in fx.said[0],
                  f"{named}: an operator's decision needs a known pkexec operator: {fx.said}")
    with tempfile.TemporaryDirectory(prefix="syrd359-auth.") as raw:
        fx = Repair(raw)
        from scripts import repository_boundary_repair as m, team_launcher
        try:
            with patched(team_launcher, **fx.names()):
                m.switchyard_repair_boundary_command("Bad Slug!", euid_getter=lambda: 0, print_func=fx.said.append); raised = None
        except SystemExit as exc:
            raised = str(exc)
        check(raised == "switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$" and fx.kinds() == [] and fx.said == [],
              f"a malformed slug is refused by the launcher's own validator first: {raised}")


def test_root_records_and_provenance_refusals() -> None:
    cases = [
        (dict(record=False), ["baseline?", "record?"], f"switchyard: root holds no provisioning record for {SLUG} at "),
        (dict(plan_problem="syrd359: plan is a link"), ["baseline?", "record?", "plan"], "switchyard: syrd359: plan is a link"),
        (dict(trusted=False), ["baseline?", "record?", "plan", "owner?"],
         f"switchyard: refusing to repair {SLUG}: root cannot establish whose installation this is. Nothing was changed."),
        (dict(release="", release_problem="syrd359: no release"), ["baseline?", "record?", "plan", "owner?", "fallback-release"],
         "switchyard: syrd359: no release"),
        (dict(divergence=("port: 1 vs 2",)), ["baseline?", "record?", "plan", "owner?", "rebuild"],
         f"switchyard: refusing to repair {SLUG}: root cannot rebuild its plan without changing what it installs. Nothing was changed."),
        (dict(walk=("syrd359: packet group-writable",)), ["baseline?", "record?", "plan", "owner?", "rebuild", "walk"],
         "switchyard: refusing to take a repair from a packet root does not control. Nothing was changed."),
        (dict(phase=(), phase_problem="syrd359: phase grew a systemctl"), ["baseline?", "record?", "plan", "owner?", "rebuild", "walk", "phase"],
         "switchyard: syrd359: phase grew a systemctl"),
        (dict(phase=()), ["baseline?", "record?", "plan", "owner?", "rebuild", "walk", "phase"], "switchyard: that packet carries no boundary phase"),
        (dict(phase_problem="syrd359: a second command rides a line"), ["baseline?", "record?", "plan", "owner?", "rebuild", "walk", "phase"],
         "switchyard: syrd359: a second command rides a line"),
    ]
    for kwargs, kinds, said in cases:
        with tempfile.TemporaryDirectory(prefix="syrd359-refuse.") as raw:
            fx = Repair(raw, **kwargs)
            check(fx.run(apply=True) == 1 and fx.kinds() == kinds and any(line.startswith(said) for line in fx.said)
                  and "journal-open" not in fx.kinds(), f"{kwargs}: refused at its own step, before any journal: {fx.kinds()} {fx.said}")
    with tempfile.TemporaryDirectory(prefix="syrd359-refuse.") as raw:
        fx = Repair(raw, packet=None)
        check(fx.run() == 1 and fx.said[-1].startswith(f"switchyard: {fx.dir / 'operator-commands.sh'} could not be read: "),
              "an unreadable packet is refused")
    with tempfile.TemporaryDirectory(prefix="syrd359-provenance.") as raw:
        fx = Repair(raw)
        fx.run()
        check(fx.log[2] == ("plan", fx.baseline, True) and ("rebuild", Path("/rel")) in fx.log and "fallback-release" not in fx.kinds()
              and ("walk", str(fx.dir / "operator-commands.sh")) in fx.log and ("phase", "PACKET") in fx.log,
              f"root's own plan read root-owned; the recorded release; root's packet beside it: {fx.log[:8]}")
        fx = Repair(tempfile.mkdtemp(prefix="syrd359-fallback.", dir=raw), release="")
        fx.run()
        check(("fallback-release", None) in fx.log and ("rebuild", Path("/fallback")) in fx.log, "no recorded release: the fallback's")


def test_an_already_closed_boundary_is_left_alone_before_any_journal() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd359-closed.") as raw:
        fx = Repair(raw, open_before=())
        check(fx.run(apply=True) == 0 and fx.said == [f"switchyard: {SLUG}'s repository boundary is already closed; nothing to do."]
              and "journal-open" not in fx.kinds() and "run" not in fx.kinds(), f"nothing journalled or run: {fx.kinds()}")
    with tempfile.TemporaryDirectory(prefix="syrd359-closed.") as raw:
        fx = Repair(raw, open_before=())
        fx.run(boundary_reader=lambda plan: fx.log.append(("reader", plan)) or [])
        check(("reader", fx.plan) in fx.log and "detect" not in fx.kinds(), "an injected reader is used instead of the launcher's")
        fx2 = Repair(tempfile.mkdtemp(prefix="syrd359-detect.", dir=raw), open_before=())
        fx2.run()
        check(next(e for e in fx2.log if e[0] == "detect")[1:] == (fx2.plan, fx2.runner), "the launcher's detector, with the runner")


def test_a_dry_run_is_shown_and_journalled() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd359-dry.") as raw:
        fx = Repair(raw)
        check(fx.run(apply=False) == 0 and "run" not in fx.kinds(), "nothing runs")
        expected = [f"switchyard: {SLUG}'s repository boundary is open:", "    x is open",
                    f"switchyard: the repair, taken from {fx.dir / 'operator-commands.sh'}:", "    sudo setfacl a", "    sudo setfacl b",
                    f"switchyard: dry run; nothing was changed. Apply it with `pkexec switchyard repair-boundary {SLUG} --apply`."]
        check(fx.said == expected and fx.log[-3] == ("journal-open",) and fx.log[-2] == ("journal-write", "stdout", "\n".join(expected) + "\n")
              and fx.log[-1] == ("journal-close", "completed", 0, "dry-run") and fx.journal.operator is OPERATOR,
              f"shown, the whole output journalled, closed completed: {fx.log[-3:]}")


def test_statements_run_in_order_and_stop_at_the_first_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd359-fail.") as raw:
        fx = Repair(raw, results=(0, 3))
        check(fx.run(apply=True) == 1, "a failed statement answers 1")
        runs = [e for e in fx.log if e[0] == "run"]
        check([r[1] for r in runs] == [["sh", "-c", "sudo setfacl a"], ["sh", "-c", "sudo setfacl b"]]
              and all(r[2] == dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for r in runs),
              f"each statement through sh -c with its output captured: {runs}")
        check(fx.said[-4:] == ["    out-1", "    out-0", "switchyard: sudo setfacl b failed with exit 3.",
                               "switchyard: the repair stopped there. What ran before it stands; running this again resumes from what is still open."]
              and fx.kinds().count("detect") == 1 and fx.log[-1] == ("journal-close", "failed", 1, "failed mid-phase"),
              f"its output said, stopped there, not re-read, journalled as failed: {fx.said[-4:]}")


def test_success_only_when_the_boundary_reads_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd359-still.") as raw:
        fx = Repair(raw, open_after=("y still open",))
        check(fx.run(apply=True) == 1 and fx.said[-2:] == ["switchyard: still open after the repair: y still open",
                                                           "switchyard: the repair ran and the boundary is not closed, so this does not report success."]
              and fx.log[-1] == ("journal-close", "failed", 1, "still open"), f"still open: refused: {fx.said[-2:]}")
    with tempfile.TemporaryDirectory(prefix="syrd359-ok.") as raw:
        fx = Repair(raw)
        check(fx.run(apply=True) == 0 and fx.said[-2] == f"switchyard: {SLUG}'s repository boundary is closed (repaired by syrd359-operator via pkexec)"
              and fx.log[-1] == ("journal-close", "completed", 0, "repaired") and fx.kinds().count("detect") == 2,
              f"read again, closed, repaired: {fx.said[-2:]}")


def test_the_journal_is_opened_first_and_closed_even_on_an_exception() -> None:
    boom = OSError("syrd359: the shell raised")
    with tempfile.TemporaryDirectory(prefix="syrd359-raise.") as raw:
        fx = Repair(raw)

        def raising(argv, **kwargs):
            fx.log.append(("run", list(argv), kwargs))
            raise boom

        from scripts import repository_boundary_repair as m, team_launcher
        from scripts.ticket_board import project_provision
        with patched(team_launcher, **fx.names()), \
                patched(project_provision, repository_boundary_phase=lambda packet: (list(fx.phase), ""),
                        repository_boundary_statements=lambda phase: [[line] for line in phase]):
            try:
                m.switchyard_repair_boundary_command(SLUG, apply=True, euid_getter=lambda: 0, operator_resolver=lambda: OPERATOR,
                                                     runner=raising, journal=fx.journal, print_func=fx.said.append); raised = None
            except OSError as exc:
                raised = exc
        check(raised is boom and fx.kinds()[-4:] == ["journal-open", "run", "journal-write", "journal-close"]
              and fx.log[-1] == ("journal-close", "failed", 1, ""), f"propagated, and still written and closed as failed: {fx.log[-4:]}")


def test_the_default_journal_is_the_rollout_attempt() -> None:
    from scripts.ticket_board import rollout_journal

    made: list = []

    class Attempt:
        def __init__(self, slug, argv, *, operator):
            made.append((slug, argv, operator)); self.log = []

        def open(self):
            made.append("open")

        def write(self, stream, text):
            made.append(("write", stream))

        def close(self, **kw):
            made.append(("close", kw))

    with tempfile.TemporaryDirectory(prefix="syrd359-attempt.") as raw:
        fx = Repair(raw)
        with patched(rollout_journal, Attempt=Attempt):
            fx.run(apply=True, journal=None)
    check(made[0] == (SLUG, ["switchyard", "repair-boundary", SLUG, "--apply"], "syrd359-operator") and made[1] == "open"
          and made[-1] == ("close", dict(status="completed", exit_status=0, detail="repaired")),
          f"the rollout attempt for the command's own argv and the operator's name: {made}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_object_and_the_defaults",
             "test_the_seams_the_local_imports_and_the_bindings")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"repository_boundary_repair_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
