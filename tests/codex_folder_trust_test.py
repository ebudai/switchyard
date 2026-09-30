#!/usr/bin/env python3
"""A Codex worker parked at "Trust this folder?" is not ready, and preparation asks.

Live MEFP, 2026-09-26: after the supported `prepare-role` for luna-1 and
luna-2, `worker-pool start` succeeded and `list` called both ready and running
-- while `directorctl capture` showed each one stopped at Codex's

    Folder access ... Trust this folder?  (Trust and continue / Quit)

Codex was not among the CLIs the owner-run trust step covered, and
`_workdir_is_trusted` answered True for any CLI it did not know, so neither
preparation nor readiness ever asked Codex anything (SYRD-279).

Codex's rule is not Claude's. Measured against Codex 0.156.1, a linked worktree
of a bare repository -- every Switchyard role worktree -- is trusted only by an
entry for the worktree itself, and a worktree of an ordinary checkout also by
the checkout's root, never by a `.git` directory. The last case here checks the
predicate against the installed Codex's own startup, when there is one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

import contained_cli  # noqa: E402
import worker_pool_lifecycle_test as lifecycle  # noqa: E402

PROJECT = lifecycle.PROJECT
#: The implementer pool, on Codex: its template carries an onboarding prompt, so
#: trust is the only thing that can stand between these workers and ready.
POOL = {**lifecycle.POOL, "runtime": "codex", "size": 2}
WORKERS = ["impl-1", "impl-2"]
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


def trust(home: Path, *paths: Path | str, level: str = "trusted") -> None:
    """Record trust exactly as Codex does after "Trust and continue"."""
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open("a", encoding="utf-8") as handle:
        for path in paths:
            handle.write(f'[projects."{path}"]\ntrust_level = "{level}"\n\n')


class Shapes:
    """A bare control repository with a role worktree, and an ordinary checkout with one."""

    def __init__(self, tmp: Path) -> None:
        seed = tmp / "seed"
        git("init", "-q", "-b", "main", str(seed))
        git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", "seed")
        self.bare = tmp / "state" / "projects" / PROJECT / "control.git"
        self.bare.parent.mkdir(parents=True)
        git("clone", "-q", "--bare", str(seed), str(self.bare))
        self.bare_worktree = tmp / "worktrees" / "impl-1"
        self.bare_worktree.parent.mkdir(parents=True, exist_ok=True)
        git("--git-dir", str(self.bare), "worktree", "add", "-q", str(self.bare_worktree), "-b", "impl-1")
        self.checkout = tmp / "checkout"
        git("clone", "-q", str(seed), str(self.checkout))
        self.checkout_worktree = tmp / "checkout-wt"
        git("-C", str(self.checkout), "worktree", "add", "-q", str(self.checkout_worktree), "-b", "wt")


def _sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd279.") as tmp:
            case(Path(tmp))
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


# --------------------------------------------------------------------------
# The predicate: what Codex itself honours
# --------------------------------------------------------------------------

#: (label, entries relative to the fixture, expected) -- each measured on 0.156.1.
CASES = (
    ("nothing recorded", lambda s: [], {"bare": False, "checkout": False}),
    ("the worktree itself", lambda s: [s.bare_worktree, s.checkout_worktree], {"bare": True, "checkout": True}),
    ("the checkout's root", lambda s: [s.checkout], {"bare": False, "checkout": True}),
    ("a .git directory", lambda s: [s.checkout / ".git"], {"bare": False, "checkout": False}),
    ("the bare repository", lambda s: [s.bare], {"bare": False, "checkout": False}),
    ("the bare repository's parent", lambda s: [s.bare.parent], {"bare": False, "checkout": False}),
)


@_sandbox
def test_the_predicate_honours_exactly_what_codex_honours(tmp: Path) -> None:
    shapes = Shapes(tmp)
    for index, (label, entries, expected) in enumerate(CASES):
        home = tmp / f"home-{index}"
        home.mkdir()
        if entries(shapes):
            trust(home, *entries(shapes))
        got = {
            "bare": team_launcher._workdir_is_trusted("codex", owner_home=home, workdir=shapes.bare_worktree),
            "checkout": team_launcher._workdir_is_trusted("codex", owner_home=home, workdir=shapes.checkout_worktree),
        }
        check(got == expected, (label, got, expected))


@_sandbox
def test_an_untrusted_answer_or_an_unreadable_config_is_not_trust(tmp: Path) -> None:
    shapes = Shapes(tmp)
    home = tmp / "home"
    trust(home, shapes.bare_worktree, level="untrusted")
    check(not team_launcher._workdir_is_trusted("codex", owner_home=home, workdir=shapes.bare_worktree), "untrusted")
    (home / ".codex" / "config.toml").write_text("[projects\nnot toml", encoding="utf-8")
    check(not team_launcher._workdir_is_trusted("codex", owner_home=home, workdir=shapes.bare_worktree), "malformed")
    check(not team_launcher._workdir_is_trusted("codex", owner_home=tmp / "nobody", workdir=shapes.bare_worktree),
          "no config at all")


def test_claude_and_agy_keep_their_own_rules() -> None:
    check("codex" in team_launcher.FIRST_RUN_TRUST_CLIS, team_launcher.FIRST_RUN_TRUST_CLIS)
    check({"claude", "agy"} <= team_launcher.FIRST_RUN_TRUST_CLIS, team_launcher.FIRST_RUN_TRUST_CLIS)


# --------------------------------------------------------------------------
# Preparation asks, through the existing owner-run trust step
# --------------------------------------------------------------------------


class Probes:
    """Answers the read-only probes readiness and the manifest make; records pane starts."""

    def __init__(self, running: set[str] = frozenset()) -> None:
        self.running = set(running)
        self.started: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        command = [str(part) for part in args]
        if "has-session" in command:
            session = command[command.index("-t") + 1].lstrip("=") if "-t" in command else ""
            return subprocess.CompletedProcess(command, 0 if session in self.running else 1)
        if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/codex\n")
        if "login" in command and "status" in command:
            return subprocess.CompletedProcess(command, 0, stdout="Logged in using ChatGPT\n")
        if "display-message" in command:
            # A running session shows its runtime; with no pane pid to walk, the
            # launcher's live-worker check reads the pane's current command.
            session = command[command.index("-t") + 1].lstrip("=").split(":", 1)[0]
            shows = command[-1] == "#{pane_current_command}" and session in self.running
            return subprocess.CompletedProcess(command, 0 if shows else 1, stdout="codex\n" if shows else "")
        self.started.append(command)
        if "pane" in command:
            self.running.add(f"{PROJECT}-{command[command.index('pane') + 2]}")
        return subprocess.CompletedProcess(command, 0)


class Tenant:
    def __init__(self, tmp: Path, *, running: set[str] = frozenset()) -> None:
        self.tmp = tmp
        self.config, self.config_path = lifecycle.config_with_pool(tmp, pool=POOL, workers=WORKERS)
        self.pool = lifecycle.pool_of(POOL)
        self.document, _ = worker_pool.expand_pool(
            lifecycle.base_document(), self.pool, project=PROJECT, worktree_base=tmp / "worktrees"
        )
        self.home = tmp / "home"
        board_skill.install_board_skill(home=self.home, source=board_skill.default_source(ROOT),
                                        source_commit="syrd279")
        # Prepared worktrees, as prepare-role leaves them: linked to control.git.
        seed = tmp / "seed"
        git("init", "-q", "-b", "main", str(seed))
        git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", "seed")
        bare = tmp / "control.git"
        git("clone", "-q", "--bare", str(seed), str(bare))
        for member in WORKERS:
            git("--git-dir", str(bare), "worktree", "add", "-q", str(self.worktree(member)), "-b", member)
        self.probes = Probes(running)

    def worktree(self, member: str) -> Path:
        return self.tmp / "worktrees" / member

    def readiness(self) -> dict:
        return {
            state.role: state
            for state in worker_pool.worker_readiness(
                self.config, self.pool, document=self.document, owner_home=self.home,
                members=WORKERS, runner=self.probes,
            )
        }

    def command(self, action: str, member: str = "") -> tuple[int, str]:
        said: list[str] = []
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda _owner: self.home
        try:
            code = team_launcher.switchyard_worker_pool_command(
                PROJECT, action=action, member=member, config_dir=self.tmp,
                registry_dir=self.tmp / "registry",
                board_reader=lambda _config: {"document": self.document},
                board_snapshot_reader=lambda _config: None,
                runner=self.probes, print_func=said.append,
            )
        finally:
            team_launcher._owner_home_for_auth = original
        return code, "\n".join(said)


@_sandbox
def test_preparation_schedules_codex_trust_for_detached_workers(tmp: Path) -> None:
    tenant = Tenant(tmp)
    manifest = team_launcher.build_first_run_setup_manifest(
        tenant.config, owner_user=team_launcher.current_user_name(), owner_home=tenant.home,
        runner=tenant.probes,
    )
    steps = {step.role: step for step in manifest.folder_trust_steps if step.cli == "codex"}
    check(set(WORKERS) <= set(steps), f"a trust step for each untrusted codex worker: {manifest.folder_trust_steps}")
    for member, step in steps.items():
        check(step.workdir == tenant.worktree(member) and step.command[:1] == ("codex",), step)

    trust(tenant.home, *(tenant.worktree(member) for member in WORKERS))
    manifest = team_launcher.build_first_run_setup_manifest(
        tenant.config, owner_user=team_launcher.current_user_name(), owner_home=tenant.home,
        runner=tenant.probes,
    )
    left = {s.role for s in manifest.folder_trust_steps if s.cli == "codex"}
    check(not (left & set(WORKERS)), f"nothing left to ask of the workers once Codex recorded trust: {left}")


# --------------------------------------------------------------------------
# Readiness: a live process is not a ready worker
# --------------------------------------------------------------------------


@_sandbox
def test_a_running_worker_at_the_trust_prompt_is_not_ready(tmp: Path) -> None:
    """The live report: running, and parked at the provider's prompt."""
    tenant = Tenant(tmp, running={f"{PROJECT}-{member}" for member in WORKERS})
    states = tenant.readiness()
    for member in WORKERS:
        state = states[member]
        check(state.session and not state.ready, state.describe())
        check(worker_pool.WORKTREE_UNTRUSTED in state.blockers, state.describe())
        check("not ready, running" in state.describe(), state.describe())
    code, said = tenant.command("list")
    check("0 ready, 2 running" in said, said)


