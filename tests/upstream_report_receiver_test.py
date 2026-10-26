#!/usr/bin/env python3
"""SYRD-686: fresh provisioning offers only the host's declared report receiver, never every local board.

In SYRD-653's User UAT, `sudo switchyard new test6` said "No board on this host is declared to take tenant
reports" and offered MEFP, Otto, PIE, SAPI, SYRD and test3 as equal choices. Nothing on that host declared
SYRD -- no registry record held `tenant_reports`, which only root's `--only accept-reports` writes -- so
SYRD-674's menu listed every registered board, each of which mints a report token and answers at its URL.

The host here is that one: six registered boards, each a real HTTP server on 127.0.0.1 answering
`GET /api/runtime-assignments` as itself, three tenants whose configurations already file to syrd, and a new
tenant, test6. A board named in the refusal's root command comes only from root's own registry outcomes, never
from a tenant's configuration, which names whatever its tenant wrote (Audit of 06da7b1b). `switchyard new`'s real choice phase runs on the phase guard's own fakes, its terminal one that
refuses to be read, so a question would fail the case. The declaration is made by the real
`switchyard upgrade syrd --only accept-reports`, and then put through every writer an upgrade runs on a
registry record. Every home is the sandbox's and no case reaches the host's registry (the SYRD-674 fixture).
The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import pwd
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "db7d6018bb7f6b87b2d0128b578c05304cbd0766"  # main before SYRD-686
CHECKS = 0
OBSERVED = ("mefp", "otto", "pie", "sapi", "syrd", "test3")
FILING_TO_SYRD = ("mefp", "otto", "pie")


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(detail)


def observed_host(d, h, *, configs_to: str = "syrd", connected_to: str = "",
                  tenants: tuple[str, ...] = FILING_TO_SYRD) -> dict[str, str]:
    """The UAT host on `h`: every board registered and answering, none declared, and `tenants` whose own
    configurations file to `configs_to`. With `connected_to`, root's records also show them connected there,
    as `connect_chosen_report_board` writes it."""
    from scripts import upstream_report_sinks as sinks

    urls = {slug: h.add_board(slug) for slug in OBSERVED if slug != "mefp"}
    urls["mefp"] = h.add_board("mefp")
    for slug in tenants:
        path = h.tmp / f"{slug}.json"
        raw = json.loads(path.read_text())
        raw["upstream_report_url"] = urls[configs_to]
        path.write_text(json.dumps(raw, indent=2))
        if connected_to:
            sinks.record_report_outcome(slug, sinks.CONNECTED, board=connected_to, url=urls[connected_to],
                                        registry_dir=h.registry, print_func=lambda _line: None)
    return urls


GENERIC = "`sudo switchyard upgrade <board> --only accept-reports`"
GENERIC_HINT = f"{GENERIC} once, then `sudo switchyard upgrade test6 --only upstream-report`"


def new_choices(d, h, **overrides):
    """`switchyard new test6`'s real choice phase: (what it returned or raised, what it said, its steps)."""
    import new_project_phases_boundary_test as p0

    world = p0.World(h.tmp)
    said: list[str] = []
    values = p0.inputs(h.tmp, role_clis=(("director", "claude"), ("main", "codex")), registry_dir=h.registry,
                       slug="test6", print_func=said.append)
    values.update(overrides)
    return p0.run(world, values), said, world.names()


