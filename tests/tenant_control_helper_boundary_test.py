#!/usr/bin/env python3
"""SYRD-391: the tenant-control helper's state and repair, against the launcher it came out of.

Fifteen definitions -- the bridge's lifecycle classification and grant, the
staged helper's state class and verification, its repair command and repair,
and the protocol-program checks and the ensure step -- moved unchanged into
`scripts/tenant_control_helper.py`, and the launcher re-exports them. This pins
what makes that safe, and the behaviour no other suite states directly:

- **No cycle, one set of objects** (the frozen state class included), whichever
  module is imported first; the module alone loads nothing of Switchyard's; the
  `runner`/`print_func` defaults are bound when the functions are defined, and
  the launcher's own eager `ensure_helper` default is still this function.
- **Seams (rule 24):** the slug pattern, the shared install root, the current
  user, the rollout recorder, the two provisioning helpers, and every name
  defined here that another definition here reads when it runs are read through
  the launcher as often as before, so a patch or rebind on the launcher reaches
  each of them, which this test shows for all of them.
- **Behaviour stated here, not elsewhere:** the narrow classification (exact
  shape, casefolded verb, slug pattern, the grant's authorized user, and any
  error meaning "no"); the grant's file, fallback root, refusals and string
  coercion; the verb an invocation is; the repair command with and without the
  recorder; the repair's announcement, argv and exit handling.

`tests/tenant_control_helper_repair_test.py` already pins the state, staleness
and ensure behaviour (hostile, absent, safe-but-stale, post-repair checks);
this test does not repeat it. Temp roots, fake runners and recorders only.
"""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import tenant_control_helper as m  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
MOVED = ("_tenant_control_can_serve", "TENANT_CONTROL_OPERATIONS", "TENANT_CONTROL_ROOT", "_tenant_control_grant",
         "_tenant_control_operation", "TENANT_CONTROL_REPAIR_LABEL", "TENANT_CONTROL_OWNER_UID", "TenantControlHelperState",
         "tenant_control_helper_state", "tenant_control_repair_command", "repair_tenant_control_helper",
         "PROTOCOL_STAGED_EXECUTABLES", "staged_protocol_states", "staged_tooling_out_of_date", "ensure_tenant_control_helper")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_tenant_control_can_serve': {'PROJECT_SLUG_RE': 1, 'TENANT_CONTROL_OPERATIONS': 1, '_tenant_control_grant': 1, 'current_user_name': 1},
    '_tenant_control_grant': {'TENANT_CONTROL_ROOT': 1},
    '_tenant_control_operation': {'TENANT_CONTROL_OPERATIONS': 1},
    'tenant_control_helper_state': {'TENANT_CONTROL_OWNER_UID': 1, 'TENANT_CONTROL_ROOT': 1, 'TenantControlHelperState': 3, 'untrusted_root_executable_reasons': 1},
    'tenant_control_repair_command': {'TENANT_CONTROL_REPAIR_LABEL': 1, '_rollout_recorder_path': 1, 'role_tooling_staging_commands': 1, 'switchyard_shared_install_root': 1},
    'repair_tenant_control_helper': {'tenant_control_repair_command': 1},
    'staged_protocol_states': {'PROTOCOL_STAGED_EXECUTABLES': 1, 'tenant_control_helper_state': 1},
    'staged_tooling_out_of_date': {'PROTOCOL_STAGED_EXECUTABLES': 1, 'TENANT_CONTROL_ROOT': 1, 'switchyard_shared_install_root': 1},
    'ensure_tenant_control_helper': {'repair_tenant_control_helper': 1, 'staged_protocol_states': 2, 'staged_tooling_out_of_date': 2},
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
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
        return exc


