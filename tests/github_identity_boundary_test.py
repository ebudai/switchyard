#!/usr/bin/env python3
"""SYRD-345: GitHub identity handling, against the launcher it came out of.

`GITHUB_IDENTITY_TIMEOUT_SECONDS`, `github_identity_status`,
`write_plan_no_follow`, `selected_key_problems`, `_plan_with_selection` and the
set and clear repair commands moved into `scripts/github_identity.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher re-exports every name, the very same objects whichever module
  is imported first, and `upgrade` and the CLI still call them by those names.
- **Seams (rule 24).** Everything else the moved code uses from the launcher --
  the owner lookups, the no-follow walk and plan reader, root's plan paths, the
  status type and remedy -- is read from it when the code runs, and so are
  `github_identity_status` and `write_plan_no_follow` where the repairs call
  them, because suites patch them there. Nothing the code binds itself is read
  through the launcher (rule 27).
- **The behaviour is unchanged,** security first: the private half is never
  opened; the public half and the ssh configuration are read through one
  no-follow descriptor each and the descriptors are closed on every path; the
  fingerprint is taken from the validated bytes on stdin; the forge probe is
  bounded, non-interactive, run in the owner's environment, and judged by its
  greeting; the owner comes from root's record; both plan authorities are read
  before either is written, and the first is put back if the second fails; the
  owner's ssh configuration is changed only by commands run as the owner.

Every account, path, runner, plan authority, owner identity and remote here is
this test's own: files live in temporary directories this test creates, and no
key, home, pinned root file, ssh, ssh-keygen, sudo or forge is touched.
"""

from __future__ import annotations

import ast
import contextlib
import io
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
MOVED = ("GITHUB_IDENTITY_TIMEOUT_SECONDS", "github_identity_status", "write_plan_no_follow", "selected_key_problems",
         "_plan_with_selection", "clear_owner_github_identity_command", "set_owner_github_identity_command")
#: The launcher's names the moved code reads when it runs, and how many times (measured on the baseline).
SEAMS = {"uid_for_user": 1, "_walk_no_follow": 2, "_owner_command_env_args": 1, "GithubIdentityStatus": 1,
         "switchyard_privileged_provision_root": 1, "privileged_baseline_plan_path": 2, "read_plan_no_follow": 3,
         "trusted_owner_identity": 2, "github_identity_remedy": 1, "github_identity_status": 1,
         "write_plan_no_follow": 4}
OWNER = "syrd345-owner"
PROJECT = "p345"
KEY = "id_syrd345"


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


def open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