def test_the_observed_host_is_refused_with_the_declaration_it_lacks() -> None:
    """As on the UAT host: tenants linked before SYRD-674 have no root outcome, so no board is named to declare."""
    import upstream_report_default_test as d
    from scripts import upstream_report_sinks as sinks

    for yes in (False, True):
        with d.host() as h:
            observed_host(d, h)
            before = h.tree()
            got, said, steps = new_choices(d, h, yes=yes)
            text = str(got)
            check(isinstance(got, SystemExit) and "confirm" not in steps,
                  f"yes={yes}: refused before the confirmation: {got!r} {steps}")
            check("no board on this host is declared to take tenant reports" in text and GENERIC in text
                  and "--no-upstream-report" in text and text.endswith("Nothing was created."),
                  f"yes={yes}: names why and the declaration to make: {text}")
            check(not any(slug in text for slug in OBSERVED) and "Report board" not in " ".join(said)
                  and not any(line.startswith("switchyard:   ") for line in said),
                  f"yes={yes}: no board offered or named, and nothing asked: {text} {said}")
            check(h.tree() == before, f"yes={yes}: nothing written")
            check(sinks.repair_hint("test6", registry_dir=h.registry) == GENERIC_HINT
                  and sinks._recorded_hint("test6", registry_dir=h.registry) == GENERIC_HINT,
                  f"yes={yes}: the opt-out's and status's repair is the declaration, then the link")


def test_root_records_name_the_board_to_declare() -> None:
    import upstream_report_default_test as d
    from scripts import upstream_report_sinks as sinks

    with d.host() as h:
        observed_host(d, h, connected_to="syrd")
        got, _said, _steps = new_choices(d, h)
        text = str(got)
        check("Root's records show mefp, otto, pie connected to syrd. Declare the board that takes this host's "
              "tenant reports, once: `sudo switchyard upgrade syrd --only accept-reports`" in text,
              f"root's own outcomes agree on syrd: it is named: {text}")
        want = ("`sudo switchyard upgrade syrd --only accept-reports` once, then "
                "`sudo switchyard upgrade test6 --only upstream-report`")
        check(sinks.repair_hint("test6", registry_dir=h.registry) == want
              and sinks._recorded_hint("test6", registry_dir=h.registry) == want,
              "and the opt-out's and status's repair name it too")
        sinks.record_report_outcome("pie", sinks.CONNECTED, board="otto", url=h.boards["otto"].url,
                                    registry_dir=h.registry, print_func=lambda _line: None)
        got, _said, _steps = new_choices(d, h)
        text = str(got)
        check("Root's records show pie connected to otto; mefp, otto connected to syrd. Declare" in text
              and GENERIC in text,
              f"root's outcomes that disagree: both said, no board named for root to declare: {text}")
    with d.host() as h:
        observed_host(d, h)
        for slug in FILING_TO_SYRD:
            sinks.record_report_outcome(slug, sinks.FAILED, board="syrd", url=h.boards["syrd"].url,
                                        detail="did not answer", registry_dir=h.registry, print_func=lambda _l: None)
        got, _said, _steps = new_choices(d, h)
        check(GENERIC in str(got) and "syrd" not in str(got), f"a failed connection is not evidence: {got}")
        for slug in FILING_TO_SYRD:
            sinks.record_report_outcome(slug, sinks.CONNECTED, board="gone", url="http://127.0.0.1:9",
                                        registry_dir=h.registry, print_func=lambda _l: None)
        got, _said, _steps = new_choices(d, h)
        check(GENERIC in str(got) and "gone" not in str(got),
              f"nor a connection to a board no longer registered: {got}")


def test_tenant_configuration_never_picks_the_root_command() -> None:
    """Audit of 06da7b1b: every tenant's configuration names the undeclared ordinary board otto. Following a
    command that named it would make a tenant-selected board every new tenant's receiver."""
    import upstream_report_default_test as d
    from scripts import upstream_report_sinks as sinks

    everyone = tuple(slug for slug in OBSERVED if slug != "otto")
    with d.host() as h:
        observed_host(d, h, configs_to="otto", tenants=everyone)
        got, _said, _steps = new_choices(d, h)
        text = str(got)
        check(isinstance(got, SystemExit) and GENERIC in text and "otto" not in text,
              f"refused, and otto is neither named nor the declaration's target: {text}")
        check(sinks.repair_hint("test6", registry_dir=h.registry) == GENERIC_HINT
              and sinks._recorded_hint("test6", registry_dir=h.registry) == GENERIC_HINT,
              "nor in the opt-out's or status's repair")
    with d.host() as h:
        observed_host(d, h, configs_to="otto", tenants=("mefp", "pie"))
        for slug in ("mefp", "pie"):
            sinks.record_report_outcome(slug, sinks.CONNECTED, board="syrd", url=h.boards["syrd"].url,
                                        registry_dir=h.registry, print_func=lambda _line: None)
        got, _said, _steps = new_choices(d, h)
        text = str(got)
        check("`sudo switchyard upgrade syrd --only accept-reports`" in text and "otto" not in text,
              f"configurations naming otto do not outweigh root's records of syrd: {text}")


