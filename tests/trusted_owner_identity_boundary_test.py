#!/usr/bin/env python3
"""SYRD-381: the trusted owner identity, against the launcher it came out of.

`TrustedOwnerIdentity` and `trusted_owner_identity` moved unchanged into
`scripts/trusted_owner_identity.py`, and the launcher re-exports both. Six
privileged commands read the identity through the launcher. This pins what
makes the move safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **The class is the same:** frozen, the same fields in the same order, the
  empty-tuple default and the `trusted` property.
- **Seams (rule 24):** root's baseline path, the root-control walk and the
  account and home lookups are read through the launcher, as is the result
  class itself, so a patch on the launcher reaches each of them, which this
  test shows for all of them. `pwd` is the module's own singleton.
- **The behaviour is unchanged:** every refusal and its message, in order; the
  stripped fields; the account and home that must agree with root's record;
  the gid from passwd, falling back to the uid only when passwd has no entry.

Root's baseline is a file in an owned temporary directory; the root-control
walk, the account and home lookups and `pwd.getpwuid` are this test's own
stand-ins, so no real passwd or group lookup is made.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import trusted_owner_identity as m  # noqa: E402

CHECKS = 0
MOVED = ("TrustedOwnerIdentity", "trusted_owner_identity")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'trusted_owner_identity': {'TrustedOwnerIdentity': 7, 'home_dir_for_user': 1, 'privileged_baseline_plan_path': 1, 'root_controlled_problems_for': 1, 'uid_for_user': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
OWNER, UID, GID = "p381-owner", 43810, 43811


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


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "trusted_owner_identity.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


class Host:
    """Root's baseline in owned temp, and stand-ins for the root-control walk, the account records and passwd."""

    def __init__(self, tmp: str, *, plan: object = None, raw: str | None = None, walk: list[str] | None = None,
                 uid: int | None = UID, home: Path | None = None, passwd: object = "gid") -> None:
        self.baseline = Path(tmp) / "etc" / "p381" / "plan.json"
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.home = home if home is not None else Path(tmp) / "home" / OWNER
        if raw is not None:
            self.baseline.write_text(raw)
        elif plan is not None:
            self.baseline.write_text(json.dumps(plan))
        self.walk, self.uid, self.passwd = walk or [], uid, passwd
        self.asked: list = []

    def getpwuid(self, uid: int):
        self.asked.append(("pwd", uid))
        if self.passwd == "missing":
            raise KeyError(f"getpwuid(): uid not found: {uid}")
        return SimpleNamespace(pw_gid=str(GID))

    def run(self, home_answer: object = "same"):
        answer = self.home if home_answer == "same" else home_answer
        stand_ins = dict(
            privileged_baseline_plan_path=seam("privileged_baseline_plan_path", lambda project: self.asked.append(("path", project)) or self.baseline),
            root_controlled_problems_for=seam("root_controlled_problems_for", lambda path: self.asked.append(("walk", path)) or list(self.walk)),
            uid_for_user=seam("uid_for_user", lambda user: self.asked.append(("uid", user)) or (self.uid if user == OWNER else None)),
            home_dir_for_user=seam("home_dir_for_user", lambda user: self.asked.append(("home", user)) or (answer if user == OWNER else None)),
            TrustedOwnerIdentity=seam("TrustedOwnerIdentity", m.TrustedOwnerIdentity),
        )
        with patched(t, **stand_ins), patched(pwd, getpwuid=self.getpwuid):
            return judged(m.trusted_owner_identity, "p381")


def refusal(result: object, *problems: str) -> bool:
    return (isinstance(result, m.TrustedOwnerIdentity) and result == m.TrustedOwnerIdentity("", Path(), -1, -1, tuple(problems))
            and not result.trusted)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.trusted_owner_identity as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.trusted_owner_identity", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.trusted_owner_identity")):
        result = python("import importlib, dataclasses, pwd; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.trusted_owner_identity as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "m.dataclass is dataclasses.dataclass and t.TrustedOwnerIdentity.__dataclass_params__.frozen, "
                        "m.pwd is pwd is t.pwd)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.json is json and m.Path is Path, "the standard-library names are the module's own")


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
                  f"{name}: the launcher imported once, first thing when it runs: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's: {imports}")
        annotation = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                      for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs)] if part is not None
                      for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected
                       and id(x) not in annotation})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        check(not bound & set(through) and "pwd" not in through, f"{name}: nothing it binds itself, nor pwd, is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "trusted_owner_identity.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))] == list(MOVED), "the class, then the function")