# --- structure -------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.github_identity as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.github_identity", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.github_identity")):
        result = python(
            "import importlib, inspect, os, subprocess; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.github_identity as g; "
            f"print(all(getattr(t, n) is getattr(g, n) for n in {MOVED!r}), g.os is os and g.subprocess is subprocess, "
            "inspect.signature(g.github_identity_status).parameters['runner'].default is subprocess.run, "
            "inspect.signature(g.set_owner_github_identity_command).parameters['print_func'].default is print)"
        )
        check(result.stdout.strip() == "True True True True",
              f"{' then '.join(order)}: every name is the launcher's too, the modules and defaults the same objects: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_the_codes_own_names() -> None:
    module = ast.parse((ROOT / "scripts" / "github_identity.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top (only for type checking, or when a function runs)")
    annotation: set[int] = set()
    for node in ast.walk(module):
        parts = ([node.returns] if isinstance(node, ast.FunctionDef) and node.returns else []) + (
            [node.annotation] if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation else [])
        for part in parts:
            annotation |= {id(x) for x in ast.walk(part)}
    for name, count in SEAMS.items():
        through = [n for n in ast.walk(module) if isinstance(n, ast.Attribute) and n.attr == name
                   and isinstance(n.value, ast.Name) and n.value.id == "launcher"]
        bare = [n for n in ast.walk(module) if isinstance(n, ast.Name) and n.id == name
                and isinstance(n.ctx, ast.Load) and id(n) not in annotation]
        check(len(through) == count and not bare, f"{name} is read at its {count} site(s), through the launcher only")
    for function in (n for n in module.body if isinstance(n, ast.FunctionDef)):
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {(a.asname or a.name) for i in ast.walk(function) if isinstance(i, ast.ImportFrom) for a in i.names}
        bound |= {n.name for n in ast.walk(function) if isinstance(n, ast.FunctionDef) and n is not function}
        bound.discard("launcher")
        through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
        check(through == [], f"{function.name}: nothing it binds itself is read through the launcher: {through}")
    local = sorted((n.module, a.name) for f in module.body if isinstance(f, ast.FunctionDef) for n in ast.walk(f)
                   if isinstance(n, ast.ImportFrom) and n.module != "scripts" for a in n.names)
    check(local == [("scripts.ticket_board.project_provision", "GITHUB_IDENTITY_BEGIN"),
                    ("scripts.ticket_board.project_provision", "GITHUB_IDENTITY_BEGIN"),
                    ("scripts.ticket_board.project_provision", "owner_github_block_removal_commands"),
                    ("scripts.ticket_board.project_provision", "owner_github_key_path"),
                    ("scripts.ticket_board.project_provision", "owner_github_key_path"),
                    ("scripts.ticket_board.project_provision", "owner_github_selection_commands"),
                    ("scripts.ticket_board.project_provision", "publication_remote_host"),
                    ("scripts.ticket_board.project_provision", "publication_uses_github"),
                    ("scripts.ticket_board.publication_boundary", "resolve_pinned_remote")],
          f"the provision and publication imports stay each function's own: {local}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {
        t.id for n in launcher.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    check(not set(MOVED) & defined, "no copy of a moved name is left defined in the launcher")
    calls = {name: [n for n in ast.walk(launcher) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == name]
             for name in ("github_identity_status", "set_owner_github_identity_command",
                          "clear_owner_github_identity_command")}
    check({k: len(v) for k, v in calls.items()} == {"github_identity_status": 1, "set_owner_github_identity_command": 1,
                                                     "clear_owner_github_identity_command": 1},
          f"upgrade and the CLI still call them by the launcher's own names: {calls}")


# --- github_identity_status ---------------------------------------------------------------------------------------


class Host:
    """An owner's ssh directory in a temporary directory, and the launcher lookups status reads, recorded."""

    def __init__(self, root: Path, *, uid: int | None = None) -> None:
        from scripts.ticket_board.project_provision import github_identity_block, owner_github_key_path

        self.home = root / "home"
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(parents=True)
        self.ssh.chmod(0o700)
        self.key = Path(owner_github_key_path(str(self.home), key_name=KEY))
        self.key.write_text("SYRD345 PRIVATE - never read\n", encoding="utf-8")
        self.key.chmod(0o600)
        self.public = self.key.with_name(self.key.name + ".pub")
        self.public.write_text("ssh-ed25519 AAAASYRD345 owner\n", encoding="utf-8")
        self.public.chmod(0o644)
        self.config = self.ssh / "config"
        self.config.write_text("Host other\n    HostName 10.0.0.1\n" + github_identity_block(str(self.home), key_name=KEY),
                               encoding="utf-8")
        self.config.chmod(0o600)
        self.uid = os.getuid() if uid is None else uid
        self.log: list[tuple] = []
        self.opened: list[str] = []

    def names(self) -> dict[str, object]:
        def walk(base: Path, relative: Path) -> tuple[int, str]:
            self.log.append(("walk", base, relative))
            return os.open(str(base / relative.parent), os.O_RDONLY | os.O_DIRECTORY), ""

        def env_args(owner: str, home: Path, argv: list[str]) -> list[str]:
            self.log.append(("env", owner, home))
            return ["syrd345-as", owner, *argv]

        return dict(uid_for_user=lambda user: self.log.append(("uid", user)) or self.uid,
                    _walk_no_follow=walk, _owner_command_env_args=env_args)

    def recording_open(self):
        real = os.open

        def opener(path, flags, *args, **kwargs):
            self.opened.append(os.fspath(path))
            return real(path, flags, *args, **kwargs)
        return patched(os, open=opener)


class Forge:
    """The two commands status runs: the fingerprint from stdin, and the bounded greeting probe."""

    def __init__(self, *, greeting: str = "Hi syrd345! You've successfully authenticated, but GitHub does not provide shell access.",
                 code: int = 1, keygen: int = 0, error: BaseException | None = None) -> None:
        self.greeting, self.code, self.keygen, self.error = greeting, code, keygen, error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:1] == ["ssh-keygen"]:
            return subprocess.CompletedProcess(argv, self.keygen, "256 SHA256:syrd345 owner (ED25519)\n", "")
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code, self.greeting, None)


def status(owner_host: Host, forge: Forge, **kwargs):
    from scripts import github_identity, team_launcher

    with patched(team_launcher, **owner_host.names()), owner_host.recording_open():
        return github_identity.github_identity_status(OWNER, owner_host.home, key_name=KEY, runner=forge, **kwargs)


def test_a_ready_identity_and_exactly_what_it_ran() -> None:
    from scripts import github_identity, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd345-status.") as raw:
        host = Host(Path(raw)); forge = Forge()
        before = open_fds()
        result = status(host, forge)
        check(open_fds() == before, "every descriptor it opened is closed")
        check(isinstance(result, team_launcher.GithubIdentityStatus) and result.problems == () and result.authenticated
              and result.ready and result.key_path == host.key and result.owner_user == OWNER and result.checked,
              f"a ready identity, as the launcher's own status type: {result}")
        check(result.public_key == "ssh-ed25519 AAAASYRD345 owner" and result.fingerprint == "256 SHA256:syrd345 owner (ED25519)"
              and "PRIVATE" not in repr(result), f"the public half and its fingerprint, and nothing private: {result}")
        keygen, probe = forge.calls
        check(keygen == (["ssh-keygen", "-l", "-f", "-"], dict(input="ssh-ed25519 AAAASYRD345 owner\n",
                                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)),
              f"fingerprinted from the validated bytes on stdin, never by path: {keygen}")
        check(probe == (["syrd345-as", OWNER, "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                         "-o", "ConnectTimeout=5", "-T", "git@github.com"],
                        dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             timeout=github_identity.GITHUB_IDENTITY_TIMEOUT_SECONDS))
              and github_identity.GITHUB_IDENTITY_TIMEOUT_SECONDS == 15.0,
              f"one bounded, non-interactive probe, in the owner's environment: {probe}")
        check(("env", OWNER, host.home) in host.log and ("uid", OWNER) in host.log,
              f"the owner's uid and environment come from the launcher: {host.log}")
        check(host.key.name not in host.opened and str(host.key) not in host.opened
              and host.public.name in host.opened and "config" in host.opened,
              f"the private half is never opened; the public half and the config are: {host.opened}")
        walked = [e[2] for e in host.log if e[0] == "walk"]
        check(walked == [Path(str(host.public).lstrip("/")), Path(str(host.config).lstrip("/"))],
              f"each read file is reached by the no-follow walk from /: {walked}")


def test_status_problems_are_read_not_assumed() -> None:
    cases = []
    with tempfile.TemporaryDirectory(prefix="syrd345-status.") as raw:
        root = Path(raw)

        def fresh(name: str) -> Host:
            path = root / name; path.mkdir(); return Host(path)

        h = fresh("dirmode"); h.ssh.chmod(0o755)
        cases.append((h, {}, f"the owner's ssh directory {h.ssh} is mode 0755 rather than 0700"))
        h = fresh("keymode"); h.key.chmod(0o644)
        cases.append((h, {}, f"the owner's GitHub key {h.key} is mode 0644 rather than 0600"))
        h = fresh("pubmode"); h.public.chmod(0o600)
        cases.append((h, {}, f"the owner's GitHub public key {h.public} is mode 0600 rather than 0644"))
        h = fresh("nokey"); h.key.unlink()
        cases.append((h, {}, f"the owner's GitHub key {h.key} does not exist"))
        h = fresh("nopub"); h.public.unlink()
        cases.append((h, {}, f"the owner's GitHub public key {h.public} does not exist"))
        h = fresh("linkpub"); h.public.unlink(); (root / "linkpub" / "elsewhere").write_text("x\n"); h.public.symlink_to(root / "linkpub" / "elsewhere")
        cases.append((h, {}, f"the owner's GitHub public key {h.public} is not a regular file"))
        h = fresh("linkkey"); h.key.unlink(); h.key.symlink_to(root / "linkkey" / "home" / ".ssh" / "config")
        cases.append((h, {}, f"the owner's GitHub key {h.key} is not a regular file"))
        h = fresh("dirpub"); h.public.unlink(); h.public.mkdir()
        cases.append((h, {}, f"the owner's GitHub public key {h.public} is not a regular file"))
        h = fresh("binary"); h.public.write_bytes(b"\xff\xfe\x00"); h.public.chmod(0o644)
        cases.append((h, {}, f"the owner's GitHub public key {h.public} is not text"))
        h = fresh("noblock"); h.config.write_text("Host other\n", encoding="utf-8"); h.config.chmod(0o600)
        cases.append((h, {}, f"{h.config} selects no managed identity for github.com; git offers no key and the push is "
                             "refused as if there were none"))
        h = fresh("otherkey")
        from scripts.ticket_board.project_provision import github_identity_block
        h.config.write_text(github_identity_block(str(h.home), key_name="id_other"), encoding="utf-8"); h.config.chmod(0o600)
        cases.append((h, {}, f"{h.config} selects an identity other than {h.key} for github.com"))
        h = fresh("uid"); h.uid = os.getuid() + 1
        cases.append((h, {}, f"the owner's ssh directory {h.ssh} is owned by uid {os.getuid()} rather than by {OWNER}"))
        for host, kwargs, want in cases:
            before = open_fds()
            result = status(host, Forge(), **kwargs)
            check(want in result.problems and not result.ready,
                  f"{want!r} is reported, and the identity is not ready: {result.problems}")
            check(open_fds() == before, f"and every descriptor is closed on that path too ({want[:40]})")
            check(host.key.name not in host.opened, f"the private half is never opened ({want[:40]})")
        h = fresh("uids"); h.uid = os.getuid() + 1
        result = status(h, Forge())
        owned = [p for p in result.problems if "rather than by " + OWNER in p]
        check(owned == [f"the owner's ssh directory {h.ssh} is owned by uid {os.getuid()} rather than by {OWNER}",
                        f"the owner's GitHub key {h.key} is owned by uid {os.getuid()} rather than by {OWNER}",
                        f"the owner's GitHub public key {h.public} is owned by uid {os.getuid()} rather than by {OWNER}",
                        f"the owner's ssh configuration {h.config} is owned by uid {os.getuid()} rather than by {OWNER}"],
              f"ownership is checked on the directory, both key halves and the configuration, in that order: {owned}")
        h = fresh("nouid"); h.uid = None
        result = status(h, Forge())
        check(not any("owned by uid" in p for p in result.problems), f"an unknown owner uid checks no ownership: {result}")


def test_the_greeting_decides_not_the_exit_status() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-status.") as raw:
        root = Path(raw)
        for i, (forge, authenticated, detail) in enumerate((
                (Forge(code=1), True, "Hi syrd345! You've successfully authenticated, but GitHub does not provide shell access."),
                (Forge(greeting="git@github.com: Permission denied (publickey).", code=255), False,
                 "git@github.com: Permission denied (publickey)."),
                (Forge(greeting="", code=255), False, "exit 255"),
                (Forge(greeting="x" * 400, code=0), False, "x" * 300),
                (Forge(error=subprocess.TimeoutExpired(["ssh"], 15)), False, "github.com did not answer within 15s"),
                (Forge(error=OSError("syrd345: no ssh")), False, "syrd345: no ssh"))):
            (root / str(i)).mkdir()
            result = status(Host(root / str(i)), forge)
            check(result.authenticated is authenticated and result.detail == detail,
                  f"authenticated only by the greeting, the detail bounded: {result.authenticated} {result.detail[:60]!r}")
        (root / "keygen").mkdir()
        result = status(Host(root / "keygen"), Forge(keygen=1))
        check(result.fingerprint == "" and result.public_key, f"a failed fingerprint is left empty: {result}")
        (root / "host").mkdir()
        forge = Forge()
        result = status(Host(root / "host"), forge, host="github.example")
        check(forge.calls[1][0][-1] == "git@github.example", f"the probe asks the host given: {forge.calls[1][0]}")


# --- write_plan_no_follow, selected_key_problems, _plan_with_selection --------------------------------------------


def plan_document(path: Path, data: dict | None = None, *, mode: int = 0o640):
    from scripts import team_launcher

    raw = path.read_bytes() if path.exists() else b"{}\n"
    return team_launcher.PlanDocument(path=path, data=data or {}, raw=raw, uid=os.getuid(), gid=os.getgid(), mode=mode)


def writer(document, body: bytes, **patches):
    from scripts import github_identity, team_launcher

    walked: list[tuple] = []

    def walk(base: Path, relative: Path) -> tuple[int, str]:
        walked.append((base, relative))
        return os.open(str(base / relative.parent), os.O_RDONLY | os.O_DIRECTORY), ""
    with patched(team_launcher, _walk_no_follow=patches.pop("walk", walk)), patched(os, **patches):
        return github_identity.write_plan_no_follow(document, body), walked


def test_the_plan_writer_replaces_in_place_and_carries_the_mode() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-plan.") as raw:
        folder = Path(raw)
        plan = folder / "plan.json"; plan.write_bytes(b"old\n"); plan.chmod(0o600)
        before = open_fds()
        problem, walked = writer(plan_document(plan, mode=0o640), b"new\n")
        check(problem == "" and plan.read_bytes() == b"new\n" and stat.S_IMODE(plan.stat().st_mode) == 0o640,
              f"replaced with the new bytes and the document's mode: {problem!r}")
        check(walked == [(Path("/"), Path(str(plan).lstrip("/")))] and open_fds() == before
              and sorted(os.listdir(folder)) == ["plan.json"],
              f"reached by the no-follow walk, every descriptor closed, nothing staged left: {os.listdir(folder)}")
        target = folder / "elsewhere.json"; target.write_bytes(b"theirs\n")
        plan.unlink(); plan.symlink_to(target)
        problem, _ = writer(plan_document(target, mode=0o600).__class__(path=plan, data={}, raw=b"", uid=os.getuid(),
                                                                        gid=os.getgid(), mode=0o600), b"ours\n")
        check(problem == "" and target.read_bytes() == b"theirs\n" and not plan.is_symlink()
              and plan.read_bytes() == b"ours\n", "a symlink at the destination is replaced, never followed")
        stale = folder / ".plan.json.switchyard-new"; stale.write_bytes(b"stale\n")
        problem, _ = writer(plan_document(plan), b"again\n")
        check(problem == "" and plan.read_bytes() == b"again\n" and not stale.exists(),
              "a stale staged file is removed and the write retried")
        owned: list[tuple] = []
        problem, _ = writer(plan_document(plan), b"root\n", geteuid=lambda: 0,
                            fchown=lambda fd, uid, gid: owned.append((uid, gid)))
        check(problem == "" and owned == [(os.getuid(), os.getgid())], f"as root, the document's owner is carried: {owned}")
        owned.clear()
        writer(plan_document(plan), b"user\n", fchown=lambda fd, uid, gid: owned.append((uid, gid)))
        check(owned == [], "and not otherwise")
        before = open_fds()

        def refuse(*a, **k):
            raise OSError(13, "Permission denied")
        problem, _ = writer(plan_document(plan), b"x\n", rename=refuse)
        check(problem == f"{plan} could not be replaced (Permission denied)" and plan.read_bytes() == b"user\n"
              and not stale.exists() and open_fds() == before,
              f"a failed write says so, cleans up its staged file and closes everything: {problem!r}")
        problem, _ = writer(plan_document(plan), b"x\n", walk=lambda base, rel: (-1, "syrd345: a link on the way"))
        check(problem == f"{plan}: syrd345: a link on the way", f"an unsafe path is refused before anything opens: {problem!r}")


def test_key_selection_checks_both_halves_and_opens_neither() -> None:
    from scripts import github_identity

    with tempfile.TemporaryDirectory(prefix="syrd345-keys.") as raw:
        home = Path(raw); ssh = home / ".ssh"; ssh.mkdir()
        (ssh / KEY).write_text("private\n"); (ssh / f"{KEY}.pub").write_text("public\n")

        def refuse(*a, **k):
            raise AssertionError("selection opened a key file")
        with patched(os, open=refuse):
            check(github_identity.selected_key_problems(home, KEY, os.getuid()) == [], "the owner's own pair is fine")
            check(github_identity.selected_key_problems(home, KEY, os.getuid() + 1) == [
                f"the private half {ssh / KEY} is owned by uid {os.getuid()} rather than by the tenant owner",
                f"the public half {ssh / (KEY + '.pub')} is owned by uid {os.getuid()} rather than by the tenant owner"],
                  "each half somebody else owns is named")
            (ssh / f"{KEY}.pub").unlink(); (ssh / f"{KEY}.pub").symlink_to(ssh / KEY)
            (ssh / "id_dir").mkdir()
            check(github_identity.selected_key_problems(home, KEY, os.getuid()) == [
                f"the public half {ssh / (KEY + '.pub')} is a symlink, so what it names is not this key"],
                  "a symlinked half is refused, not followed")
            check(github_identity.selected_key_problems(home, "id_dir", os.getuid()) == [
                f"the private half {ssh / 'id_dir'} is not a regular file",
                f"the public half {ssh / 'id_dir.pub'} does not exist"], "a directory and a missing half")
        check(sorted(os.listdir(ssh)) == sorted([KEY, f"{KEY}.pub", "id_dir"]), "and nothing was created")


def test_the_selection_rewrites_only_its_two_fields() -> None:
    from scripts import github_identity

    base = {"project": PROJECT, "owner_user": OWNER, "zeta": [1, 2]}
    document = SimpleNamespace(data=dict(base))
    body = github_identity._plan_with_selection(document, KEY, "gh-345")
    check(body == ('{\n  "owner_github_host_alias": "gh-345",\n  "owner_github_key_name": "id_syrd345",\n'
                   '  "owner_user": "syrd345-owner",\n  "project": "p345",\n  "zeta": [\n    1,\n    2\n  ]\n}\n').encode(),
          f"every other field kept, sorted, two-space JSON with a newline: {body!r}")
    check(document.data == base, "and the document itself is not changed")
    for data in ({**base, "owner_github_key_name": KEY, "owner_github_host_alias": "gh-345"},):
        check(github_identity._plan_with_selection(SimpleNamespace(data=data), KEY, "gh-345") is None, "already right: None")
    check(github_identity._plan_with_selection(SimpleNamespace(data={"owner_github_key_name": None}), "", "") is None,
          "a missing or null selection is the empty selection")


# --- the set and clear repairs -------------------------------------------------------------------------------------


class Repair:
    """The launcher facilities the repairs read, answering from this test's own objects, into one log."""

    def __init__(self, home: Path, *, trusted: bool = True, documents: dict | None = None, fail_write: str = "",
                 remedy: str = "", fingerprint: str = "SHA256:syrd345") -> None:
        self.home = home
        self.trusted = trusted
        self.root_plan = Path("/nonexistent/syrd345/root/plan.json")
        self.tenant_plan = Path("/nonexistent/syrd345/tenant/plan.json")
        self.documents = documents if documents is not None else {
            self.root_plan: {"project": PROJECT}, self.tenant_plan: {"project": PROJECT}}
        self.fail_write, self.remedy, self.fingerprint = fail_write, remedy, fingerprint
        self.log: list[tuple] = []

    def document(self, path: Path):
        from scripts import team_launcher
        data = self.documents[path]
        return team_launcher.PlanDocument(path=path, data=dict(data), raw=f"RAW {path.parent.name}".encode(),
                                         uid=0, gid=0, mode=0o644)

    def names(self) -> dict[str, object]:
        from scripts import team_launcher
        L = self.log

        def read(path, *, require_root_owned=False):
            L.append(("read", path, require_root_owned))
            if self.documents.get(path) is None:
                return None, f"syrd345: {path} is unreadable"
            return self.document(path), ""

        def write(document, body):
            L.append(("write", document.path, body))
            return f"syrd345: {document.path} refused" if self.fail_write and str(document.path) == self.fail_write \
                and body != document.raw else ""

        def identity(project):
            L.append(("identity", project))
            return team_launcher.TrustedOwnerIdentity(owner_user=OWNER, owner_home=self.home, owner_uid=os.getuid(),
                                                     owner_gid=os.getgid(),
                                                     problems=() if self.trusted else ("syrd345: root has no record",))

        def status_of(owner, home, **kwargs):
            L.append(("status", owner, home, kwargs))
            return SimpleNamespace(fingerprint=self.fingerprint)

        return dict(read_plan_no_follow=read, write_plan_no_follow=write, trusted_owner_identity=identity,
                    privileged_baseline_plan_path=lambda project: L.append(("root-plan", project)) or self.root_plan,
                    github_identity_status=status_of,
                    github_identity_remedy=lambda status, *, project: L.append(("remedy", project)) or self.remedy,
                    switchyard_privileged_provision_root=lambda: Path("/nonexistent/syrd345/registration"))

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


class Shell:
    """The runner: records each command, answering from a table keyed by its first word."""

    def __init__(self, **codes: int) -> None:
        self.codes = codes
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, self.codes.get(argv[0], 0), "", "syrd345: stderr\n")