def test_the_one_declared_receiver_is_chosen_and_nothing_else_is_offered() -> None:
    import upstream_report_default_test as d
    from scripts import upstream_report_sinks as sinks

    with d.host() as h:
        urls = observed_host(d, h)
        status, said = d.upgrade("syrd", "--only", "accept-reports")
        check(status == 0 and "connects every new tenant to it by default" in said, f"root declares syrd: {said}")
        for yes in (False, True):
            got, lines, steps = new_choices(d, h, yes=yes)
            report = getattr(got, "upstream_report", None)
            check(report == sinks.ReportChoice(sinks.CONNECT, board="syrd", url=urls["syrd"],
                                               source="the host's declared report board"),
                  f"yes={yes}: syrd, chosen without a question: {got!r}")
            check(any(f"test6 files them to syrd at {urls['syrd']}" in line for line in lines)
                  and not any(line.startswith("switchyard:   ") for line in lines),
                  f"yes={yes}: said before the confirmation, nothing listed: {lines}")
        eligible = [b.slug for b in sinks.report_boards(exclude="test6", registry_dir=h.registry) if b.declared]
        check(eligible == ["syrd"], f"of six boards, only syrd is a receiver: {eligible}")


def test_the_declaration_survives_every_upgrade_writer() -> None:
    import upstream_report_default_test as d
    from scripts import agent_cli_promotion
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks
    from scripts.board_ui_record import record_board_ui_users

    with d.host() as h:
        urls = observed_host(d, h)
        status, said = d.upgrade("syrd", "--only", "accept-reports")
        check(status == 0, f"declared: {said}")
        held = json.loads((h.registry / "syrd.json").read_text())["tenant_reports"]
        config = launcher.load_project_config("syrd", h.tmp / "syrd.json")
        quiet = lambda _line: None  # noqa: E731
        # A human account other than the project's, as root approves for the board UI.
        human = next(e.pw_name for e in pwd.getpwall() if e.pw_uid >= 1000 and e.pw_name != h.owner
                     and e.pw_shell.endswith(("sh", "zsh", "bash")))
        problems = record_board_ui_users(config, registry_dir=h.registry, operator_users=(human,),
                                         approval_path=h.tmp / "no-approval", journal_root=h.tmp / "journal",
                                         print_func=quiet)
        problems += agent_cli_promotion.refresh_registered_agent_clis(config, registry_dir=h.registry, print_func=quiet)
        sinks.record_report_outcome("syrd", sinks.OPTED_OUT, detail="its own board", registry_dir=h.registry,
                                    print_func=quiet)
        record = json.loads((h.registry / "syrd.json").read_text())
        check(record.get("board_ui") and record.get("agent_clis") and record.get("upstream_report"),
              f"each writer wrote the record: {sorted(record)} {problems}")
        check(record.get("tenant_reports") == held, f"and the declaration is as root made it: {record}")
        status, said = d.upgrade("syrd", "--only", "accept-reports")
        check(status == 0 and json.loads((h.registry / "syrd.json").read_text())["tenant_reports"] == held,
              f"declared again: unchanged: {said}")
        got, _lines, _steps = new_choices(d, h)
        check(getattr(getattr(got, "upstream_report", None), "board", "") == "syrd"
              and got.upstream_report.url == urls["syrd"], f"after the upgrade writers, still syrd: {got!r}")
        install = (ROOT / "install").read_text(encoding="utf-8")
        check("/etc/switchyard" not in install and "registry" not in install,
              "install never names the registry the declaration lives in")


