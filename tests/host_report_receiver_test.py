#!/usr/bin/env python3
"""SYRD-742: the first board on a host becomes its tenant-report receiver, and a host without one says so first.

The report receiver is root's declaration, in that board's registry record (SYRD-686). Nothing set it when a host's
first board was created, so the next user to run `switchyard new` answered every role question, then was refused
and told to run `sudo switchyard upgrade syrd --only accept-reports`. Now:

- `switchyard new` on a host with no board registered declares the board it creates the host's receiver, once that
  board answers, as `--only accept-reports` does. It says so before its confirmation, and `--no-upstream-report`
  still declares nothing;
- every later `new` on the host connects to it with no other command;
- doing it again changes nothing, and a receiver declared meanwhile is never replaced;
- a host that has boards but no single valid declared receiver is refused before the role questions, with the
  repair, and nothing is created;
- an undeclared board is still never offered or inferred.

Each board is a real HTTP server on 127.0.0.1 answering as its project, every home is the sandbox's, and no case
reaches the host's registry: `upstream_report_default_test`'s host, reused whole.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import upstream_report_default_test as d  # noqa: E402
from upstream_report_default_test import check, closed_url, host, new_until_sign_in, said_by  # noqa: E402


def declared(h: d.Host) -> list[str]:
    return sorted(path.stem for path in h.registry.glob("*.json") if d.json.loads(path.read_text()).get("tenant_reports"))


def first_board(h: d.Host) -> str:
    """`mefp` as the only board registered, answering as itself."""
    for path in h.registry.glob("*.json"):
        path.unlink()
    return h.add_board("mefp")


def test_the_first_board_is_decided_the_receiver_and_says_so() -> None:
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        first_board(h)
        (h.registry / "mefp.json").unlink()  # not registered yet: it is the board being created
        choice = sinks.choose_report_board("mefp", registry_dir=h.registry)
        check((choice.state, choice.board, choice.source) == (sinks.RECEIVER, "mefp", "the first board on this host"),
              f"no board registered: the new one is the host's receiver: {choice}")
        summary = sinks.choice_summary("mefp", choice, registry_dir=h.registry)
        check("mefp is the first board on this host, so it is declared the board that takes this host's tenant "
              "reports" in summary and "--no-upstream-report" in summary and "WARNING" not in summary,
              f"said before the confirmation, with the opt-out: {summary}")
        opted = sinks.choose_report_board("mefp", no_upstream_report=True, registry_dir=h.registry)
        check(opted.state == sinks.OPTED_OUT, f"--no-upstream-report still opts out, and declares nothing: {opted}")


def test_a_clean_host_leaves_exactly_one_receiver_and_the_next_tenant_reports_to_it() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        url = first_board(h)
        env = h.home / ".config" / "mefp" / "ticket-board.env"
        config, said, board = new_until_sign_in(h, sinks.ReportChoice(sinks.RECEIVER, board="mefp",
                                                                      source=sinks.FIRST_BOARD))
        held = h.record("mefp").get("tenant_reports") or {}
        check(declared(h) == ["mefp"] and held.get("accepted") is True and held.get("recorded_at"),
              f"exactly one root-declared receiver, in its registry record: {declared(h)} {held}")
        check("TICKET_BOARD_REPORT_BINDING_KEY=" in env.read_text(),
              "with its key for project-bound credentials, as --only accept-reports gives it")
        check(h.boards["mefp"].requests >= 1, "it was asked whether it answers as itself before it was declared")
        check(board.report_summary.startswith("switchyard: Switchyard reports: mefp is this host's report receiver")
              and board.report_summary in said and not config.upstream_report_url,
              f"said at the end, and it files to no board itself: {board.report_summary}")
        check(not h.record("mefp")["upstream_report"], "no filing outcome is recorded for the receiver itself")
        status = sinks.report_status_lines(["mefp"], registry_dir=h.registry)
        check(status == ["switchyard: reports: mefp: the board this host's tenants report to (declared by root)"],
              f"status says what it is: {status}")
        # Another user's tenant, later: no accept-reports, and it is connected to the receiver.
        h.write_config("otto")
        choice = sinks.choose_report_board("otto", registry_dir=h.registry)
        check((choice.state, choice.board, choice.url) == (sinks.CONNECT, "mefp", url)
              and choice.source == "the host's declared report board",
              f"the next tenant reports to the receiver with no other command: {choice}")
        d.no_token_anywhere(h, *said)
        check(launcher.load_project_config("mefp", h.config_path).upstream_report_url == "",
              "the receiver's own configuration carries no report link")


def test_doing_it_again_changes_nothing_and_a_receiver_is_never_replaced() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        first_board(h)
        config = launcher.load_project_config("mefp", h.config_path)
        said: list[str] = []
        check(sinks.declare_host_receiver(config, registry_dir=h.registry, print_func=said.append).startswith(
            "switchyard: Switchyard reports: mefp is this host's report receiver"), f"declared: {said}")
        again = h.tree()
        sinks.declare_host_receiver(config, registry_dir=h.registry, print_func=said.append)
        check(h.tree() == again, "declared twice, written once")
        receiver = sinks.ReportChoice(sinks.RECEIVER, board="mefp", source=sinks.FIRST_BOARD)
        with patch.object(sinks, "recorded_report_decision", lambda _slug: receiver):
            sinks.resume_report_connection(config, config_path=h.config_path, registry_dir=h.registry,
                                           print_func=said.append)
        check(h.tree() == again, "and a resumed provision of a receiver already declared writes nothing")
    with host() as h:
        first_board(h)
        h.add_board("syrd", declared=True)  # declared while mefp was being created
        before = h.tree()
        said = []
        config = launcher.load_project_config("mefp", h.config_path)
        line = sinks.declare_host_receiver(config, registry_dir=h.registry, print_func=said.append)
        check(h.tree() == before and declared(h) == ["syrd"] and "syrd is declared already" in line
              and "never replaced" in line and "--only upstream-report" in line,
              f"a receiver declared meanwhile is kept, and nothing is written: {line}")


def test_a_receiver_declared_while_the_new_board_starts_is_never_joined_by_a_second() -> None:
    """Audit of baafa275: another setup declares a receiver while this board starts or is validated."""
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        first_board(h)
        config = launcher.load_project_config("mefp", h.config_path)
        seen: list[list[str]] = []

        def meanwhile(_url: str) -> bool:  # while mefp's board starts, another root setup completes
            h.add_board("syrd", declared=True)
            seen.append(declared(h))
            return True

        said: list[str] = []
        line = sinks.declare_host_receiver(config, registry_dir=h.registry, wait=meanwhile, print_func=said.append)
        check(seen == [["syrd"]] and declared(h) == ["syrd"] and "mefp was not declared" in line
              and "never replaced" in line and any("mefp was not declared to take tenant reports: syrd" in s for s in said),
              f"declared during the wait: exactly one receiver remains, and the loser says it was not declared: "
              f"{declared(h)} {line} {said}")


def test_two_first_boards_declared_at_once_leave_exactly_one() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    for attempt in range(5):
        with host() as h:
            for path in h.registry.glob("*.json"):
                path.unlink()
            h.add_board("mefp")
            h.add_board("syrd")  # two first-board setups, each started before the other was registered
            both = d.threading.Barrier(2, timeout=10)
            lines: dict[str, str] = {}

            def setup(slug: str) -> None:
                config = launcher.load_project_config(slug, h.tmp / f"{slug}.json")
                lines[slug] = sinks.declare_host_receiver(config, registry_dir=h.registry,
                                                          wait=lambda _url: both.wait() is None or True,
                                                          print_func=lambda _line: None)

            threads = [d.threading.Thread(target=setup, args=(slug,)) for slug in ("mefp", "syrd")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            won = [slug for slug, line in lines.items() if "is this host's report receiver, declared by root" in line]
            lost = [slug for slug, line in lines.items() if f"{slug} was not declared" in line]
            check(len(declared(h)) == 1 and won == declared(h) and len(lost) == 1 and set(won + lost) == {"mefp", "syrd"},
                  f"attempt {attempt}: both past their first look, exactly one is declared and the other says it "
                  f"was not: {declared(h)} {lines}")


def test_a_declaration_waits_for_the_host_wide_lock_held_by_another_process() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    holder = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
              "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.readline()\n")
    with host() as h:
        first_board(h)
        config = launcher.load_project_config("mefp", h.config_path)
        other = d.subprocess.Popen([sys.executable, "-c", holder, str(h.registry)], stdin=d.subprocess.PIPE,
                                   stdout=d.subprocess.PIPE, text=True)
        try:
            check(other.stdout.readline().strip() == "held", "fixture: another process holds the declarations")
            lines: list[str] = []
            thread = d.threading.Thread(target=lambda: lines.append(sinks.declare_host_receiver(
                config, registry_dir=h.registry, wait=lambda _url: True, print_func=lambda _line: None)))
            thread.start()
            thread.join(1.5)
            check(thread.is_alive() and declared(h) == [], "while it is held, nothing is declared: the write waits")
            h.add_board("syrd", declared=True)  # the holder's declaration
        finally:
            other.stdin.close()
            other.wait(10)
        thread.join(30)
        check(declared(h) == ["syrd"] and lines and "mefp was not declared" in lines[0],
              f"released, the waiting declaration sees the holder's and writes nothing: {declared(h)} {lines}")


def test_a_receiver_that_cannot_be_declared_yet_says_the_exact_command_and_resume_finishes_it() -> None:
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    with host() as h:
        for path in h.registry.glob("*.json"):
            path.unlink()
        h.add_board("mefp", url=closed_url())
        config = launcher.load_project_config("mefp", h.config_path)
        said: list[str] = []
        waited: list[str] = []
        line = sinks.declare_host_receiver(config, registry_dir=h.registry, wait=lambda url: waited.append(url) or False,
                                           print_func=said.append)
        check(declared(h) == [] and "`sudo switchyard upgrade mefp --only accept-reports`" in line
              and any("did not answer" in s for s in said) and waited,
              f"a board that does not answer yet is not declared; the exact command is said: {line} {said}")
        h.add_board("mefp")  # it comes up
        receiver = sinks.ReportChoice(sinks.RECEIVER, board="mefp", source=sinks.FIRST_BOARD)
        with patch.object(sinks, "recorded_report_decision", lambda _slug: receiver):
            sinks.resume_report_connection(launcher.load_project_config("mefp", h.config_path),
                                           config_path=h.config_path, registry_dir=h.registry, print_func=said.append)
        check(declared(h) == ["mefp"], f"resume-provision carries the decision out: {said[-1:]}")
    # Root's record of the decision, as `record_report_decision` writes it, is read back for a resume.
    document = {"schema": sinks.DECISION_SCHEMA, "project": "mefp", "state": sinks.RECEIVER, "board": "mefp",
                "url": "", "source": sinks.FIRST_BOARD}
    with patch.object(launcher, "read_plan_no_follow", lambda _path, **_k: (d.SimpleNamespace(data=document), "")):
        back = sinks.recorded_report_decision("mefp")
    check(back == sinks.ReportChoice(sinks.RECEIVER, board="mefp", source=sinks.FIRST_BOARD),
          f"root's record of the decision keeps it for a resume: {back}")


def test_a_host_with_no_valid_receiver_refuses_before_the_role_questions() -> None:
    import new_project_phases_boundary_test as p0

    for label, boards, repair in (
            ("none declared", (("syrd", False), ("otto", False)), "`sudo switchyard upgrade <board> --only accept-reports`"),
            ("two declared", (("syrd", True), ("otto", True)), "--upstream-report-board <otto|syrd>"),
            ("the one declared has no report token", (("syrd", "no-token"),),
             "`sudo switchyard upgrade syrd --only refuse-reports`")):
        with host() as h:
            for slug, how in boards:
                h.add_board(slug, declared=bool(how), token=how != "no-token")
            world = p0.World(h.tmp)
            before = h.tree()
            got = p0.run(world, p0.inputs(h.tmp, project_name="N", slug="s742", agent_name=p0.OWNER,
                                          project_path=h.tmp / "p", registry_dir=h.registry))
            check(isinstance(got, SystemExit) and "needs a decision about Switchyard reports" in str(got)
                  and "Nothing was created." in str(got) and repair in str(got)
                  and not {"auth-home", "plan", "review", "confirm"} & set(world.names()) and h.tree() == before,
                  f"{label}: refused with the repair before the role questions, nothing created: {got} {world.names()}")
            if label == "none declared":
                check("otto" not in str(got) and "syrd" not in str(got),
                      f"{label}: no undeclared board is offered or inferred: {got}")


def test_the_decision_comes_before_the_role_questions_in_the_phase() -> None:
    from scripts import new_project_phases

    fn = next(n for n in d.ast.parse(Path(new_project_phases.__file__).read_text()).body
              if isinstance(n, d.ast.FunctionDef) and n.name == "_resolve_new_project_choices")
    body = [d.ast.unparse(s) for s in fn.body]
    root = next(i for i, s in enumerate(body) if s.startswith("if euid_getter() != 0:"))
    decided = next(i for i, s in enumerate(body) if "upstream_report_sinks.choose_report_board(" in s)
    asked = next(i for i, s in enumerate(body) if "_prompt_switchyard_role_plan(" in s)
    summary = next(i for i, s in enumerate(body) if "upstream_report_sinks.choice_summary(" in s)
    confirm = next(i for i, s in enumerate(body) if "launcher._confirm_switchyard_new(" in s)
    check(root < decided < asked < summary < confirm,
          f"after the root check, before the role questions; said before the confirmation: "
          f"{root} {decided} {asked} {summary} {confirm}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"host_report_receiver_test: {d.CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