def with_keys(root: Path) -> Path:
    home = root / "owner-home"; (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / KEY).write_text("private\n"); (home / ".ssh" / f"{KEY}.pub").write_text("public\n")
    return home


def set_identity(repair: Repair, shell: Shell, *, euid: int = 0, key_name: str = KEY, **kwargs):
    from scripts import github_identity, team_launcher

    said: list[str] = []
    config = SimpleNamespace(project=PROJECT)
    with patched(team_launcher, **repair.names()), patched(os, geteuid=lambda: euid):
        code = github_identity.set_owner_github_identity_command(
            config, config_path=repair.tenant_plan.with_name("p345.json"), key_name=key_name, runner=shell,
            print_func=said.append, **kwargs)
    return code, said


def test_set_refuses_before_reading_anything_it_should_not() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-set.") as raw:
        home = with_keys(Path(raw))
        for bad in ("", "  ", "keys/id_x"):
            repair = Repair(home); shell = Shell()
            code, said = set_identity(repair, shell, key_name=bad)
            check(code == 1 and said == [f"switchyard: {bad!r} is not a key file name. Name one of the owner's keys, "
                                         "without a path."] and repair.log == [] and shell.calls == [],
                  f"{bad!r} is not a key name, and nothing is asked: {said}")
        repair = Repair(home, trusted=False); shell = Shell()
        code, said = set_identity(repair, shell)
        check(code == 1 and said == ["switchyard: syrd345: root has no record",
                                     f"switchyard: refusing to change {PROJECT}'s publication identity: root cannot "
                                     "establish whose it is. Nothing was changed."]
              and repair.kinds() == ["identity"] and shell.calls == [],
              f"an untrusted owner stops it before any plan is read: {said} {repair.kinds()}")
        repair = Repair(home); shell = Shell()
        code, said = set_identity(repair, shell, key_name="id_missing")
        check(code == 1 and said[-1] == f"switchyard: id_missing is not a key pair {OWNER} owns, so there is nothing to "
                                        "select. Nothing was changed, and no key was created."
              and repair.kinds() == ["identity"], f"a key the owner does not have is refused: {said}")
        repair = Repair(home, documents={Repair(home).root_plan: {"project": PROJECT}, Repair(home).tenant_plan: None})
        code, said = set_identity(repair, Shell())
        check(code == 1 and "write" not in repair.kinds()
              and said[-1] == f"switchyard: {PROJECT}'s publication identity is recorded in two places and both have to "
                              "be writable, or the two disagree afterwards. Nothing was changed."
              and [e[1:] for e in repair.log if e[0] == "read"] == [(repair.root_plan, True), (repair.tenant_plan, False)],
              f"both authorities are read, root's as root's, before anything is written: {said}")