class patched:
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
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_control_helper as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_eager_defaults() -> None:
    for order in (("scripts.tenant_control_helper", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_control_helper")):
        result = python("import importlib, inspect, subprocess, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_control_helper as m; "
                        "d = lambda f, n: inspect.signature(f).parameters[n].default; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(d(f, 'runner') is subprocess.run and d(f, 'print_func') is print "
                        "for f in (m.repair_tenant_control_helper, m.ensure_tenant_control_helper)) "
                        "and d(m.tenant_control_helper_state, 'name') == 'switchyard-tenant-control' "
                        "and d(m.tenant_control_helper_state, 'owner_uid') is None, "
                        "d(t._switchyard_exec_through_tenant_control, 'ensure_helper') is m.ensure_tenant_control_helper "
                        "and dataclasses.is_dataclass(m.TenantControlHelperState) "
                        "and m.TenantControlHelperState.__dataclass_params__.frozen)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.json is json and m.shlex is shlex and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")
    check((m.TENANT_CONTROL_OPERATIONS, m.TENANT_CONTROL_ROOT, m.TENANT_CONTROL_REPAIR_LABEL, m.TENANT_CONTROL_OWNER_UID, m.PROTOCOL_STAGED_EXECUTABLES)
          == ({"start", "stop", "status", "recover-display"}, Path("/usr/local/lib/switchyard"), "tenant-control-repair", 0,
              ("switchyard-tenant-control", "switchyard-display-attach")), "the constants' values, root the entitled owner")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "tenant_control_helper.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name
                    or (isinstance(n, (ast.Assign, ast.AnnAssign)) and ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target) == name))
        if not isinstance(node, ast.FunctionDef):
            check(not any(isinstance(x, ast.Name) and x.id == "launcher" for x in ast.walk(node)), f"{name}: nothing of the launcher's at definition time")
            continue
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[1]) == imports[0],
              f"{name}: the launcher imported once, first thing after the docstring: {imports}")
        skip = {id(y) for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None] for y in ast.walk(d)}
        skip |= {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs)] if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import json", "import shlex", "import subprocess", "from dataclasses import dataclass",
                  "from pathlib import Path", "from typing import Any, Callable, Mapping, Sequence"], f"only the standard library at the top: {top}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)
             for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(order == list(MOVED), f"the fifteen in the launcher's order: {order}")


def test_the_launcher_reexports_the_fifteen() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_control_helper"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the fifteen, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # The interleaved sudo check stays reachable on the launcher: defined there, or -- since SYRD-414 moved it with the
    # command crossing -- re-exported there, unaliased; either way it is not one of these fifteen.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                and n.module != "scripts.tenant_control_helper" for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and "_switchyard_user_can_prompt_for_sudo" in defined | exported,
          "the launcher defines none of them, and keeps the interleaved sudo check, its own or re-exported")


# --- classification ------------------------------------------------------------------------------------------------