def test_two_declarations_are_refused_naming_only_those() -> None:
    import upstream_report_default_test as d
    from scripts import upstream_report_sinks as sinks

    with d.host() as h:
        observed_host(d, h)
        for slug in ("syrd", "otto"):
            status, said = d.upgrade(slug, "--only", "accept-reports")
            check(status == 0, f"{slug} declared: {said}")
        check("refuses until one is named" in said, f"root is told what a second declaration does: {said}")
        before = h.tree()
        got, said_lines, steps = new_choices(d, h)
        text = str(got)
        check(isinstance(got, SystemExit) and "confirm" not in steps
              and "2 boards are declared to take tenant reports (otto, syrd)" in text
              and "not offered as one. Name one with --upstream-report-board <otto|syrd>" in text
              and "--only refuse-reports" in text
              and "pie (" not in text and "sapi" not in text and "test3" not in text and h.tree() == before,
              f"refused, naming only the declared boards and the repair, nothing written: {text} {said_lines}")
        got, _said, _steps = new_choices(d, h, upstream_report_board="syrd")
        check(getattr(getattr(got, "upstream_report", None), "board", "") == "syrd",
              f"naming one of them decides it: {got!r}")
        want = ("`sudo switchyard upgrade test6 --only upstream-report --upstream-report-board <slug>`, "
                "naming one of: otto, syrd")
        hint = sinks.repair_hint("test6", registry_dir=h.registry)
        check(hint == want, f"an opt-out's repair names only the declared boards: {hint}")
        hint = sinks._recorded_hint("test6", registry_dir=h.registry)
        check(hint == want, f"and so does status, from the registry alone: {hint}")
    with d.host() as h:
        observed_host(d, h)
        hint = sinks._recorded_hint("test6", registry_dir=h.registry)
        check(hint == ("`sudo switchyard upgrade <board> --only accept-reports` once, then "
                       "`sudo switchyard upgrade test6 --only upstream-report`"),
              f"with none declared, status names the declaration first, and no board: {hint}")


BEFORE_SCENARIO = r'''
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
import upstream_report_default_test as d
from scripts import upstream_report_sinks as sinks
with d.host() as h:
    for slug in ("otto", "pie", "sapi", "syrd", "test3"):
        h.add_board(slug)
    lines = []
    choice = sinks.choose_report_board("test6", registry_dir=h.registry, print_func=lines.append,
                                       input_func=lambda _prompt: "none")
    print(json.dumps({"state": choice.state, "lines": lines}))
'''


def tree_at(commit: str, where: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "install"], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(where, filter="data")
    return where


def before() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd686-main.") as raw:
        tree = tree_at(BEFORE, Path(raw) / "tree")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        done = subprocess.run([sys.executable, "-c", BEFORE_SCENARIO, str(tree)], capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL, cwd=str(tree),
                              env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    check(done.returncode == 0, f"before: main's run finished: {done.stdout[-500:]} {done.stderr[-2000:]}")
    got = json.loads(done.stdout.strip().splitlines()[-1])
    offered = [line.split(". ", 1)[1].split(" ")[0] for line in got["lines"] if line.startswith("switchyard:   ")]
    check(offered == [*OBSERVED, "create"] and got["state"] == "opted-out",
          f"before: every registered board offered as a receiver, as in the UAT: {got}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if os.environ.get("SYRD686_MUTATION_SKIP_BEFORE") != "1":
        before()
    print(f"upstream_report_receiver_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    raise SystemExit(main())