def test_set_dry_run_and_non_root_write_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-set.") as raw:
        home = with_keys(Path(raw))
        repair = Repair(home, documents={Repair(home).root_plan: {"owner_github_key_name": KEY, "owner_github_host_alias": "gh"},
                                         Repair(home).tenant_plan: {"project": PROJECT}})
        shell = Shell()
        code, said = set_identity(repair, shell, dry_run=True, host_alias=" gh ")
        check(code == 0 and "write" not in repair.kinds() and shell.calls == [] and said == [
            f"switchyard: would record {PROJECT}'s publication identity as {KEY} with host alias gh",
            f"switchyard:   {repair.root_plan} already records it",
            f"switchyard:   {repair.tenant_plan} would be updated",
            f"switchyard: would have {OWNER} select {home / '.ssh' / KEY} in {home}/.ssh/config, managed block only"],
              f"a dry run says what it would do and does none of it: {said}")
        repair = Repair(home); shell = Shell()
        code, said = set_identity(repair, shell, euid=1000, host_alias="gh")
        check(code == 1 and "write" not in repair.kinds() and shell.calls == [] and said == [
            f"switchyard: recording {PROJECT}'s publication identity writes root's own plan. Run: "
            f"sudo switchyard set-owner-identity {PROJECT} --key-name {KEY} --host-alias gh"],
              f"not root: the command to run instead, and nothing written: {said}")