def test_only_a_granted_lifecycle_verb_for_this_caller_is_served() -> None:
    grants = {"p391": {"authorized_user": "p391-me", "project": "p391"}}
    with patched(t, _tenant_control_grant=seam("_tenant_control_grant", lambda project: grants.get(project, {})),
                 current_user_name=seam("current_user_name", lambda: "p391-me")):
        for argv, expected in ((["stop", "p391"], True), (["STATUS", "p391"], True), (["recover-display", "p391"], True),
                               (["start", "p391"], True), (["upgrade", "p391"], False), (["stop"], False),
                               (["stop", "p391", "--now"], False), (["stop", "P391"], False), (["stop", "../p391"], False),
                               (["stop", "p391-other"], False), (["stop", "p391\n"], False)):
            check(m._tenant_control_can_serve(argv) is expected, f"{argv}: {expected}")
    with patched(t, _tenant_control_grant=lambda project: {"authorized_user": "p391-me"}, current_user_name=lambda: "p391-me"):
        # A grant for ANY project, so the slug pattern alone decides these.
        for slug, expected in (("p391", True), ("p391\n", False), ("../p391", False), ("P391", False), ("p391/x", False), ("", False)):
            check(m._tenant_control_can_serve(["stop", slug]) is expected, f"slug {slug!r}: {expected}, by the pattern alone")
    with patched(t, _tenant_control_grant=lambda project: {"authorized_user": "someone-else"}, current_user_name=lambda: "p391-me"):
        check(m._tenant_control_can_serve(["stop", "p391"]) is False, "a grant for somebody else: no")
    with patched(t, _tenant_control_grant=lambda project: {}, current_user_name=lambda: "p391-me"):
        check(m._tenant_control_can_serve(["stop", "p391"]) is False, "no grant: no")
    with patched(t, _tenant_control_grant=refuse("the grant"), current_user_name=lambda: "p391-me"):
        check(m._tenant_control_can_serve(["stop", "p391"]) is False, "an error while classifying is a quiet no")
    with patched(t, TENANT_CONTROL_OPERATIONS={"syrd391-verb"}, PROJECT_SLUG_RE=__import__("re").compile(r"^SLUG$"),
                 _tenant_control_grant=lambda project: {"authorized_user": "p391-me"}, current_user_name=lambda: "p391-me"):
        check(m._tenant_control_can_serve(["syrd391-verb", "SLUG"]) is True and m._tenant_control_can_serve(["stop", "p391"]) is False,
              "the verbs and the slug pattern are the launcher's")
        check(m._tenant_control_operation(["syrd391-verb", "SLUG"], "slug") == "syrd391-verb", "and so are the operation's verbs")
        REACHED.update({"TENANT_CONTROL_OPERATIONS", "PROJECT_SLUG_RE"})


def test_the_grant_is_roots_record_for_this_project_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "p391").mkdir()
        grant = root / "p391" / "control-grant.json"
        check(m._tenant_control_grant("p391", root=root) == {}, "no record: nothing")
        grant.write_text("{not json")
        check(judged(m._tenant_control_grant, "p391", root=root) == {}, "a record that is not JSON: nothing")
        grant.write_text(json.dumps(["p391"]))
        check(m._tenant_control_grant("p391", root=root) == {}, "a record that is not an object: nothing")
        grant.write_text(json.dumps({"project": "other", "authorized_user": "x"}))
        check(m._tenant_control_grant("p391", root=root) == {}, "a record naming another project: nothing")
        grant.write_text(json.dumps({"project": "p391", "authorized_user": "p391-me", "uid": 1391}))
        check(m._tenant_control_grant("p391", root=root) == {"project": "p391", "authorized_user": "p391-me", "uid": "1391"},
              "this project's record, every value a string")
        with patched(t, TENANT_CONTROL_ROOT=root):
            check(m._tenant_control_grant("p391")["authorized_user"] == "p391-me", "no root given: the launcher's, read when it runs")
            REACHED.add("TENANT_CONTROL_ROOT")


def test_the_verb_an_invocation_is() -> None:
    for argv, expected in ((["p391"], "start"), (["P391"], "start"), (["p391", "x"], ""), (["Stop", "p391"], "stop"),
                           (["status", "P391"], "status"), (["stop", "other"], ""), (["upgrade", "p391"], ""),
                           (["stop", "p391", "x"], ""), ([], "")):
        check(m._tenant_control_operation(argv, "p391") == expected, f"{argv}: {expected!r}")


# --- the repair ----------------------------------------------------------------------------------------------------