@_sandbox
def test_start_and_preflight_name_the_preparation_that_asks(tmp: Path) -> None:
    tenant = Tenant(tmp)
    code, said = tenant.command("start", "impl-1")
    check(code == 1 and "impl-1 not started" in said, (code, said))
    check("folder-trust prompt" in said and "prepare-role" in said and "--role impl-1" in said, said)
    check(tenant.probes.started == [], tenant.probes.started)
    code, said = tenant.command("preflight")
    line = next((line for line in said.splitlines() if "trust:" in line or " trust " in line), "")
    check(code == 1 and "codex has not trusted the worktree of impl-1, impl-2" in said, said)
    check(said.count("prepare-role") >= 2, said)


@_sandbox
def test_trust_completed_through_the_provider_then_the_worker_is_ready(tmp: Path) -> None:
    tenant = Tenant(tmp)
    check(not tenant.readiness()["impl-1"].ready, "untrusted before")
    # What "Trust and continue" leaves behind -- and what the trust step's
    # completion check (`_workdir_is_trusted`) waits for.
    trust(tenant.home, tenant.worktree("impl-1"))
    state = tenant.readiness()["impl-1"]
    check(state.ready, state.describe())
    code, said = tenant.command("start", "impl-1")
    check(code == 0 and "impl-1 started" in said, (code, said))
    code, said = tenant.command("preflight")
    check("impl-2" in said and "not trusted the worktree of impl-1," not in said, said)