def test_set_writes_both_then_selects_as_the_owner_and_verifies() -> None:
    from scripts import github_identity
    from scripts.ticket_board.project_provision import owner_github_selection_commands

    with tempfile.TemporaryDirectory(prefix="syrd345-set.") as raw:
        home = with_keys(Path(raw))
        repair = Repair(home); shell = Shell()
        code, said = set_identity(repair, shell, host_alias="gh-345", host="github.example")
        writes = [e for e in repair.log if e[0] == "write"]
        check([w[1] for w in writes] == [repair.root_plan, repair.tenant_plan]
              and all(w[2] == github_identity._plan_with_selection(repair.document(w[1]), KEY, "gh-345") for w in writes),
              f"root's plan, then the tenant's, each with the selection: {writes}")
        script = "set -eu\n" + "\n".join(owner_github_selection_commands(OWNER, str(home), key_name=KEY,
                                                                          host="github.example", host_alias="gh-345"))
        check(shell.calls == [(["sh", "-c", script], dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))],
              f"the ssh config is changed by the owner-run selection commands, once: {shell.calls}")
        check(("status", OWNER, home, dict(key_name=KEY, host="github.example", runner=shell)) in repair.log
              and ("remedy", PROJECT) in repair.log,
              f"then the selection is verified through the launcher's status and remedy: {repair.log}")
        check(code == 0 and said == [f"switchyard: recorded {KEY} in {repair.root_plan}",
                                     f"switchyard: recorded {KEY} in {repair.tenant_plan}",
                                     f"switchyard: {home}/.ssh/config now selects {home / '.ssh' / KEY} for github.example",
                                     "switchyard: fingerprint: SHA256:syrd345",
                                     f"switchyard: {OWNER} can publish to GitHub as {KEY}"], f"what it says: {said}")
        done = {"owner_github_key_name": KEY, "owner_github_host_alias": ""}
        repair = Repair(home, documents={Repair(home).root_plan: done, Repair(home).tenant_plan: done})
        code, said = set_identity(repair, Shell())
        check(code == 0 and "write" not in repair.kinds() and said[:2] == [
            f"switchyard: {repair.root_plan} already records {KEY}", f"switchyard: {repair.tenant_plan} already records {KEY}"],
              f"already recorded: no write, and it still selects and verifies: {said}")


