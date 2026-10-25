#!/usr/bin/env python3
"""SYRD-674: a new tenant is connected for reports by default, or told at creation that it is not.

`pie` was created with no report link and nothing said so; its Director found out when `file-report`
refused, and the repair wanted a URL the host registry already knew. Every board mints a report token,
so which board takes the host's tenant reports is root's declaration, in that board's registry record.

Each board here is a real HTTP server on 127.0.0.1 answering the read every board serves
(`GET /api/runtime-assignments`) as the project it is registered as -- or as another, or not at all --
so the reachability check is the shipped one, never stood in. Every home is the sandbox's, set on
every `upstream_report` default (SYRD-238 once wrote test tokens into a live board's credential), and
no case reaches the host's registry. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9ebb314838cf894bfcf36975366a2aafa9f4bdc5"  # main before SYRD-674
TOKEN = "syrd674-report-token"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


class Board:
    """A board answering the runtime-assignments read as `answers`, on a port of its own."""

    def __init__(self, answers: str) -> None:
        body = json.dumps({"project": answers, "authority_mode": "process", "assignments": {}}).encode()
        self.requests = 0
        board = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server name
                board.requests += 1
                self.send_response(200 if self.path == "/api/runtime-assignments" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_url() -> str:
    """A port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def config_payload(tmp: Path, slug: str, owner: str, *, board_url: str = "") -> dict:
    (tmp / f"repo-{slug}").mkdir(exist_ok=True)
    layout = tmp / f"layout-{slug}.json"
    layout.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": [
        {"Command": "", "SessionRestoreId": i, "WorkingDirectory": ""} for i in range(2)]}), encoding="utf-8")
    payload = {
        "project": slug, "project_name": slug.upper(), "layout": str(layout), "repository": str(tmp / f"repo-{slug}"),
        "run_as_user": owner, "session_dir": str(tmp / f"state-{slug}"), "role_state_isolation": True,
        "desktop_access": {"mode": "headless"},
        "roles": [{"role": role, "slot": index, "cli": ["claude"], "live_commands": ["claude"],
                   "target": f"{slug}-{role}:0.0", "tmux_session": f"{slug}-{role}"}
                  for index, role in enumerate(("director", "main"))],
    }
    if board_url:
        payload["board_url"] = board_url
    return payload


class Host:
    """A registry of boards and one tenant, `mefp`, every home the sandbox's."""

    def __init__(self, tmp: Path) -> None:
        from scripts import team_launcher as launcher

        self.tmp, self.owner = tmp, launcher.current_user_name()
        self.home = tmp / "sandbox-home"
        self.home.mkdir()
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.boards: dict[str, Board] = {}
        # A tenant has a board of its own, which is a board another tenant could be offered.
        self.config_path = self.write_config("mefp", board_url=closed_url())
        self.register("mefp", self.config_path)
        self.credential = self.home / ".config" / "mefp" / "upstream-report.env"

    def resolver(self, _user: str) -> Path:
        return self.home

    def write_config(self, slug: str, *, board_url: str = "") -> Path:
        path = self.tmp / f"{slug}.json"
        path.write_text(json.dumps(config_payload(self.tmp, slug, self.owner, board_url=board_url), indent=2),
                        encoding="utf-8")
        return path

    def register(self, slug: str, config_path: Path, *, declared: bool = False) -> None:
        record = {"schema": "switchyard.project-registry.v1", "slug": slug, "name": slug.upper(),
                  "config_path": str(config_path)}
        if declared:
            record["tenant_reports"] = {"accepted": True, "recorded_at": "then", "recorded_by": "root"}
        (self.registry / f"{slug}.json").write_text(json.dumps(record), encoding="utf-8")

    def add_board(self, slug: str, *, declared: bool = False, token: bool = True, answers: str | None = None,
                  url: str | None = None) -> str:
        if url is None:
            if slug in self.boards:
                self.boards.pop(slug).close()
            board = Board(slug if answers is None else answers)
            self.boards[slug] = board
            url = board.url
        self.register(slug, self.write_config(slug, board_url=url), declared=declared)
        env = self.home / ".config" / slug / "ticket-board.env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text((f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}-{slug}\n" if token else "")
                       + "TICKET_BOARD_WRITE_TOKEN=never-copied\n", encoding="utf-8")
        return url

    def record(self, slug: str = "mefp") -> dict:
        record = json.loads((self.registry / f"{slug}.json").read_text())
        record.setdefault("upstream_report", {})  # unrecorded reads as no state, and fails the check that wants one
        return record

    def tree(self) -> dict[str, tuple]:
        seen = {}
        for path in sorted(self.tmp.rglob("*")):
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
            seen[str(path.relative_to(self.tmp))] = (stat.S_IMODE(info.st_mode), digest)
        return seen

    def close(self) -> None:
        for board in self.boards.values():
            board.close()