@_sandbox
def test_an_already_ready_worker_is_unchanged(tmp: Path) -> None:
    tenant = Tenant(tmp, running={f"{PROJECT}-impl-1"})
    trust(tenant.home, *(tenant.worktree(member) for member in WORKERS))
    state = tenant.readiness()["impl-1"]
    check(state.ready and state.session and "ready, running" in state.describe(), state.describe())
    code, said = tenant.command("start", "impl-1")
    check(code == 0 and "already running" in said and tenant.probes.started == [], (code, said))
    code, said = tenant.command("preflight")
    check("has not trusted" not in said, said)


# --------------------------------------------------------------------------
# The installed Codex agrees, when there is one
# --------------------------------------------------------------------------


#: How long the header must stand with no trust question under it before
#: "Codex opened without asking" is an answer.
HEADER_SETTLE_SECONDS = 5.0
#: How long one real Codex observation may take, sign-in included; every
#: process it starts is stopped and reaped within its cleanup budget after.
OBSERVE_SECONDS = 150


def _codex_env(home: Path, codex_home: Path, tmux_dir: Path) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(codex_home),
            "TERM": "xterm-256color", "TMUX_TMPDIR": str(tmux_dir), "LANG": "C.UTF-8"}


def _observe(spec: dict) -> dict:
    """One real Codex observation. Runs as a contained child: see `_contained_codex`.

    Codex daemonises an app-server and clones plugins into its home, and both
    outlive the tmux session that started it. Nothing here tries to catch them:
    the parent's containment stops and reaps every descendant of this process
    before the home is removed (SYRD-524).
    """
    env = spec["env"]
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    # A placeholder key gets it past sign-in; nothing is ever sent with it. Its
    # output goes to /dev/null, never a pipe a daemon could hold open.
    subprocess.run(["codex", "login", "--with-api-key"], input="sk-placeholder-never-sent\n", env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, timeout=60)
    name = spec["name"]
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "160", "-y", "40", "-c", spec["cwd"], "codex"],
                   env=env, check=True)
    socket = subprocess.run(["tmux", "display-message", "-p", "#{socket_path}"], env=env,
                            capture_output=True, text=True).stdout.strip()
    assert socket.startswith(env["TMUX_TMPDIR"]), f"reached a live tmux server: {socket}"
    screen, result, header_at = "", {}, None
    try:
        for _ in range(40):
            time.sleep(0.5)
            screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", name], env=env,
                                    capture_output=True, text=True).stdout
            if "Trust this folder" in screen:
                break
            # Codex 0.159 draws its header half a second before the question
            # (measured, SYRD-524): "it opened" is only an answer once the
            # header has stood for a while with no question under it.
            if "OpenAI Codex (v" in screen and spec["mode"] == "prompt":
                header_at = header_at if header_at is not None else time.monotonic()
                if time.monotonic() - header_at >= HEADER_SETTLE_SECONDS:
                    break
        if "Sign in with ChatGPT" in screen or "Trust this folder" not in screen and "OpenAI Codex (v" not in screen:
            return {"observed": False}  # never reached the question; this observation proves nothing
        result = {"observed": True, "prompts": "Trust this folder" in screen}
        if spec["mode"] == "answer" and result["prompts"]:
            subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], env=env)
            trusted = False
            for _ in range(20):
                time.sleep(0.5)
                trusted = team_launcher._workdir_is_trusted("codex", owner_home=Path(spec["owner"]),
                                                            workdir=Path(spec["cwd"]))
                if trusted:
                    break
            result["trusted"] = trusted
        return result
    finally:
        # A courtesy. The containment is what guarantees nothing is left.
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)