def test_set_puts_the_first_plan_back_when_the_second_fails() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-set.") as raw:
        home = with_keys(Path(raw))
        repair = Repair(home, fail_write=str(Repair(home).tenant_plan)); shell = Shell()
        code, said = set_identity(repair, shell)
        writes = [(e[1], e[2]) for e in repair.log if e[0] == "write"]
        check(writes[-1] == (repair.root_plan, b"RAW root") and len(writes) == 3,
              f"the root plan it had written is put back with its original bytes: {writes}")
        check(code == 1 and shell.calls == [] and said == [
            f"switchyard: recorded {KEY} in {repair.root_plan}",
            f"switchyard: syrd345: {repair.tenant_plan} refused",
            f"switchyard: {repair.root_plan} was put back as it was",
            f"switchyard: {PROJECT}'s publication identity was not changed, and neither plan was left disagreeing with "
            "the other."], f"and nothing is selected: {said}")
        repair = Repair(home, fail_write=str(Repair(home).root_plan))
        code, said = set_identity(repair, Shell())
        check(code == 1 and [e[1] for e in repair.log if e[0] == "write"] == [repair.root_plan],
              "a first write that fails leaves nothing to put back")


def test_set_reports_a_selection_or_verification_that_fails() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-set.") as raw:
        home = with_keys(Path(raw))
        repair = Repair(home); shell = Shell(sh=3)
        code, said = set_identity(repair, shell)
        check(code == 1 and "status" not in repair.kinds() and said[-2:] == [
            f"switchyard: could not select {KEY} for {OWNER} (exit 3): syrd345: stderr",
            f"switchyard: both plans record {KEY}; {home}/.ssh/config does not yet. Rerun this command, which is safe to "
            "repeat."], f"a failed selection says how to finish: {said}")
        repair = Repair(home, remedy="REMEDY syrd345", fingerprint="")
        code, said = set_identity(repair, Shell())
        check(code == 1 and "switchyard: fingerprint: " not in " ".join(said) and said[-2:] == [
            "REMEDY syrd345",
            f"switchyard: {KEY} is selected and recorded, but it did not authenticate. Register its public half with the "
            "forge, or select a different key."], f"a key that does not authenticate is reported with the remedy: {said}")