def test_the_launcher_reexports_both() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.trusted_owner_identity"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the two, unaliased")
    check(not {getattr(n, "name", None) for n in tree.body} & set(MOVED), "the launcher defines neither itself")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_answer_class() -> None:
    fields = dataclasses.fields(m.TrustedOwnerIdentity)
    check([f.name for f in fields] == ["owner_user", "owner_home", "owner_uid", "owner_gid", "problems"]
          and [f.default for f in fields][4] == () and all(f.default is dataclasses.MISSING for f in fields[:4]),
          "the owner, home, uid and gid, then the problems, defaulting to none")
    ok = judged(m.TrustedOwnerIdentity, OWNER, Path("/h"), UID, GID)
    check(isinstance(ok, m.TrustedOwnerIdentity) and ok.problems == () and ok.trusted
          and not m.TrustedOwnerIdentity("", Path(), -1, -1, ("x",)).trusted, f"built without problems, as the success path does; trusted is their absence: {ok!r}")
    check(isinstance(judged(setattr, ok, "owner_uid", 0), dataclasses.FrozenInstanceError), "frozen")


def test_a_matching_owner_is_trusted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home" / OWNER
        host = Host(tmp, plan={"owner_user": f"  {OWNER} ", "owner_home": f" {home} ", "other": 1})
        result = host.run()
        check(result == m.TrustedOwnerIdentity(OWNER, home, UID, GID, ()) and isinstance(result, m.TrustedOwnerIdentity) and result.trusted
              and isinstance(result.owner_gid, int),
              f"root's record and the host agree: the owner, its home, uid and passwd's gid: {result!r}")
        check(host.asked == [("path", "p381"), ("walk", str(host.baseline)), ("uid", OWNER), ("home", OWNER), ("pwd", UID)],
              f"root-control first, then the stripped name through the account lookups, then passwd: {host.asked}")
        host = Host(tmp, plan={"owner_user": OWNER, "owner_home": str(home)}, passwd="missing")
        check(host.run() == m.TrustedOwnerIdentity(OWNER, home, UID, UID, ()), "no passwd entry: the gid falls back to the uid")
        host = Host(tmp, plan={"owner_user": OWNER, "owner_home": f"{home}/"})
        check(host.run() == m.TrustedOwnerIdentity(OWNER, home, UID, GID, ()), "the homes are compared as paths: a trailing slash is the same home")


def test_every_refusal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home" / OWNER
        good = {"owner_user": OWNER, "owner_home": str(home)}
        host = Host(tmp, plan=good, walk=["group-writable /etc", "owned by uid 7"])
        result = host.run()
        check(refusal(result, f"{host.baseline} is not root-controlled, so it cannot say who this tenant's owner is",
                      "group-writable /etc", "owned by uid 7") and [a[0] for a in host.asked] == ["path", "walk"],
              f"a baseline root does not control says nothing, and nothing else is asked: {result!r} {host.asked}")
        host = Host(tmp)
        host.baseline.unlink(missing_ok=True)
        result = host.run()
        check(refusal(result, f"{host.baseline} could not be read: [Errno 2] No such file or directory: '{host.baseline}'"), f"{result!r}")
        result = Host(tmp, raw="{ not json").run()
        check(refusal(result, f"{host.baseline} could not be read: Expecting property name enclosed in double quotes: line 1 column 3 (char 2)"),
              f"not JSON: {result!r}")
        check(refusal(Host(tmp, plan=[good]).run(), f"{host.baseline} is not a plan document"), "not a mapping")
        check(refusal(Host(tmp, plan={"owner_home": str(home)}).run(), f"{host.baseline} records no owner_user"), "no owner")
        check(refusal(Host(tmp, plan={"owner_user": OWNER, "owner_home": "  "}).run(), f"{host.baseline} records no owner_home"), "a blank home")
        check(refusal(Host(tmp, plan={}).run(), f"{host.baseline} records no owner_user", f"{host.baseline} records no owner_home"), "both, in order")
        host = Host(tmp, plan=good, uid=None)
        check(refusal(host.run(), f"{OWNER} is not an account on this host") and ("pwd", None) not in host.asked, "no such account")
        check(refusal(Host(tmp, plan=good).run(home_answer=None), f"{OWNER} is not an account on this host"), "no home for the account")
        host = Host(tmp, plan=good)
        result = host.run(home_answer=Path("/elsewhere"))
        check(refusal(result, f"{host.baseline} records {OWNER}'s home as {home}, and this host says /elsewhere. Nothing was changed: "
                              "which one is right is not this command's to decide.") and not any(a[0] == "pwd" for a in host.asked),
              f"root's record and the host disagree: no winner picked, and passwd is never asked: {result!r}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_both")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"trusted_owner_identity_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