def _contained_codex(home: "contained_cli.ContainedHome", spec: dict, *, label: str) -> dict:
    """Run one observation contained in `home`, and return what it saw.

    A run that did not finish -- a timeout, a crash -- or whose descendants
    could not all be stopped is a failure, never "not observed".
    """
    outcome = home.run([sys.executable, str(Path(__file__).resolve()), "--observe", json.dumps(spec)],
                       env=spec["env"], timeout=OBSERVE_SECONDS, label=label)
    check(outcome.status == "completed" and outcome.returncode == 0,
          f"{label}: the observation did not finish: {outcome.reason or outcome.stderr[-800:]}")
    check(outcome.clean, f"{label}: {outcome.reason}")
    answers = [line for line in outcome.stdout.splitlines() if line.startswith("{")]
    check(answers, f"{label}: the observation reported nothing: {outcome.stdout[-400:]}")
    return json.loads(answers[-1])


def test_an_observation_that_did_not_finish_cleanly_is_a_failure_not_a_skip() -> None:
    """No real Codex: what `_contained_codex` makes of a run that timed out, crashed or left something."""
    import bounded_run

    def outcome(**overrides) -> bounded_run.Outcome:
        fields = dict(label="x", status="completed", returncode=0, seconds=1.0, timeout=OBSERVE_SECONDS,
                      stdout='{"observed": true, "prompts": true}\n', stderr="")
        fields.update(overrides)
        return bounded_run.Outcome(**fields)

    class Home:
        def __init__(self, result):
            self.result = result

        def run(self, argv, **_kwargs):
            return self.result

    spec = {"env": {}}
    check(_contained_codex(Home(outcome()), spec, label="fine") == {"observed": True, "prompts": True}, "a clean run")
    for label, bad in (("timeout", outcome(status="timeout", returncode=None, reason="no result within 150s")),
                       ("crash", outcome(returncode=1, stdout="", stderr="Traceback ...")),
                       ("unclean", outcome(unreaped=(4242,), reason="still present after cleanup: unreaped=[4242]"))):
        try:
            _contained_codex(Home(bad), spec, label=label)
            failed = False
        except AssertionError:
            failed = True
        check(failed, f"a {label} observation was accepted")


