#!/usr/bin/env python3
"""SYRD-377: release alignment and the Director's divergence report, against the launcher they came out of.

Six definitions -- the deploy-marker readers, the live build probe,
`ReleaseAlignment`, `release_alignment` and `director_release_divergence_report`
-- moved unchanged into `scripts/release_alignment.py`, with the section heading
that introduced them, and the launcher re-exports them. This pins what makes
that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **Definition-time bindings are the same objects:** the `dataclass` decorator;
  every default is None, and the board opener falls back to the launcher's
  `_open_board_url` only when called.
- **Seams (rule 24):** every launcher name these bodies read -- the upgrade
  records it re-exports included -- and every sibling read when a body runs,
  is read through the launcher as often as before, so a patch on the launcher
  reaches each of them, which this test shows for all of them.
- **The behaviour is unchanged:** the marker fallbacks, the live build and its
  failures, only a full commit as a pin, root's journal read only where it is
  readable, the ordered refusals, the legacy-workflow refusal, and what the
  Director may and may not claim.

Every boundary is this test's own: release trees and links live in owned
temporary directories, the board is an injected opener, and the effective uid,
file access, shared release, board root, journals, pin and workflow lookups are
stand-ins. Nothing on the host is read or changed.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import release_alignment as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402

CHECKS = 0
MOVED = ("_read_deploy_sha_marker", "_current_tenant_release", "ReleaseAlignment", "_live_board_build_id", "release_alignment",
         "director_release_divergence_report")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_current_tenant_release': {'_read_deploy_sha_marker': 2},
    '_live_board_build_id': {'_open_board_url': 1},
    'release_alignment': {'ReleaseAlignment': 1, '_current_tenant_release': 1, '_live_board_build_id': 1, '_read_switchyard_release_marker': 1, '_tenant_board_root_from_config_or_plan': 1, 'declared_workflow_presence': 1, 'privileged_upgrade_journal_path': 1, 'read_upgrade_journal': 2, 'read_upgrade_source': 1, 'switchyard_shared_install_root': 1, 'upgrade_phase_observation': 1, 'upgrade_phase_state': 2},
    'director_release_divergence_report': {'release_alignment': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
SHA = "a" * 40
OTHER = "b" * 40
PIN = "c" * 40
HEADING = ("# --------------------------------------------------------------------------\n"
           "# The release phase: proved from the host, never from a claim (SYRD-117)\n"
           "# --------------------------------------------------------------------------\n")


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
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
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


def board(payload: object = None, *, error: Exception | None = None, asked: list | None = None):
    """An injected opener: the running board, answering one JSON payload or failing."""
    @contextmanager
    def opener(url: str):
        if asked is not None:
            asked.append(url)
        if error is not None:
            raise error
        yield io.StringIO(json.dumps(payload))
    return opener


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "release_alignment.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.release_alignment as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_bindings() -> None:
    for order in (("scripts.release_alignment", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.release_alignment")):
        result = python("import importlib, dataclasses, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.release_alignment as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "m.dataclass is t.dataclass is dataclasses.dataclass, "
                        "t.ReleaseAlignment.__module__ == 'scripts.release_alignment' and t.ReleaseAlignment.__dataclass_params__.frozen, "
                        "all(inspect.signature(getattr(m, n)).parameters['opener'].default is None "
                        "for n in ('_live_board_build_id', 'release_alignment', 'director_release_divergence_report')))")
        check(result.stdout.strip() == "True True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.json is json and m.re.fullmatch is __import__("re").fullmatch, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    for name in MOVED:
        node = module_def(name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs (after its docstring): {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's, imports nothing: {imports}")
        # Annotations are never evaluated (the module defers them), so a bare name there reads nothing.
        annotation = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                      for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs)] if part is not None
                      for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected
                       and id(x) not in annotation})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        check(not bound & set(through) and "launcher" not in bound, f"{name}: nothing it binds itself is read through the launcher")
    source = (ROOT / "scripts" / "release_alignment.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))] == list(MOVED), "the six in the launcher's order")
    check(HEADING + "\n\n@dataclass(frozen=True)\nclass ReleaseAlignment:" in source, "the section heading moved with the block it introduces")
    live = module_def("_live_board_build_id")
    check("open_url = opener or launcher._open_board_url" in [ast.unparse(x) for x in live.body],
          "the launcher's opener is taken only when none is given, when the probe runs")


def test_the_launcher_reexports_the_six() -> None:
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.release_alignment"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the six, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body}
    check(not defined & set(MOVED) and "The release phase: proved from the host" not in source,
          f"the launcher defines none of them, and no longer carries their heading: {defined & set(MOVED)}")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_which_release_the_board_root_names() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check(m._current_tenant_release(root) == (None, ""), "no current link: nothing deployed")
        release = root / "releases" / "r1"
        release.mkdir(parents=True)
        (root / "current").symlink_to(release)
        check(m._current_tenant_release(root) == (release.resolve(), "r1"), "no marker: the release's own name")
        (release / ".pgu-deploy-sha").write_text(f"  {SHA}\n")
        check(m._current_tenant_release(root) == (release.resolve(), SHA) and m._read_deploy_sha_marker(release) == SHA,
              "the marker, trimmed, wins")
        check(m._read_deploy_sha_marker(root / "absent") == "", "an unreadable marker is no marker")
        (root / "current").unlink()
        (root / "current").symlink_to(root / "releases" / "gone")
        check(m._current_tenant_release(root) == ((root / "releases" / "gone").resolve(), "gone"), "a dangling link still names its release")
        (root / "current").unlink()
        (root / "current").symlink_to(release)
        asked: list = []
        with patched(t, _read_deploy_sha_marker=seam("_read_deploy_sha_marker",
                                                     lambda path: asked.append(path.name) or ("" if path.name == "current" else OTHER))):
            check(m._current_tenant_release(root) == (release.resolve(), OTHER) and asked == ["current", "r1"],
                  f"the link's marker first, then the release's, through the launcher: {asked}")


def test_what_the_running_board_says() -> None:
    config = lambda url: SimpleNamespace(project="p377", board_url=url)  # noqa: E731
    check(m._live_board_build_id(config(""), opener=refuse("the board")) == ("", "p377 declares no board url"), "no board to ask")
    asked: list = []
    check(m._live_board_build_id(config("http://127.0.0.1:9/api/tickets/"), opener=board({"build_id": f" {SHA} "}, asked=asked)) == (SHA, "")
          and asked == ["http://127.0.0.1:9/api/board"], f"the board root, then its build id, trimmed: {asked}")
    check(m._live_board_build_id(config("http://b/api"), opener=board({})) == ("", "the board at http://b reported no build id")
          and judged(m._live_board_build_id, config("http://b"), opener=board([SHA])) == ("", "the board at http://b reported no build id"),
          "no build id, or a payload that is not a mapping")
    check(judged(m._live_board_build_id, config("http://b"), opener=board(error=OSError("refused"))) == ("", "the board at http://b could not be read (refused)"),
          "a board that cannot be read is not a proof")
    with patched(t, _open_board_url=seam("_open_board_url", board({"build_id": OTHER}))):
        check(m._live_board_build_id(config("http://b")) == (OTHER, ""), "no opener given: the launcher's")
        check(m._live_board_build_id(config("http://b"), opener=board({"build_id": SHA})) == (SHA, ""), "an injected opener wins")


def alignment(**kw: object) -> "m.ReleaseAlignment":
    return m.ReleaseAlignment(project="p377", **kw)


def test_the_alignment_record_and_its_refusals() -> None:
    fields = dataclasses.fields(m.ReleaseAlignment)
    check([f.name for f in fields] == ["project", "shared_release", "deployed_release", "live_build", "pinned_release",
                                       "trusted_release_state", "trusted_readable", "tenant_release_state", "tenant_observation",
                                       "board_runs_declared_workflow", "legacy_without_workflow", "errors"]
          and [f.default for f in fields][1:] == ["", "", "", "", "", True, "", "", True, False, ()], "the facts, in order, and their defaults")
    check(isinstance(judged(setattr, alignment(), "live_build", SHA), dataclasses.FrozenInstanceError), "frozen")
    good = alignment(deployed_release=SHA, live_build=SHA)
    check(good.board_is_serving_its_release and good.deployed_matches_pin and good.close_refusals() == [], "serving, unpinned: closable")
    check(not alignment(live_build="").board_is_serving_its_release and alignment(deployed_release=SHA, pinned_release=SHA).deployed_matches_pin
          and not alignment(deployed_release=SHA, pinned_release=PIN).deployed_matches_pin, "serving and pin, each")
    check(alignment(deployed_release=SHA, live_build=SHA, trusted_release_state="done").diverged is False and alignment().diverged is False,
          "done over a proven deployment, or nothing over nothing: agreed")
    check(alignment(trusted_release_state="done").diverged and good.diverged
          and alignment(deployed_release=SHA, live_build=SHA, pinned_release=PIN, trusted_release_state="done").diverged,
          "done over no proof, a proof under a phase not done, or done over the wrong commit: diverged")
    check(alignment(tenant_release_state="done", trusted_release_state="ready").diverged
          and not alignment(tenant_release_state="done", trusted_readable=False).diverged, "the tenant copy disagreeing counts only when root's is readable")
    check(not alignment(deployed_release=SHA, live_build=SHA, trusted_readable=False).diverged,
          "and an unreadable root journal never makes a divergence, even over a proven deployment")
    check(alignment().close_refusals() == ["p377's board root names no deployed release, so there is no deployment to close the phase over"],
          "nothing deployed")
    check(alignment(deployed_release=SHA, errors=("board down",)).close_refusals() == ["the running board did not report a build id (board down)"]
          and alignment(deployed_release=SHA).close_refusals() == ["the running board did not report a build id"], "no live build, with why")
    refusals = alignment(deployed_release=SHA, live_build=OTHER, pinned_release=PIN, legacy_without_workflow=True).close_refusals()
    check(refusals[:2] == [f"the running board reports build {OTHER}, but p377's deployed release is {SHA}; the board is not serving what was deployed",
                           f"the deployed release is {SHA}, but this upgrade was pinned to {PIN}; closing the phase would claim a deploy that did not happen"]
          and len(refusals) == 3 and refusals[2].startswith("p377's board is running no declared workflow")
          and refusals[2].endswith("`pkexec switchyard migrate-workflow p377 --apply` to install it"),
          f"every refusal, in order, the legacy workflow last: {refusals}")
    check(alignment(pinned_release=PIN).close_refusals()[1:] == [], "no deployment: the pin is not compared")


class Marker(SimpleNamespace):
    pass


def aligned(*, euid: int = 1006, readable: bool = True, source: dict | None = None, marker: object = None, board_root: object = "set",
            presence: object = None, trusted: dict | None = None, tenant: dict | None = None, opener=None):
    """Run release_alignment with every launcher facility, euid and os.access stood in; returns the record and the calls."""
    calls: list = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if board_root == "set":
            board_root = root / "board"
            (board_root / "releases" / SHA).mkdir(parents=True)
            (board_root / "current").symlink_to(board_root / "releases" / SHA)
        journal_path = root / "upgrade.json"

        def journal(config, *, config_path, trusted_flag=False, **kw):
            flag = kw.get("trusted", trusted_flag)
            calls.append(("journal", flag))
            return (trusted if flag else tenant) or {}
        stand_ins = dict(
            switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: root / "opt"),
            _read_switchyard_release_marker=seam("_read_switchyard_release_marker", lambda path: calls.append(("marker", path.name)) or marker),
            _tenant_board_root_from_config_or_plan=seam("_tenant_board_root_from_config_or_plan", lambda config, config_path: board_root),
            read_upgrade_source=seam("read_upgrade_source", lambda config: source or {}),
            privileged_upgrade_journal_path=seam("privileged_upgrade_journal_path", lambda config: journal_path),
            read_upgrade_journal=seam("read_upgrade_journal", journal),
            upgrade_phase_state=seam("upgrade_phase_state", lambda j, phase: str(((j.get("phases") or {}).get(phase) or {}).get("state") or "")),
            upgrade_phase_observation=seam("upgrade_phase_observation", lambda j, phase: (j.get("observations") or {}).get(phase) or {}),
            declared_workflow_presence=seam("declared_workflow_presence", lambda config, *, config_path: presence
                                            or SimpleNamespace(board_problem="", board_document=True, legacy_without_workflow=False)),
        )
        accessed: list = []
        with patched(t, **stand_ins), patched(os, geteuid=lambda: euid,
                                               access=lambda path, mode: accessed.append((Path(path).name, mode)) or readable):
            result = judged(m.release_alignment, SimpleNamespace(project="p377", board_url="http://b"), config_path=root / "p377.json",
                            opener=opener or board({"build_id": SHA}))
    return result, calls, accessed


def test_the_alignment_is_read_from_the_host() -> None:
    record, calls, accessed = aligned(marker=Marker(marker_commit=OTHER, marker_error=""), source={"deploy_ref": f" {PIN} "},
                                      trusted={"phases": {"release": {"state": "ready"}}},
                                      tenant={"phases": {"release": {"state": "done"}}, "observations": {"release": {"state": "seen"}}})
    check(record == m.ReleaseAlignment(project="p377", shared_release=OTHER, deployed_release=SHA, live_build=SHA, pinned_release=PIN,
                                       trusted_release_state="ready", trusted_readable=True, tenant_release_state="done",
                                       tenant_observation="seen", board_runs_declared_workflow=True, legacy_without_workflow=False, errors=()),
          f"every fact, from where it lives: {record}")
    check(calls[0] == ("marker", "current") and ("journal", True) in calls and ("journal", False) in calls
          and accessed == [("upgrade.json", os.R_OK)], f"the shared release's current, both journals, root's asked about first: {calls} {accessed}")
    check(aligned(source={"deploy_ref": "origin/main"})[0].pinned_release == "" and aligned(source={"deploy_ref": SHA.upper()})[0].pinned_release == "",
          "only a full lowercase commit is a pin")
    record, calls, accessed = aligned(readable=False, trusted={"phases": {"release": {"state": "done"}}})
    check(not record.trusted_readable and record.trusted_release_state == "" and ("journal", True) not in calls
          and record.errors == ("root's journal is not readable from this account; run this as an operator to compare it",),
          f"root's journal unreadable: not read, and said so: {record} {calls}")
    record, calls, accessed = aligned(euid=0, readable=False, trusted={"phases": {"release": {"state": "done"}}})
    check(record.trusted_readable and record.trusted_release_state == "done" and accessed == [], "root reads it without asking")
    record, _calls, _ = aligned(marker=Marker(marker_commit="", marker_error="no marker in the shared release"), board_root=None,
                                opener=board(error=OSError("down")),
                                presence=SimpleNamespace(board_problem="the board could not be asked", board_document=False, legacy_without_workflow=True))
    check(isinstance(record, m.ReleaseAlignment)
          and record.errors == ("no marker in the shared release", "p377 serves no tenant board release", "the board at http://b could not be read (down)",
                            "the board could not be asked")
          and record.deployed_release == "" and record.live_build == "" and not record.board_runs_declared_workflow and record.legacy_without_workflow,
          f"each missing reading says why, in order: {getattr(record, 'errors', record)!r}")
    check(aligned(presence=SimpleNamespace(board_problem="stale", board_document=True, legacy_without_workflow=False))[0].errors == (),
          "a board problem beside a running document is not an error here")
    with patched(t, ReleaseAlignment=seam("ReleaseAlignment", lambda **kw: ("syrd377", kw["project"])),
                 _current_tenant_release=seam("_current_tenant_release", lambda board_root: (None, OTHER)),
                 _live_board_build_id=seam("_live_board_build_id", lambda config, *, opener: ("live", ""))):
        check(aligned()[0] == ("syrd377", "p377"), "the launcher's record, release reader and probe are the ones used")


def test_what_the_director_says() -> None:
    handed: list = []
    the_opener = board({})

    def said(record):
        with patched(t, release_alignment=seam("release_alignment", lambda config, *, config_path, opener: handed.append((config_path, opener)) or record)):
            return m.director_release_divergence_report(SimpleNamespace(project="p377"), config_path=Path("/c"), opener=the_opener)
    first = ("switchyard: finish-upgrade does not close p377's release phase. That phase is root's record and this command is "
             "unprivileged by design, so what it wrote is an observation beside the phases, not a phase.")
    done = said(alignment(trusted_release_state="done"))
    check(done == [first, "switchyard: root's journal already records the release phase done for p377; nothing is outstanding."], f"{done}")
    refused = said(alignment(deployed_release=SHA, live_build=OTHER))
    check(refused[:2] == [first, "switchyard: p377's board is not serving a deployment that could close the phase yet:"]
          and len(refused) == 3 and refused[2].startswith(f"  - the running board reports build {OTHER}"), f"{refused}")
    pending = said(alignment(deployed_release=SHA, live_build=SHA))
    check(pending[1] == (f"switchyard: p377's board is serving {SHA} and every check passes, but the authoritative release phase is recorded "
                         "pending. An operator closes it with `pkexec switchyard release-status p377 --close`, which re-verifies the live build "
                         "and deploys, restarts and rolls back nothing."), f"{pending}")
    unreadable = said(alignment(deployed_release=SHA, live_build=SHA, trusted_readable=False, trusted_release_state="done"))
    check("the authoritative release phase is not readable from this account." in unreadable[1], f"never claims what it cannot read: {unreadable}")
    check(handed and all(h == (Path("/c"), the_opener) for h in handed), "the caller's path and opener are handed on")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_bindings",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_six")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"release_alignment_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
