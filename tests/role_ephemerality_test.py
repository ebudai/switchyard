#!/usr/bin/env python3
"""SYRD-689: a persistent role resumes its conversation however its generated config was marked.

On MEFP's start on 2026-10-27 the Director, which its workflow declares a persistent `system` role, did not
resume its recorded Codex conversation: its generated launcher entry carried `fresh_session_per_ticket`, so the
launch asked `/api/context-recovery/director`, was told `fresh` -- "director holds no active ticket", the
answer for an ephemeral implementer between tickets -- and set the conversation aside. `project_roles` copied
every field of an existing entry forward and reconciled only `ephemeral` with the workflow.

The workflow here is the shipped example document, projected by the real `project_roles`; its `main` is made
an ephemeral implementer, and its Director is persistent as declared. The launches are the real
`_start_role_session` and `attach_role_to_slot`, with tmux stood in for and the board's context-recovery read
answered through its documented seam (`role_context_recovery.board_get`) exactly as MEFP's board answered. The
provider's own store holds each conversation, as a resumable one does (SYRD-638). Every home is a disposable
one. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import getpass
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "df9516244a9ab56283d76a0f1a409f9c09f377bb"  # main before SYRD-689
CHECKS = 0
PROJECT = "cerulean"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(detail)


def document() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text(encoding="utf-8"))
    for spec in cfg["roles"]:
        if spec["name"] == "main":
            spec.update(ephemeral=True, runtime="claude")  # a genuinely ephemeral implementer, on the Director's CLI
    return cfg


class Kit:
    """A projected tenant on a disposable home: its config, session store and provider store."""

    def __init__(self, tmp: Path) -> None:
        from scripts import team_launcher as launcher
        from scripts.workflow_launcher import project_roles

        self.launcher, self.home = launcher, tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)  # never the live home (SYRD-627)
        raw = {"project": PROJECT, "layout": "layout.json", "repository": str(tmp / "project"),
               "worktree_base": str(tmp / "worktrees"), "run_as_user": getpass.getuser(),
               "board_url": "http://127.0.0.1:9", "session_dir": str(self.home / "pane-sessions"),
               "desktop_access": {"mode": "headless"},
               # MEFP's Director: the flag in its existing entry, carried forward by every projection before this.
               "roles": [{"role": "director", "cli": ["claude"], "fresh_session_per_ticket": True}]}
        (tmp / "project").mkdir()
        self.document = document()
        self.projected = project_roles(raw, self.document)
        visible = sum(1 for r in self.projected["roles"] if not r.get("detached"))
        (tmp / "layout.json").write_text(json.dumps(launcher._new_project_layout_payload(visible)), encoding="utf-8")
        self.config_path = tmp / f"{PROJECT}.json"
        self.asked: list[str] = []
        self.slots: dict[str, int] = {}
        self.answers: dict[str, dict] = {}
        launcher.live_command_matches_role = lambda role, runner=None: True
        launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
        launcher.RESUME_STARTUP_POLL_SECONDS = 0.05
        from scripts import role_context_recovery

        role_context_recovery.board_get = self.board_get

    def board_get(self, url: str) -> dict:
        role = url.rsplit("/", 1)[-1]
        self.asked.append(role)
        return self.answers.get(role) or {"role": role, "action": "fresh",
                                          "reason": f"{role} holds no active ticket", "ticket": None}

    def write(self, payload: dict) -> None:
        self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def stale(self, *, detached: str = "") -> dict:
        """The projection as MEFP's file held it: the Director marked fresh-per-ticket though declared persistent.
        With `detached`, that role is out of its slot, so attach-role can bring it back."""
        payload = copy.deepcopy(self.projected)
        next(r for r in payload["roles"] if r["role"] == "director")["fresh_session_per_ticket"] = True
        for entry in payload["roles"]:
            if entry["role"] == detached:
                self.slots[detached] = entry.pop("slot")
                entry["detached"] = True
        return payload

    def config(self):
        return self.launcher.load_project_config(PROJECT, self.config_path)

    def role(self, name: str):
        return next(r for r in self.config().roles if r.role == name)

    def converse(self, role, session_id: str) -> None:
        from provider_store_fixture import write_session

        Path(role.workdir).mkdir(parents=True, exist_ok=True)
        path = self.launcher._claude_project_dir_for_workdir(role.workdir, home=self.home) / f"{session_id}.jsonl"
        write_session(path, "claude", session_id, role.workdir)

    def session_dir(self, role) -> Path:
        path = self.launcher.role_session_dir(self.config(), role)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record(self, role, session_id: str) -> Path:
        path = self.session_dir(role) / self.launcher.session_file_name(role.target)
        path.write_text(json.dumps({"target": role.target, "session_id": session_id, "source": "test",
                                    "updated_at": time.time()}) + "\n", encoding="utf-8")
        return path

    def recorded(self, role) -> str:
        return self.launcher.session_id_for_role(role, self.session_dir(role))

    def tree(self, role) -> dict[str, str]:
        return {p.name: p.read_text() for p in sorted(self.session_dir(role).iterdir()) if p.is_file()
                and not p.name.endswith(".resume.log")}

    def start(self, role) -> dict:
        calls: list[list[str]] = []

        def tmux(args, **_kwargs):
            args = [str(a) for a in args]
            calls.append(args)
            if args[:1] != ["tmux"]:
                raise AssertionError(f"refused: {args}")
            return subprocess.CompletedProcess(args, 1 if args[1] == "has-session" else 0, "", "")

        err = io.StringIO()
        mark = len(self.asked)
        with contextlib.redirect_stderr(err):
            code = self.launcher._start_role_session(role, session_dir=self.session_dir(role),
                                                     pane_state_dir=self.home / "pane-state", prefer_resume=True,
                                                     seed_source="syrd689.start", runner=tmux)
        started = [call[-1] for call in calls if call[1:2] == ["new-session"]]
        return {"code": code, "started": started[0] if started else "", "said": err.getvalue(),
                "asked": self.asked[mark:]}

    def attach(self, name: str) -> dict:
        calls: list[list[str]] = []

        def tmux(args, **_kwargs):
            args = [str(a) for a in args]
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[1:2] == ["has-session"] else 0, "", "")

        role = self.role(name)
        err = io.StringIO()
        mark = len(self.asked)
        with contextlib.redirect_stderr(err):
            code = self.launcher.attach_role_to_slot(self.config(), config_path=self.config_path, role_name=name,
                                                     slot=self.slots[name], session_dir=self.session_dir(role),
                                                     pane_state_dir=self.home / "pane-state", runner=tmux,
                                                     print_func=lambda *_: None)
        started = [call[-1] for call in calls if call[1:2] == ["new-session"]]
        return {"code": code, "started": started[0] if started else "", "said": err.getvalue(),
                "asked": self.asked[mark:]}


@contextlib.contextmanager
def kit():
    saved_home = os.environ.get("HOME")
    from scripts import role_context_recovery
    from scripts import team_launcher as launcher

    saved = (role_context_recovery.board_get, launcher.live_command_matches_role,
             launcher.RESUME_STARTUP_TIMEOUT_SECONDS, launcher.RESUME_STARTUP_POLL_SECONDS)
    with tempfile.TemporaryDirectory(prefix="syrd689.") as raw:
        try:
            yield Kit(Path(raw))
        finally:
            (role_context_recovery.board_get, launcher.live_command_matches_role,
             launcher.RESUME_STARTUP_TIMEOUT_SECONDS, launcher.RESUME_STARTUP_POLL_SECONDS) = saved
            if saved_home is not None:
                os.environ["HOME"] = saved_home


# ------------------------------------------------------------------ the projection


def test_the_projection_writes_the_flag_from_the_declared_role() -> None:
    from scripts.workflow_launcher import project_roles

    with kit() as k:
        roles = {r["role"]: r for r in k.projected["roles"]}
        check("fresh_session_per_ticket" not in roles["director"] and "ephemeral" not in roles["director"],
              f"the persistent Director sheds the flag its existing entry carried: {roles['director']}")
        check(roles["main"].get("fresh_session_per_ticket") is True and roles["main"].get("ephemeral") is True,
              f"the ephemeral implementer gets both, from the document alone: {roles['main']}")
        check(all("fresh_session_per_ticket" not in r for name, r in roles.items() if name != "main"),
              f"and no other role carries it: {sorted(n for n, r in roles.items() if 'fresh_session_per_ticket' in r)}")
        check(project_roles(k.projected, k.document) == k.projected, "projecting again changes nothing")
        stale = k.stale()
        check(project_roles(stale, k.document) == k.projected, "a stale projection is rewritten exactly as declared")
        # A role declared from another's template inherits its vendor arguments, never its ticket policy.
        cloned = copy.deepcopy(k.document)
        for spec in cloned["roles"]:
            if spec["name"] == "ops":
                spec["template_role"] = "main"
        seeded = copy.deepcopy(k.projected)
        seeded["roles"] = [r for r in seeded["roles"] if r["role"] != "ops"]
        ops = next(r for r in project_roles(seeded, cloned)["roles"] if r["role"] == "ops")
        check("fresh_session_per_ticket" not in ops and "ephemeral" not in ops,
              f"a persistent role templated on an ephemeral one is persistent: {ops}")
        undeclared = copy.deepcopy(k.document)
        next(s for s in undeclared["roles"] if s["name"] == "main").pop("ephemeral")
        main = next(r for r in project_roles(k.projected, undeclared)["roles"] if r["role"] == "main")
        check("fresh_session_per_ticket" not in main and "ephemeral" not in main,
              f"turning ephemerality off in the document turns the launcher's fresh start off too: {main}")


def test_a_stale_projection_is_loaded_reported_and_repaired_as_declared() -> None:
    from scripts import role_ephemerality
    from scripts import team_launcher as launcher
    from scripts import workflow_manage

    with kit() as k:
        k.write(k.stale())
        director, main = k.role("director"), k.role("main")
        check(not director.fresh_session_per_ticket and not director.ephemeral
              and main.fresh_session_per_ticket and main.ephemeral,
              f"loaded as declared, whatever the file says: {director.fresh_session_per_ticket} {main.fresh_session_per_ticket}")
        check(not launcher._uses_fresh_session_per_ticket(director), "so no launch path treats the Director as fresh")
        lines = role_ephemerality.mismatch_lines(PROJECT, json.loads(k.config_path.read_text()))
        check(lines == [
            "switchyard: cerulean's generated config gives director fresh_session_per_ticket true, ephemeral false, but "
            "its workflow declares director persistent: it resumes its recorded conversation; it is launched as declared",
            "switchyard: to rewrite cerulean's projection from its workflow, its Director runs "
            "`switchyard finish-upgrade cerulean` from its own session; nothing is restarted"],
              f"the mismatch is said, with the repair: {lines}")
        # The repair: finish-upgrade's projection step, the real `_reconcile_projection`, from the Director's side.
        record = k.record(director, "director-current")
        before = k.tree(director)
        check(workflow_manage._reconcile_projection(k.config_path, k.document) is True,
              "the stale projection is rewritten")
        held = next(r for r in json.loads(k.config_path.read_text())["roles"] if r["role"] == "director")
        check("fresh_session_per_ticket" not in held and role_ephemerality.mismatch_lines(
            PROJECT, json.loads(k.config_path.read_text())) == [], f"and agrees with its workflow: {held}")
        check(k.tree(director) == before and json.loads(record.read_text())["session_id"] == "director-current",
              "the Director's recorded conversation is untouched by the repair")
        check(workflow_manage._reconcile_projection(k.config_path, k.document) is False, "run again, nothing to do")
        # Only `ephemeral` copied onto the Director is a mismatch too: the two cannot disagree with the workflow.
        marked = copy.deepcopy(k.projected)
        next(r for r in marked["roles"] if r["role"] == "director")["ephemeral"] = True
        lines = role_ephemerality.mismatch_lines(PROJECT, marked)
        check(len(lines) == 2 and "gives director fresh_session_per_ticket false, ephemeral true" in lines[0],
              f"an ephemeral flag on a persistent role is said as well: {lines}")
    with kit() as k:
        legacy = k.stale()
        legacy.pop("workflow")
        k.write(legacy)
        check(k.role("director").fresh_session_per_ticket and role_ephemerality.mismatch_lines(PROJECT, legacy) == [],
              "a config no workflow projected keeps the per-role opt-in it always was (PGU-917)")


def test_the_launch_and_status_say_it_before_anything_starts() -> None:
    from scripts import project_launch

    fn = next(n for n in ast.parse(Path(project_launch.__file__).read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "launch_project")
    body = [ast.unparse(s) for s in fn.body]
    said = next((i for i, s in enumerate(body) if "launcher.report_projection_mismatches(" in s), None)
    first_effect = next((i for i, s in enumerate(body) if "process_authority_board_compatibility" in s), None)
    check(said is not None and first_effect is not None and said < first_effect,
          f"the launch says it before its first check against the board: {said} < {first_effect}")
    from scripts import team_launcher as launcher

    with kit() as k:
        k.write(k.stale())
        said: list[str] = []
        launcher.report_projection_mismatches(k.config(), config_path=k.config_path, print_func=said.append)
        check(len(said) == 2 and "gives director fresh_session_per_ticket true" in said[0]
              and "`switchyard finish-upgrade cerulean`" in said[1], f"the launch's report, as it prints it: {said}")
        k.write(k.projected)
        said.clear()
        launcher.report_projection_mismatches(k.config(), config_path=k.config_path, print_func=said.append)
        check(said == [], f"and nothing for a projection that agrees with its workflow: {said}")
        k.write(k.stale())
        entries = [launcher.SwitchyardProjectEntry(slug=PROJECT, name=PROJECT, config_path=k.config_path)]
        out: list[str] = []
        quiet = lambda *a, **kw: subprocess.CompletedProcess([], 0, "", "")  # noqa: E731
        saved = launcher._switchyard_entries
        launcher._switchyard_entries = lambda **_kw: entries
        try:
            launcher.switchyard_status_command(registry_dir=k.home / "registry", config_dir=k.home / "no-config",
                                               runner=quiet, owner_tmux_reader=lambda _c: (set(), ""),
                                               assignments_reader=lambda _c: ({}, ""), print_func=out.append)
        finally:
            launcher._switchyard_entries = saved
        check(any("gives director fresh_session_per_ticket true" in line for line in out)
              and any("`switchyard finish-upgrade cerulean`" in line for line in out),
              f"switchyard status reports it too: {out}")


# ------------------------------------------------------------------ the launch


def test_a_persistent_director_resumes_its_conversation_whatever_the_file_says() -> None:
    with kit() as k:
        k.write(k.stale())
        director = k.role("director")
        k.converse(director, "director-current")
        record = k.record(director, "director-current")
        for label, detached in (("a start", ""), ("attach-role", "director")):
            k.write(k.stale(detached=detached))
            got = k.start(k.role("director")) if not detached else k.attach("director")
            check(got["code"] == 0 and "--resume director-current" in got["started"] and got["asked"] == [],
                  f"{label}: the exact recorded conversation resumed, and the board never asked: {got}")
            check(k.recorded(director) == "director-current" and record.is_file()
                  and not any(p.name.endswith(".superseded") for p in k.session_dir(director).iterdir()),
                  f"{label}: the record still names it and nothing was set aside: {k.tree(director)}")


def test_a_genuinely_ephemeral_implementer_still_recovers_by_the_board() -> None:
    with kit() as k:
        k.write(k.stale())
        main = k.role("main")
        k.converse(main, "main-previous")
        k.record(main, "main-previous")
        got = k.start(main)
        check(got["code"] == 0 and "--resume" not in got["started"] and got["asked"] == ["main"]
              and "main holds no active ticket" in got["said"],
              f"holding no ticket, the ephemeral implementer starts fresh, as the board says: {got}")
        check(k.recorded(main) != "main-previous", "and its previous conversation is set aside")
        k.converse(main, "main-ticket")
        k.answers["main"] = {"role": "main", "action": "resume", "session_id": "main-ticket", "ticket": "CER-7",
                             "reason": "CER-7 is main's active ticket, in conversation main-ticket", "runtime": "claude"}
        got = k.start(main)
        check(got["code"] == 0 and "--resume main-ticket" in got["started"] and got["asked"] == ["main"]
              and k.recorded(main) == "main-ticket",
              f"mid-ticket, SYRD-626's recovery resumes that ticket's conversation: {got}")


BEFORE_SCENARIO = r'''
import json, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
import role_ephemerality_test as t
with t.kit() as k:
    k.write(k.stale())
    director = k.role("director")
    k.converse(director, "director-current")
    k.record(director, "director-current")
    got = k.start(director)
    print(json.dumps({"fresh": director.fresh_session_per_ticket, "got": got, "recorded": k.recorded(director)}))
'''


def tree_at(commit: str, where: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(where, filter="data")
    return where


def before() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd689-main.") as raw:
        tree = tree_at(BEFORE, Path(raw) / "tree")
        # This suite's own kit, run against main's launcher and fixtures: main has no file of this name.
        (tree / "tests" / "role_ephemerality_test.py").write_text(Path(__file__).read_text(encoding="utf-8"),
                                                                  encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        done = subprocess.run([sys.executable, "-c", BEFORE_SCENARIO, str(tree)], capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=str(tree),
                              env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    check(done.returncode == 0, f"before: main's run finished: {done.stdout[-500:]} {done.stderr[-2000:]}")
    got = json.loads(done.stdout.strip().splitlines()[-1])
    check(got["fresh"] is True and got["got"]["asked"] == ["director"] and "--resume" not in got["got"]["started"]
          and "director holds no active ticket" in got["got"]["said"] and got["recorded"] != "director-current",
          f"before: the persistent Director asked the board, was told fresh, and lost its conversation: {got}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if os.environ.get("SYRD689_MUTATION_SKIP_BEFORE") != "1":
        before()
    print(f"role_ephemerality_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    raise SystemExit(main())
