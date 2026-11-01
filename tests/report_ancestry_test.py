#!/usr/bin/env python3
"""SYRD-721: project report ancestry, and one report filed safely to one ancestor or to each.

Production-built boards on one cluster, served over real HTTP: `syrd`
(Switchyard) and `sapi` (Stellaris API) take reports, each with its own report
secret and binding key; `mefp` reports to SAPI, which reports to Switchyard; `pie`
is a tenant connected to Switchyard before ancestry existed. Root's half runs as
root's code does, against a scratch registry and home; the panes' half is the real
`ticket-board-write` and `ticket-board-read`.

Acceptance, case by case:
1. pie files to Switchyard exactly as before, and again after its connection is
   migrated into its declared parent.
2. mefp lists both ancestors and previews and files to SAPI only, Switchyard only,
   or both.
3. Both: one correlated ticket on each board after one exact confirmation.
4. A failure after SAPI took the report is said and resumable, without a second
   SAPI ticket; a lost answer too.
5. Follow-ups to one correlated ticket or all of them, idempotent and resumable.
6. Cycles, non-ancestors, identity mismatches, missing or foreign credentials are
   refused before anything is written; no token is ever printed or recorded.
7. Single-upstream filing and follow-ups keep working.
8. Nothing fans out because ancestry exists.

Also: root's declaration (verified, journaled, idempotent, dry-runnable, refused
before writing), a parent change and removal that erase nothing, the board page's
read, digest compatibility with main, and the upgrade from main's schema.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CHECKS = 0
SECRETS = {"syrd": ("syrd-secret-must-never-print", "syrd-binding-key-must-never-print"),
           "sapi": ("sapi-secret-must-never-print", "sapi-binding-key-must-never-print")}
NAMES = {"syrd": ("Switchyard", "SYRD"), "sapi": ("Stellaris API", "SAPI"), "mefp": ("Morfane's Epic Fix Patch", "MEFP"),
         "pie": ("Pie", "PIE")}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


@dataclass(frozen=True)
class Tenant:
    """The fields of a project configuration the report code reads."""

    project: str
    run_as_user: str
    upstream_report_url: str = ""
    upstream_report_token_file: str = ""


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main before this ticket: the parent of the commit that added this test, or HEAD before it is committed."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "tests/report_ancestry_test.py"], capture_output=True, text=True).stdout.split()
    return found[-1] + "^" if found else "HEAD"


def main() -> int:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster

    from unittest import mock

    from scripts import report_parent, upstream_report as ur, upstream_report_sinks as sinks
    from scripts.ticket_board import report_ancestry as ra, report_confirmation as rc, report_follow_up as rf

    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    user = getpass.getuser()
    outputs: list[str] = []
    servers: list = []
    with temporary_cluster(prefix="syrd721-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd721.") as scratch_name:
        scratch = Path(scratch_name)
        home, pane, registry = scratch / "home", scratch / "pane", scratch / "registry"
        for path in (home, pane, registry):
            path.mkdir(mode=0o700)
        os.environ[ra.REGISTRY_DIR_ENV] = str(registry)  # the board service's read of the registry, in this process

        def build(db: str, tree: Path = ROOT) -> str:
            admin = t.conninfo(cluster.socket_dir, cluster.port, db)
            t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
            t.psql(admin, (tree / "scripts/ticket_board/schema.sql").read_text())
            if t.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
                t.create_roles(admin)
            t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
            migrate(admin, tree)
            return admin

        def migrate(admin: str, tree: Path) -> None:
            done = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
            check(done.returncode == 0, f"migrations apply: {done.stderr[-600:]}")
            t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())

        def serve(db: str) -> tuple[object, str]:
            name, prefix = NAMES[db]
            secret, key = SECRETS.get(db, ("", ""))
            app = t.TicketBoardApp(cluster.root / f"{db}-frames", cluster.root / f"{db}-assets", project=db,
                                   project_name=name, ticket_prefix=prefix,
                                   database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
            server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier(),
                                         report_token=secret or None, report_binding_key=key or None)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return server, f"http://127.0.0.1:{server.server_port}"

        admins = {db: build(db) for db in ("syrd", "sapi", "mefp", "pie")}
        served = {db: serve(db) for db in admins}
        urls = {db: url for db, (_server, url) in served.items()}

        def sql(db: str, statement: str) -> str:
            return t.psql(admins[db], statement).strip()

        def count(db: str, title: str = "") -> int:
            where = f" WHERE title = '{title}'" if title else ""
            return int(sql(db, f"SELECT count(*) FROM ticket_board.tickets{where};"))

        def comments(db: str, ticket: str) -> list[str]:
            return json.loads(sql(db, "SELECT coalesce(json_agg(who || ': ' || text ORDER BY id), '[]') FROM "
                                      f"ticket_board.ticket_comments WHERE ticket_id = '{ticket}';"))

        # -- the host: registry records, configurations and each receiving board's environment --------------------
        def record(slug: str, **extra: object) -> None:
            config = scratch / f"{slug}.config.json"
            config.write_text(json.dumps({"project": slug, "board_url": urls[slug], "run_as_user": user}))
            (registry / f"{slug}.json").write_text(json.dumps({"schema": ra.REGISTRY_SCHEMA, "slug": slug,
                                                               "name": NAMES[slug][0], "config_path": str(config),
                                                               **extra}))

        for slug in admins:
            record(slug)
        for slug, (secret, key) in SECRETS.items():
            env_file = home / ".config" / slug / "ticket-board.env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={secret}\nTICKET_BOARD_REPORT_BINDING_KEY={key}\n")
        homes = {"home_for_user": lambda _user: home}
        bound = {"takes_bound": lambda _url: True}
        roots = {"registry_dir": registry, **homes}
        said: list[str] = []
        outputs_root = said.append

        def reg(slug: str) -> dict:
            return json.loads((registry / f"{slug}.json").read_text())

        # -- root: sapi -> syrd, mefp -> sapi ---------------------------------------------------------------------
        sapi = Tenant("sapi", user)
        check(report_parent.settle(sapi, urls["syrd"], **roots, **bound, print_func=outputs_root) == [],
              f"root declares Switchyard as SAPI's report parent: {said[-4:]}")
        mefp = Tenant("mefp", user, upstream_report_url=urls["sapi"])
        check(ur.refresh_upstream_report_credential(mefp, **roots, **bound, print_func=outputs_root) == [],
              "mefp is given SAPI's credential, as a connection does")
        mefp = replace(mefp, upstream_report_token_file=str(home / ".config/mefp/upstream-report.env"))
        dry = (registry / "mefp.json").read_text()
        check(report_parent.settle(mefp, urls["sapi"], **roots, **bound, dry_run=True, print_func=outputs_root) == []
              and (registry / "mefp.json").read_text() == dry
              and not (home / ".config/mefp/report-credentials").exists(),
              "a dry run says what it would declare and issue, and writes nothing")
        check(report_parent.settle(mefp, urls["sapi"], **roots, **bound, print_func=outputs_root) == [],
              f"root declares SAPI as mefp's report parent: {said[-5:]}")
        parent = reg("mefp")[ra.PARENT_KEY]
        check({k: parent[k] for k in ("project", "url", "ticket_prefix")} == {"project": "sapi", "url": urls["sapi"],
                                                                           "ticket_prefix": "SAPI"}
              and parent["recorded_at"] and len(reg("mefp")[ra.HISTORY_KEY]) == 1
              and reg("mefp")[ra.HISTORY_KEY][0]["to"] == "sapi",
              f"the declaration is root's record, verified, with its history: {reg('mefp')}")
        creds = {slug: ra.read_token(home / f".config/mefp/report-credentials/{slug}.env")[0] for slug in ("sapi", "syrd")}
        check(creds == {slug: rf.bound_token(SECRETS[slug][1], "mefp") for slug in ("sapi", "syrd")}
              and oct((home / ".config/mefp/report-credentials/syrd.env").stat().st_mode & 0o777) == "0o600",
              "each ancestor's own credential, bound to mefp, owner-only: never one board's token at another")
        check(set(reg("mefp")[ra.CREDENTIALS_KEY]) == {"sapi", "syrd"}
              and all(note["bound"] for note in reg("mefp")[ra.CREDENTIALS_KEY].values()),
              f"root notes which it issued, and no token: {reg('mefp')[ra.CREDENTIALS_KEY]}")
        check(report_parent.settle(mefp, urls["sapi"], **roots, **bound, print_func=outputs_root) == []
              and len(reg("mefp")[ra.HISTORY_KEY]) == 1 and any("already" in line for line in said[-4:]),
              "the same declaration again changes nothing")
        # A run that wrote a credential but not root's note of it: the next run repairs the note, the token untouched.
        held = home / ".config/mefp/report-credentials/syrd.env"
        noted = reg("mefp")
        noted[ra.CREDENTIALS_KEY].pop("syrd")
        (registry / "mefp.json").write_text(json.dumps(noted))
        held.unlink()
        syrd_only = [link for link in ra.chain("mefp", registry)[0] if link.project == "syrd"]
        with mock.patch.object(report_parent, "_update_record", lambda *_a, **_k: "simulated registry failure"):
            first = report_parent.issue_credentials(mefp, syrd_only, **roots, **bound, print_func=outputs_root)
        written = ra.read_token(held)[0]
        check(any("simulated registry failure" in problem for problem in first) and written
              and "syrd" not in reg("mefp")[ra.CREDENTIALS_KEY],
              f"the credential is written and its note is not: {first}")
        record_bytes = (registry / "mefp.json").read_bytes()
        shown = len(said)
        dry_repair = report_parent.issue_credentials(mefp, syrd_only, **roots, **bound, dry_run=True,
                                                     print_func=outputs_root)
        check(dry_repair == [] and any("would repair root's record that it issued mefp syrd's credential (missing)" in line
                                       for line in said[shown:])
              and (registry / "mefp.json").read_bytes() == record_bytes and ra.read_token(held)[0] == written,
              f"a dry run says it would repair the missing note, and writes nothing: {said[shown:]}")
        again = report_parent.issue_credentials(mefp, syrd_only, **roots, **bound, print_func=outputs_root)
        note = reg("mefp")[ra.CREDENTIALS_KEY].get("syrd", {})
        check(again == [] and note.get("issued_at") and note.get("bound") is True and ra.read_token(held)[0] == written,
              f"the retry repairs root's note and leaves the credential as it was: {again} {note}")

        # -- root refuses before writing ------------------------------------------------------------------------------
        record("syrd", report_parent={"project": "mefp", "url": urls["mefp"], "ticket_prefix": "MEFP"})
        _links, cycle = report_parent.proposed_chain("sapi", urls["syrd"], registry_dir=registry)
        check("cycle: sapi -> syrd -> mefp -> sapi" in cycle, f"root refuses a cycle: {cycle}")
        before_bytes = {p.name: p.read_bytes() for p in registry.iterdir()}
        sapi_config = scratch / "sapi.config.json"
        loaded = Tenant("sapi", user, upstream_report_url=urls["syrd"])
        from scripts import team_launcher as launcher

        connected: list = []  # the connection resolves real homes: it must never run here, refused or not
        with mock.patch.object(launcher, "connect_upstream_report", lambda *a, **k: connected.append(a) or (a[0], [])):
            code = ur.connect_upstream_report_command(
                "upstream-report", loaded, config_path=sapi_config, dry_run=False, registry_dir=registry,
                upstream_report_url="", upstream_report_token_file="", print_func=outputs_root,
                upstream_report_board="syrd")
        check(code == 1 and connected == [] and {p.name: p.read_bytes() for p in registry.iterdir()} == before_bytes
              and any("cycle" in line for line in said[-3:]),
              f"--only upstream-report refuses that parent before connecting, and writes nothing: {said[-3:]}")
        record("syrd")
        _links, wrong = report_parent.proposed_chain("mefp", "http://127.0.0.1:9", registry_dir=registry)
        check("no project registered on this host serves" in wrong, f"an unregistered board is refused: {wrong}")
        record("sapi", report_parent={"project": "ghost", "url": "http://127.0.0.1:9"})
        ghost_links, ghost = ra.chain("mefp", registry)
        check([link.project for link in ghost_links] == ["sapi"] and "its report parent ghost is not registered" in ghost,
              f"an unregistered ancestor is never a link, and stops the chain: {ghost_links} {ghost}")
        record("sapi", report_parent={"project": "syrd", "url": urls["mefp"], "ticket_prefix": "SYRD"})
        _links, mismatch = report_parent.proposed_chain("mefp", urls["sapi"], registry_dir=registry)
        check("answers as 'mefp'" in mismatch, f"root refuses an ancestor that does not answer as itself: {mismatch}")
        imposter_app = t.TicketBoardApp(cluster.root / "imposter-frames", cluster.root / "imposter-assets",
                                        project="imposter", project_name="Imposter", ticket_prefix="SAPI",
                                        database_url=t.conninfo(cluster.socket_dir, cluster.port, "pie", t.SERVICE_ROLE))
        imposter = t.TicketBoardServer(("127.0.0.1", 0), imposter_app, director_notifier=t.QuietNotifier())
        threading.Thread(target=imposter.serve_forever, daemon=True).start()
        servers.append(imposter)
        sapi_config.write_text(json.dumps({"project": "sapi", "run_as_user": user,
                                           "board_url": f"http://127.0.0.1:{imposter.server_port}"}))
        _links, posing = report_parent.proposed_chain("mefp", f"http://127.0.0.1:{imposter.server_port}",
                                                      registry_dir=registry)
        check("is 'imposter', not sapi's board" in posing,
              f"root refuses a parent whose board answers as another project: {posing}")
        record("sapi")
        # A parent and a different board further up with one ticket prefix: refused once the parent has said its
        # prefix, before anything is written (Audit of 52f543e1). imposter is registered here, answering as itself.
        imposter_url = f"http://127.0.0.1:{imposter.server_port}"
        (scratch / "imposter.config.json").write_text(json.dumps({"project": "imposter", "board_url": imposter_url,
                                                                  "run_as_user": user}))
        (registry / "imposter.json").write_text(json.dumps({"schema": ra.REGISTRY_SCHEMA, "slug": "imposter",
                                                            "name": "Imposter",
                                                            "config_path": str(scratch / "imposter.config.json")}))
        record("sapi", report_parent={"project": "imposter", "url": imposter_url, "ticket_prefix": "SAPI"})
        watched = [scratch / "mefp.config.json", home / ".config/mefp/upstream-report.env"]
        before_bytes = {p.name: p.read_bytes() for p in registry.iterdir()}
        before_files = [p.read_bytes() for p in watched]
        defaults = ur.connect_upstream_report.__kwdefaults__
        real_home, defaults["home_for_user"] = defaults["home_for_user"], homes["home_for_user"]
        try:
            code = ur.connect_upstream_report_command(
                "upstream-report", mefp, config_path=scratch / "mefp.config.json", dry_run=False,
                registry_dir=registry, upstream_report_url="", upstream_report_token_file="",
                print_func=outputs_root, upstream_report_board="sapi")
        finally:
            defaults["home_for_user"] = real_home
        check(code == 1 and any("sapi and imposter are recorded with the same ticket prefix" in line for line in said[-3:])
              and {p.name: p.read_bytes() for p in registry.iterdir()} == before_bytes
              and [p.read_bytes() for p in watched] == before_files,
              f"a repeated ticket prefix is refused, the registry, configuration and credential unchanged: {said[-3:]}")
        (registry / "imposter.json").unlink()
        record("sapi")
        check(report_parent.settle(sapi, urls["syrd"], **roots, **bound, print_func=outputs_root) == [],
              "SAPI's parent is declared again")

        # -- switchyard new's path: the board it connects is the parent, declared with each ancestor's credential --
        from scripts import team_launcher as launcher

        defaults = ur.connect_upstream_report.__kwdefaults__
        real_home = defaults["home_for_user"]
        defaults["home_for_user"] = homes["home_for_user"]  # bound at import; sandboxed here, as report_intake does
        try:
            with mock.patch.object(launcher, "load_project_config", lambda _slug, _path: "loaded"):
                sinks.connect_chosen_report_board(Tenant("pie", user), config_path=scratch / "pie.config.json",
                                                  choice=sinks.ReportChoice(sinks.CONNECT, board="syrd", url=urls["syrd"],
                                                                            source="the host's declared report board"),
                                                  registry_dir=registry, print_func=outputs_root)
        finally:
            defaults["home_for_user"] = real_home
        declared = reg("pie")
        check(declared.get(ra.PARENT_KEY, {}).get("project") == "syrd"
              and declared.get(ra.HISTORY_KEY, [{}])[-1].get("reason") == "switchyard new (the host's declared report board)"
              and (home / ".config/pie/report-credentials/syrd.env").is_file(),
              f"switchyard new declares the board it connects as the report parent: {said[-6:]}")
        record("pie")
        shutil.rmtree(home / ".config/pie")

        # -- the panes ----------------------------------------------------------------------------------------------
        base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(pane),
                    "TICKET_BOARD_CALLER_ROLE": "director", ra.REGISTRY_DIR_ENV: str(registry)}

        def pane_env(project: str, parent: str) -> dict[str, str]:
            return {**base_env, "TICKET_BOARD_URL": urls[project], "TICKET_BOARD_PROJECT": project,
                    "TICKET_BOARD_WRITE_TOKEN": served[project][0].write_token, "TICKET_BOARD_REPORT_URL": urls[parent],
                    "TICKET_BOARD_REPORT_ORIGIN_PROJECT": project,
                    "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE": str(home / f".config/{project}/upstream-report.env")}

        def tool(name: str, env: dict, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
            done = subprocess.run([sys.executable, str(ROOT / "scripts" / name), *args], capture_output=True, text=True,
                                  timeout=90, env=env, input=stdin)
            outputs.append(done.stdout + done.stderr)
            return done

        def digest_of(done: subprocess.CompletedProcess[str]) -> str:
            for line in done.stdout.splitlines():
                if line.strip().startswith(("digest:", "  digest:")):
                    return line.split()[1]
            return ""

        mefp_env = pane_env("mefp", "sapi")

        def write(*args: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess[str]:
            return tool("ticket-board-write", env or mefp_env, *args, stdin=stdin)

        def report_args(title: str, to: str = "", ref: str = "mefp:MEFP-1") -> list[str]:
            return ["file-report", "--title", title, "--body", "What failed, in the library and the platform.",
                    "--external-source-ref", ref, *(["--to", to] if to else [])]

        def file(title: str, to: str = "", env: dict | None = None, ref: str = "mefp:MEFP-1"):
            preview = write(*report_args(title, to, ref), env=env)
            return preview, write(*report_args(title, to, ref), "--submit-upstream", "--expect", digest_of(preview),
                                  env=env)

        # 1 + 7. pie: connected to Switchyard before ancestry; files exactly as before, then after migration --------
        sinks.record_report_outcome("pie", sinks.CONNECTED, board="syrd", url=urls["syrd"], registry_dir=registry,
                                    print_func=outputs_root)
        pie = Tenant("pie", user, upstream_report_url=urls["syrd"])
        check(ur.refresh_upstream_report_credential(pie, **roots, **bound, print_func=outputs_root) == [],
              "pie holds Switchyard's credential, bound to pie")
        pie = replace(pie, upstream_report_token_file=str(home / ".config/pie/upstream-report.env"))
        pie_env = pane_env("pie", "syrd")
        listed = tool("ticket-board-read", pie_env, "report-ancestors", "--json")
        legacy = json.loads(listed.stdout) if listed.returncode == 0 else {}
        check([e["project"] for e in legacy.get("ancestors", [])] == ["syrd"]
              and legacy["ancestors"][0]["recorded_as"] == "connected",
              f"before migration, pie's parent is its one connection: {listed.stdout[-300:]}{listed.stderr[-300:]}")
        preview, done = file("Pie before migration", env=pie_env, ref="pie:PIE-1")
        check(preview.returncode == 0 and "file-report PREVIEW: nothing has been filed." in preview.stdout
              and "Switchyard (syrd), tickets SYRD-N" in preview.stdout and done.returncode == 0,
              f"pie previews and files to Switchyard as before: {preview.stdout[-300:]}{done.stderr[-300:]}")
        pie_first = json.loads(done.stdout).get("id", "") if done.returncode == 0 else ""
        check(report_parent.migrate(pie, **roots, **bound, print_func=outputs_root) == []
              and reg("pie").get(ra.PARENT_KEY, {}).get("project") == "syrd"
              and reg("pie")[ra.HISTORY_KEY][-1]["reason"] == "migrated from its one upstream connection"
              and (home / ".config/pie/report-credentials/syrd.env").is_file(),
              f"a full upgrade migrates pie's connection into its declared parent: {said[-4:]}")
        check(report_parent.migrate(pie, **roots, **bound, print_func=outputs_root) == []
              and len(reg("pie").get(ra.HISTORY_KEY, [])) == 1, "and migrating again changes nothing")
        _preview, done = file("Pie after migration", env=pie_env, ref="pie:PIE-2")
        pie_second = json.loads(done.stdout).get("id", "") if done.returncode == 0 else ""
        shape = "SELECT state || ' ' || assignee || ' ' || origin_project FROM ticket_board.tickets WHERE id = '{}';"
        check(pie_first and pie_second and sql("syrd", shape.format(pie_first)) == sql("syrd", shape.format(pie_second))
              == "analysis unassigned pie",
              f"after migration pie's report lands exactly as before: {pie_first} {pie_second}")
        follow = ["report-follow-up", "--ticket", pie_first, "--text", "A single-parent follow-up."]
        preview = tool("ticket-board-write", pie_env, *follow)
        sent = tool("ticket-board-write", pie_env, *follow, "--submit-upstream", "--expect", digest_of(preview))
        check(sent.returncode == 0 and any(c.startswith("reporter:pie: A single-parent follow-up.")
                                           for c in comments("syrd", pie_first)),
              f"a single-parent follow-up works as before: {sent.stderr[-300:]}")

        # 2. mefp lists its ancestors -------------------------------------------------------------------------------
        listed = tool("ticket-board-read", mefp_env, "report-ancestors")
        text = listed.stdout
        check(listed.returncode == 0 and "1. Stellaris API (sapi, tickets SAPI-N) -- immediate parent" in text
              and "2. Switchyard (syrd, tickets SYRD-N)" in text and text.count("identity verified") == 2
              and text.count("usable, bound to mefp") == 2 and "All ancestors (Stellaris API, Switchyard)" in text
              and "http://" not in text, f"report-ancestors names both, nearest first, by name: {text}{listed.stderr}")
        answer = json.loads(tool("ticket-board-read", mefp_env, "report-ancestors", "--json").stdout)
        check([(e["project"], e["ticket_prefix"], e["identity_verified"], e["reachable"], e["credential"]["usable"])
               for e in answer["ancestors"]] == [("sapi", "SAPI", True, True, True), ("syrd", "SYRD", True, True, True)]
              and answer["immediate_parent"]["project"] == "sapi",
              f"and as JSON: {answer}")

        # 8. no fan-out because ancestry exists ------------------------------------------------------------------------
        counts = (count("sapi"), count("syrd"))
        _preview, done = file("Plain report")
        check(done.returncode == 0 and (count("sapi"), count("syrd")) == (counts[0] + 1, counts[1]),
              f"without --to, one ticket on the immediate parent and none elsewhere: {done.stderr[-300:]}")

        # 2. one ancestor at a time, by project, name or prefix --------------------------------------------------------
        preview, done = file("SAPI only", to="sapi")
        check("Destination: Stellaris API. This would create 1 NEW ticket on ANOTHER project's board:" in preview.stdout
              and done.returncode == 0
              and count("sapi", "SAPI only") == 1 and count("syrd", "SAPI only") == 0,
              f"--to sapi files on SAPI only: {preview.stdout[-500:]}{done.stderr[-300:]}")
        named = [digest_of(write(*report_args("Switchyard only", to))) for to in ("syrd", "Switchyard", "syrd")]
        by_prefix = digest_of(write(*report_args("Switchyard only", "SYRD")))
        check(len(set(named + [by_prefix])) == 1 and named[0], "--to resolves by project, name and ticket prefix alike")
        _preview, done = file("Switchyard only", to="Switchyard")
        check(done.returncode == 0 and count("syrd", "Switchyard only") == 1 and count("sapi", "Switchyard only") == 0,
              f"--to Switchyard files on Switchyard only: {done.stderr[-300:]}")

        # 3. all ancestors: one exact confirmation, one correlated ticket on each board ---------------------------------
        title = "Both boards"
        preview = write(*report_args(title, "all"))
        digest = digest_of(preview)
        check(preview.returncode == 0 and "Destination: All ancestors (Stellaris API, Switchyard). This would create 2 "
              "NEW tickets" in preview.stdout and "1. Stellaris API (sapi, tickets SAPI-N): a new ticket" in preview.stdout
              and "2. Switchyard (syrd, tickets SYRD-N): a new ticket" in preview.stdout and "http://" not in preview.stdout
              and count("sapi", title) == count("syrd", title) == 0,
              f"the preview names every destination and how many tickets, and files nothing: {preview.stdout}")
        wrong = write(*report_args(title, "all"), "--submit-upstream", "--expect", "0" * 24)
        check(wrong.returncode != 0 and "is not the report that was previewed" in wrong.stderr
              and count("sapi", title) == count("syrd", title) == 0, f"a wrong digest files nothing: {wrong.stderr}")
        done = write(*report_args(title, "all"), "--submit-upstream", "--expect", digest)
        result = json.loads(done.stdout) if done.returncode == 0 else {}
        group = result.get("report_group", "")
        tickets = {d["project"]: d["ticket_id"] for d in result.get("destinations", [])}
        check(done.returncode == 0 and [d["outcome"] for d in result["destinations"]] == ["created", "created"]
              and count("sapi", title) == count("syrd", title) == 1,
              f"one ticket on each board: {done.stdout[-400:]}{done.stderr[-400:]}")
        for slug in ("sapi", "syrd"):
            check(sql(slug, f"SELECT origin_project || ' ' || external_source_ref FROM ticket_board.tickets WHERE id = "
                            f"'{tickets[slug]}';") == "mefp mefp:MEFP-1"
                  and any(f"Report group {group}: mefp filed this report as one of a group" in c
                          for c in comments(slug, tickets[slug]))
                  and sql(slug, f"SELECT report_group FROM ticket_board.report_confirmations WHERE ticket_id = "
                                f"'{tickets[slug]}';") == group,
                  f"{slug}'s ticket carries its origin, source and the shared report group, recorded and said")
        receipt_path = pane / ".local/state/switchyard/mefp/report-receipts" / f"{group}.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {"destinations": {}}
        check({slug: entry.get("ticket_id") for slug, entry in receipt["destinations"].items()} == tickets
              and oct(receipt_path.stat().st_mode & 0o777) == "0o600",
              f"the reporting project keeps a receipt per destination, owner-only: {receipt}")
        again = write(*report_args(title, "all"), "--submit-upstream", "--expect", digest)
        check(again.returncode == 0 and [d["outcome"] for d in json.loads(again.stdout)["destinations"]] ==
              ["already", "already"] and count("sapi", title) == count("syrd", title) == 1,
              "the same confirmation again creates nothing")
        _preview, done = file("Plain first, then all")
        preview = write(*report_args("Plain first, then all", "all"))
        check("This would create 1 NEW ticket" in preview.stdout and "already filed as SAPI-" in preview.stdout,
              f"a report filed to the parent alone is known when it is later filed to all: {preview.stdout}")
        done = write(*report_args("Plain first, then all", "all"), "--submit-upstream", "--expect", digest_of(preview))
        check(done.returncode == 0 and count("sapi", "Plain first, then all") == 1
              and count("syrd", "Plain first, then all") == 1, "and only the missing board gets it")

        # 4. a failure after the first destination, then a confirmed retry ------------------------------------------
        refuse = "ON FUNCTION ticket_board.file_confirmed_report(text, text, text, text, text, text, text)"
        sql("syrd", f"REVOKE EXECUTE {refuse} FROM ticket_board_service;")
        title = "Partial filing"
        preview = write(*report_args(title, "all"))
        digest = digest_of(preview)
        partial = write(*report_args(title, "all"), "--submit-upstream", "--expect", digest)
        check(partial.returncode != 0 and "1 of 2 destination(s) took it; 1 FAILED" in partial.stderr
              and "Stellaris API (sapi): created SAPI-" in partial.stderr and "Switchyard (syrd): FAILED" in partial.stderr
              and "files only where the report is missing" in partial.stderr
              and count("sapi", title) == 1 and count("syrd", title) == 0,
              f"a partial result is said exactly, and the command fails: {partial.stderr[-900:]}")
        sql("syrd", f"GRANT EXECUTE {refuse} TO ticket_board_service;")
        retry = write(*report_args(title, "all"), "--submit-upstream", "--expect", digest)
        outcomes = [d["outcome"] for d in json.loads(retry.stdout)["destinations"]] if retry.returncode == 0 else []
        check(outcomes == ["already", "created"] and count("sapi", title) == 1 and count("syrd", title) == 1,
              f"the confirmed retry completes only the missing board: {retry.stdout[-300:]}{retry.stderr[-300:]}")
        receipt_path = pane / ".local/state/switchyard/mefp/report-receipts" / \
            f"{json.loads(retry.stdout)['report_group']}.json"
        receipt_path.unlink(missing_ok=True)
        lost = write(*report_args(title, "all"), "--submit-upstream", "--expect", digest)
        check(lost.returncode == 0 and [d["outcome"] for d in json.loads(lost.stdout)["destinations"]] ==
              ["already", "already"] and count("sapi", title) == count("syrd", title) == 1,
              "with the receipt lost, each board answers with the ticket it already created")

        # 5. follow-ups: one correlated ticket, or all of them -------------------------------------------------------
        group_args = ["report-follow-up", "--group", group, "--kind", "correction"]
        preview = write(*group_args, "--to", "all", "--text", "The platform half is a timeout.")
        check(preview.returncode == 0 and f"1. {tickets['sapi']} on Stellaris API" in preview.stdout
              and f"2. {tickets['syrd']} on Switchyard" in preview.stdout and "http://" not in preview.stdout,
              f"a follow-up preview names each ticket: {preview.stdout}{preview.stderr}")
        sent = write(*group_args, "--to", "all", "--text", "The platform half is a timeout.", "--submit-upstream",
                     "--expect", digest_of(preview))
        mine = "reporter:mefp: Correction from the reporter: The platform half is a timeout."
        check(sent.returncode == 0 and all(sum(c.startswith(mine) for c in comments(s, tickets[s])) == 1
                                           for s in tickets),
              f"one follow-up on each correlated ticket: {sent.stderr[-400:]} {comments('syrd', tickets['syrd'])}")
        again = write(*group_args, "--to", "all", "--text", "The platform half is a timeout.", "--submit-upstream",
                      "--expect", digest_of(preview))
        check(again.returncode == 0 and all(sum(c.startswith(mine) for c in comments(s, tickets[s])) == 1
                                            for s in tickets) and "already there" in again.stderr,
              "the same follow-up again adds nothing")
        one = ["report-follow-up", "--group", group, "--to", "sapi", "--text", "Only SAPI needs this."]
        sent = write(*one, "--submit-upstream", "--expect", digest_of(write(*one)))
        check(sent.returncode == 0 and any("Only SAPI needs this." in c for c in comments("sapi", tickets["sapi"]))
              and not any("Only SAPI needs this." in c for c in comments("syrd", tickets["syrd"])),
              "a follow-up to one destination reaches that one")
        by_ticket = ["report-follow-up", "--to", "syrd", "--ticket", tickets["syrd"], "--text", "By ticket."]
        sent = write(*by_ticket, "--submit-upstream", "--expect", digest_of(write(*by_ticket)))
        check(sent.returncode == 0 and any("By ticket." in c for c in comments("syrd", tickets["syrd"])),
              f"or to one ancestor's ticket by ID: {sent.stderr[-300:]}")
        foreign = write("report-follow-up", "--to", "syrd", "--ticket", pie_first, "--text", "Not mine.")
        check(foreign.returncode != 0 and f"{pie_first} on Switchyard was filed from pie" in foreign.stderr
              and not any("Not mine." in c for c in comments("syrd", pie_first)),
              f"another project's report is refused: {foreign.stderr[-300:]}")
        refused = write("report-follow-up", "--to", "all", "--ticket", tickets["syrd"], "--text", "x")
        check(refused.returncode != 0 and "a ticket ID belongs to one board" in refused.stderr,
              "--to all with one ticket ID is refused")
        sql("syrd", "REVOKE EXECUTE ON FUNCTION ticket_board.append_report_follow_up(text, text, text, text, text, "
                    "text) FROM ticket_board_service;")
        words = ["--to", "all", "--text", "Resumable follow-up."]
        preview = write(*group_args, *words)
        partial = write(*group_args, *words, "--submit-upstream", "--expect", digest_of(preview))
        sql("syrd", "GRANT EXECUTE ON FUNCTION ticket_board.append_report_follow_up(text, text, text, text, text, "
                    "text) TO ticket_board_service;")
        retry = write(*group_args, *words, "--submit-upstream", "--expect", digest_of(preview))
        check(partial.returncode != 0 and "1 FAILED" in partial.stderr and retry.returncode == 0
              and [d["outcome"] for d in json.loads(retry.stdout)["destinations"]] == ["already", "added"]
              and all(sum("Resumable follow-up." in c for c in comments(s, tickets[s])) == 1 for s in tickets),
              f"a partial follow-up is said, and its retry completes it once: {partial.stderr[-400:]}")
        plain = ["report-follow-up", "--ticket", tickets["sapi"], "--text", "Single-parent path, grouped report."]
        sent = write(*plain, "--submit-upstream", "--expect", digest_of(write(*plain)))
        check(sent.returncode == 0, f"the single-parent follow-up still works on a grouped report: {sent.stderr[-300:]}")

        # 6. refused before anything is written ---------------------------------------------------------------------
        def refused_before(label: str, words: str, to: str = "all") -> None:
            counts = (count("sapi"), count("syrd"))
            done = write(*report_args(f"Refused: {label}", to))
            check(done.returncode != 0 and words in done.stderr and "Nothing was sent." in done.stderr
                  and (count("sapi"), count("syrd")) == counts, f"{label} is refused before writes: {done.stderr}")

        refused_before("a non-ancestor", "is not one of this project's report ancestors", to="pie")
        record("sapi", name="Switchyard", report_parent=reg("sapi")[ra.PARENT_KEY])
        refused_before("an ambiguous name", "names more than one of", to="Switchyard")
        record("sapi", report_parent=reg("sapi")[ra.PARENT_KEY])
        held = home / ".config/mefp/report-credentials/syrd.env"
        kept = held.read_bytes()
        held.unlink()
        refused_before("a missing credential", "Switchyard (syrd): mefp holds no usable credential for its board (missing)")
        held.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={rf.bound_token(SECRETS['syrd'][1], 'pie')}\n")
        refused_before("another project's credential", "bound to pie, not mefp")
        held.write_bytes(kept)
        mefp_record = reg("mefp")
        record("mefp", report_parent={**mefp_record[ra.PARENT_KEY], "url": urls["pie"]},
               report_parent_history=mefp_record[ra.HISTORY_KEY], report_credentials=mefp_record[ra.CREDENTIALS_KEY])
        refused_before("an identity mismatch", "the board recorded for sapi answers as 'pie'")
        record("mefp", report_parent={**mefp_record[ra.PARENT_KEY], "ticket_prefix": "SAPX"},
               report_parent_history=mefp_record[ra.HISTORY_KEY], report_credentials=mefp_record[ra.CREDENTIALS_KEY])
        refused_before("a ticket prefix other than root recorded", "names ticket prefix 'SAPI', and root recorded 'SAPX'")
        record("mefp", report_parent={**mefp_record[ra.PARENT_KEY], "url": urls["syrd"]},
               report_parent_history=mefp_record[ra.HISTORY_KEY], report_credentials=mefp_record[ra.CREDENTIALS_KEY])
        refused_before("two links to one board", "sapi and syrd are recorded with the same board")
        (registry / "mefp.json").write_text(json.dumps(mefp_record))
        record("syrd", report_parent={"project": "syrd", "url": urls["syrd"], "ticket_prefix": "SYRD"})
        refused_before("a project that is its own parent", "syrd names itself as its own report parent")
        record("syrd", report_parent={"project": "mefp", "url": urls["mefp"], "ticket_prefix": "MEFP"})
        refused_before("a cycle", "its report ancestry is a cycle: mefp -> sapi -> syrd -> mefp")
        cyclic = tool("ticket-board-read", mefp_env, "report-ancestors")
        check("the chain stops here: its report ancestry is a cycle" in cyclic.stdout, "report-ancestors says so too")
        record("syrd")

        # The board page's read: the same answer, credentials as root's records say, nothing secret.
        with urllib.request.urlopen(urls["mefp"] + "/api/report-ancestry", timeout=20) as response:
            page = json.loads(response.read().decode())
        outputs.append(json.dumps(page))
        check([(e["project"], e["identity_verified"], e["credential"].get("issued")) for e in page["ancestors"]] ==
              [("sapi", True, True), ("syrd", True, True)]
              and not any("usable" in e["credential"] for e in page["ancestors"])
              and page["ancestors"][0]["credential"]["state"].startswith("issued by root at"),
              f"the board page's read lists both ancestors: {page}")
        with urllib.request.urlopen(urls["mefp"] + "/", timeout=20) as response:
            html = response.read().decode()
        check('id="reportAncestry"' in html and "/api/report-ancestry" in html
              and "run ticket-board-read report-ancestors in a pane" in html,
              "the board page shows it, and points to the pane's live check for usability")

        # A parent change and a removal erase nothing ------------------------------------------------------------
        syrd_link = ra.Link("syrd", "Switchyard", urls["syrd"], "SYRD", "declared")
        check(report_parent.record_parent("mefp", syrd_link, registry_dir=registry, reason="test change",
                                          print_func=outputs_root) == ""
              and [h["to"] for h in reg("mefp")[ra.HISTORY_KEY]] == ["sapi", "syrd"]
              and [link.project for link in ra.chain("mefp", registry)[0]] == ["syrd"],
              "a parent change is recorded with its history")
        refused_before("a former ancestor", "is not one of this project's report ancestors", to="sapi")
        former = ["report-follow-up", "--group", group, "--to", "sapi", "--text", "After the parent changed."]
        sent = write(*former, "--submit-upstream", "--expect", digest_of(write(*former)))
        check(sent.returncode == 0 and any("After the parent changed." in c for c in comments("sapi", tickets["sapi"])),
              f"a report filed before the change is still followed up through its recorded credential: "
              f"{sent.stderr[-300:]}")
        mefp_config = scratch / "mefp.config.json"
        mefp_config.write_text(json.dumps({"project": "mefp", "board_url": urls["mefp"], "run_as_user": user,
                                           "upstream_report_url": urls["syrd"],
                                           "upstream_report_token_file": mefp.upstream_report_token_file}))
        check(report_parent.remove(mefp, config_path=mefp_config, registry_dir=registry, dry_run=True,
                                   print_func=outputs_root) == [] and reg("mefp")[ra.PARENT_KEY]["project"] == "syrd",
              "removing a parent dry-runs")
        check(report_parent.remove(mefp, config_path=mefp_config, registry_dir=registry, print_func=outputs_root) == []
              and reg("mefp")[ra.PARENT_KEY]["project"] == "" and len(reg("mefp")[ra.HISTORY_KEY]) == 3
              and "upstream_report_url" not in json.loads(mefp_config.read_text())
              and (home / ".config/mefp/report-credentials/sapi.env").is_file() and receipt_path.parent.is_dir(),
              "removing the parent declares none and unlinks the configuration, and keeps credentials and receipts")
        refused_before("no parent", "root's records name no report parent for mefp")
        after = ["report-follow-up", "--group", group, "--to", "all", "--text", "After the parent was removed."]
        sent = write(*after, "--submit-upstream", "--expect", digest_of(write(*after)))
        check(sent.returncode == 0, f"and its reports can still be followed up: {sent.stderr[-300:]}")

        # Digest compatibility, and a malformed group --------------------------------------------------------------
        main_tree = tree_at(base_commit(), scratch / "main")
        old = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0, sys.argv[1]); "
                              "from scripts.ticket_board import report_confirmation as rc; "
                              "print(rc.report_digest(destination={'project': 'syrd', 'ticket_prefix': 'SYRD'}, "
                              "source_project='pie', title='T', body='B', requested_stage='backlog', "
                              "external_source_ref='r'))", str(main_tree)], capture_output=True, text=True)
        check(old.stdout.strip() == rc.report_digest(destination={"project": "syrd", "ticket_prefix": "SYRD"},
                                                     source_project="pie", title="T", body="B",
                                                     requested_stage="backlog", external_source_ref="r"),
              "a report with no group confirms exactly as main's did, so an older client still files")
        same = {"destination": {"project": "syrd", "ticket_prefix": "SYRD"}, "source_project": "pie", "title": "T",
                "body": "B"}
        check(len({rc.report_digest(**same), rc.report_digest(**same, report_group="rg-" + "1" * 20),
                   rc.report_digest(**same, report_group="rg-" + "2" * 20)}) == 3,
              "the report group is bound into a report's confirmation")
        block = (ROOT / "scripts/ticket_board/migrations/pgu1023_syrd721_report_groups.sql").read_text()
        block = block.split("BEGIN;\n", 1)[1].split("-- schema.sql stops here")[0].replace("-- SYRD-721 (pgu1023):",
                                                                                          "-- SYRD-721:")
        check(block in (ROOT / "scripts/ticket_board/schema.sql").read_text(),
              "schema.sql carries the migration's report-group block word for word")
        request = urllib.request.Request(urls["syrd"] + "/api/tickets/actions/file_report", method="POST",
                                         data=json.dumps({"title": "x", "body": "y", "origin_project": "pie",
                                                          "confirmation": "a" * 24, "report_group": "rg-nothex"}).encode(),
                                         headers={"Content-Type": "application/json", t.REPORT_TOKEN_HEADER:
                                                  rf.bound_token(SECRETS["syrd"][1], "pie")})
        try:
            urllib.request.urlopen(request, timeout=10)
            malformed = 0
        except urllib.error.HTTPError as exc:
            malformed = exc.code
        check(malformed == 400, f"a malformed report group is refused: {malformed}")

        # The upgrade from main's schema ----------------------------------------------------------------------------
        upgraded = build("upgraded", main_tree)
        service = t.conninfo(cluster.socket_dir, cluster.port, "upgraded", t.SERVICE_ROLE)
        t.psql(service, "SELECT * FROM ticket_board.file_confirmed_report('Before', 'B', 'pie', '', '', '" + "c" * 24
               + "');")
        migrate(upgraded, ROOT)
        definition = ("SELECT pg_get_functiondef('ticket_board.file_confirmed_report(text, text, text, text, text, "
                      "text, text)'::regprocedure);")
        column = ("SELECT column_default || ' ' || is_nullable FROM information_schema.columns WHERE table_schema = "
                  "'ticket_board' AND table_name = 'report_confirmations' AND column_name = 'report_group';")
        check(t.psql(upgraded, definition) == sql("syrd", definition) and t.psql(upgraded, column) == sql("syrd", column),
              "the upgraded board has exactly what a new board has")
        check(t.psql(service, "SELECT created::text FROM ticket_board.file_confirmed_report('Before', 'B', 'pie', '', "
                               "'', '" + "c" * 24 + "');") == "false"
              and t.psql(upgraded, "SELECT report_group FROM ticket_board.report_confirmations;") == "",
              "a confirmation from before the upgrade still replays, with no group")

        for server in servers:
            server.shutdown()
            server.server_close()

        # No token anywhere --------------------------------------------------------------------------------------
        tokens = [value for pair in SECRETS.values() for value in pair]
        tokens += [rf.bound_token(SECRETS[s][1], p) for s in SECRETS for p in ("mefp", "pie", "sapi")]
        written = [p.read_text() for p in (pane / ".local/state").rglob("*.json")] + \
                  [p.read_text() for p in registry.iterdir()]
        leaked = [token[:12] for token in tokens if any(token in text for text in outputs + said + written)]
        check(leaked == [], f"no token in any output, receipt or registry record: {leaked}")
        if os.environ.get("SYRD721_DUMP"):  # what each command printed, for a reader of this suite
            Path(os.environ["SYRD721_DUMP"]).write_text("\n=====\n".join(outputs + said))
    print(f"report_ancestry_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
