#!/usr/bin/env python3
"""SYRD-379: the tenant release carrier, its rendered commands, the deploy report and the transaction's pointer helpers.

Thirteen definitions moved unchanged from `scripts/team_launcher.py` into
`scripts/tenant_release_report.py`, and the launcher re-exports them. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads only its default's leaf, `scripts.release_refs`.
- **Definition-time bindings are the same objects:** the `dataclass` decorator,
  the report's default deploy ref (the leaf's), its default runner
  (`subprocess.run`) and `print`.
- **Seams (rule 24):** every launcher name these bodies read -- the release
  resolver, constants and quoting helpers included -- and every sibling read
  when a body runs, is read through the launcher as often as before, so a patch
  on the launcher reaches each of them, which this test shows for all of them.
  The project-provision helpers are still imported inside the two functions
  that use them.
- **The behaviour is unchanged:** every rendered command, byte for byte, the
  owner boundary, the recorder, the report's order and its omission of a unit
  install from a tenant-writable directory, and every branch of the
  transaction and the pointer restore.

No rendered command is ever executed: the runners are this test's own fakes,
and the release pointers are symlinks in owned temporary directories.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import inspect
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import release_refs  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts import tenant_release_report as m  # noqa: E402
from scripts.ticket_board.project_provision import readable_system_unit_path, system_unit_proof_chain  # noqa: E402

CHECKS = 0
MOVED = ("TenantReleaseStatus", "tenant_release_deploy_command", "tenant_release_listener_command", "recorded_rollout_command",
         "tenant_release_unit_install_command", "release_update_blocked", "_format_release_path", "report_tenant_release_upgrade",
         "OWNER_BOUNDARY_SCRIPT", "_owner_boundary_env_args", "capture_release_pointer", "deploy_release_in_transaction",
         "restore_release_pointer")
LOCAL_IMPORTS = {"tenant_release_deploy_command": "from scripts.ticket_board.project_provision import readable_system_unit_path",
                 "tenant_release_unit_install_command": "from scripts.ticket_board.project_provision import system_unit_proof_chain"}
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'tenant_release_deploy_command': {'SWITCHYARD_RELEASE_MARKER_NAME': 1, '_owner_boundary_env_args': 2, '_quote_command': 2},
    'tenant_release_listener_command': {'_owner_boundary_env_args': 1, '_quote_command': 1},
    'recorded_rollout_command': {'_repo_root': 1, 'switchyard_shared_install_root': 1},
    'tenant_release_unit_install_command': {'_quote_command': 5},
    'report_tenant_release_upgrade': {'_format_release_path': 1, '_format_release_sha': 1, '_quote_command': 1, 'recorded_rollout_command': 3, 'release_update_blocked': 1, 'switchyard_privileged_provision_root': 1, 'tenant_release_deploy_command': 1, 'tenant_release_listener_command': 3, 'tenant_release_status': 1, 'tenant_release_unit_install_command': 1},
    '_owner_boundary_env_args': {'DEFAULT_PANE_BASE_PATH': 1, 'OWNER_BOUNDARY_SCRIPT': 1, '_owner_home_bin_dirs': 1, '_prepend_paths': 1},
    'capture_release_pointer': {'_tenant_board_root_from_config_or_plan': 1},
    'deploy_release_in_transaction': {'tenant_release_deploy_command': 1, 'tenant_release_status': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
QUOTE = t._quote_command
OLD, NEW = "a" * 40, "b" * 40


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


def status(**kw: object) -> "m.TenantReleaseStatus":
    base = dict(board_root=Path("/nonexistent/board"), owner_user="p379-owner", owner_home=Path("/nonexistent/home/p379 owner"),
                provisioned_system_unit=None, commit_git_dir="", current_release=Path("/nonexistent/board/releases/old"),
                current_sha=OLD, target_sha=NEW, deploy_ref="origin/main", source_repo=Path("/nonexistent/src"))
    base.update(kw)
    return m.TenantReleaseStatus(**base)


BOUNDARY = dict(DEFAULT_PANE_BASE_PATH="/usr/bin:/bin", _owner_home_bin_dirs=lambda home: [f"{home}/.local/bin"])


def boundary(command: list[str], st: "m.TenantReleaseStatus") -> list[str]:
    """What the owner boundary wraps a command in, spelled out independently of the code under test."""
    return ["sh", "-c", m.OWNER_BOUNDARY_SCRIPT, "sh", st.owner_user, "env", f"HOME={st.owner_home}",
            f"PATH={st.owner_home}/.local/bin:/usr/bin:/bin", *command]


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "tenant_release_report.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name
             or isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_its_leaf_at_import() -> None:
    result = python("import sys, scripts.tenant_release_report as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.release_refs']",
          f"it imports on its own and loads only its default's leaf: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.tenant_release_report", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_release_report")):
        result = python("import importlib, inspect, builtins, subprocess, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_release_report as m, scripts.release_refs as r; "
                        "d = inspect.signature(m.report_tenant_release_upgrade).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "d['deploy_ref'].default is r.DEFAULT_TENANT_RELEASE_DEPLOY_REF is t.DEFAULT_TENANT_RELEASE_DEPLOY_REF, "
                        "d['runner'].default is subprocess.run and d['print_func'].default is builtins.print, "
                        "m.dataclass is dataclasses.dataclass and t.TenantReleaseStatus.__dataclass_params__.frozen)")
        check(result.stdout.strip() == "True True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(inspect.signature(m.deploy_release_in_transaction).parameters["print_func"].default is builtins.print
          and inspect.signature(m.deploy_release_in_transaction).parameters["runner"].default is inspect.Parameter.empty
          and inspect.signature(m.restore_release_pointer).parameters["runner"].default is inspect.Parameter.empty,
          "the transaction's print default, and runners that must be given")
    check(m.os is os and m.shlex is shlex and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


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
        local = [LOCAL_IMPORTS[name]] if name in LOCAL_IMPORTS else []
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher", *local] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs; its own local import kept: {imports}")
        else:
            check(imports == local, f"{name}: reads nothing of the launcher's: {imports}")
        annotation = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                      for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs)] if part is not None
                      for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected
                       and id(x) not in annotation})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        bound |= {a.asname or a.name for x in ast.walk(node) if isinstance(x, ast.ImportFrom) and x.module != "scripts" for a in x.names}
        check(not bound & set(through), f"{name}: nothing it binds itself, its local import included, is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "tenant_release_report.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check([line for line in top if "scripts" in line] == ["from scripts.release_refs import DEFAULT_TENANT_RELEASE_DEPLOY_REF"],
          f"only the default's leaf is imported at the top: {top}")
    order = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else ast.unparse(n.targets[0])
             for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(order == list(MOVED), f"the thirteen in the launcher's order: {order}")


def test_the_launcher_reexports_the_thirteen() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_release_report"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the thirteen, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED), f"the launcher defines none of them itself: {defined & set(MOVED)}")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_carrier() -> None:
    fields = dataclasses.fields(m.TenantReleaseStatus)
    check([f.name for f in fields] == ["board_root", "owner_user", "owner_home", "provisioned_system_unit", "commit_git_dir", "current_release",
                                       "current_sha", "target_sha", "deploy_ref", "source_repo", "resolve_error", "clone_source_repo",
                                       "board_port", "board_socket"]
          and [f.default for f in fields][10:] == ["", None, "", ""], "the fields in order, the last four defaulted")
    check(isinstance(judged(setattr, status(), "target_sha", OLD), dataclasses.FrozenInstanceError), "frozen")
    check(not status().unchanged and status(target_sha=OLD).unchanged and not status(current_sha="", target_sha="").unchanged
          and not status(target_sha="").unchanged, "unchanged only when both are known and equal")


def test_the_owner_boundary() -> None:
    check(m.OWNER_BOUNDARY_SCRIPT == 'target=$1; shift; if [ "$(id -un)" = "$target" ]; then exec "$@"; fi; exec sudo -u "$target" "$@"',
          "the owner and the command are positional arguments, never interpolated")
    with patched(t, DEFAULT_PANE_BASE_PATH="/syrd379/bin", _owner_home_bin_dirs=seam("_owner_home_bin_dirs", lambda home: [f"{home}/b"]),
                 _prepend_paths=seam("_prepend_paths", lambda base, dirs: f"{':'.join(dirs)}+{base}"),
                 OWNER_BOUNDARY_SCRIPT="syrd379-script"):
        args = m._owner_boundary_env_args("o w", Path("/h o"), ["cmd", "a b"])
    check(args == ["sh", "-c", "syrd379-script", "sh", "o w", "env", "HOME=/h o", "PATH=/h o/b+/syrd379/bin", "cmd", "a b"],
          f"the launcher's script, base path and home bins decide: {args}")
    REACHED.update({"DEFAULT_PANE_BASE_PATH", "OWNER_BOUNDARY_SCRIPT"})


def test_the_deploy_command() -> None:
    with patched(t, _quote_command=seam("_quote_command", QUOTE), _owner_boundary_env_args=seam("_owner_boundary_env_args", t._owner_boundary_env_args), **BOUNDARY):
        st = status()
        env = ["TICKET_BOARD_OWNER_HOME=/nonexistent/home/p379 owner", "TICKET_BOARD_PROJECT=p379", "BOARD_ROOT=/nonexistent/board", "DEPLOY_REF=origin/main"]
        check(m.tenant_release_deploy_command(st, "p379") == QUOTE(boundary(
            ["env", *env, "SOURCE_REPO=/nonexistent/src", "/nonexistent/src/scripts/ticket-board-service.sh", "deploy-restart"], st)),
              "the plain deploy, as the tenant, with only what it needs")
        st = status(board_port="8779", board_socket="/nonexistent/b.sock", provisioned_system_unit=Path("/nonexistent/priv/p379-ticket-board.service"),
                    commit_git_dir="/nonexistent/cache")
        full = env + ["BOARD_PORT=8779", "BOARD_UNIX_SOCKET=/nonexistent/b.sock",
                      f"TICKET_BOARD_PROVISIONED_SYSTEM_UNIT={readable_system_unit_path('p379')}", "TICKET_BOARD_COMMIT_GIT_DIR=/nonexistent/cache"]
        check(m.tenant_release_deploy_command(st, "p379") == QUOTE(boundary(
            ["env", *full, "SOURCE_REPO=/nonexistent/src", "/nonexistent/src/scripts/ticket-board-service.sh", "deploy-restart"], st)),
              "its own port and socket, the readable unit copy, and the commit cache, in that order")
        with patched(t, SWITCHYARD_RELEASE_MARKER_NAME=".syrd379-marker"):
            st = status(clone_source_repo=Path("/nonexistent/cl one.git"))
            line = m.tenant_release_deploy_command(st, "p379")
        script = ('tmpdir="$(mktemp -d)"' f" && git --git-dir='/nonexistent/cl one.git' archive {NEW}" ' | tar -x -C "$tmpdir"'
                  f" && printf '%s\\n' '{{\"commit\":\"{NEW}\"}}' >\"$tmpdir/.syrd379-marker\"" " && env "
                  + " ".join(shlex.quote(v) for v in env) + ' SOURCE_REPO="$tmpdir" "$tmpdir/scripts/ticket-board-service.sh" deploy-restart')
        check(line == QUOTE(boundary(["sh", "-c", script], st)), f"a clone is archived, marked with the launcher's marker name, and deployed from: {line}")
        odd = m.tenant_release_deploy_command(status(clone_source_repo=Path("/nonexistent/c.git"), target_sha="v 1; x"), "p379")
        check(" archive 'v 1; x' | tar" in shlex.split(odd)[-1] if isinstance(odd, str) else False, f"the archived revision is quoted: {odd}")
    REACHED.add("SWITCHYARD_RELEASE_MARKER_NAME")


def test_the_listener_command() -> None:
    with patched(t, _quote_command=QUOTE, **BOUNDARY):
        st = status()
        check(isinstance(judged(m.tenant_release_listener_command, st, "p379", "restart"), ValueError), "only stop and start")
        bus = 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
        check(m.tenant_release_listener_command(st, "p 379", "stop") == QUOTE(boundary(["sh", "-c", bus + "systemctl --user stop 'p 379-ticket-board-notify-listener.service'"], st))
              and m.tenant_release_listener_command(st, "p379", "start") == QUOTE(boundary(
                  ["sh", "-c", bus + "systemctl --user daemon-reload && systemctl --user start p379-ticket-board-notify-listener.service"], st)),
              "the tenant's own bus, and a reload before a start")


def test_the_recorded_step() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched(t, _repo_root=seam("_repo_root", lambda: root / "checkout"), switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: root / "opt")):
            line = m.recorded_rollout_command(status(), "p379", "a && b 'c'", label="deploy-restart")
            check(line == f"sudo {root}/checkout/scripts/switchyard-record-rollout p379 --target-commit {NEW} --label deploy-restart -- bash -c 'a && b '\"'\"'c'\"'\"''",
                  f"no installed recorder: this checkout's; the whole line in one bash -c: {line}")
            installed = root / "opt" / "current" / "scripts" / "switchyard-record-rollout"
            installed.parent.mkdir(parents=True); installed.write_text("")
            check(m.recorded_rollout_command(status(target_sha=""), "p379", "x") == f"sudo {installed} p379 -- bash -c x",
                  "the installed release's recorder, no target or label when there is none")


def test_the_unit_install_chain() -> None:
    check(m.tenant_release_unit_install_command(status(), "p379") == "", "no provisioned unit: nothing to install")
    with patched(t, _quote_command=seam("_quote_command", QUOTE)):
        unit = Path("/nonexistent/priv/p379-ticket-board.service")
        line = m.tenant_release_unit_install_command(status(provisioned_system_unit=unit), "p379")
    expected = " && ".join([
        QUOTE(["sudo", "install", "-m", "0644", str(unit), "/etc/systemd/system/p379-ticket-board.service"]),
        QUOTE(["sudo", "install", "-m", "0644", "/nonexistent/priv/p379-ticket-board-canary.service", "/etc/systemd/system/p379-ticket-board-canary.service"]),
        QUOTE(["sudo", "install", "-m", "0644", "-o", "p379-owner", "-g", "p379-owner", "/nonexistent/priv/p379-ticket-board-notify-listener.service",
               "/nonexistent/home/p379 owner/.config/systemd/user/p379-ticket-board-notify-listener.service"]),
        *system_unit_proof_chain("p379", QUOTE([str(unit)])),
        QUOTE(["sudo", "systemctl", "daemon-reload"])])
    check(line == expected, f"board, canary, the owner's listener, the readable proof copy, then a reload: {line}")


def test_whether_a_safe_update_exists() -> None:
    check(m.release_update_blocked(None) == "", "no release to update")
    check(m.release_update_blocked(status(target_sha="", resolve_error="no such ref")) == "no such ref"
          and m.release_update_blocked(status(target_sha="")) == "origin/main did not resolve", "an unresolved ref, with why")
    check(m.release_update_blocked(status()) == "generated board, canary, and listener units are incomplete", "a change without units")
    check(m.release_update_blocked(status(target_sha=OLD)) == "" and m.release_update_blocked(status(provisioned_system_unit=Path("/u"))) == "",
          "unchanged, or units present: a safe update exists")
    check(m._format_release_path(None) == "(none)" and m._format_release_path(Path("/r")) == "/r", "a release path, or none")


def report(st, *, privileged: Path = Path("/nonexistent/priv"), **kw):
    said: list[str] = []
    asked: list = []
    runner = object()
    stand_ins = dict(
        tenant_release_status=seam("tenant_release_status", lambda config, **k: asked.append(k) or st),
        _format_release_sha=seam("_format_release_sha", lambda sha: sha or "(none)"),
        _format_release_path=seam("_format_release_path", m._format_release_path),
        release_update_blocked=seam("release_update_blocked", m.release_update_blocked),
        switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: privileged),
        tenant_release_listener_command=seam("tenant_release_listener_command", lambda s, p, action: f"<listener {action}>"),
        tenant_release_unit_install_command=seam("tenant_release_unit_install_command", lambda s, p: "<units>" if s.provisioned_system_unit else ""),
        tenant_release_deploy_command=seam("tenant_release_deploy_command", lambda s, p: "<deploy>"),
        recorded_rollout_command=seam("recorded_rollout_command", lambda s, p, command, *, label="": f"<recorded {label}: {command}>"),
        _quote_command=seam("_quote_command", QUOTE),
    )
    with patched(t, **stand_ins):
        result = judged(m.report_tenant_release_upgrade, SimpleNamespace(project="p379"), config_path=Path("/c"), source_repo=Path("/s"),
                        commit_git_dir="/g", deploy_ref="ref", runner=runner, print_func=said.append, **kw)
    return result, said, asked, runner


def test_the_deploy_report() -> None:
    result, said, asked, runner = report(None)
    check(result is None and said == [] and asked == [dict(config_path=Path("/c"), source_repo=Path("/s"), commit_git_dir="/g", deploy_ref="ref", runner=runner)],
          "no tenant release: nothing said, and the resolver was handed exactly the caller's values")
    old = f"switchyard: p379 deployed board release old: {OLD} at /nonexistent/board/releases/old"
    st = status(target_sha="", resolve_error="gone")
    result, said, _, _ = report(st)
    check(result is st and said == [old, "switchyard: p379 deployed board release new: (unresolved origin/main: gone)",
                                     "switchyard: cannot produce a safe release update for p379: gone"], f"unresolved: {said}")
    st = status(target_sha=OLD)
    check(report(st)[1] == [old, f"switchyard: p379 deployed board release new: {OLD} from origin/main",
                            "switchyard: p379 deployed board release unchanged; no release deploy needed"], "unchanged: nothing to deploy")
    st = status()
    check(report(st)[1] == [old, f"switchyard: p379 deployed board release new: {NEW} from origin/main",
                            "switchyard: cannot produce a safe release update for p379: generated board, canary, and listener units are incomplete"],
          "no units: refused, and nothing rendered")
    with tempfile.TemporaryDirectory() as tmp:
        priv = Path(tmp) / "priv"
        st = status(provisioned_system_unit=priv / "p379" / "p379-ticket-board.service")
        result, said, _, _ = report(st, privileged=priv)
        check(said[2:6] == ["switchyard: matching-release deployment sequence (keep the listener stopped through migrations):", "  <listener stop>",
                            "  <recorded install units: <units>>", "  <recorded deploy-restart: <deploy>>"]
              and said[6] == "  <listener start>"
              and said[7] == f"  <recorded close release: {QUOTE(['switchyard', 'release-status', 'p379', '--close'])}>"
              and said[8].startswith("switchyard: if a step fails, stop there.") and "`<listener start>`" in said[8]
              and said[9].startswith("switchyard: each recorded step prints where its record is")
              and said[10].endswith("this command does not restart panes") and len(said) == 11,
              f"a root-owned unit source: the whole sequence, recorded, in order: {said}")
        st = status(provisioned_system_unit=Path(tmp) / "tenant" / "p379-ticket-board.service")
        result, said, _, _ = report(st, privileged=priv)
        check(said[3] == "  <listener stop>" and said[4].startswith(f"switchyard: omitting the unit-install step for p379: it would install from {Path(tmp) / 'tenant'}")
              and not any("install units" in line for line in said) and said[5] == "  <recorded deploy-restart: <deploy>>",
              f"a tenant-writable unit source: the install is omitted, not printed with a warning: {said}")


def test_the_release_pointer_is_captured_and_put_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = Path(tmp) / "board"
        (board / "releases" / "old").mkdir(parents=True)
        (board / "releases" / "new").mkdir()
        with patched(t, _tenant_board_root_from_config_or_plan=seam("_tenant_board_root_from_config_or_plan", lambda config, path: None)):
            check(m.capture_release_pointer(SimpleNamespace(project="p379")) == (None, ""), "no board root: nothing to capture")
        with patched(t, _tenant_board_root_from_config_or_plan=lambda config, path: board):
            check(m.capture_release_pointer(SimpleNamespace(project="p379")) == (board / "current", ""), "no pointer yet: nothing it pointed at")
            (board / "current").symlink_to("releases/old")
            captured = m.capture_release_pointer(SimpleNamespace(project="p379"), config_path=Path("/c"))
        check(captured == (board / "current", "releases/old"), f"the link's own text is kept: {captured}")
        runner = refuse("a runner")
        pointer, target = captured
        check(m.restore_release_pointer(None, "x", runner=runner) == [] and m.restore_release_pointer(pointer, "", runner=runner) == [],
              "nothing captured: nothing to restore")
        before = os.lstat(pointer).st_ino
        check(m.restore_release_pointer(pointer, "releases/old", runner=runner) == [] and os.readlink(pointer) == "releases/old"
              and os.lstat(pointer).st_ino == before and not os.path.lexists(board / ".current.rollback"), "already there: the very same link, untouched")
        (board / "current").unlink(); (board / "current").symlink_to("releases/new")
        (board / ".current.rollback").write_text("stale")
        check(m.restore_release_pointer(pointer, "releases/old", runner=runner) == [] and os.readlink(pointer) == "releases/old"
              and not (board / ".current.rollback").exists() and not os.path.islink(board / ".current.rollback"),
              "moved: put back through a staged link, a stale stage cleared first")
        (board / "current").unlink(); (board / "current").symlink_to("releases/new")
        (board / ".current.rollback").symlink_to("releases/gone")
        check(m.restore_release_pointer(pointer, "releases/old", runner=runner) == [] and os.readlink(pointer) == "releases/old",
              "a dangling stage is cleared too")
        missing = Path(tmp) / "absent" / "current"
        answer = judged(m.restore_release_pointer, missing, "releases/old", runner=runner)
        check(answer == [f"could not restore the release pointer {missing}: [Errno 2] No such file or directory: 'releases/old' -> '{Path(tmp) / 'absent' / '.current.rollback'}'"],
              f"a failure is said: {answer}")


class Result(SimpleNamespace):
    pass


def test_the_release_is_switched_inside_the_transaction() -> None:
    said: list[str] = []
    calls: list = []

    def run(st, *, runner=None, deploy="<deploy>"):
        said.clear()
        with patched(t, tenant_release_status=seam("tenant_release_status", lambda config, **k: st),
                     tenant_release_deploy_command=seam("tenant_release_deploy_command", lambda s, p: deploy)):
            return judged(m.deploy_release_in_transaction, SimpleNamespace(project="p379"), config_path=Path("/c"), source_repo=Path("/s"),
                          commit_git_dir=None, deploy_ref="ref", runner=runner or refuse("the deploy"), print_func=said.append)
    check(run(None) == ([], False), "no tenant release: nothing to switch or roll back")
    check(run(status(target_sha="", resolve_error="gone")) == (["the release to deploy could not be resolved: gone"], False), "unresolved")
    check(run(status(target_sha=OLD)) == ([], False), "unchanged: nothing restarted")
    check(run(status(), deploy="") == (["no deploy command could be built for this release"], False), "no command")
    runner = lambda argv, **kw: calls.append((argv, kw)) or Result(returncode=3, stderr="x" * 500 + " \n", stdout="")  # noqa: E731
    check(run(status(), runner=runner) == ([f"deploying {NEW} failed (exit 3): {'x' * 400}"], False)
          and calls == [(["sh", "-c", "<deploy>"], dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))] and said == [],
          f"a failed deploy: its exit and at most 400 characters of what it said: {calls}")
    check(run(status(), runner=lambda argv, **kw: Result(returncode=1, stderr="  ", stdout=""))[0] == [f"deploying {NEW} failed (exit 1): no output"],
          "a silent failure says so")
    check(run(status(), runner=lambda argv, **kw: Result(returncode=0, stderr="", stdout="")) == ([], True)
          and said == [f"switchyard: p379 board release deployed: {NEW}"], "a deploy: said, and the board restarted")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_only_its_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_thirteen")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"tenant_release_report_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