@contextlib.contextmanager
def host():
    """A Host, with every home-derived default in `upstream_report` the sandbox's and the registry its own."""
    from scripts import team_launcher as launcher
    from scripts import upstream_report

    with tempfile.TemporaryDirectory(prefix="syrd674.") as raw:
        h = Host(Path(raw))
        saved = {}
        for name in dir(upstream_report):
            fn = getattr(upstream_report, name)
            defaults = getattr(fn, "__kwdefaults__", None) or {}
            if callable(fn) and "home_for_user" in defaults and getattr(fn, "__module__", "") == upstream_report.__name__:
                saved[name] = defaults["home_for_user"]
                defaults["home_for_user"] = h.resolver
        check(len(saved) >= 6, f"fixture: every home default in upstream_report is the sandbox's: {sorted(saved)}")
        try:
            with patch.object(launcher, "switchyard_registry_dir", lambda: h.registry):
                yield h
        finally:
            for name, value in saved.items():
                getattr(upstream_report, name).__kwdefaults__["home_for_user"] = value
            h.close()


def said_by(function, *args, **kwargs) -> tuple[object, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = function(*args, **kwargs)
        except SystemExit as exc:
            result = SimpleNamespace(exit=str(exc.code))
    return result, out.getvalue()


def upgrade(*argv: str) -> tuple[object, str]:
    from scripts import team_launcher as launcher

    return said_by(launcher.switchyard_main, ["upgrade", *argv])


def no_token_anywhere(h: Host, *texts: str) -> None:
    for path in h.registry.iterdir():
        check(TOKEN not in path.read_text(), f"no report token in a registry record: {path.name}")
    for text in texts:
        check(TOKEN not in text, f"no report token in what was said: {text[:200]}")


# ------------------------------------------------------------------ discovery and the decision


def test_discovery_finds_the_one_declared_board() -> None:
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        h.add_board("syrd", declared=True)
        h.add_board("otto")
        said: list[str] = []
        choice = sinks.choose_report_board("pie", yes=True, registry_dir=h.registry, print_func=said.append)
        check(choice.state == sinks.CONNECT and choice.board == "syrd" and choice.url == h.boards["syrd"].url
              and choice.source == "the host's declared report board" and said == [],
              f"one declared board: the default, nothing asked even under --yes: {choice} {said}")
        summary = sinks.choice_summary("pie", choice, registry_dir=h.registry)
        check(f"pie files them to syrd at {h.boards['syrd'].url}" in summary and "before any pane starts" in summary,
              f"said before the confirmation: {summary}")
        h.register("mefp", h.config_path, declared=True)
        choice, said = said_by(sinks.choose_report_board, "mefp", yes=True, registry_dir=h.registry)
        check(getattr(choice, "board", "") == "syrd", f"a declared board is never its own default: {choice} {said}")


def test_no_single_default_is_never_guessed() -> None:
    from scripts import upstream_report_sinks as sinks

    cases = {
        "none declared": dict(syrd=dict(), otto=dict()),
        "two declared": dict(syrd=dict(declared=True), otto=dict(declared=True)),
        "declared without a token": dict(syrd=dict(declared=True, token=False), otto=dict()),
    }
    reasons = {"none declared": "no board on this host is declared to take tenant reports",
               "two declared": "2 boards are declared to take tenant reports (otto, syrd)",
               "declared without a token": "syrd is declared to take tenant reports, but"}
    for label, boards in cases.items():
        with host() as h:
            for slug, kwargs in boards.items():
                h.add_board(slug, **kwargs)
            before = h.tree()
            choice, said = said_by(sinks.choose_report_board, "pie", yes=True, registry_dir=h.registry)
            exit_text = getattr(choice, "exit", "")
            check(reasons[label] in exit_text and "syrd (SYRD)" in exit_text and "otto (OTTO)" in exit_text
                  and "--upstream-report-board <slug>" in exit_text and "--no-upstream-report" in exit_text
                  and "--only accept-reports" in exit_text and exit_text.endswith("Nothing was created."),
                  f"{label}: refused under --yes, naming why, the boards and the flags: {exit_text}")
            check(h.tree() == before, f"{label}: and nothing written: {said}")
            answers = iter(["9", "nobody", "otto"])
            prompts: list[str] = []
            said_lines: list[str] = []
            choice = sinks.choose_report_board("pie", registry_dir=h.registry, print_func=said_lines.append,
                                               input_func=lambda prompt: prompts.append(prompt) or next(answers))
            check(choice.state == sinks.CONNECT and choice.board == "otto" and choice.source == "the operator's answer"
                  and len(prompts) == 3 and prompts[0] == "Report board [1-3 or none]: ",
                  f"{label}: asked, re-asked on a wrong answer, and the operator's choice kept: {choice} {prompts}")
            check([line.split(". ")[1].split(" ")[0] for line in said_lines if line.startswith("switchyard:   ")]
                  == ["mefp", "otto", "syrd", "create"], f"{label}: every board offered, and none: {said_lines}")
            check(any("none. create pie" in line for line in said_lines),
                  f"{label}: every board offered, and none: {said_lines}")
            choice = sinks.choose_report_board("pie", registry_dir=h.registry, print_func=lambda _l: None,
                                               input_func=lambda _p: "none")
            check(choice.state == sinks.OPTED_OUT, f"{label}: none is an opt-out: {choice}")
    with host() as h:
        h.add_board("syrd", token=False)
        h.add_board("otto")
        answers = iter(["syrd", "2"])  # mefp, otto, syrd
        lines: list[str] = []
        choice = sinks.choose_report_board("pie", registry_dir=h.registry, print_func=lines.append,
                                           input_func=lambda _p: next(answers))
        check(choice.board == "otto" and any("syrd cannot take pie's reports" in line and
                                             "TICKET_BOARD_TENANT_REPORT_TOKEN" in line for line in lines),
              f"a chosen board with no report token is refused and asked again: {choice} {lines}")


def test_nothing_registered_is_unavailable_not_refused() -> None:
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        (h.registry / "mefp.json").unlink()
        choice = sinks.choose_report_board("pie", yes=True, registry_dir=h.registry)
        check(choice.state == sinks.UNAVAILABLE, f"no other board at all: unavailable, and creation goes on: {choice}")
        summary = sinks.choice_summary("pie", choice, registry_dir=h.registry)
        check(summary.startswith("switchyard: WARNING: pie will not be able to file Switchyard reports")
              and "`sudo switchyard upgrade pie --only upstream-report`" in summary,
              f"with the consequence and the repair: {summary}")


def test_an_explicit_choice_must_be_a_registered_board_not_its_own() -> None:
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        h.add_board("syrd", declared=True)
        url = h.add_board("otto")
        h.add_board("sapi", token=False)
        stray = closed_url()
        for kwargs, want in (
            (dict(upstream_report_board="otto"), ("connect", "otto", "--upstream-report-board")),
            (dict(upstream_report_url=url + "/"), ("connect", "otto", "--upstream-report-url")),
            (dict(no_upstream_report=True), ("opted-out", "", "--no-upstream-report")),
        ):
            choice = sinks.choose_report_board("pie", yes=True, registry_dir=h.registry, **kwargs)
            check((choice.state, choice.board, choice.source) == want, f"{kwargs}: {choice}")
        for kwargs, refusal in (
            (dict(upstream_report_board="nobody"), "--upstream-report-board nobody names no board registered"),
            (dict(upstream_report_url=stray), f"--upstream-report-url {stray} names no board registered"),
            (dict(upstream_report_board="pie"), "pie cannot file reports to its own board"),
            (dict(upstream_report_board="sapi"), "pie cannot file reports to sapi:"),
            (dict(upstream_report_board="otto", upstream_report_url=h.boards["syrd"].url), "names no board registered"),
            (dict(no_upstream_report=True, upstream_report_board="otto"), "cannot be combined"),
        ):
            got, _said = said_by(sinks.choose_report_board, "pie", yes=True, registry_dir=h.registry, **kwargs)
            check(refusal in getattr(got, "exit", "") and "Nothing was created." in got.exit,
                  f"{kwargs}: refused before anything exists: {got}")


def test_new_decides_before_its_confirmation() -> None:
    """The real P0, its launcher seams the phase guard's own fakes: the decision and its line come before the one
    confirmation, and a refusal comes before it too."""
    import new_project_phases_boundary_test as p0
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        h.add_board("syrd", declared=True)
        world = p0.World(h.tmp)
        said: list[str] = []
        values = p0.inputs(h.tmp, role_clis=(("director", "claude"), ("main", "codex")), registry_dir=h.registry,
                           print_func=said.append, yes=True)
        log = world.log
        printed_at: list[int] = []
        values["print_func"] = lambda line: (said.append(line), printed_at.append(len(log)))
        got = p0.run(world, values)
        check(got.upstream_report.state == sinks.CONNECT and got.upstream_report.board == "syrd",
              f"P0 hands on the decision: {got.upstream_report}")
        line = next((i for i, text in enumerate(said) if "Switchyard reports: syrd369 files them to syrd" in text), None)
        confirm = next((i for i, entry in enumerate(log) if entry[0] == "confirm"), None)
        check(line is not None and confirm is not None and printed_at[line] <= confirm,
              f"said before the confirmation: {said} {line} {confirm}")
    with host() as h:
        h.add_board("syrd")
        h.add_board("otto")
        world = p0.World(h.tmp)
        got = p0.run(world, p0.inputs(h.tmp, role_clis=(("director", "claude"), ("main", "codex")),
                                      registry_dir=h.registry, yes=True))
        check(isinstance(got, SystemExit) and "needs a decision about Switchyard reports" in str(got)
              and "confirm" not in world.names(), f"an undecided host refuses before the confirmation: {got}")


# ------------------------------------------------------------------ carried out before any pane


def new_until_sign_in(h: Host, choice) -> tuple[object, list[str], object]:
    """`switchyard_new_command`, P0 answering `choice`, P3 real but for provisioning, stopped at sign-in."""
    from scripts import board_ui_record
    from scripts import team_launcher as launcher

    reached: dict = {}
    said: list[str] = []
    fields = {f.name: None for f in __import__("dataclasses").fields(launcher.NewProjectChoices)}
    choices = SimpleNamespace(**{**fields, "resolved_slug": "mefp", "owner_user": h.owner,
                                 "stages": SimpleNamespace(begin=lambda _name: None), "upstream_report": choice})
    preflight = SimpleNamespace(effective_source_repo=None, precheck_plan=None, selected_role_clis=None,
                                selected_router=(), selected_documentation=(), worktree_branch=None)

    def sign_in(**kwargs):
        reached["config"] = kwargs["config"]
        return 7

    stand_ins = dict(_resolve_new_project_choices=lambda **_k: choices, _check_new_project_preflight=lambda **_k: preflight,
                     _prepare_new_project_accounts=lambda **_k: SimpleNamespace(provision_dir=h.config_path.parent),
                     new_project_command=lambda *_a, **_k: 0, _commit_project_git_changes=lambda **_k: None,
                     prepare_project_desktop=lambda config, **_k: config, _register_switchyard_project=lambda *_a, **_k: None,
                     _prepare_first_run_auth_worktrees=lambda *_a, **_k: None, _run_new_project_sign_in=sign_in)
    board = {}

    def capture(**kwargs):
        board["result"] = original(**kwargs)
        return board["result"]

    original = launcher._prepare_new_project_board
    with contextlib.ExitStack() as stack:
        for name, value in stand_ins.items():
            stack.enter_context(patch.object(launcher, name, value))
        stack.enter_context(patch.object(board_ui_record, "record_board_ui_users", lambda *_a, **_k: []))
        stack.enter_context(patch.object(launcher, "_prepare_new_project_board", capture))
        status = launcher.switchyard_new_command(registry_dir=h.registry, print_func=said.append)
    check(status == 7, f"it went on to sign-in: {status} {said}")
    return reached["config"], said, board["result"]


def test_the_default_is_connected_before_the_first_launch() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        url = h.add_board("syrd", declared=True)
        choice = sinks.choose_report_board("mefp", yes=True, registry_dir=h.registry)
        config, said, board = new_until_sign_in(h, choice)
        check(config.upstream_report_url == url and all(role.env.get("TICKET_BOARD_REPORT_URL") == url
                                                        and role.env.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE")
                                                        == str(h.credential) for role in config.roles),
              f"every pane sign-in and the first launch get carries the link: {[r.env.get('TICKET_BOARD_REPORT_URL') for r in config.roles]}")
        info = h.credential.lstat()
        check(h.credential.read_text() == f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}-syrd\n"
              and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(),
              f"the report-only credential, 0600 and the owner's, nothing else copied: {oct(info.st_mode)}")
        outcome = h.record()["upstream_report"]
        check({k: outcome.get(k) for k in ("state", "board", "url", "detail")} == {"state": "connected", "board": "syrd",
                                                                               "url": url, "detail": ""},
              f"root's registry record says so: {outcome}")
        check(h.boards["syrd"].requests == 1, f"the board was asked, once, whether it answers as itself")
        check(board.report_summary in said and "files them to syrd" in board.report_summary,
              f"and the creation summary carries the line: {board.report_summary}")
        no_token_anywhere(h, *said)
        loaded = launcher.load_project_config("mefp", h.config_path)
        check(loaded.roles[0].env.get("TICKET_BOARD_REPORT_URL") == url, "the configuration the panes launch from")