class Remote:
    """The pinned remote, as root records it: the publication boundary's lookup, patched where it lives."""

    def __init__(self, remote: str, problem: str = "") -> None:
        self.remote, self.problem = remote, problem
        self.asked: list = []

    def __call__(self, project, *, registration_root, declared_remote=""):
        self.asked.append((project, registration_root, declared_remote))
        return self.remote, self.problem


def clear_identity(repair: Repair, shell: Shell, remote: Remote, *, euid: int = 0, **kwargs):
    from scripts import github_identity, team_launcher
    from scripts.ticket_board import publication_boundary

    said: list[str] = []
    with patched(team_launcher, **repair.names()), patched(os, geteuid=lambda: euid), \
            patched(publication_boundary, resolve_pinned_remote=remote):
        code = github_identity.clear_owner_github_identity_command(
            SimpleNamespace(project=PROJECT), config_path=repair.tenant_plan.with_name("p345.json"), runner=shell,
            print_func=said.append, **kwargs)
    return code, said


def test_clear_refuses_a_tenant_that_publishes_to_github() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-clear.") as raw:
        home = with_keys(Path(raw))
        for remote, why in (("git@github.com:syrd/p345.git", "it publishes to git@github.com:syrd/p345.git, which is "
                                                             "GitHub, so the identity is in use."),
                            ("", "its publication remote is not established (syrd345: no pin).")):
            repair = Repair(home); shell = Shell(); pinned = Remote(remote, "syrd345: no pin")
            code, said = clear_identity(repair, shell, pinned)
            check(code == 1 and said == [f"switchyard: refusing to clear {PROJECT}'s GitHub identity: {why} Nothing was "
                                         "changed."]
                  and "read" not in repair.kinds() and shell.calls == [],
                  f"refused from root's pinned remote alone, before any plan is read: {said} {repair.kinds()}")
            check(pinned.asked == [(PROJECT, Path("/nonexistent/syrd345/registration"), "")],
                  f"the remote is root's pin, from root's registration root: {pinned.asked}")
        pinned = Remote("git@gh-345:syrd/p345.git")
        repair = Repair(home, documents={Repair(home).root_plan: {"owner_github_host_alias": "gh-345"},
                                         Repair(home).tenant_plan: {}})
        code, said = clear_identity(repair, Shell(), pinned, registration_root=Path("/nonexistent/syrd345/given"))
        check(code == 1 and [e[1:] for e in repair.log if e[0] == "read"] == [(repair.root_plan, True)]
              and "GitHub, so the identity is in use" in said[0] and pinned.asked[0][1] == Path("/nonexistent/syrd345/given"),
              f"a hosted alias is judged from root's own plan only, never the tenant's: {said} {repair.log}")


