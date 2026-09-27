#!/usr/bin/env python3
"""SYRD-375: `switchyard new`'s support helpers and stage reporting, against the launcher they came out of.

Eighteen top-level definitions -- the role-plan checks, the owner and path
names, the initial artifact and its ownership, the first-run worktrees, the
confirmation, the wrapper's result file, the presentation announcement,
`ProvisioningStages` and their constants -- moved unchanged into
`scripts/new_project_support.py`, and the launcher re-exports them. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads only the ticket-board leaf it takes its default from.
- **The defaults are the same objects**, bound when the definitions run: the
  announcement's `report` is the moved writer, the stages' clock is
  `time.monotonic`, the artifact's implementer roles are the leaf's.
- **Seams (rule 24)**: every launcher name these bodies read, and every name
  defined here that another definition here reads when it runs, is read
  through the launcher as often as before -- so a patch on the launcher reaches
  each of them, which this test shows for all of them.
- **The behaviour is unchanged**, the privileged result-file writer above all:
  it still parses the caller's ids, refuses a bad project name before opening
  anything, hands the write to `_run_as_account`, opens without following a
  link or creating a file, checks the file is a regular file of that uid
  before writing, and always closes it.

Every trust boundary is this test's own fake: the account switch runs the
write in this process, as this user, against files this test owns; the
runner, the prompt, the clock and every launcher helper are stand-ins. No
account is switched and no child is started.
"""

from __future__ import annotations

import ast
import errno
import inspect
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import new_project_support as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
MOVED = ("SWITCHYARD_DESIGN_FILE_NAME", "NEW_RESULT_FILE_ENV", "NEW_PROJECT_REQUIRED_ROLES", "NEW_PROJECT_FIXED_ROLE_NAMES",
         "_dedupe_role_cli_pairs", "_require_new_project_roles", "print_role_plan_review", "_agent_owner_user", "_project_dir",
         "_resolve_project_path", "_write_initial_switchyard_project_artifact", "_chown_switchyard_project_files",
         "_prepare_first_run_auth_worktrees", "_confirm_switchyard_new", "_report_new_project_to_caller",
         "announce_new_project_presentation", "ProvisioningStages", "NEW_PROJECT_STAGES")