def test_the_repair_command_is_the_recorded_staging_step() -> None:
    staged: list = []
    commands = lambda project, release, *, staging_root: staged.append((project, release, staging_root)) or ["install -d /x", "echo 'a b'"]
    script = "install -d /x\necho 'a b'"
    with patched(t, role_tooling_staging_commands=seam("role_tooling_staging_commands", commands),
                 switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: Path("/opt/sy")),
                 _rollout_recorder_path=seam("_rollout_recorder_path", lambda: None)):
        check(m.tenant_control_repair_command("p391") == f"bash -c {shlex.quote(script)}" and staged == [("p391", "/opt/sy/current", None)],
              f"no recorder: the staging step itself, from the shared release: {staged}")
    with patched(t, role_tooling_staging_commands=commands, switchyard_shared_install_root=refuse("the shared root"),
                 _rollout_recorder_path=lambda: Path("/usr/local/bin/record rollout")):
        command = m.tenant_control_repair_command("p391", release_root="/r/1", root=Path("/stage"))
        check(command == " ".join(["sudo", shlex.quote("/usr/local/bin/record rollout"), "p391", "--label", "tenant-control-repair",
                                   "--", "bash", "-c", shlex.quote(script)]) and staged[-1] == ("p391", "/r/1", Path("/stage")),
              f"with the recorder: the step through it under its own label, the given release and root: {command}")
        with patched(t, TENANT_CONTROL_REPAIR_LABEL="syrd391-label"):
            check("--label syrd391-label" in m.tenant_control_repair_command("p391", release_root="/r/1"), "the label is the launcher's")
            REACHED.add("TENANT_CONTROL_REPAIR_LABEL")


def test_the_repair_is_announced_run_and_judged_by_its_exit() -> None:
    said: list = []
    ran: list = []
    with patched(t, tenant_control_repair_command=seam("tenant_control_repair_command", lambda project, **kw: ran.append(("command", project, kw)) or "REPAIR")):
        for code, expected in ((0, ""), (None, ""), (3, "repairing the tenant control helper for p391 failed (exit 3); the rollout journal records the attempt")):
            def runner(argv, **kwargs):
                ran.append(("run", argv, said[-1] if said else None))
                return subprocess.CompletedProcess(argv, code)
            check(m.repair_tenant_control_helper("p391", release_root="/r", root=Path("/s"), runner=runner, print_func=said.append) == expected,
                  f"exit {code}: {expected!r}")
        check(ran[0] == ("command", "p391", {"release_root": "/r", "root": Path("/s")})
              and ran[1] == ("run", ["bash", "-c", "REPAIR"], "switchyard: p391: the tenant control helper is not staged; repairing it from the current release before continuing"),
              f"the command for this project, announced before it is run through bash: {ran[:2]}")
        check(m.repair_tenant_control_helper("p391", runner=lambda argv, **k: object(), print_func=said.append)
              == "repairing the tenant control helper for p391 failed (exit 1); the rollout journal records the attempt",
              "a result with no return code counts as a failure")


# --- reach for the state, protocol and ensure seams ----------------------------------------------------------------


def test_the_state_protocol_and_ensure_seams_are_the_launchers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        built: list = []

        class Recorded(m.TenantControlHelperState):
            def __init__(self, **kwargs):
                built.append(kwargs["project"])
                super().__init__(**kwargs)

        with patched(project_provision, untrusted_root_executable_reasons=refuse("project_provision's own ownership check")), \
                patched(t, TenantControlHelperState=seam("TenantControlHelperState", Recorded),
                     untrusted_root_executable_reasons=seam("untrusted_root_executable_reasons", lambda path, *, boundary, owner_uid: [f"uid {owner_uid}"]),
                     TENANT_CONTROL_OWNER_UID=4391, TENANT_CONTROL_ROOT=root):
            (root / "p391").mkdir()
            (root / "p391" / "switchyard-tenant-control").write_text("#!/bin/sh\n")
            (root / "p391" / "switchyard-tenant-control").chmod(0o755)
            state = m.tenant_control_helper_state("p391")
            check(isinstance(state, Recorded) and state.reasons == ("uid 4391",) and built == ["p391"],
                  f"the class, the ownership check, the entitled uid and the root are all the launcher's: {state}")
            REACHED.add("TENANT_CONTROL_OWNER_UID")
        seen: list = []
        with patched(t, PROTOCOL_STAGED_EXECUTABLES=("syrd391-a", "syrd391-b"),
                     tenant_control_helper_state=seam("tenant_control_helper_state", lambda project, **kw: seen.append((kw["name"], kw["grant"])) or kw["name"])):
            check(m.staged_protocol_states("p391", grant={"project": "p391"}) == {"syrd391-a": "syrd391-a", "syrd391-b": "syrd391-b"}
                  and seen == [("syrd391-a", {"project": "p391"}), ("syrd391-b", None)],
                  f"each protocol program the launcher names, checked by the launcher's verifier; the grant once, with the first: {seen}")
            REACHED.add("PROTOCOL_STAGED_EXECUTABLES")
        release = root / "release"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "switchyard-tenant-control").write_text("new")
        with patched(t, switchyard_shared_install_root=lambda: root, TENANT_CONTROL_ROOT=root):
            (root / "release").rename(root / "current")
            check(m.staged_tooling_out_of_date("p391") == ["switchyard-tenant-control"], "the shared release and the staging root are the launcher's")
        ok = m.TenantControlHelperState(project="p391", path=root, present=True)
        absent = m.TenantControlHelperState(project="p391", path=root, present=False)
        asked: list = []
        with patched(t, staged_protocol_states=seam("staged_protocol_states", lambda project, **kw: asked.append("states") or {"a": absent if not asked.count("repair") else ok}),
                     staged_tooling_out_of_date=seam("staged_tooling_out_of_date", lambda project, **kw: asked.append("stale") or []),
                     repair_tenant_control_helper=seam("repair_tenant_control_helper", lambda project, **kw: asked.append("repair") or "")):
            m.ensure_tenant_control_helper("p391", runner=refuse("a real runner"), print_func=lambda line: None)
        check(asked == ["states", "stale", "repair", "states", "stale"], f"states, staleness, repair, and both again after it -- the launcher's: {asked}")