def test_a_board_that_does_not_answer_is_never_connected() -> None:
    from scripts import upstream_report_sinks as sinks

    for label, kwargs, why in (("unreachable", dict(url=closed_url()), "did not answer"),
                               ("another project", dict(answers="cerulean"), "is 'cerulean', not syrd's board")):
        with host() as h:
            url = h.add_board("syrd", declared=True, **kwargs)
            choice = sinks.ReportChoice(sinks.CONNECT, board="syrd", url=url, source="the host's declared report board")
            before_config = h.config_path.read_bytes()
            config, said, board = new_until_sign_in(h, choice)
            check(not config.upstream_report_url and h.config_path.read_bytes() == before_config
                  and not h.credential.exists(), f"{label}: no link and no credential written: {said}")
            outcome = h.record()["upstream_report"]
            check(outcome.get("state") == "failed" and outcome.get("board") == "syrd" and why in outcome.get("detail", ""),
                  f"{label}: recorded as failed, with why: {outcome}")
            check(board.report_summary.startswith("switchyard: WARNING: mefp will not be able to file Switchyard reports")
                  and "`sudo switchyard upgrade mefp --only upstream-report`" in board.report_summary,
                  f"{label}: the summary warns, with the repair, which needs no URL: {board.report_summary}")
            if label == "unreachable":
                held = h.tree()
                status, said = upgrade("mefp", "--only", "upstream-report")
                check(status == 1 and "did not answer" in said and h.tree() == held,
                      f"the repair against a board still down refuses, and writes nothing, its record included: {said}")
                # The board comes up; the printed repair runs as printed and connects it.
                live = Board("syrd")
                h.boards["live"] = live
                # No longer declared, so only the board recorded for mefp can resolve it.
                h.register("syrd", h.write_config("syrd", board_url=live.url))
                status, said = upgrade("mefp", "--only", "upstream-report")
                check(status == 0 and h.record()["upstream_report"].get("state") == "connected"
                      and json.loads(h.config_path.read_text())["upstream_report_url"] == live.url,
                      f"the repair, retried, connects to the recorded board: {said}")
                again = h.tree()
                status, said = upgrade("mefp", "--only", "upstream-report")
                check(status == 0 and h.tree() == again and "already holds" in said,
                      f"and run again writes nothing: {said}")