#: Measured on the baseline launcher: each moved body's reads of launcher globals (annotations and defaults excluded).
SEAMS = {
    '_dedupe_role_cli_pairs': {'NEW_PROJECT_FIXED_ROLE_NAMES': 1, 'NEW_PROJECT_RESERVED_ROLE_NAMES': 1, '_validate_new_project_cli': 1, '_validate_new_project_implementer_role': 1},
    '_require_new_project_roles': {'NEW_PROJECT_REQUIRED_ROLES': 1},
    '_project_dir': {'_slug_from_project_name': 1},
    '_write_initial_switchyard_project_artifact': {'PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS': 1, 'PROJECT_DESIGN_DEFAULT_GATES': 1, 'ProjectDesignArtifact': 1, '_dedupe_role_cli_pairs': 1, '_default_role_cli_pairs': 1, '_project_design_markdown': 1, '_write_json_atomic': 1, 'project_design_artifact_payload': 1, 'runtime_catalog': 1, 'validate_ticket_prefix': 1},
    '_chown_switchyard_project_files': {'_switchyard_dir': 2},
    '_prepare_first_run_auth_worktrees': {'_control_repository_owned_roots': 1, '_owner_project_git_runner': 1, 'ensure_project_worktrees': 1},
    '_confirm_switchyard_new': {'_read_prompt': 1},
    '_report_new_project_to_caller': {'PROJECT_SLUG_RE': 1, '_int_env': 2, '_run_as_account': 1},
    'announce_new_project_presentation': {'LAYOUT_MODE_VIEWER': 1, 'NEW_RESULT_FILE_ENV': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- SystemExit is an answer here, and so is anything a mutant raises
        return exc


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


def seam(name: str, function):
    """A launcher stand-in that records, when it is called, that the launcher's name was reached."""
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "new_project_support.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name
             or isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_its_leaf_at_import() -> None:
    result = python("import sys, scripts.new_project_support as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    loaded = result.stdout.strip()
    check(result.returncode == 0 and "'scripts.ticket_board.project_provision'" in loaded,
          f"it imports on its own, its default's leaf included: {loaded}{result.stderr[-400:]}")
    names = ast.literal_eval(loaded)
    check(all(n == "scripts.ticket_board" or n.startswith("scripts.ticket_board.") for n in names) and "scripts.team_launcher" not in names,
          f"and nothing of Switchyard's but that leaf's own package -- the launcher least of all: {names}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.new_project_support", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_support")):
        result = python("import importlib, inspect, time; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_support as m; "
                        "from scripts.ticket_board import project_provision as pp; "
                        "d = lambda f, n: inspect.signature(f).parameters[n].default; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "d(m.announce_new_project_presentation, 'report') is m._report_new_project_to_caller is t._report_new_project_to_caller, "
                        "d(m.ProvisioningStages.__init__, 'monotonic') is time.monotonic, "
                        "d(m._write_initial_switchyard_project_artifact, 'implementer_roles') is pp.DEFAULT_PROJECT_IMPLEMENTER_ROLES)")
        check(result.stdout.strip() == "True True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    signature = lambda f, n: inspect.signature(f).parameters[n].default
    check(signature(m.announce_new_project_presentation, "report") is m._report_new_project_to_caller,
          "the announcement's default report is the moved writer itself")
    check(signature(m.ProvisioningStages.__init__, "monotonic") is time.monotonic, "the stages' clock is time.monotonic")
    check(signature(m._write_initial_switchyard_project_artifact, "implementer_roles") is project_provision.DEFAULT_PROJECT_IMPLEMENTER_ROLES
          == ("app", "main"), "the artifact's implementer roles are the leaf's own tuple")
    check(m.os is os and m.time is time and m.Path is Path, "the standard-library names are the module's own, the same objects")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    for name in MOVED:
        node = module_def(name)
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher"]
                  and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs (after its docstring): {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's, imports nothing: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        check(not bound & set(through),
              f"{name}: nothing it binds itself is read through the launcher: {bound & set(through)}")
    tree = ast.parse((ROOT / "scripts" / "new_project_support.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("team_launcher" in line for line in top), f"the launcher is never imported at the top: {top}")
    check([getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
          == list(MOVED), "the eighteen are defined in the launcher's own order, the writer before the announcement")


def test_the_launcher_reexports_the_eighteen() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.new_project_support"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the eighteen, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets
                                                               if isinstance(x, ast.Name)}
    check(not defined & set(MOVED), f"and the launcher defines none of them itself: {defined & set(MOVED)}")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_constants() -> None:
    check(m.SWITCHYARD_DESIGN_FILE_NAME == "PROJECT_DESIGN.md" and m.NEW_RESULT_FILE_ENV == "SWITCHYARD_NEW_RESULT_FILE",
          "the design file and the wrapper's variable")
    check(m.NEW_PROJECT_REQUIRED_ROLES == ("director",) and m.NEW_PROJECT_FIXED_ROLE_NAMES == frozenset({"designer", "director", "audit"}),
          "the required and fixed roles")
    check(m.NEW_PROJECT_STAGES == ("host and agent CLI checks", "project accounts and files", "database and board",
                                   "provider sign-in and folder trust", "role panes"), "the stages, in order")


def test_role_pairs_are_normalized_deduplicated_and_reserved_names_refused() -> None:
    validated: list = []
    with patched(t, NEW_PROJECT_RESERVED_ROLE_NAMES=frozenset({"designer", "director", "audit", "user", "keeper"}),
                 NEW_PROJECT_FIXED_ROLE_NAMES=frozenset({"designer", "director", "audit", "keeper"}),
                 _validate_new_project_implementer_role=seam("_validate_new_project_implementer_role",
                                                             lambda role: validated.append(("role", role)) or role),
                 _validate_new_project_cli=seam("_validate_new_project_cli",
                                                lambda cli, *, context: validated.append(("cli", cli, context)) or f"<{cli}>")):
        pairs = judged(m._dedupe_role_cli_pairs, [(" Main ", "codex"), ("keeper", "claude"), ("main", "agy"), ("Director", "claude")])
        check(pairs == (("main", "<codex>"), ("keeper", "<claude>"), ("director", "<claude>")),
              f"normalized, first pairing kept, a fixed reserved name allowed: {pairs}")
        check(validated == [("role", "main"), ("cli", "codex", "CLI for main"), ("cli", "claude", "CLI for keeper"),
                            ("role", "main"), ("cli", "agy", "CLI for main"), ("cli", "claude", "CLI for director")],
              f"a non-reserved role validated, every CLI validated, even a repeat's: {validated}")
        refused = judged(m._dedupe_role_cli_pairs, [("User", "codex")])
        check(isinstance(refused, SystemExit) and str(refused) == "role 'user' is reserved", f"a reserved, non-fixed name: {refused!r}")
    REACHED.update({"NEW_PROJECT_RESERVED_ROLE_NAMES", "NEW_PROJECT_FIXED_ROLE_NAMES"})  # 'keeper' took both launcher values


def test_required_roles() -> None:
    with patched(t, NEW_PROJECT_REQUIRED_ROLES=("director", "keeper", "scribe")):
        refused = judged(m._require_new_project_roles, [("director", "claude"), ("main", "codex")])
        check(isinstance(refused, SystemExit) and str(refused) == "switchyard: required roles missing: keeper, scribe",
              f"every missing role named, in order: {refused!r}")
        check(m._require_new_project_roles([("scribe", "x"), ("keeper", "y"), ("director", "z")]) is None, "all present: nothing")
    REACHED.add("NEW_PROJECT_REQUIRED_ROLES")
    check(isinstance(judged(m._require_new_project_roles, []), SystemExit), "the director is required by default")


def test_the_role_plan_review() -> None:
    said: list[str] = []
    plan = [SimpleNamespace(role="director", cli="claude", model="", effort=""),
            SimpleNamespace(role="main", cli="codex", model="gpt", effort="high")]
    m.print_role_plan_review(plan, print_func=said.append)
    check(said == ["switchyard: roles to create:", "  director: claude", "  main: codex -> gpt -> effort high"], f"{said}")


def test_owner_and_project_paths() -> None:
    check(m._agent_owner_user(" syrd375 ") == "syrd375-agent" and m._agent_owner_user("x-agent") == "x-agent", "the -agent suffix, once")
    refused = judged(m._agent_owner_user, "  ")
    check(isinstance(refused, SystemExit) and str(refused) == "switchyard: agent name cannot be empty", f"{refused!r}")
    with patched(t, _slug_from_project_name=seam("_slug_from_project_name", lambda name: f"slug<{name}>")):
        check(m._project_dir(Path("/nonexistent/syrd375"), "owner", "My Project")
              == Path("/nonexistent/syrd375/owner/Projects/slug<My Project>"), "home / owner / Projects / slug")
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("SYRD375_BASE")
        os.environ["SYRD375_BASE"] = tmp
        try:
            resolved = judged(m._resolve_project_path, "$SYRD375_BASE/a/../b")
        finally:
            if saved is None:
                os.environ.pop("SYRD375_BASE")
            else:
                os.environ["SYRD375_BASE"] = saved
        check(resolved == Path(tmp).resolve() / "b", f"variables expanded and the path resolved, not required to exist: {resolved}")


class Artifact:
    def __init__(self, **fields: object) -> None:
        self.fields = fields


def test_the_initial_artifact() -> None:
    written: list = []
    calls: list = []
    stand_ins = dict(
        ProjectDesignArtifact=seam("ProjectDesignArtifact", Artifact),
        validate_ticket_prefix=seam("validate_ticket_prefix", lambda slug: calls.append(("prefix", slug)) or slug.upper()),
        _dedupe_role_cli_pairs=seam("_dedupe_role_cli_pairs", lambda pairs: calls.append(("dedupe", pairs)) or ("deduped",)),
        _default_role_cli_pairs=seam("_default_role_cli_pairs", lambda roles, **kw: calls.append(("defaults", roles, kw)) or (("d", "c"),)),
        runtime_catalog=SimpleNamespace(CATALOG_VERSION=375),
        PROJECT_DESIGN_DEFAULT_GATES={"gate": "g"},
        PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS={"grant": "x", "shell": "old"},
        _project_design_markdown=seam("_project_design_markdown", lambda slug, *, title, body: f"# {slug} {title} {body}"),
        _write_json_atomic=seam("_write_json_atomic", lambda path, payload: written.append((path, payload))),
        project_design_artifact_payload=seam("project_design_artifact_payload", lambda artifact: ("payload", artifact)),
    )
    with tempfile.TemporaryDirectory() as tmp, patched(t, **stand_ins):
        base = Path(tmp)
        kwargs = dict(project_name="Proj", slug="p375", owner_user="o", project_dir=base / "repo",
                      artifact_path=base / "a" / "b" / "artifact.json", design_document=base / "DESIGN.md", owner_shell="/bin/zsh")
        check(judged(m._write_initial_switchyard_project_artifact, **kwargs) is None, "written, nothing refused")
        check(len(written) == 1 and written[0][0] == kwargs["artifact_path"] and written[0][1][0] == "payload", f"one JSON write: {written}")
        fields = written[0][1][1].fields
        check(fields["implementer_roles"] == ("app", "main") and fields["audit_roles"] == ("audit",) and fields["ticket_prefix"] == "P375"
              and fields["role_clis"] == ("deduped",) and fields["catalog_version"] == 0 and fields["gates"] == {"gate": "g"}
              and fields["capability_grants"] == {"grant": "x", "shell": "/bin/zsh", "agy_credential_source": "",
                                                  "agy_credential_source_origin": "unset"}
              and fields["include_audit"] is True and fields["push_policy"] == "director-main-only", f"default fields: {fields}")
        check(calls[1] == ("defaults", ("app", "main"), {"include_designer": True, "include_audit": True, "audit_roles": ("audit",)})
              and calls[2] == ("dedupe", (("d", "c"),)), f"no role CLIs given: the defaults, deduplicated: {calls}")
        check(kwargs["artifact_path"].parent.is_dir() and kwargs["design_document"].read_text(encoding="utf-8") == "# p375 Proj Design in progress.",
              "the artifact's directory made and the designer's document started")
        kwargs["design_document"].write_text("kept", encoding="utf-8")
        written.clear(); calls.clear()
        m._write_initial_switchyard_project_artifact(**kwargs, role_clis=[("r", "c")], role_models={"z": "1", "a": "2"},
                                                     role_efforts={"b": "hi"}, include_designer=False, include_audit=False,
                                                     implementer_roles=["x"])
        fields = written[0][1][1].fields
        check(fields["role_models"] == (("a", "2"), ("z", "1")) and fields["role_efforts"] == (("b", "hi"),)
              and fields["catalog_version"] == 375 and fields["audit_roles"] == () and fields["include_audit"] is False
              and fields["implementer_roles"] == ("x",) and calls[-1] == ("dedupe", [("r", "c")]),
              f"models and efforts sorted, the catalog version taken, given CLIs deduplicated: {fields} {calls}")
        check(kwargs["design_document"].read_text(encoding="utf-8") == "kept", "no designer: the document is left alone")
        m._write_initial_switchyard_project_artifact(**kwargs)
        check(kwargs["design_document"].read_text(encoding="utf-8") == "kept", "and a designer's existing document is never rewritten")
        written.clear()
        refused = judged(m._write_initial_switchyard_project_artifact, **kwargs, implementer_roles=["main", "audit"])
        check(isinstance(refused, SystemExit) and str(refused) == "roles cannot be both implementers and auditors: audit" and not written,
              f"an overlap refused before anything is written: {refused!r}")
    REACHED.update({"runtime_catalog", "PROJECT_DESIGN_DEFAULT_GATES", "PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS"})


def test_the_project_files_are_handed_to_the_owner() -> None:
    ran: list = []
    for code in (0, 1):
        with patched(t, _switchyard_dir=seam("_switchyard_dir", lambda d: d / ".sy")):
            runner = lambda argv: ran.append(argv) or SimpleNamespace(returncode=code)  # noqa: E731
            answer = judged(m._chown_switchyard_project_files, owner_user="o", project_dir=Path("/nonexistent/syrd375"), runner=runner)
        if code == 0:
            check(answer is None and ran == [["chown", "-R", "o:o", "/nonexistent/syrd375/.sy"]], f"the runner is asked, once: {ran}")
        else:
            check(isinstance(answer, SystemExit) and str(answer) == "switchyard: failed to assign /nonexistent/syrd375/.sy to o",
                  f"a failure is the command's own: {answer!r}")


def test_the_first_run_worktrees() -> None:
    events: list = []
    owner_runner = object()
    stand_ins = dict(
        _owner_project_git_runner=seam("_owner_project_git_runner", lambda **kw: events.append(("owner", kw)) or owner_runner),
        _control_repository_owned_roots=seam("_control_repository_owned_roots", lambda config: ["root"]),
        ensure_project_worktrees=seam("ensure_project_worktrees", lambda config, *, refresh, runner: events.append(("ensure", refresh, runner))),
    )
    runner = object()
    config = lambda **kw: SimpleNamespace(**{"repository": Path("/nonexistent/r"), "pane_launcher": None,  # noqa: E731
                                             "run_as_user": "", "control_repository": None, **kw})
    with patched(t, **stand_ins):
        m._prepare_first_run_auth_worktrees(config(repository=None), runner=runner)
        check(events == [], "no repository: nothing")
        m._prepare_first_run_auth_worktrees(config(), runner=runner)
        check(events == [("ensure", True, runner)], f"no owner to run as: the caller's runner, refreshed: {events}")
        events.clear()
        m._prepare_first_run_auth_worktrees(config(run_as_user="o", control_repository=Path("/nonexistent/c")), runner=runner)
        check(events == [("owner", {"owner_user": "o", "project_dir": Path("/nonexistent/r"), "owned_roots": ["root"], "runner": runner}),
                         ("ensure", True, owner_runner)], f"an owner and a control repository: the owner's git runner: {events}")
        events.clear()
        m._prepare_first_run_auth_worktrees(config(run_as_user="o"), runner=runner)
        check(events == [("ensure", True, runner)], f"an owner but no control repository or launcher: the caller's runner: {events}")


def test_the_confirmation() -> None:
    said: list[str] = []
    fields = dict(slug="p375", owner_user="o", project_name="P", project_dir=Path("/nonexistent/p"))
    with patched(t, _read_prompt=refuse("the prompt")):
        m._confirm_switchyard_new(**fields, yes=True, input_func=refuse("input"), print_func=said.append)
    check(said == ["switchyard: project name: P", "switchyard: slug: p375", "switchyard: owner user: o",
                   "switchyard: project path: /nonexistent/p"], f"shown, and --yes asks nothing: {said}")
    for answer, expected in ((" Yes ", None), ("y", None), ("n", "switchyard: cancelled"), ("", "switchyard: cancelled")):
        asked: list = []
        feed = lambda prompt: prompt  # noqa: E731
        prompt = seam("_read_prompt", lambda text, *, input_func: asked.append((text, input_func)) or answer)
        with patched(t, _read_prompt=prompt):
            result = judged(m._confirm_switchyard_new, **fields, yes=False, input_func=feed, print_func=lambda line: None)
        check(asked == [("Proceed? [y/N]: ", feed)], f"asked once, with the caller's input: {asked}")
        check((result is None) if expected is None else (isinstance(result, SystemExit) and str(result) == expected),
              f"{answer!r}: {result!r}")


class Account:
    """`_run_as_account`, as this test's own fake: the write runs here, as this user, and a failure is False."""

    def __init__(self) -> None:
        self.calls: list = []
        self.errors: list = []

    def __call__(self, uid: int, gid: int, action) -> bool:
        REACHED.add("_run_as_account")
        self.calls.append((uid, gid))
        try:
            action()
        except OSError as exc:
            self.errors.append(exc)
            return False
        return True


def test_the_result_file_is_written_only_as_its_owner() -> None:
    uid, gid = os.getuid(), os.getgid()
    own = {"SUDO_UID": str(uid), "SUDO_GID": str(gid)}
    ints: list = []
    with tempfile.TemporaryDirectory() as tmp, patched(t, _int_env=seam("_int_env", lambda v: ints.append(v) or (None if v is None else int(v)))):
        result = Path(tmp) / "result"
        result.write_text("previous contents")
        account = Account()
        with patched(t, _run_as_account=account):
            check(m._report_new_project_to_caller(str(result), "p375", environ=own) is True
                  and result.read_text() == "p375" and account.calls == [(uid, gid)] and ints == [str(uid), str(gid)],
                  f"the owner's own regular file: truncated and written, as the caller's ids: {result.read_text()!r} {account.calls}")
            result.write_text("x")
            target = Path(tmp) / "target"; target.write_text("untouched")
            link = Path(tmp) / "link"; link.symlink_to(target)
            check(m._report_new_project_to_caller(str(link), "p375", environ=own) is False and target.read_text() == "untouched"
                  and account.errors[-1].errno == errno.ELOOP, f"a link is not followed: {account.errors[-1:]}")
            missing = Path(tmp) / "missing"
            check(m._report_new_project_to_caller(str(missing), "p375", environ=own) is False and not missing.exists()
                  and account.errors[-1].errno == errno.ENOENT, "nothing is created")
            check(m._report_new_project_to_caller(tmp, "p375", environ=own) is False and account.errors[-1].errno == errno.EISDIR,
                  "a directory is refused at the open")
            other = {"SUDO_UID": str(uid + 1), "SUDO_GID": str(gid)}
            # The open truncates, as the caller -- the account the write runs as -- could itself; the check then refuses.
            check(m._report_new_project_to_caller(str(result), "p375", environ=other) is False and result.read_text() == ""
                  and isinstance(account.errors[-1], PermissionError) and account.calls[-1] == (uid + 1, gid),
                  f"a file that is not the caller's is never written: {account.errors[-1]!r}")
            result.write_text("x")
            fifo = Path(tmp) / "fifo"; os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                check(m._report_new_project_to_caller(str(fifo), "p375", environ=own) is False
                      and isinstance(account.errors[-1], PermissionError), "only a regular file is written")
            finally:
                os.close(reader)
            before = len(account.calls)
            for bad in ("../etc", "P375", "", "a b"):
                check(m._report_new_project_to_caller(str(result), bad, environ=own) is False, f"{bad!r} is never reported")
            check(m._report_new_project_to_caller("", "p375", environ=own) is False
                  and m._report_new_project_to_caller(str(result), "p375", environ={"SUDO_UID": str(uid)}) is False
                  and m._report_new_project_to_caller(str(result), "p375", environ={}) is False,
                  "no path, or no caller ids: nothing")
            check(len(account.calls) == before and result.read_text() == "x", "and none of those reaches the account switch")
            with patched(t, PROJECT_SLUG_RE=re.compile("^P375$")):
                check(m._report_new_project_to_caller(str(result), "P375", environ=own) is True and result.read_text() == "P375",
                      "the launcher's project pattern decides")
            REACHED.add("PROJECT_SLUG_RE")
    closes: list = []
    real_close = os.close
    with tempfile.TemporaryDirectory() as tmp, patched(t, _run_as_account=Account()), \
            patched(os, close=lambda fd: closes.append(fd) or real_close(fd), write=refuse("a write after a failed check")):
        result = Path(tmp) / "result"; result.write_text("")
        check(m._report_new_project_to_caller(str(result), "p375", environ={"SUDO_UID": str(uid + 1), "SUDO_GID": str(gid)}) is False
              and len(closes) == 1, f"the file is closed even when the check refuses it: {closes}")


def test_the_announcement() -> None:
    reports: list = []
    report = lambda path, project, *, environ: reports.append((path, project, environ)) or True  # noqa: E731
    with patched(t, LAYOUT_MODE_VIEWER="syrd375-viewer", NEW_RESULT_FILE_ENV="SYRD375_RESULT"):
        said: list[str] = []
        m.announce_new_project_presentation("p375", resolved_layout_mode="viewer", environ={}, report=report, print_func=said.append)
        check(said == ["switchyard: full pane window started for p375"] and reports == [],
              "a mode other than the launcher's viewer opened its own window")
        said.clear()
        env = {"SYRD375_RESULT": "/nonexistent/r", "SWITCHYARD_NEW_RESULT_FILE": "/nonexistent/wrong"}
        m.announce_new_project_presentation("p375", resolved_layout_mode="syrd375-viewer", environ=env, report=report, print_func=said.append)
        check(reports == [("/nonexistent/r", "p375", env)]
              and said == ["switchyard: every pane of p375 is up; its window opens next, in your own session"],
              f"the launcher's viewer mode and result variable decide: {reports} {said}")
    REACHED.update({"LAYOUT_MODE_VIEWER", "NEW_RESULT_FILE_ENV"})
    for env, answer in (({}, True), ({t.NEW_RESULT_FILE_ENV: "/nonexistent/r"}, False)):
        said = []
        m.announce_new_project_presentation("p375", resolved_layout_mode=t.LAYOUT_MODE_VIEWER, environ=env,
                                            report=lambda *a, **k: answer, print_func=said.append)
        check(said == ["switchyard: every pane of p375 is up, but this command runs as root, which has no screen, so it cannot open "
                       "the window. Open it from your desktop session with: switchyard p375"], f"no wrapper, or it failed: {said}")
    said = []
    with patched(t, _run_as_account=refuse("the account switch")):
        m.announce_new_project_presentation("p375", resolved_layout_mode=t.LAYOUT_MODE_VIEWER, environ={}, print_func=said.append)
    check(len(said) == 1 and "cannot open the window" in said[0], "the default writer is not reached without a result file")


def test_the_stages() -> None:
    ticks = iter([10.0, 10.5, 12.0, 15.25])
    said: list[str] = []
    stages = m.ProvisioningStages(["one", "two"], print_func=said.append, monotonic=lambda: next(ticks))
    stages.begin("one")
    stages.begin("two", waits_for_you=True)
    stages.begin("extra")
    stages.finish()
    check(said == ["switchyard: [1/2] one (0.0s in)", "switchyard: [2/2] two -- this step waits for you (0.5s in)",
                   "switchyard: [3/2] extra (2.0s in)", "switchyard: provisioned in 5.2s: one 0.5s, two 1.5s, extra 3.2s"],
          f"each stage, its place, its wait, and what each cost: {said}")
    check(stages.names == ("one", "two") and stages.current is None, "the names kept as a tuple, nothing left open")
    quiet: list[str] = []
    m.ProvisioningStages(("a",), print_func=quiet.append, monotonic=lambda: 1.0).finish()
    check(quiet == [], "finishing before any stage says nothing")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_only_its_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_eighteen")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"new_project_support_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