def test_clear_dry_run_non_root_and_missing_authorities() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd345-clear.") as raw:
        home = with_keys(Path(raw))
        local = Remote("/srv/git/p345.git")
        repair = Repair(home, documents={Repair(home).root_plan: {"owner_github_key_name": KEY}, Repair(home).tenant_plan: {}})
        shell = Shell()
        code, said = clear_identity(repair, shell, local, dry_run=True)
        check(said[:1] == [f"switchyard: {PROJECT} publishes to /srv/git/p345.git, not GitHub"],
              f"a local remote is not GitHub, so the clear goes on: {said[:1]}")
        ssh_config = home / ".ssh" / "config"
        from scripts.ticket_board.project_provision import GITHUB_IDENTITY_BEGIN
        import shlex
        probe = ["sudo", "-u", OWNER, "sh", "-c",
                 f"[ -f {shlex.quote(str(ssh_config))} ] && grep -qx {shlex.quote(GITHUB_IDENTITY_BEGIN)} "
                 f"{shlex.quote(str(ssh_config))}"]
        check(shell.calls == [(probe, dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))],
              f"whether there is a managed block is asked as the owner, and only that: {shell.calls}")
        check(code == 0 and "write" not in repair.kinds() and said == [
            f"switchyard: {PROJECT} publishes to /srv/git/p345.git, not GitHub",
            f"switchyard:   {repair.root_plan} would have owner_github_key_name and owner_github_host_alias cleared",
            f"switchyard:   {repair.tenant_plan} records no GitHub identity",
            f"switchyard:   {ssh_config}: would have Switchyard's managed GitHub block removed, as {OWNER}, every other "
            "stanza kept",
            "switchyard: nothing was written"], f"a dry run: {said}")
        repair = Repair(home); shell = Shell()
        code, said = clear_identity(repair, shell, local, euid=1000)
        check(code == 1 and "write" not in repair.kinds() and said[-1] == (
            f"switchyard: clearing {PROJECT}'s GitHub identity writes root's own plan. Run: sudo switchyard "
            f"set-owner-identity {PROJECT} --clear"), f"not root: {said}")
        repair = Repair(home, documents={Repair(home).root_plan: None, Repair(home).tenant_plan: {}})
        code, said = clear_identity(repair, Shell(), local)
        check(code == 1 and "identity" not in repair.kinds() and said[-1] == (
            f"switchyard: {PROJECT}'s publication identity is recorded in two places and both have to be readable to "
            "clear it. Nothing was changed."), f"an unreadable authority stops it: {said}")
        repair = Repair(home, trusted=False)
        code, said = clear_identity(repair, Shell(), local)
        check(code == 1 and "write" not in repair.kinds() and said[-1] == (
            f"switchyard: refusing to clear {PROJECT}'s GitHub identity: root cannot establish whose it is. Nothing was "
            "changed."), f"an untrusted owner stops it: {said}")


def test_clear_writes_restores_and_removes_the_block_as_the_owner() -> None:
    from scripts import github_identity
    from scripts.ticket_board.project_provision import owner_github_block_removal_commands

    with tempfile.TemporaryDirectory(prefix="syrd345-clear.") as raw:
        home = with_keys(Path(raw))
        local = Remote("/srv/git/p345.git")
        recorded = {"owner_github_key_name": KEY, "owner_github_host_alias": "gh"}
        repair = Repair(home, documents={Repair(home).root_plan: recorded, Repair(home).tenant_plan: recorded})
        shell = Shell()
        code, said = clear_identity(repair, shell, local)
        writes = [(e[1], e[2]) for e in repair.log if e[0] == "write"]
        check(writes == [(p, github_identity._plan_with_selection(repair.document(p), "", ""))
                         for p in (repair.root_plan, repair.tenant_plan)], f"both cleared, root's first: {writes}")
        removal = "set -eu\n" + "\n".join(owner_github_block_removal_commands(OWNER, str(home)))
        check(shell.calls[1] == (["sh", "-c", removal], dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)),
              f"the managed block is removed by the owner-run commands: {shell.calls[1:]}")
        check(code == 0 and said[-1] == f"switchyard: removed Switchyard's GitHub block from {home}/.ssh/config; nothing "
                                        "else in it changed", f"what it says: {said}")
        repair = Repair(home, documents={Repair(home).root_plan: recorded, Repair(home).tenant_plan: recorded},
                        fail_write=str(Repair(home).tenant_plan))
        shell = Shell()
        code, said = clear_identity(repair, shell, local)
        check(code == 1 and [(e[1], e[2]) for e in repair.log if e[0] == "write"][-1] == (repair.root_plan, b"RAW root")
              and len(shell.calls) == 1 and said[-2:] == [
                  f"switchyard: {repair.root_plan} was put back as it was",
                  f"switchyard: {PROJECT}'s GitHub identity was not cleared, and neither plan was left disagreeing with "
                  "the other."], f"a failed second write puts the first back and removes nothing: {said}")
        repair = Repair(home, documents={Repair(home).root_plan: recorded, Repair(home).tenant_plan: recorded})
        code, said = clear_identity(repair, Shell(sudo=0, sh=4), local)
        check(code == 1 and said[-1] == (
            f"switchyard: both plans are cleared, but Switchyard's GitHub block could not be removed from "
            f"{home}/.ssh/config (exit 4): syrd345: stderr. It is inert for a local remote; run this again to remove it."),
              f"a failed removal says the plans are done and how to finish: {said}")
        repair = Repair(home)
        code, said = clear_identity(repair, Shell(sudo=1), local)
        check(code == 0 and "write" not in repair.kinds() and said[-1] == (
            f"switchyard: {PROJECT} records no GitHub identity; nothing to clear"), f"nothing recorded: {said}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_the_codes_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"github_identity_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
