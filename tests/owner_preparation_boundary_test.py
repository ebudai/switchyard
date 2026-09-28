#!/usr/bin/env python3
"""SYRD-368: new-project owner-account and project-directory preparation, against the launcher it came out of.

`OwnerUserProvisionResult`, `_owner_user_verbatim`, `ExistingOwnerUser`,
`_existing_owner_user`, `_confirm_existing_owner_user`,
`_resolve_owner_shell_path`, `_owner_project_install_args`,
`_owner_project_install_command`, `_owner_project_install_commands`,
`_enable_owner_linger_args`, `_owner_linger_show_args`,
`_owner_linger_is_enabled`, `_existing_project_path_is_usable`,
`_precheck_project_path_before_mutating`,
`_verify_project_path_writable_by_owner` and
`_ensure_owner_user_and_project_dir` moved into `scripts/owner_preparation.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports only the standard library at its top; the
  provisioning helpers are still imported inside the functions that use them.
- **One set of objects.** The launcher re-exports every name -- both classes
  included -- whichever module is imported first, so `switchyard_new_command`
  and the suites that patch these on the launcher reach them.
- **Seams (rule 24).** The host-account lookups, the prompt, the group lookup,
  the shared capability defaults, every moved helper another moved definition
  calls, and both classes' construction are read from the launcher when they
  run. Nothing they bind is read through it.
- **The behaviour is unchanged:** the owner name, the existing-owner
  confirmation, the shell resolution, the rendered install argv, linger, the
  permission precheck, the writable-by-owner check, and the preparation's order,
  refusals and result.

Every facility is this test's own fake: uid, home, group, prompt, `os.access`,
`shutil.which` and every runner. Runners record and never run; the only
filesystem effects are directories created inside temporary directories this
test owns.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("OwnerUserProvisionResult", "_owner_user_verbatim", "ExistingOwnerUser", "_existing_owner_user", "_confirm_existing_owner_user",
         "_resolve_owner_shell_path", "_owner_project_install_args", "_owner_project_install_command", "_owner_project_install_commands",
         "_enable_owner_linger_args", "_owner_linger_show_args", "_owner_linger_is_enabled", "_existing_project_path_is_usable",
         "_precheck_project_path_before_mutating", "_verify_project_path_writable_by_owner", "_ensure_owner_user_and_project_dir")
OWN = ("os", "shlex", "shutil", "stat", "subprocess", "dataclass", "Path", "Any", "Callable")
SEAMS = {
    "_existing_owner_user": {"_owner_user_verbatim": 1, "uid_for_user": 1, "home_dir_for_user": 1, "ExistingOwnerUser": 1},
    "_confirm_existing_owner_user": {"_existing_owner_user": 1, "_prompt_bool": 1},
    "_resolve_owner_shell_path": {"PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS": 1},
    "_owner_project_install_args": {"_resolve_owner_shell_path": 1, "_owner_project_install_command": 1},
    "_owner_project_install_commands": {"_owner_project_install_command": 1},
    "_owner_linger_is_enabled": {"_owner_linger_show_args": 1},
    "_existing_project_path_is_usable": {"uid_for_user": 1, "_group_ids_for_user": 1},
    "_precheck_project_path_before_mutating": {"_existing_project_path_is_usable": 1},
    "_ensure_owner_user_and_project_dir": {"_precheck_project_path_before_mutating": 1, "_owner_project_install_commands": 1,
                                           "_owner_project_install_args": 1, "_enable_owner_linger_args": 1, "_owner_linger_is_enabled": 1,
                                           "_verify_project_path_writable_by_owner": 1, "OwnerUserProvisionResult": 1},
}
OWNER = "syrd368-owner"


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


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


#: Every launcher facility that could reach the host for real, refused.
LIVE = dict(uid_for_user=refuse("the passwd uid lookup"), home_dir_for_user=refuse("the passwd home lookup"),
            _group_ids_for_user=refuse("the group lookup"), _prompt_bool=refuse("the prompt"))


def recorder(answers: dict, calls: list):
    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        answer = answers.get(argv[0], 0)
        return subprocess.CompletedProcess(argv, answer if isinstance(answer, int) else answer[0], answer[1] if isinstance(answer, tuple) else "", "")
    return run


def done(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.owner_preparation as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.owner_preparation", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.owner_preparation")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.owner_preparation as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_imports_and_the_classes() -> None:
    module = ast.parse((ROOT / "scripts" / "owner_preparation.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    names = [n.name for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(names == list(MOVED), f"the sixteen, in baseline order: {names}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in [n for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        annotations = set()
        for x in ast.walk(f):
            if isinstance(x, ast.arg) and x.annotation: annotations |= {id(y) for y in ast.walk(x.annotation)}
            if isinstance(x, ast.FunctionDef) and x.returns: annotations |= {id(y) for y in ast.walk(x.returns)}
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every and id(n) not in annotations})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")
    check(sum(sum(v.values()) for v in SEAMS.values()) == 21, "twenty-one call-time reads in all")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    locals_ = {name: [ast.unparse(n) for n in ast.walk(functions[name]) if isinstance(n, ast.ImportFrom) and n.module != "scripts"]
               for name in ("_owner_project_install_command", "_owner_project_install_commands")}
    check(locals_ == {"_owner_project_install_command": ["from scripts.ticket_board.project_provision import TENANT_SOURCE_MODE"],
                      "_owner_project_install_commands": ["from scripts.ticket_board.project_provision import owned_ancestor_dirs"]},
          f"the provisioning helpers are still imported where they are used: {locals_}")

    from scripts import owner_preparation as m, team_launcher

    for cls, fields in ((m.OwnerUserProvisionResult, ["created", "linger_enabled", "shell_path"]), (m.ExistingOwnerUser, ["name", "uid", "home"])):
        check(cls.__dataclass_params__.frozen and cls is getattr(team_launcher, cls.__name__) and [f.name for f in dataclasses.fields(cls)] == fields,
              f"{cls.__name__}: frozen, one object, its fields")
    check(m.OwnerUserProvisionResult(True, False).shell_path == "", "the result's shell path defaults to empty")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on (SYRD-425) --
    # re-exported there, unaliased.
    reexported = {a.name for n in launcher.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                  for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_group_ids_for_user", "_read_prompt", "_prompt_bool", "switchyard_new_command"} <= defined | reexported,
          f"the launcher defines none of them, and keeps the shared prompt and group helpers: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.owner_preparation"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all sixteen: {exported}")


# --- the owner --------------------------------------------------------------------------------------------------------


def test_the_owner_name_and_an_existing_owner() -> None:
    from scripts import team_launcher as t

    check(judged(t._owner_user_verbatim, "  " + OWNER + " ") == OWNER, "stripped")
    refused = judged(t._owner_user_verbatim, "   ")
    check(isinstance(refused, SystemExit) and str(refused) == "switchyard: owner user cannot be empty", f"empty refused: {refused!r}")
    asked: list = []
    with patched(t, **dict(LIVE, uid_for_user=lambda name: asked.append(("uid", name)) or None)):
        check(judged(t._existing_owner_user, f" {OWNER} ") is None and asked == [("uid", OWNER)], "no such account: none, the home not asked")
    with patched(t, **dict(LIVE, uid_for_user=lambda name: 1368, home_dir_for_user=lambda name: Path("/fixture/homes") / name)):
        got = judged(t._existing_owner_user, OWNER)
    check(got == t.ExistingOwnerUser(OWNER, 1368, Path("/fixture/homes") / OWNER), f"its uid and home: {got}")
    with patched(t, **dict(LIVE, uid_for_user=lambda name: 1368, home_dir_for_user=lambda name: None)):
        got = judged(t._existing_owner_user, OWNER)
    check(got.home == Path("/home") / OWNER and isinstance(judged(setattr, got, "uid", 0), dataclasses.FrozenInstanceError),
          f"no home recorded: /home/<owner>; frozen: {got}")


def confirm(existing, *, allow=False, source="", prompt=None):
    from scripts import team_launcher as t

    said: list = []
    with patched(t, **dict(LIVE, _existing_owner_user=lambda owner: existing, _prompt_bool=prompt or refuse("the prompt"))):
        got = judged(t._confirm_existing_owner_user, OWNER, allow_existing_owner_user=allow, input_func=refuse("input"),
                     print_func=said.append, agy_credential_source=source)
    return got, said


def test_reusing_an_existing_owner_is_confirmed() -> None:
    from scripts import team_launcher as t

    existing = t.ExistingOwnerUser(OWNER, 1368, Path("/fixture/homes/o"))
    detail = f"{OWNER} (uid 1368, home /fixture/homes/o)"
    check(confirm(None) == (None, []), "no existing account: nothing said, nothing asked")
    got, said = confirm(existing, allow=True)
    check(got is None and said == [f"warning: switchyard: owner user {detail} already exists"], f"allowed: warned, not asked: {said}")
    got, said = confirm(existing, allow=True, source="sam@example.org")
    check(len(said) == 2 and said[1] == (f"warning: switchyard: the agy credential from sam@example.org will be installed into {OWNER}, and every role of this "
                      "project will be able to act as the Google account sam@example.org signed in to agy with"), f"the credential consent: {said}")
    asked: list = []
    got, said = confirm(existing, prompt=lambda question, *, default, input_func: asked.append((question, default)) or True)
    check(got is None and asked == [(f"Use existing owner user {detail}", False)], f"asked, defaulting to no: {asked}")
    got, _ = confirm(existing, prompt=lambda question, *, default, input_func: False)
    check(isinstance(got, SystemExit) and str(got) == "switchyard: cancelled", f"declined: cancelled: {got!r}")
    got, _ = confirm(existing, prompt=lambda question, *, default, input_func: (_ for _ in ()).throw(SystemExit("switchyard: no input available")))
    check(isinstance(got, SystemExit) and str(got) == f"switchyard: owner user {detail} already exists; pass --allow-existing-owner-user to reuse it"
          and got.__suppress_context__, f"no input: told how to allow it: {got!r}")
    got, _ = confirm(existing, prompt=lambda question, *, default, input_func: (_ for _ in ()).throw(SystemExit("other failure")))
    check(isinstance(got, SystemExit) and str(got) == "other failure", f"any other refusal passes through: {got!r}")


# --- the shell and the install argv ---------------------------------------------------------------------------------------


def shell(requested, *, executable=(), which=None, default="fish"):
    from scripts import owner_preparation as m, team_launcher as t

    fake_os = SimpleNamespace(access=lambda path, mode: path in executable and mode == os.X_OK, X_OK=os.X_OK)
    fake_shutil = SimpleNamespace(which=lambda name: (which or {}).get(name))
    with patched(m, os=fake_os, shutil=fake_shutil), patched(t, PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS={"shell": default}):
        return judged(t._resolve_owner_shell_path, requested)


def test_the_owner_shell() -> None:
    check(isinstance(shell("  "), SystemExit) and str(shell("  ")) == "switchyard: owner shell cannot be empty", "empty")
    check(shell(" /opt/zsh ", executable={"/opt/zsh"}) == "/opt/zsh", "an executable path")
    got = shell("/opt/zsh")
    check(isinstance(got, SystemExit) and str(got) == "switchyard: owner shell '/opt/zsh' is not executable; install it or choose an installed shell",
          f"a path that is not executable: {got!r}")
    check(shell("zsh", which={"zsh": "/usr/bin/zsh"}) == "/usr/bin/zsh", "a name found on PATH")
    check(shell("fish", executable={"/bin/bash", "/bin/sh"}) == "/bin/bash" and shell("fish", executable={"/bin/sh"}) == "/bin/sh",
          "the default shell missing: bash, then sh")
    got = shell("fish")
    check(isinstance(got, SystemExit) and str(got) == ("switchyard: default owner shell 'fish' is unavailable and no fallback shell "
                                                       "(/bin/bash or /bin/sh) is executable"), f"no fallback: {got!r}")
    got = shell("zsh", executable={"/bin/bash"})
    check(isinstance(got, SystemExit) and str(got) == "switchyard: owner shell 'zsh' was not found on PATH; install it or choose an installed shell",
          f"only the configured default falls back: {got!r}")
    check(isinstance(shell("fish", executable={"/bin/bash"}, default="tcsh"), SystemExit), "the default is the shared capability's, read when it runs")


def test_the_install_argv() -> None:
    from scripts import team_launcher as t
    from scripts.ticket_board.project_provision import TENANT_SOURCE_MODE

    check(judged(t._owner_project_install_command, OWNER, "/h/Projects/p") == ["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", OWNER, "-g", OWNER, "/h/Projects/p"],
          "install -d with the tenant source mode, owned by the owner")
    with patched(t, **dict(LIVE, _resolve_owner_shell_path=lambda requested: f"/resolved/{requested}")):
        got = judged(t._owner_project_install_args, OWNER, Path("/h/Projects/p"), shell="zsh")
    check(got == [["id", "-u", OWNER], ["useradd", "-m", "-s", "/resolved/zsh", OWNER], t._owner_project_install_command(OWNER, "/h/Projects/p")],
          f"id, useradd with the resolved shell, then install: {got}")
    with patched(t, **dict(LIVE, _resolve_owner_shell_path=lambda requested: f"/resolved/{requested}")):
        check(judged(t._owner_project_install_args, OWNER, Path("/p"))[1][3] == "/resolved/fish", "fish by default")
    got = judged(t._owner_project_install_commands, OWNER, Path("/h/o/Projects/syrd/p"), owner_home=Path("/h/o"))
    check([c[-1] for c in got] == ["/h/o/Projects", "/h/o/Projects/syrd", "/h/o/Projects/syrd/p"],
          f"every directory below the home, outermost first, the target included: {got}")
    check([c[-1] for c in judged(t._owner_project_install_commands, OWNER, Path("/elsewhere/p"), owner_home=Path("/h/o"))] == ["/elsewhere/p"]
          and [c[-1] for c in judged(t._owner_project_install_commands, OWNER, Path("/h/o/p"))] == ["/h/o/p"],
          "outside the home, or no home named: the path alone")


def test_linger() -> None:
    from scripts import team_launcher as t

    check(judged(t._enable_owner_linger_args, OWNER) == ["loginctl", "enable-linger", OWNER]
          and judged(t._owner_linger_show_args, OWNER) == ["loginctl", "show-user", OWNER, "-p", "Linger", "--value"], "the linger argv")
    calls: list = []
    for answer, want in ((done(0, " yes \n"), True), (done(0, "no\n"), False), (done(1, "yes"), False), (SimpleNamespace(returncode=0), False)):
        calls.clear()
        got = judged(t._owner_linger_is_enabled, OWNER, runner=lambda argv, **kw: calls.append((argv, kw)) or answer)
        check(got is want and calls == [(["loginctl", "show-user", OWNER, "-p", "Linger", "--value"],
                                         {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True})], f"{answer}: {got}")


# --- the project path ---------------------------------------------------------------------------------------------------


def usable(path, *, uid, groups=()):
    from scripts import team_launcher as t

    with patched(t, **dict(LIVE, uid_for_user=lambda name: uid, _group_ids_for_user=lambda name: set(groups))):
        return judged(t._existing_project_path_is_usable, path, OWNER)


def test_whose_bits_are_asked() -> None:
    me, my_gid = os.getuid(), os.getgid()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p"
        check(usable(path, uid=me) is False, "missing: not usable")
        path.write_text("x")
        path.chmod(0o777)
        check(usable(path, uid=me) is False, "not a directory: not usable, whatever its bits")
        path.unlink(); path.mkdir()
        for mode, as_owner, as_group, as_other in ((0o700, True, False, False), (0o070, False, True, False), (0o007, False, False, True),
                                                   (0o500, False, False, False), (0o600, False, False, False)):
            path.chmod(mode)
            check((usable(path, uid=me) is as_owner, usable(path, uid=me + 4000, groups=[my_gid]) is as_group,
                   usable(path, uid=me + 4000, groups=[]) is as_other, usable(path, uid=None, groups=[my_gid]) is as_other)
                  == (True, True, True, True), f"mode {mode:o}: the owner's, the group's or the other bits, by who owns it")
        path.chmod(0o700)


def test_the_precheck_refuses_before_anything_changes() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p"
        with patched(t, **dict(LIVE, _existing_project_path_is_usable=refuse("the permission check"))):
            check(judged(t._precheck_project_path_before_mutating, OWNER, path) is None, "missing: nothing to check")
        path.write_text("x")
        got = judged(t._precheck_project_path_before_mutating, OWNER, path)
        check(isinstance(got, SystemExit) and str(got) == f"switchyard: project path {path} already exists but is not a directory", f"a file: {got!r}")
        path.unlink(); path.mkdir()
        with patched(t, **dict(LIVE, _existing_project_path_is_usable=lambda p, owner: False)):
            got = judged(t._precheck_project_path_before_mutating, OWNER, path)
        check(isinstance(got, SystemExit) and str(got) == (f"switchyard: project path {path} already exists but is not readable and writable by {OWNER}; "
                                                           "choose a writable path or fix ownership/permissions first"), f"unusable: {got!r}")
        with patched(t, **dict(LIVE, _existing_project_path_is_usable=lambda p, owner: True)):
            check(judged(t._precheck_project_path_before_mutating, OWNER, path) is None, "usable: fine")


def test_the_owner_is_asked_whether_it_can_write() -> None:
    from scripts import team_launcher as t

    calls: list = []
    path = Path("/fixture/it's here")
    q = "'/fixture/it'\"'\"'s here'"
    check(judged(t._verify_project_path_writable_by_owner, OWNER, path, runner=lambda argv, **kw: calls.append((argv, kw)) or done(0)) is None
          and calls == [(["sudo", "-u", OWNER, "sh", "-lc", f"test -d {q} && test -r {q} && test -w {q} && test -x {q}"],
                         {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})], f"the exact quoted check, as the owner: {calls}")
    got = judged(t._verify_project_path_writable_by_owner, OWNER, path, runner=lambda argv, **kw: done(1))
    check(isinstance(got, SystemExit) and str(got) == (f"switchyard: project path {path} is not readable and writable by {OWNER}; "
                                                       "choose a writable path or fix ownership/permissions first"), f"refused: {got!r}")


# --- the preparation ----------------------------------------------------------------------------------------------------


def prepare(tmp: Path, *, exists: bool, id_rc: int, answers: dict | None = None, linger=True):
    from scripts import team_launcher as t

    home = tmp / "home" / OWNER
    project = home / "Projects" / "syrd" / "p368"
    if exists:
        project.mkdir(parents=True)
    log: list = []
    runner_answers = {"id": id_rc, **(answers or {})}
    calls: list = []
    seams = dict(LIVE,
                 _precheck_project_path_before_mutating=lambda owner, path: log.append(("precheck", path.exists())),
                 _resolve_owner_shell_path=lambda requested: f"/resolved/{requested}",
                 _owner_linger_is_enabled=lambda owner, *, runner: log.append(("linger?", owner)) or linger,
                 _verify_project_path_writable_by_owner=lambda owner, path, *, runner: log.append(("verify", path.is_dir())))
    with patched(t, **seams):
        got = judged(t._ensure_owner_user_and_project_dir, OWNER, project, runner=recorder(runner_answers, calls), shell="zsh", owner_home=home)
    return got, log, calls, project, home


def test_a_new_owner_is_created_then_given_linger_then_the_tree() -> None:
    from scripts import team_launcher as t
    from scripts.ticket_board.project_provision import TENANT_SOURCE_MODE

    with tempfile.TemporaryDirectory() as tmp:
        got, log, calls, project, home = prepare(Path(tmp), exists=False, id_rc=1)
        check(got == t.OwnerUserProvisionResult(created=True, linger_enabled=True, shell_path="/resolved/zsh"), f"created, lingering, its shell: {got}")
        check([c[0] for c in calls] == [["id", "-u", OWNER], ["useradd", "-m", "-s", "/resolved/zsh", OWNER], ["loginctl", "enable-linger", OWNER],
                                       *[["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", OWNER, "-g", OWNER, str(d)]
                                         for d in (home / "Projects", home / "Projects" / "syrd", project)]]
              and calls[0][1] == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
              f"id, useradd, linger, then one install per directory, outermost first: {[c[0] for c in calls]}")
        check(log == [("precheck", False), ("verify", True)], f"the precheck first, the verification last, after the tree exists: {log}")
        check(project.is_dir() and (home / "Projects" / "syrd").is_dir(), "each named directory is created here too")
        modes = {d: stat.S_IMODE(d.stat().st_mode) & 0o777 for d in (home / "Projects", home / "Projects" / "syrd", project)}
        umask = os.umask(0); os.umask(umask)
        check(all(m == int(TENANT_SOURCE_MODE, 8) & 0o777 & ~umask for m in modes.values()), f"at the install mode (less this umask): {modes}")


def test_an_existing_owner_is_never_modified() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        got, log, calls, project, home = prepare(Path(tmp), exists=True, id_rc=0)
        check(got == t.OwnerUserProvisionResult(created=False, linger_enabled=False, shell_path="") and [c[0][0] for c in calls] == ["id"]
              and log == [("precheck", True), ("linger?", OWNER), ("verify", True)],
              f"existing owner and project: only asked, linger checked, verified: {got} {calls} {log}")
    with tempfile.TemporaryDirectory() as tmp:
        got, log, calls, project, home = prepare(Path(tmp), exists=False, id_rc=0, linger=False)
        check(isinstance(got, SystemExit) and str(got) == (f"switchyard: existing user '{OWNER}' does not have linger enabled; "
                                                           "switchyard refuses to modify an existing owner user")
              and [c[0][0] for c in calls] == ["id"] and not project.exists(), f"no linger: refused before anything is created: {got!r}")
    with tempfile.TemporaryDirectory() as tmp:
        got, log, calls, project, home = prepare(Path(tmp), exists=False, id_rc=0)
        check(got.created is False and [c[0][0] for c in calls] == ["id", "install", "install", "install"] and project.is_dir(),
              "existing owner, missing project: only the tree is installed")


def test_existence_is_read_before_the_precheck() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home" / OWNER
        project = home / "Projects" / "p368"
        calls: list = []
        # A precheck that finds the directory appeared: the snapshot taken before it still says "missing".
        seams = dict(LIVE, _precheck_project_path_before_mutating=lambda owner, path: path.mkdir(parents=True),
                     _owner_linger_is_enabled=lambda owner, *, runner: True,
                     _verify_project_path_writable_by_owner=lambda owner, path, *, runner: None)
        with patched(t, **seams):
            judged(t._ensure_owner_user_and_project_dir, OWNER, project, runner=recorder({"id": 0}, calls), owner_home=home)
        check([c[0][0] for c in calls] == ["id", "install", "install"], f"existence is read before the precheck runs: {calls}")


def test_every_failure_stops_in_order() -> None:
    for answers, message, ran in (
            ({"useradd": 9}, f"switchyard: failed to create user '{OWNER}'", ["id", "useradd"]),
            ({"loginctl": 9}, f"switchyard: failed to enable linger for created user '{OWNER}'", ["id", "useradd", "loginctl"])):
        with tempfile.TemporaryDirectory() as tmp:
            got, log, calls, project, home = prepare(Path(tmp), exists=False, id_rc=1, answers=answers)
            check(isinstance(got, SystemExit) and str(got) == message and [c[0][0] for c in calls] == ran and not project.exists()
                  and [e[0] for e in log] == ["precheck"], f"{answers}: {got!r} {calls}")
    with tempfile.TemporaryDirectory() as tmp:
        got, log, calls, project, home = prepare(Path(tmp), exists=False, id_rc=0, answers={"install": 9})
        check(isinstance(got, SystemExit) and str(got) == f"switchyard: failed to create project directory {home / 'Projects'}"
              and [c[0][0] for c in calls] == ["id", "install"] and not (home / "Projects").exists(),
              f"the first failed install stops it, and nothing is created: {got!r}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_local_imports_and_the_classes")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"owner_preparation_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