def test_an_opt_out_and_no_board_are_recorded_and_warned() -> None:
    from scripts import upstream_report_sinks as sinks

    for state, source, why in (("opted-out", "--no-upstream-report", "opted out with --no-upstream-report"),
                               ("unavailable", "no other board is registered on this host",
                                "no other board is registered on this host")):
        with host() as h:
            choice = sinks.ReportChoice(state, source=source)
            config, said, board = new_until_sign_in(h, choice)
            check(not config.upstream_report_url and not h.credential.exists(), f"{state}: no link: {said}")
            check(h.record()["upstream_report"].get("state") == state, f"{state}: recorded: {h.record()}")
            check(f"({why})" in board.report_summary and "refuses until it is connected" in board.report_summary
                  and board.report_summary in said, f"{state}: the warning names it: {board.report_summary}")


def test_new_says_it_again_last() -> None:
    from scripts import new_project_phases

    fn = next(n for n in ast.parse(Path(new_project_phases.__file__).read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "_launch_new_project_panes")
    body = [ast.unparse(s) for s in fn.body]
    check(body[-3:-1] == ["if report_summary:\n    print_func(report_summary)", "stages.finish()"],
          f"the panes phase says the report line last, before it finishes: {body[-3:]}")


# ------------------------------------------------------------------ the repair


def test_the_repair_needs_no_url_when_one_board_is_declared() -> None:
    with host() as h:
        url = h.add_board("syrd", declared=True)
        h.add_board("otto")
        status, said = upgrade("mefp", "--only", "upstream-report")
        check(status == 0 and json.loads(h.config_path.read_text())["upstream_report_url"] == url
              and h.record()["upstream_report"].get("state") == "connected", f"resolved to the declared board: {said}")
        check("no mefp pane is running" in said, f"and it says whether running panes need a relaunch: {said}")


def test_the_repair_refuses_to_guess_and_writes_nothing() -> None:
    with host() as h:
        h.add_board("syrd")
        h.add_board("otto")
        before = h.tree()
        status, said = upgrade("mefp", "--only", "upstream-report")
        check(status == 1 and "mefp records no report board and no board on this host is declared" in said
              and "--upstream-report-board <slug>" in said and "otto (OTTO)" in said and "syrd (SYRD)" in said
              and h.tree() == before, f"two boards, none declared: refused, naming them, nothing written: {said}")
        status, said = upgrade("mefp", "--only", "upstream-report", "--upstream-report-board", "otto")
        check(status == 0 and h.record()["upstream_report"].get("board") == "otto", f"naming one connects it: {said}")


def test_running_panes_are_told_to_relaunch() -> None:
    from scripts import team_launcher as launcher
    from scripts.ticket_board import write_cli

    with host() as h:
        url = h.add_board("syrd", declared=True)

        def tmux(argv, **_kwargs):
            check(argv[:2] == ["tmux", "list-panes"], f"the owner's own tmux is asked: {argv}")
            return subprocess.CompletedProcess(argv, 0, "mefp-director:0.0\nother:0.0\n", "")

        config = launcher.load_project_config("mefp", h.config_path)
        said: list[str] = []
        status = launcher.upgrade_project_command(config, config_path=h.config_path, only="upstream-report",
                                                  registry_dir=h.registry, runner=tmux, print_func=said.append)
        note = next((line for line in said if "were launched before this link" in line), "")
        check(status == 0 and "1 of 2 mefp panes (director)" in note
              and "`switchyard stop mefp` then `switchyard start mefp`" in note and "by hand with `" in note,
              f"the running pane is named, with the relaunch: {said}")
        by_hand = note.split("by hand with `", 1)[1].split("`", 1)[0].split()
        check(by_hand[:1] == ["ticket-board-write"] and by_hand[-1] == "...", f"one whole command: {by_hand}")
        args = write_cli._build_parser().parse_args([*by_hand[1:-1], "--title", "t", "--body", "b"])
        check((args.report_board_url, args.report_token_file, args.origin_project) == (url, str(h.credential), "mefp"),
              f"and it parses as ticket-board-write's own, everything a pane from before lacks named: {by_hand}")
        said.clear()
        status = launcher.upgrade_project_command(launcher.load_project_config("mefp", h.config_path),
                                                  config_path=h.config_path, only="upstream-report",
                                                  registry_dir=h.registry, runner=tmux, print_func=said.append)
        check(status == 0 and not any("relaunch" in line for line in said),
              f"a link already in place, its token re-read from the file: no relaunch: {said}")


def test_an_opt_out_for_an_existing_tenant() -> None:
    with host() as h:
        url = h.add_board("syrd", declared=True)
        status, said = upgrade("mefp", "--only", "upstream-report", "--no-upstream-report")
        check(status == 0 and h.record()["upstream_report"].get("state") == "opted-out"
              and "files no Switchyard reports, as its operator chose" in said, f"recorded: {said}")
        status, said = upgrade("mefp", "--only", "upstream-report")
        check(status == 0 and "had opted out of Switchyard reports; this connection replaces that record" in said
              and h.record()["upstream_report"].get("state") == "connected", f"connecting replaces it, saying so: {said}")
        before = h.tree()
        status, said = upgrade("mefp", "--only", "upstream-report", "--no-upstream-report")
        check(status == 1 and f"it files them to {url}" in said and h.tree() == before,
              f"a linked tenant is not opted out by a record alone: {said}")


# ------------------------------------------------------------------ root's declaration


def test_root_declares_the_report_board() -> None:
    with host() as h:
        h.add_board("syrd", token=False)
        before = h.tree()
        status, said = upgrade("syrd", "--only", "accept-reports")
        check(status == 1 and "syrd cannot take tenant reports" in said and h.tree() == before,
              f"a board with no report token is not declared: {said}")
        h.add_board("otto", url=closed_url())
        status, said = upgrade("otto", "--only", "accept-reports")
        check(status == 1 and "did not answer" in said and "tenant_reports" not in h.record("otto"),
              f"nor one that does not answer: {said}")
        h.add_board("syrd")
        status, said = upgrade("syrd", "--only", "accept-reports", "--dry-run")
        check(status == 0 and "would record" in said and "tenant_reports" not in h.record("syrd"), f"dry run: {said}")
        status, said = upgrade("syrd", "--only", "accept-reports")
        held = h.record("syrd").get("tenant_reports") or {}
        check(status == 0 and held.get("accepted") is True and held.get("recorded_at") and "recorded_by" in held
              and "connects every new tenant to it by default" in said, f"declared: {said} {held}")
        again = h.tree()
        status, _said = upgrade("syrd", "--only", "accept-reports")
        check(status == 0 and h.tree() == again, "declared twice, written once")
        status, said = upgrade("syrd", "--only", "refuse-reports")
        check(status == 0 and "tenant_reports" not in h.record("syrd"), f"and withdrawn: {said}")


# ------------------------------------------------------------------ status, and a resumed provision


def test_status_tells_each_state_apart_from_registry_records_alone() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        url = h.add_board("syrd", declared=True)
        for slug, state in (("pie", "opted-out"), ("sapi", "unavailable"), ("test3", "failed"), ("otto", "connected")):
            h.register(slug, h.write_config(slug))
            sinks.record_report_outcome(slug, state, board="syrd" if state in ("failed", "connected") else "",
                                        url=url, detail="the board did not answer" if state == "failed" else "",
                                        registry_dir=h.registry, print_func=lambda _l: None)
        out: list[str] = []
        quiet = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")  # noqa: E731
        launcher.switchyard_status_command(registry_dir=h.registry, config_dir=h.tmp / "no-config", runner=quiet,
                                           owner_tmux_reader=lambda _c: (set(), ""),
                                           assignments_reader=lambda _c: ({}, ""), print_func=out.append)
        lines = {line.split(": ")[2]: line for line in out if line.startswith("switchyard: reports: ")}
        check(set(lines) == {"mefp", "otto", "pie", "sapi", "syrd", "test3"}, f"a line for every project: {out}")
        check("files them to syrd at" in lines.get("otto", ""), f"status of otto: {lines.get('otto')}")
        check("opted out: files none" in lines.get("pie", "") and "`sudo switchyard upgrade pie --only upstream-report`" in lines.get("pie", ""),
              f"status of pie: {lines.get('pie')}")
        check("no board was available when it was created" in lines.get("sapi", ""), f"status of sapi: {lines.get('sapi')}")
        check("NOT CONNECTED" in lines.get("test3", "") and "the board did not answer" in lines.get("test3", ""), f"status of test3: {lines.get('test3')}")
        check("not recorded; check it with `sudo switchyard upgrade mefp --only upstream-report --dry-run`" in lines.get("mefp", ""),
              f"status of mefp: {lines.get('mefp')}")
        check("the board this host's tenants report to (declared by root)" in lines.get("syrd", ""), f"status of syrd: {lines.get('syrd')}")
        out.clear()
        launcher.switchyard_status_command(registry_dir=h.registry, config_dir=h.tmp / "no-config", json_output=True,
                                           runner=quiet,
                                           owner_tmux_reader=lambda _c: (set(), ""),
                                           assignments_reader=lambda _c: ({}, ""), print_func=out.append)
        reports = json.loads(out[0])["reports"]
        check({slug: r["state"] for slug, r in reports.items()} == {
            "mefp": "not-recorded", "otto": "connected", "pie": "opted-out", "sapi": "unavailable", "syrd": "not-recorded",
            "test3": "failed"} and reports["syrd"]["takes_tenant_reports"] is True, f"and in --json: {reports}")


def test_a_resumed_provision_carries_out_the_decision_before_its_roles() -> None:
    from scripts import resume_provision_command
    from scripts import upstream_report_sinks as sinks

    fn = next(n for n in ast.parse(Path(resume_provision_command.__file__).read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "_finish_provision_after_packet")
    at = next((i for i, s in enumerate(fn.body) if "upstream_report_sinks.resume_report_connection(" in ast.unparse(s)), None)
    start = next((i for i, s in enumerate(fn.body) if isinstance(s, ast.If) and ast.unparse(s.test) == "start_roles"), -1)
    check(at is not None and at < start, f"resume connects before it starts any role: {at} < {start}")
    from scripts import team_launcher as launcher

    for label, decision, declared, want in (
        ("new's decision honoured", sinks.ReportChoice(sinks.CONNECT, board="otto", url="", source="--upstream-report-board"), False, "otto"),
        # Audit of 265dbd9b: no record is never the host's default now -- that would reverse an opt-out.
        ("no decision, one declared board", None, True, ""),
        ("no decision, nothing to decide by", None, False, ""),
        ("new's opt-out honoured", sinks.ReportChoice(sinks.OPTED_OUT, source="--no-upstream-report"), True, "opted-out"),
    ):
        with host() as h:
            urls = {"syrd": h.add_board("syrd", declared=declared), "otto": h.add_board("otto")}
            if decision is not None and decision.board:
                decision = sinks.ReportChoice(decision.state, board=decision.board, url=urls[decision.board],
                                              source=decision.source)
            before = h.tree()
            said: list[str] = []
            with patch.object(sinks, "recorded_report_decision", lambda _slug: decision):
                config = sinks.resume_report_connection(launcher.load_project_config("mefp", h.config_path),
                                                        config_path=h.config_path, registry_dir=h.registry,
                                                        print_func=said.append)
            if want in urls:
                check(config.upstream_report_url == urls[want] and h.record()["upstream_report"].get("state") == "connected",
                      f"{label}: connected to {want} before any role starts: {said}")
            elif want == "opted-out":
                check(not config.upstream_report_url and h.record()["upstream_report"].get("state") == "opted-out",
                      f"{label}: {said}")
            else:
                check(h.tree() == before and any("root holds no record of the report board `switchyard new` chose" in s
                                                 and "--only upstream-report" in s for s in said),
                      f"{label}: said, not guessed, nothing written: {said}")
            if want in urls:
                again = h.tree()
                sinks.resume_report_connection(config, config_path=h.config_path, registry_dir=h.registry,
                                               print_func=said.append)
                check(h.tree() == again, f"{label}: a resume run again writes nothing")


def test_new_keeps_its_decision_before_anything_is_created() -> None:
    """Audit of 265dbd9b: the decision was kept only in P3, so a provision interrupted in P2 resumed with the host's
    default -- reversing an explicit opt-out. The real P0, its launcher seams the phase guard's fakes, root's record on
    the documented provision-root seam; then the real resume with nothing between, as after an interruption anywhere
    past P0."""
    import new_project_phases_boundary_test as p0
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    clis = (("director", "claude"), ("main", "codex"))
    for label, kwargs, want in (("an explicit opt-out", dict(no_upstream_report=True), "opted-out"),
                                ("a board other than the declared one", dict(upstream_report_board="otto"), "otto")):
        with host() as h, patch.dict(os.environ, {launcher.PRIVILEGED_PROVISION_ROOT_ENV: str(h.tmp / "provision")}):
            h.add_board("syrd", declared=True)
            otto = h.add_board("otto")
            world = p0.World(h.tmp)
            got = p0.run(world, p0.inputs(h.tmp, role_clis=clis, registry_dir=h.registry, yes=True, slug="mefp",
                                          **kwargs))
            check(isinstance(got, launcher.NewProjectChoices) and world.names()[-2:] == ["confirm", "stages"]
                  and sinks.decision_path("mefp").is_file(),
                  f"{label}: kept by P0 itself, once confirmed, before any later phase: {got!r} {world.names()[-3:]}")
            info = sinks.decision_path("mefp").lstat()
            check(stat.S_IMODE(info.st_mode) == 0o600 and stat.S_IMODE(info.st_mode) & 0o077 == 0,
                  f"{label}: root-only: {oct(info.st_mode)}")
            said: list[str] = []
            config = sinks.resume_report_connection(launcher.load_project_config("mefp", h.config_path),
                                                    config_path=h.config_path, registry_dir=h.registry,
                                                    print_func=said.append)
            outcome = h.record()["upstream_report"]
            if want == "opted-out":
                check(not config.upstream_report_url and outcome.get("state") == "opted-out" and not h.credential.exists(),
                      f"{label}: survives the resume unchanged, not reversed to the declared board: {outcome} {said}")
            else:
                check(config.upstream_report_url == otto and outcome.get("board") == "otto",
                      f"{label}: survives the resume unchanged, not swapped for the declared board: {outcome} {said}")
    with host() as h, patch.dict(os.environ, {launcher.PRIVILEGED_PROVISION_ROOT_ENV: str(h.tmp / "provision")}):
        world = p0.World(h.tmp, fail="confirm")
        got = p0.run(world, p0.inputs(h.tmp, role_clis=clis, registry_dir=h.registry, yes=True, slug="mefp",
                                      no_upstream_report=True))
        check(isinstance(got, SystemExit) and not sinks.decision_path("mefp").exists(),
              f"a declined confirmation keeps nothing: {got!r}")
    with host() as h, patch.dict(os.environ, {launcher.PRIVILEGED_PROVISION_ROOT_ENV: str(h.tmp / "blocked")}):
        (h.tmp / "blocked").write_text("not a directory", encoding="utf-8")
        world = p0.World(h.tmp)
        got = p0.run(world, p0.inputs(h.tmp, role_clis=clis, registry_dir=h.registry, yes=True, slug="mefp",
                                      no_upstream_report=True))
        check(isinstance(got, SystemExit) and "report decision could not be kept" in str(got)
              and str(got).endswith("Nothing was created."),
              f"a decision that cannot be kept stops new in P0, before anything is created: {got!r}")


NS_ROUND_TRIP = r'''
import json, os, stat, sys
sys.path.insert(0, sys.argv[1])
from scripts import team_launcher as launcher
from scripts import upstream_report_sinks as sinks
assert os.geteuid() == 0
os.environ[launcher.PRIVILEGED_PROVISION_ROOT_ENV] = sys.argv[2]
choice = sinks.ReportChoice(sinks.CONNECT, board="syrd", url="http://127.0.0.1:1", source="--upstream-report-board")
said = []
sinks.record_report_decision("pie", choice)
path = sinks.decision_path("pie")
info = path.lstat()
back = sinks.recorded_report_decision("pie")
os.replace(path, path.with_name("elsewhere.json"))
os.symlink(path.with_name("elsewhere.json"), path)
linked = sinks.recorded_report_decision("pie")
print(json.dumps({"said": said, "mode": oct(stat.S_IMODE(info.st_mode)), "dir": oct(stat.S_IMODE(path.parent.lstat().st_mode)),
                  "back": back == choice, "linked": linked is None, "text": path.with_name("elsewhere.json").read_text()}))
'''


def test_roots_decision_record_round_trips_and_follows_no_link() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd674-ns.") as raw:
        done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, "-c", NS_ROUND_TRIP, str(ROOT),
                               str(Path(raw) / "provision")], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
    check(done.returncode == 0, f"the namespace run finished: {done.stdout[-500:]} {done.stderr[-1500:]}")
    got = json.loads(done.stdout.strip().splitlines()[-1])
    check(got["said"] == [] and got["mode"] == "0o600" and got["dir"] == "0o700" and got["back"],
          f"root's decision is kept root-only beside its plan, and read back as written: {got}")
    check(got["linked"], f"a link at it is not read through: {got}")
    check("token" not in got["text"].lower(), f"and it holds no secret: {got['text']}")


# ------------------------------------------------------------------ main, before


BEFORE_SCENARIO = r'''
import contextlib, io, json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from scripts import team_launcher as launcher
from scripts import upstream_report
import upstream_report_credential_test as cred
closed = sys.argv[2]
out = {}
with tempfile.TemporaryDirectory(prefix="syrd674-before.") as raw:
    tmp = Path(raw)
    registry, board_env, owner, resolver = cred._host(tmp, board_url=closed)
    board_env.parent.mkdir(parents=True, exist_ok=True)
    board_env.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=before-token\n", encoding="utf-8")
    config, path = cred._tenant(tmp)
    (registry / "mefp.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": "mefp",
                                                    "name": "MEFP", "config_path": str(path)}), encoding="utf-8")
    for name in dir(upstream_report):
        fn = getattr(upstream_report, name)
        if callable(fn) and "home_for_user" in (getattr(fn, "__kwdefaults__", None) or {}):
            fn.__kwdefaults__["home_for_user"] = resolver
    def run(*argv):
        said = io.StringIO()
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said), \
                patch.object(launcher, "switchyard_registry_dir", lambda: registry):
            status = launcher.switchyard_main(["upgrade", "mefp", "--only", "upstream-report", *argv])
        return status, said.getvalue()
    out["urlless"] = run()
    out["unreachable"] = run("--upstream-report-url", closed)
    out["linked"] = json.loads(path.read_text()).get("upstream_report_url")
print(json.dumps(out))
'''


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def before() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd674-main.") as raw:
        tree = tree_at(BEFORE, Path(raw) / "tree")
        closed = closed_url()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        done = subprocess.run([sys.executable, "-c", BEFORE_SCENARIO, str(tree), closed], capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL, cwd=str(tree),
                              env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    check(done.returncode == 0, f"before: main's run finished: {done.stdout[-500:]} {done.stderr[-2000:]}")
    got = json.loads(done.stdout.strip().splitlines()[-1])
    status, said = got["urlless"]
    check(status == 1 and "records no upstream report board; name it with --upstream-report-url" in said,
          f"before: the repair needs the URL the registry already knows: {said}")
    status, said = got["unreachable"]
    check(status == 0 and "report connection is in place" in said and got["linked"] == closed,
          f"before: a board nothing answers for is connected to, and reported in place: {said}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if os.environ.get("SYRD674_MUTATION_SKIP_BEFORE") != "1":
        before()
    print(f"upstream_report_default_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    raise SystemExit(main())