# --- the staged programs' state, staleness and the ensure step ------------------------------------------------------
#: Stated from the functions' contracts over owned temp files. The ownership/path check is the launcher's
#: `untrusted_root_executable_reasons`, stood in by a recorder (the real one reads ACLs through getfacl and looks up
#: accounts, which the containment guard refuses); everything else is the real code.

BRIDGE, DISPLAY = "switchyard-tenant-control", "switchyard-display-attach"


class Ownership:
    """The launcher's ownership check, answering chosen reasons per file name and recording what it was asked."""

    def __init__(self, reasons: dict | None = None) -> None:
        self.reasons, self.asked = reasons or {}, []

    def __call__(self, path, *, boundary, owner_uid):
        self.asked.append((Path(path).name, Path(boundary), owner_uid))
        return list(self.reasons.get(Path(path).name, []))


def stage(directory: Path, name: str, body: bytes = b"current", mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(body)
    target.chmod(mode)
    return target


def test_a_staged_programs_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        own = Ownership()
        real_lstat = Path.lstat
        # The defining module's check is refused too: a path past the launcher must fail here, never reach the host.
        with patched(t, untrusted_root_executable_reasons=own), \
                patched(project_provision, untrusted_root_executable_reasons=refuse("project_provision's own ownership check")):
            for slug in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
                with patched(Path, lstat=refuse("a filesystem call")):
                    state = m.tenant_control_helper_state(slug, root=root)
                check(state == m.TenantControlHelperState(project=slug, path=root, present=False,
                                                          reasons=(f"{slug!r} is not a project slug this may be aimed at",))
                      and not state.usable and not state.repairable, f"{slug!r}: refused before touching the disk")
            path = root / "p391" / BRIDGE
            absent = m.tenant_control_helper_state("p391", root=root)
            check(absent == m.TenantControlHelperState(project="p391", path=path, present=False) and absent.repairable and not absent.usable
                  and own.asked == [], "no file: absent and repairable, and nothing asked about ownership")
            check(m.tenant_control_helper_state("p391", root=root, grant={"project": "other"}).reasons
                  == (f"the grant at {root / 'p391'} records project other rather than p391",), "a grant naming another tenant")
            for grant in ({}, {"authorized_user": "x"}, {"project": ""}, {"project": "p391"}):
                check(m.tenant_control_helper_state("p391", root=root, grant=grant).reasons == (), f"{grant}: not a disagreement")
            stage(root / "p391", BRIDGE)
            state = m.tenant_control_helper_state("p391", root=root)
            check(state.usable and state.present and own.asked == [(BRIDGE, root, 0)],
                  f"present and clean: usable, checked against the staging root with root (uid 0) entitled: {own.asked}")
            asked_before = len(own.asked)
            m.tenant_control_helper_state("p391", root=root, owner_uid=4391, name=DISPLAY)
            check(len(own.asked) == asked_before, "a different name that is absent is not checked")
            stage(root / "p391", DISPLAY)
            m.tenant_control_helper_state("p391", root=root, owner_uid=4391, name=DISPLAY)
            check(own.asked[-1] == (DISPLAY, root, 4391), "an explicit entitled uid and name are used")
            (root / "p391" / BRIDGE).chmod(0o644)
            check(m.tenant_control_helper_state("p391", root=root).reasons == (f"{path} is not executable",), "not executable")
            own.reasons[BRIDGE] = ["owned by uid 1000"]
            (root / "p391" / BRIDGE).chmod(0o755)
            check(m.tenant_control_helper_state("p391", root=root, grant={"project": "other"}).reasons
                  == (f"the grant at {root / 'p391'} records project other rather than p391", "owned by uid 1000"),
                  "the grant's disagreement, then the ownership check's reasons, in that order")
            (root / "shut").mkdir()
            (root / "shut" / "x").mkdir()
            (root / "shut").chmod(0)
            try:
                try:
                    real_lstat(root / "shut" / "x" / BRIDGE)
                except OSError as exc:
                    expected = exc
                state = m.tenant_control_helper_state("x", root=root / "shut")
            finally:
                (root / "shut").chmod(0o700)
            check(state == m.TenantControlHelperState(project="x", path=root / "shut" / "x" / BRIDGE, present=False,
                                                      reasons=(f"{root / 'shut' / 'x' / BRIDGE} cannot be inspected: {expected}",)),
                  f"a path that cannot be inspected is neither absent nor present: {state}")


def test_staleness_is_a_byte_comparison_of_safe_files_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        release = root / "release"
        stage(release / "scripts", BRIDGE, b"current")
        stage(release / "scripts", DISPLAY, b"current-display")
        staging = root / "staging" / "p391"
        stage(staging, BRIDGE, b"current")
        stage(staging, DISPLAY, b"old-display")
        out = lambda **kw: m.staged_tooling_out_of_date("p391", release_root=str(release), root=root / "staging", **kw)
        check(out() == [DISPLAY], "only the file whose bytes differ")
        check(out(only=[BRIDGE]) == [], "only the files named are compared")
        (staging / DISPLAY).unlink()
        (staging / DISPLAY).symlink_to(release / "scripts" / DISPLAY)
        check(out() == [DISPLAY], "a symlink is not current, whatever it points at")
        (staging / DISPLAY).unlink()
        check(out() == [DISPLAY], "a missing file is not current")
        (release / "scripts" / DISPLAY).unlink()
        check(out() == [], "a program this release does not carry is not this check's business")
        (staging / BRIDGE).chmod(0)
        try:
            check(out() == [BRIDGE], "a file that cannot be read is not current")
        finally:
            (staging / BRIDGE).chmod(0o755)


def ensure(root: Path, release: Path, *, reasons: dict | None = None, stage_on_repair: dict | None = None, code: int = 0):
    """ensure_tenant_control_helper over owned temp, with the ownership check and the staging command stood in."""
    said: list = []
    ran: list = []

    def runner(argv, **kwargs):
        ran.append(list(argv))
        for name, body in (stage_on_repair or {}).items():
            stage(root / "p391", name, body)
        return subprocess.CompletedProcess(argv, code)

    with patched(t, untrusted_root_executable_reasons=Ownership(reasons), _rollout_recorder_path=lambda: None,
                 role_tooling_staging_commands=lambda project, release_root, *, staging_root: [f"stage {project}"]), \
            patched(project_provision, untrusted_root_executable_reasons=refuse("project_provision's own ownership check"),
                    role_tooling_staging_commands=refuse("project_provision's own staging commands")):
        answer = judged(m.ensure_tenant_control_helper, "p391", release_root=str(release), root=root, runner=runner, print_func=said.append)
    return answer, said, ran


def test_refuse_repair_or_carry_on_per_protocol_program() -> None:
    announce = "switchyard: p391: the tenant control helper is not staged; repairing it from the current release before continuing"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        release = base / "release"
        stage(release / "scripts", BRIDGE, b"b1")
        stage(release / "scripts", DISPLAY, b"d1")
        root = base / "staged"
        current = {BRIDGE: b"b1", DISPLAY: b"d1"}

        answer, said, ran = ensure(root, release, stage_on_repair=current)
        check(answer is None and ran == [["bash", "-c", "bash -c 'stage p391'"]] and said == [
            f"switchyard: p391: {BRIDGE} is not staged; repairing it from the current release before continuing",
            f"switchyard: p391: {DISPLAY} is not staged; repairing it from the current release before continuing",
            announce,
            f"switchyard: p391: {BRIDGE} repaired at {root / 'p391' / BRIDGE}",
            f"switchyard: p391: {DISPLAY} repaired at {root / 'p391' / DISPLAY}",
            "switchyard: p391's staged tooling is this release's"],
            f"absent: each named, the repair announced and run once, then each reported repaired: {said} {ran}")

        answer, said, ran = ensure(root, release)
        check(answer is None and said == [] and ran == [], "correct and current: nothing said, nothing run")

        stage(root / "p391", DISPLAY, b"old")
        answer, said, ran = ensure(root, release, stage_on_repair=current)
        check(answer is None and len(ran) == 1 and said == [
            f"switchyard: p391's staged tooling is from an older release ({DISPLAY}); restaging it from the current one before continuing",
            announce, "switchyard: p391's staged tooling is this release's"], f"safe but stale: restaged: {said}")

        answer, said, ran = ensure(root, release, reasons={DISPLAY: ["writable by others"]})
        check(isinstance(answer, SystemExit) and str(answer) == "\n".join([
            f"switchyard: refusing to run {root / 'p391' / DISPLAY} as root:",
            "switchyard:   writable by others",
            "switchyard: this is not repaired automatically, and nothing was run in its place. A staged program somebody "
            "else can write is not version drift"]) and ran == [] and said == [],
            f"hostile: refused with its reasons, and nothing run: {answer!r}")

        (root / "p391" / DISPLAY).unlink()
        answer, said, ran = ensure(root, release, code=3)
        check(isinstance(answer, SystemExit) and str(answer)
              == "switchyard: repairing the tenant control helper for p391 failed (exit 3); the rollout journal records the attempt",
              f"a failed repair stops the launch: {answer!r}")

        answer, said, ran = ensure(root, release)
        check(isinstance(answer, SystemExit) and str(answer) == (
            "switchyard: p391's staged tooling is still unusable after repair, so it could not be brought up to this release: "
            f"{DISPLAY}: still not staged"), f"a repair that stages nothing is caught: {answer!r}")

        answer, said, ran = ensure(root, release, stage_on_repair={DISPLAY: b"old"})
        check(isinstance(answer, SystemExit) and str(answer).endswith(f": {DISPLAY}: still not this release's"),
              f"a repair that leaves old bytes is caught: {answer!r}")

        (root / "p391" / DISPLAY).unlink()
        answer, said, ran = ensure(root, release, reasons={DISPLAY: ["owned by uid 1000"]}, stage_on_repair={DISPLAY: b"d1"})
        check(isinstance(answer, SystemExit) and str(answer).endswith(f": {DISPLAY}: owned by uid 1000") and len(ran) == 1,
              f"an absent file repaired into a hostile one is caught after the repair: {answer!r}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_eager_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_fifteen")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"tenant_control_helper_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