def _real_codex_available() -> bool:
    return shutil.which("codex") is not None and shutil.which("tmux") is not None


def test_the_installed_codex_agrees_with_the_predicate() -> None:
    if not _real_codex_available():
        return
    with contained_cli.ContainedHome("syrd279.") as home:
        tmp = home.root
        shapes = Shapes(tmp)
        observed = 0
        for index, (label, entries, _expected) in enumerate(CASES):
            # One file, read by both: the owner home the predicate is given, whose
            # .codex is the CODEX_HOME the real Codex starts with.
            owner_home = tmp / f"owner-{index}"
            codex_home = owner_home / ".codex"
            codex_home.mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                "".join(f'[projects."{path}"]\ntrust_level = "trusted"\n\n' for path in entries(shapes)),
                encoding="utf-8",
            )
            for shape, cwd in (("bare", shapes.bare_worktree), ("checkout", shapes.checkout_worktree)):
                predicted = team_launcher._workdir_is_trusted("codex", owner_home=owner_home, workdir=cwd)
                seen = _contained_codex(home, {
                    "mode": "prompt", "name": f"c{index}{shape}", "cwd": str(cwd), "owner": str(owner_home),
                    "env": _codex_env(tmp / f"h-{index}-{shape}", codex_home, tmp),
                }, label=f"{label}/{shape}")
                if not seen["observed"]:
                    continue
                observed += 1
                check(seen["prompts"] == (not predicted),
                      (label, shape, "codex prompts" if seen["prompts"] else "codex opens",
                       "predicted trusted" if predicted else "predicted untrusted"))
        check(observed >= 2, f"the real Codex was never observed past sign-in ({observed})")


def test_answering_the_real_prompt_is_what_completes_the_trust_step() -> None:
    """The step's completion check reads what Codex writes when the owner answers.

    The trust step waits on `_workdir_is_trusted`; this answers the installed
    Codex's own prompt with its default ("Trust and continue") and asserts the
    predicate turns true -- for Switchyard's bare-repository worktree, where
    Codex records the worktree, and an ordinary checkout, where it records the
    checkout's root.
    """
    if not _real_codex_available():
        return
    with contained_cli.ContainedHome("syrd279.") as home:
        tmp = home.root
        shapes = Shapes(tmp)
        answered = 0
        for shape, cwd in (("bare", shapes.bare_worktree), ("checkout", shapes.checkout_worktree)):
            owner = tmp / f"answer-{shape}"
            codex_home = owner / ".codex"
            codex_home.mkdir(parents=True)
            check(not team_launcher._workdir_is_trusted("codex", owner_home=owner, workdir=cwd), (shape, "before"))
            seen = _contained_codex(home, {
                "mode": "answer", "name": f"answer-{shape}", "cwd": str(cwd), "owner": str(owner),
                "env": _codex_env(owner, codex_home, tmp),
            }, label=f"answer/{shape}")
            if not seen.get("prompts"):
                continue  # never reached the question; nothing was observed
            check(seen.get("trusted"), (shape, "the owner's answer did not satisfy the completion check"))
            answered += 1
        check(answered == 2, f"the real prompt was answered in both shapes ({answered})")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"codex_folder_trust_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--observe"]:
        print(json.dumps(_observe(json.loads(sys.argv[2]))), flush=True)
        raise SystemExit(0)
    raise SystemExit(main())
