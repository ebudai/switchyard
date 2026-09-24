#!/usr/bin/env python3
"""SYRD-262: root's bounded rebind of a declared workflow's pane roles.

MEFP's revision-3 declaration named Claude for a Director and Ops that run
Codex. The board serves a role's assignment, and grants its process any
authority, only when the declared runtime and target equal what the pane
registered -- so both were hidden, and the Director, who alone may configure
the workflow, could not correct it. Nobody may act as the Director to do so.

The repair is `switchyard rebind-workflow-panes`: root, through the database
owner's connection deploys already use, changes only runtime/target/slot of
roles that already exist, to values derived from root's verified tenant
configuration, against the exact revision and document it previewed.

This drives it against a disposable board whose socket takes roles from
processes, with Director and Ops panes that are real processes in an isolated
tmux, registered as Codex the way the launcher registers them:

  * before the rebind, the Director pane's director write is refused;
  * the real command, with its SQL executed by real psql, rebinds exactly
    those two roles, attributed, with the old document kept;
  * after it, the SAME pane's director write succeeds -- no restart;
  * stages, transitions, tickets and every other role are untouched;

and it pins what must stay impossible: the function is unreachable from the
board's own database role, a stale or edited document is refused, and no
field but runtime/target/slot, no unknown role, no foreign target and no
unknown runtime gets through. A privileged child runs the command's real root
prologue -- root's plan and verified tenant configuration -- against the same
fixtures `adopt-workflow` is tested with.
"""

from __future__ import annotations

import copy
import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import legacy_workflow_equivalence_test as equivalence  # noqa: E402
import team_launcher as tl  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from ticket_board import legacy_workflow as lw  # noqa: E402
from ticket_board import project_provision as pv  # noqa: E402
from ticket_board.peer_identity import read_process  # noqa: E402
from ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer  # noqa: E402
from ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "mefp"
CHECKS = 0
FUNCTION = "ticket_board.rebind_declared_pane_roles"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tenant_config(scratch: Path):
    """MEFP's four panes as its configuration runs them: Codex Director and Ops."""
    path = scratch / "tenant" / f"{PROJECT}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "project": PROJECT,
        "layout": "layout.json",
        "board_url": "http://127.0.0.1:1/",
        "board_socket": str(scratch / "run" / "board.sock"),
        "role_state_isolation": True,
        "roles": [
            {"role": role, "cli": [f"/usr/local/bin/{cli}"], "slot": slot,
             "workdir": str(scratch / "worktrees" / role)}
            # MEFP's four-pane window: slots 0-3, as the tenant ran it before
            # the migration copied the example's 1/2/5/4 over them.
            for role, cli, slot in (("director", "codex", 0), ("main", "codex", 1),
                                    ("ops", "codex", 2), ("audit", "claude", 3))
        ],
    }), encoding="utf-8")
    return path, tl.load_project_config(PROJECT, path)


def revision_three(config) -> dict:
    """The live defect: correct document, except Director and Ops declared as Claude,
    and a designer bound to a pane that does not exist -- as MEFP's revision 3 is."""
    plan = equivalence.mefp_plan()
    good = lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(ROOT),
        stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan),
        panes={role.role: tl.role_pane_declaration(role) for role in config.roles},
    ).document
    bad = copy.deepcopy(good)
    example_slots = {"director": 1, "main": 2, "ops": 5, "audit": 4}
    for role in bad["roles"]:
        if role["name"] in {"director", "ops"}:
            role["runtime"] = "claude"
        if role["name"] in example_slots:
            role["slot"] = example_slots[role["name"]]
        if role["name"] == "designer":
            role["runtime"], role["target"] = "claude", f"{PROJECT}-designer:0.0"
    return validate(bad, project=PROJECT)


def admin_json(board, sql: str):
    return json.loads(t.psql(board.admin, sql) or "null")


def snapshot(board) -> dict:
    """Everything a rebind must not touch."""
    return admin_json(board, """
SELECT jsonb_build_object(
  'stages', (SELECT jsonb_agg(to_jsonb(s) ORDER BY s.rank) FROM ticket_board.workflow_stages s),
  'transitions', (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.from_stage, x.to_stage, x.action_name)
                  FROM ticket_board.workflow_transitions x),
  'tickets', (SELECT coalesce(jsonb_agg(to_jsonb(k) ORDER BY k.id), '[]') FROM ticket_board.tickets k),
  'revisions', (SELECT count(*) FROM ticket_board.workflow_revisions));""")


def live_state(board) -> tuple[int, dict]:
    row = admin_json(board, "SELECT jsonb_build_object('revision', revision, 'document', document) "
                            "FROM ticket_board.workflow_configuration WHERE singleton;")
    return int(row["revision"]), row["document"]


def call_rebind(board, conninfo: str, revision, document, bindings, why="SYRD-262 test"):
    """Call the function exactly as the command does: psql variables, -f -."""
    return subprocess.run(
        ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-v", f"rev={revision}",
         "-v", f"expected={json.dumps(document, sort_keys=True)}",
         "-v", f"bindings={json.dumps(bindings, sort_keys=True)}", "-v", f"why={why}",
         conninfo, "-f", "-"],
        input=f"SELECT {FUNCTION}(:rev, :'expected'::jsonb, :'bindings'::jsonb, :'why');\n",
        text=True, capture_output=True, check=False,
    )


class Tmux:
    """Our own tmux server; -S and no TMUX, so nothing here reaches a live one."""

    def __init__(self, socket: Path, env: dict[str, str]) -> None:
        self.socket, self.env = socket, env

    def run(self, *args: str) -> str:
        return subprocess.run(["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
                              env=self.env, capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def pane(self, name: str, command: str) -> int:
        self.run("new-session", "-d", "-s", name, "-x", "80", "-y", "24", command)
        return int(self.run("display-message", "-p", "-t", f"{name}:0.0", "#{pane_pid}"))

    def kill(self) -> None:
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"], env=self.env,
                       capture_output=True, check=False)


def wait_for(path: Path, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} never appeared")
        time.sleep(0.1)


DIRECTOR_WRITE = """
import json, sys, urllib.request
sys.path.insert(0, sys.argv[1])
from scripts.ticket_board.write_client import TicketBoardWriteClient
board_url, socket_path = sys.argv[2], sys.argv[3]
current = json.load(urllib.request.urlopen(board_url + "/api/workflow"))
client = TicketBoardWriteClient(board_url=board_url, socket_path=socket_path, caller_role="director")
try:
    client.configure_workflow(current["document"], expected_revision=current["revision"], dry_run=True)
except Exception as exc:
    print("REFUSED", exc)
    raise SystemExit(1)
print("DIRECTOR-WRITE-OK")
"""


class Journal:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed: dict = {}
        self.operator = None

    def open(self) -> None:
        pass

    def write(self, _stream: str, text: str) -> None:
        self.written.append(text)

    def close(self, **kwargs) -> dict:
        self.closed = kwargs
        return kwargs


OPERATOR = types.SimpleNamespace(name="eric", source="pkexec", known=True)


INTENDED = {"director": "codex", "ops": "codex"}


def run_command(board, plan, verified, config, *, apply=False, expect="", journal=None,
                euid=0, operator=OPERATOR, resolver=True, runtimes=INTENDED, slots=None):
    said: list[str] = []
    ran: list[list[str]] = []
    record_at_sql: list[str] = []

    def sql_runner(args, **kwargs):
        # What the trusted record says at the moment the board is written.
        record_at_sql.append(Path(verified).read_text())
        # The command's own argv and stdin, executed by real psql. Only the
        # privilege hop and the host's admin URL are swapped for the test's.
        ran.append(list(args))
        check(args[:4] == ["sudo", "-u", "postgres", "psql"], f"the command runs psql as postgres: {args}")
        real = [a if a != plan.admin_database_url else board.admin for a in args[3:]]
        return subprocess.run(real, **kwargs)

    code = tl.switchyard_rebind_workflow_panes_command(
        PROJECT, apply=apply, expect=expect, runtimes=runtimes, slots=slots,
        euid_getter=lambda: euid, operator_resolver=lambda: operator,
        tenant_resolver=(lambda *a, **k: (plan, verified, config)) if resolver else (lambda *a, **k: None),
        board_reader=lambda _c: live_state(board) + ("",),
        registrations_reader=lambda _p: (admin_json(board, (
            "SELECT coalesce(json_agg(json_build_object('role', role, 'runtime', runtime, "
            "'target', actual_target, 'pid', process_pid, 'start_time', process_start_time) "
            "ORDER BY role), '[]') FROM ticket_board.role_runtime_assignments;")), ""),
        sql_runner=sql_runner, journal=journal or Journal(), print_func=said.append,
    )
    run_command.record_at_sql = record_at_sql
    return code, "\n".join(said), ran


def test_the_migration_carries_the_schema_definition_exactly() -> None:
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    migration = (ROOT / "scripts/ticket_board/migrations/pgu958_syrd262_rebind_pane_roles.sql").read_text()
    start = schema.index(f"CREATE OR REPLACE FUNCTION {FUNCTION}(")
    end_marker = f"REVOKE ALL ON FUNCTION {FUNCTION}(bigint, jsonb, jsonb, text) FROM PUBLIC;\n"
    block = schema[start:schema.index(end_marker, start) + len(end_marker)]
    check(block in migration, "the migration copy is the schema's, character for character")
    rbac = (ROOT / "scripts/ticket_board/rbac.sql").read_text()
    check("rebind_declared_pane_roles" not in rbac, "and no role is ever granted it")


def main_board_cases() -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    with temporary_cluster(prefix="syrd262-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd262-") as raw:
        scratch = Path(raw)
        config_path, config = tenant_config(scratch)
        plan = equivalence.mefp_plan()
        # The tenant here is this test's own account: it owns the files root
        # will verify and replace.
        plan = types.SimpleNamespace(**{**plan.__dict__, "admin_database_url": "postgresql:///host-admin",
                                        "owner_user": account})
        board = equivalence.Board(cluster, "rebind")
        socket_path = Path(config.board_socket)
        unix = TicketBoardUnixServer(socket_path, board.app, events=board.server.events,
                                     director_notifier=t.QuietNotifier(),
                                     role_authority=ProcessRoleAuthority(board.app, account))
        threading.Thread(target=unix.serve_forever, daemon=True).start()
        tmux = None
        try:
            # MEFP's history, in order: the legacy seed; its panes start and
            # register on that board, which takes a first registration's
            # runtime as the role's binding; THEN revision 3 lands on top and
            # rebinds them to the example's Claude. A registration against
            # revision 3 itself would have been refused.
            t.psql(board.admin, pv.render_workflow_sql(equivalence.mefp_plan()))

            # Real panes, registered as the launcher registers them.
            env = {k: v for k, v in os.environ.items() if k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
            env.update({"PATH": "/usr/bin:/bin", "HOME": str(scratch), "LANG": "C.UTF-8"})
            tmux = Tmux(scratch / "tmux.sock", env)
            driver = scratch / "director_write.py"
            driver.write_text(DIRECTOR_WRITE)
            base = board.base
            for role in config.roles:
                if role.role not in {"director", "ops"}:
                    continue
                runtime, target = tl.role_runtime_binding(role)
                steps = "; ".join(
                    f"while [ ! -e {scratch}/go-{role.role}-{n} ]; do sleep 0.1; done; "
                    f"{sys.executable} -I {driver} {ROOT} {base} {socket_path} "
                    f">{scratch}/out-{role.role}-{n} 2>&1; echo $? >{scratch}/rc-{role.role}-{n}"
                    for n in (1, 2)
                )
                pid = tmux.pane(f"{PROJECT}-{role.role}", f"sh -c '{steps}; sleep 600'")
                started = read_process(pid)
                board.app.register_runtime_assignment(
                    role=role.role, runtime=runtime, target=target,
                    worktree=role.workdir, session_dir=str(scratch / "sessions" / role.role),
                    process_pid=pid, process_start_time=started.start_time,
                    process_uid=os.getuid(), expected_generation=0,
                )
            with board.app._pg_connect() as conn:
                board.app._pg_set_caller_role(conn, "director")
                conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)",
                             (json.dumps(revision_three(config)),))
            revision, document = live_state(board)

            # ...and what `workflow_manage apply` then did to the tenant: kept the
            # projection it replaced in its rollback journal, and rewrote the
            # configuration FROM the declaration -- so the trusted record says
            # Claude too, and a restart would launch Claude (SYRD-262).
            from scripts.workflow_launcher import projection_files

            projected = projection_files(config_path, document)
            (config_path.parent / "workflow-before-0.json").write_text(json.dumps({
                "previous": {"revision": 0, "document": None},
                "desired": document,
                "previous_files": {str(path): (path.read_text() if path.exists() else None)
                                   for path in projected},
            }), encoding="utf-8")
            for path, text in projected.items():
                path.write_text(text, encoding="utf-8")
            config = tl.load_project_config(PROJECT, config_path)
            check({r.role: tl.role_runtime_binding(r)[0] for r in config.roles}["director"] == "claude",
                  "the defect reached the trusted record: the configuration now says claude")

            # The live preview's failure: nothing differs between the record
            # and the declaration, while the live panes are hidden. That must
            # be a refusal, not "nothing to do".
            code, said, ran = run_command(board, plan, config_path, config, runtimes={})
            check(code == 1 and not ran and "refusing" in said and "divergent        director" in said,
                  f"a no-op over divergent live panes is refused: {said}")

            served = set(board.app.runtime_targets())
            check("director" not in served and "ops" not in served,
                  f"the defect: the Codex panes are registered and hidden: {served}")

            def director_write(n: int) -> tuple[str, str]:
                (scratch / f"go-director-{n}").touch()
                wait_for(scratch / f"rc-director-{n}")
                return ((scratch / f"rc-director-{n}").read_text().strip(),
                        (scratch / f"out-director-{n}").read_text())

            rc, said = director_write(1)
            check(rc == "1" and "REFUSED" in said, f"and the Director's own pane has no authority: {said}")

            # -- security: what the function refuses --------------------------
            service = t.conninfo(cluster.socket_dir, cluster.port, "rebind", t.SERVICE_ROLE)
            good = {"director": {"runtime": "codex", "target": f"{PROJECT}-director:0.0", "slot": 1}}
            before = snapshot(board)
            for label, conninfo, rev, doc, bindings, why, expect in (
                ("the board's own database role", service, revision, document, good, "x", "permission denied"),
                ("a stale revision", board.admin, revision - 1, document, good, "x", "changed since it was reviewed"),
                ("an edited document", board.admin, revision, {**document, "queue": {}}, good, "x",
                 "changed since it was reviewed"),
                ("a capability", board.admin, revision, document,
                 {"director": {**good["director"], "capabilities": ["configure_workflow"]}}, "x",
                 "exactly runtime, target and slot"),
                ("a missing field", board.admin, revision, document,
                 {"director": {"runtime": "codex", "target": f"{PROJECT}-director:0.0"}}, "x",
                 "exactly runtime, target and slot"),
                ("a new role", board.admin, revision, document,
                 {"intruder": {"runtime": "codex", "target": f"{PROJECT}-intruder:0.0", "slot": 3}}, "x",
                 "no declared role"),
                ("an unknown runtime", board.admin, revision, document,
                 {"director": {**good["director"], "runtime": "bash"}}, "x", "unknown runtime"),
                ("another project's pane", board.admin, revision, document,
                 {"director": {**good["director"], "target": "syrd-director:0.0"}}, "x",
                 "is not a pane of this project"),
                ("a slot off the screen", board.admin, revision, document,
                 {"director": {**good["director"], "slot": 9}}, "x", "invalid visible slot"),
                ("a target another role holds", board.admin, revision, document,
                 {"director": {**good["director"], "target": f"{PROJECT}-main:0.0"}}, "x",
                 "active pane targets must be unique"),
                ("no reason", board.admin, revision, document, good, "  ", "must say what it repairs"),
            ):
                done = call_rebind(board, conninfo, rev, doc, bindings, why)
                check(done.returncode != 0 and expect in done.stderr, f"{label} is refused: {done.stderr}")
            check(snapshot(board) == before and live_state(board)[0] == revision,
                  "and none of those wrote anything")

            # -- the command: preview, then apply ------------------------------
            verified = config_path
            code, said, ran = run_command(board, plan, verified, config)
            check(code == 0 and not ran, f"a preview writes nothing: {said}")
            check("director: runtime claude -> codex" in said and "ops: runtime claude -> codex" in said, said)
            # Revision 3's projection also put a designer pane into the tenant's
            # configuration. It is not the operator's to change here, and a
            # rebind never adds or removes roles: it is left exactly as it is.
            check("designer" not in "".join(l for l in said.splitlines() if l.startswith("  changes")),
                  f"a role the operator did not name is not rebound: {said}")
            check("operator         director: runtime codex (the operator's decision)" in said, said)
            check("evidence       configuration now: runtime claude, slot 1" in said,
                  f"the record's value is shown: {said}")
            check("tenant journal before its last workflow write: runtime codex, slot 0 (in workflow-before-0.json)" in said,
                  f"the tenant's pre-migration choice is shown as evidence: {said}")
            check("evidence       live registration: codex/mefp-director:0.0" in said, said)
            sections: dict[str, list[str]] = {}
            current = ""
            for line in said.splitlines():
                if line.startswith("  | --- "):
                    current = line[len("  | --- "):]
                elif line.startswith("  | ") and current and line[4:5] in "+-" and not line[4:].startswith("+++"):
                    sections.setdefault(current, []).append(line[4:])
            check(sections.get("declared") and all('"runtime"' in l for l in sections["declared"]),
                  f"the document diff touches only runtime: {sections.get('declared')}")
            config_diff = sections.get(str(config_path)) or []
            check(config_diff and all(any(k in l for k in ('"runtime"', '"cli"', '"live_commands"', '"codex"', '"claude"'))
                                      for l in config_diff),
                  f"and the configuration's diff only what follows the runtime: {config_diff}")
            check("matches, so it is served as soon as the rebind lands -- no restart" in said,
                  f"and says the live panes will be served without a restart: {said}")
            digest = said.split("--expect ")[-1].strip()

            code, said, ran = run_command(board, plan, verified, config, apply=True)
            check(code == 1 and not ran, f"apply without the reviewed digest is refused: {said}")
            code, said, ran = run_command(board, plan, verified, config, apply=True, expect="0" * 64)
            check(code == 1 and not ran, f"and with a digest that is not this rebind: {said}")
            code, said, ran = run_command(board, plan, verified, config, apply=True, euid=1000)
            check(code == 1 and not ran and "pkexec" in said, said)
            code, said, ran = run_command(board, plan, verified, config, apply=True, expect=digest,
                                          operator=types.SimpleNamespace(name="x", source="sudo", known=True))
            check(code == 1 and not ran, f"an unrecorded elevation is refused: {said}")
            code, said, ran = run_command(board, plan, verified, config, apply=True, expect=digest,
                                          resolver=False)
            check(code == 1 and not ran, f"so is an unverified configuration: {said}")

            for runtimes, expected in (({"intruder": "codex"}, "is not a pane role"),
                                       ({"director": "bash"}, "is not a runtime")):
                code, said, ran = run_command(board, plan, verified, config, runtimes=runtimes)
                check(code == 1 and not ran and expected in said, f"{runtimes} is refused: {said}")
            # The digest covers the files: one edited after the preview is not
            # the one that was reviewed.
            original = config_path.read_text()
            config_path.write_text(original.replace('"project"', ' "project"', 1))
            code, said, ran = run_command(board, plan, verified, config, apply=True, expect=digest)
            check(code == 1 and not ran and "is not what this would write now" in said,
                  f"a record edited since the preview is refused: {said}")
            config_path.write_text(original)

            journal = Journal()
            before = snapshot(board)
            record_before = config_path.stat()
            code, said, ran = run_command(board, plan, verified, config, apply=True, expect=digest,
                                          journal=journal)
            check(code == 0 and len(ran) == 1, f"the reviewed rebind is applied: {said}")
            at_sql = json.loads(run_command.record_at_sql[0])
            check({r["role"]: r["cli"][0] for r in at_sql["roles"]}["director"] == "codex",
                  "the trusted record was reconciled BEFORE the board was written")
            sent = json.loads(next(a for a in ran[0] if a.startswith("bindings="))[len("bindings="):])
            check(set(sent) == {"director", "ops"}
                  and all(set(b) == {"runtime", "target", "slot"} for b in sent.values()),
                  f"the bindings it sends are runtime/target/slot of the two pane roles only: {sent}")
            check(json.loads(next(a for a in ran[0] if a.startswith("expected="))[len("expected="):]) == document,
                  "and the document it swaps against is the one it previewed")
            new_revision, rebound = live_state(board)
            after = snapshot(board)
            check(after["stages"] == before["stages"] and after["transitions"] == before["transitions"]
                  and after["tickets"] == before["tickets"],
                  "stages, transitions and tickets are exactly as they were")
            old_roles = {r["name"]: r for r in document["roles"]}
            for role in rebound["roles"]:
                old = old_roles[role["name"]]
                changed = {k for k in set(role) | set(old) if role.get(k) != old.get(k)}
                allowed = {"runtime"} if role["name"] in {"director", "ops"} else set()
                check(changed == allowed, f"{role['name']} changed {changed}")
            actor = admin_json(board, "SELECT to_jsonb(actor) FROM ticket_board.workflow_revisions "
                                      "ORDER BY revision DESC LIMIT 1;")
            check(actor.startswith("rebind: SYRD-262 pane rebind by eric"), f"attributed: {actor}")
            kept = admin_json(board, f"SELECT document FROM ticket_board.workflow_revisions WHERE revision={revision};")
            check(kept == document, "the previous document is kept as rollback evidence")
            evidence = json.loads(journal.written[0])
            check(evidence["document"] == document and evidence["rebound"] == rebound
                  and journal.closed.get("status") == "succeeded", "and in root's journal too")
            check(str(config_path) in evidence["files"]
                  and '"claude"' in evidence["files"][str(config_path)]["before"],
                  "with the configuration's previous content, to put back if needed")
            reconciled = {r.role: tl.role_runtime_binding(r)[0]
                          for r in tl.load_project_config(PROJECT, config_path).roles}
            check(reconciled["director"] == "codex" and reconciled["ops"] == "codex"
                  and reconciled["main"] == "codex" and reconciled["audit"] == "claude",
                  f"the trusted record now says what runs, so a restart launches Codex: {reconciled}")
            record_after = config_path.stat()
            check((record_after.st_uid, record_after.st_gid, record_after.st_mode)
                  == (record_before.st_uid, record_before.st_gid, record_before.st_mode),
                  "and the tenant's file keeps its owner and mode")

            served = set(board.app.runtime_targets())
            check({"director", "ops"} <= served, f"the Codex panes are served now: {served}")
            rc, said = director_write(2)
            check(rc == "0" and "DIRECTOR-WRITE-OK" in said,
                  f"and the SAME Director pane has its authority back, with no restart: {said}")

            # -- the slots, which the same defect copied from the example --------
            # After the runtime repair the declaration and the reconciled record
            # still say 1/2/5/4; the four-pane window shows 0-3. The operator
            # states the layout; the runtimes just repaired must not move.
            layout = {"director": 0, "main": 1, "ops": 2, "audit": 3}
            config = tl.load_project_config(PROJECT, config_path)
            for slots, expected in (({"director": 7}, "is not a visible slot"),
                                    ({"intruder": 0}, "is not a pane role"),
                                    ({"director": 2}, "visible slots must be unique")):
                code, said, ran = run_command(board, plan, verified, config, runtimes={}, slots=slots)
                check(code == 1 and not ran and expected in said, f"{slots} is refused: {said}")
            code, said, ran = run_command(board, plan, verified, config, runtimes={}, slots=layout)
            check(code == 0 and not ran, f"the slot preview writes nothing: {said}")
            check("director: slot 1 -> 0" in said and "ops: slot 5 -> 2" in said and "audit: slot 4 -> 3" in said,
                  f"each move is shown: {said}")
            check("operator         director: slot 0 (the operator's decision)" in said,
                  f"as the operator's decision, not an inference: {said}")
            check("tenant journal before its last workflow write: runtime codex, slot 2 (in workflow-before-0.json)" in said,
                  f"with the pre-migration layout as evidence: {said}")
            sections = {}
            current = ""
            for line in said.splitlines():
                if line.startswith("  | --- "):
                    current = line[len("  | --- "):]
                elif line.startswith("  | ") and current and line[4:5] in "+-" and not line[4:].startswith("+++"):
                    sections.setdefault(current, []).append(line[4:])
            check(sections.get("declared") and all('"slot"' in l for l in sections["declared"]),
                  f"the document diff touches only slots: {sections.get('declared')}")
            check(all('"slot"' in l for l in sections.get(str(config_path)) or ['"slot"']),
                  f"and so does the record's: {sections.get(str(config_path))}")
            slot_review = said.split("--expect ")[-1].strip()
            before = snapshot(board)
            code, said, ran = run_command(board, plan, verified, config, runtimes={}, slots=layout,
                                          apply=True, expect=slot_review)
            check(code == 0 and len(ran) == 1, f"the reviewed layout is applied: {said}")
            _, placed = live_state(board)
            declared = {r["name"]: r for r in placed["roles"]}
            check({name: declared[name]["slot"] for name in layout} == layout,
                  f"the declaration shows the four-pane layout: {[(n, declared[n]['slot']) for n in layout]}")
            check(declared["director"]["runtime"] == "codex" and declared["ops"]["runtime"] == "codex",
                  "and the runtime repair is untouched")
            after = snapshot(board)
            check(after["stages"] == before["stages"] and after["transitions"] == before["transitions"]
                  and after["tickets"] == before["tickets"], "stages, transitions and tickets unchanged")
            record = {r.role: (tl.role_runtime_binding(r)[0], r.slot)
                      for r in tl.load_project_config(PROJECT, config_path).roles}
            check({k: record[k] for k in layout} == {"director": ("codex", 0), "main": ("codex", 1),
                                                      "ops": ("codex", 2), "audit": ("claude", 3)},
                  f"the trusted record agrees, runtimes kept: {record}")
            check({"director", "ops"} <= set(board.app.runtime_targets()),
                  "and the live panes are still served")
            config = tl.load_project_config(PROJECT, config_path)
            # From here the board's current state is the slot repair's.
            new_revision, rebound = live_state(board)
            sent = {name: {field: declared[name][field] for field in ("runtime", "target", "slot")}
                    for name in ("director", "ops")}

            code, said, ran = run_command(board, plan, verified, config)
            check(code == 0 and "already match" in said and not ran, f"a rerun has nothing to do: {said}")
            check(live_state(board)[0] == new_revision, "and writes no new revision")
            again = call_rebind(board, board.admin, new_revision, rebound, sent)
            check(again.returncode == 0 and again.stdout.strip() == str(new_revision),
                  f"the same bindings again return the same revision: {again.stdout} {again.stderr}")
            check(live_state(board)[0] == new_revision, "and the function itself writes nothing new")

            # A database that says yes while the board shows otherwise is not a
            # rebind; a database that refuses is journaled as failed. Both are
            # driven from a board reader frozen at revision 3, previewed first
            # so the digest is the one that state produces.
            stale_state = (revision, document)

            def faked(sql_result, *, apply, expect=""):
                said_fake: list[str] = []
                journal = Journal()
                code = tl.switchyard_rebind_workflow_panes_command(
                    PROJECT, apply=apply, expect=expect, runtimes=INTENDED, euid_getter=lambda: 0,
                    operator_resolver=lambda: OPERATOR,
                    tenant_resolver=lambda *a, **k: (plan, verified, tl.load_project_config(PROJECT, config_path)),
                    board_reader=lambda _c: stale_state + ("",),
                    registrations_reader=lambda _p: ([], ""),
                    sql_runner=lambda args, **k: sql_result(args),
                    journal=journal, print_func=said_fake.append,
                )
                return code, said_fake, journal

            _, preview, _ = faked(lambda args: None, apply=False)
            stale_review = next(l for l in preview if "--expect " in l).split("--expect ")[-1].strip()
            code, said_fake, journal = faked(lambda args: subprocess.CompletedProcess(args, 0, "", ""),
                                             apply=True, expect=stale_review)
            check(code == 1 and any("the board now serves" in l for l in said_fake),
                  f"a write the board does not show is reported, not claimed: {said_fake}")
            check(journal.closed.get("status") == "failed", f"and journaled as failed: {journal.closed}")

            code, said_fake, journal = faked(
                lambda args: subprocess.CompletedProcess(args, 3, "", "ERROR: workflow changed since it was reviewed"),
                apply=True, expect=stale_review)
            check(code == 1 and "refused the rebind" in "\n".join(said_fake), said_fake[-1])
            check(journal.closed.get("status") == "failed", f"a refusal is journaled as failed: {journal.closed}")

            # Root never follows a link the tenant put where its record was.
            elsewhere = config_path.with_name("elsewhere.json")
            config_path.rename(elsewhere)
            config_path.symlink_to(elsewhere)
            try:
                code, said, ran = run_command(board, plan, verified, config, apply=True, expect=digest)
                check(code == 1 and not ran and "symlink" in said,
                      f"a configuration that became a link is refused, nothing written: {said}")
            finally:
                config_path.unlink()
                elsewhere.rename(config_path)
        finally:
            if tmux is not None:
                tmux.kill()
            unix.shutdown()
            unix.server_close()
            board.close()


def privileged_prologue_cases() -> None:
    """The command's real root prologue, as uid 0, on adopt-workflow's fixtures.

    Everything above injects the resolver. This is the shipped default: root's
    plan rebuilt from root's own record, and the tenant configuration only if
    root verifies it -- which a configuration anybody in its group could have
    rewritten is not.
    """
    import adopt_workflow_test as adopt

    # The module object the fixtures patch: `scripts.team_launcher`, not the
    # top-level `team_launcher` this file imports for everything else.
    launcher = adopt.launcher

    with tempfile.TemporaryDirectory(prefix="syrd262-privileged.") as raw:
        root = Path(raw)
        root.chmod(0o755)
        os.environ[adopt.SEAM] = str(root)
        os.environ[adopt.JOURNAL_ENV] = str(root / "journal")
        os.environ["PKEXEC_UID"] = "0"
        os.environ.pop("SUDO_USER", None)
        _release, home, _installed, tenant_plan, config_path = adopt.adopting_fixture(root)
        registry = root / "registry"
        registry.mkdir(exist_ok=True)
        said: list[str] = []
        # adopt-workflow's own sequence: root's baseline rebuilt for this
        # tenant first, then the command as root would run it.
        with adopt.owner_patches(home), adopt.tenant_identity(home, owner=adopt.TENANT):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                adopt.SLUG, config_path.parent,
                {"workflow": tenant_plan.workflow, "port": tenant_plan.port}, source_repo=_release,
            )
        check(rebuilt is not None, f"root's baseline for the fixture: {problem}")
        with adopt.owner_patches(home):
            resolved = launcher.root_verified_tenant(adopt.SLUG, registry_dir=registry, print_func=said.append)
        check(resolved is not None, f"root's own plan and a verified configuration: {said}")
        plan, verified, config = resolved
        check(plan.project == adopt.SLUG and config.project == adopt.SLUG, f"{plan.project} {config.project}")
        check(Path(verified).resolve() == config_path.resolve(), f"the configuration root verified: {verified}")

        # A mode its group could rewrite is repaired by root before the strict
        # check (normalize_tenant_config_mode, SYRD-167), then trusted.
        mode = config_path.stat().st_mode
        config_path.chmod(mode | 0o020)
        with adopt.owner_patches(home):
            repaired = launcher.root_verified_tenant(adopt.SLUG, registry_dir=registry, print_func=said.append)
        check(repaired is not None and not config_path.stat().st_mode & 0o022,
              f"root repairs a writable mode, then verifies: {oct(config_path.stat().st_mode)}")

        # What root must never follow: the configuration swapped for a link.
        real = config_path.with_name("elsewhere.json")
        config_path.rename(real)
        config_path.symlink_to(real)
        said.clear()
        try:
            with adopt.owner_patches(home):
                refused = launcher.root_verified_tenant(adopt.SLUG, registry_dir=registry, print_func=said.append)
                command_said: list[str] = []
                code = launcher.switchyard_rebind_workflow_panes_command(
                    adopt.SLUG, euid_getter=lambda: 0, operator_resolver=lambda: adopt.OPERATOR,
                    registry_dir=registry, print_func=command_said.append,
                    board_reader=lambda _c: (_ for _ in ()).throw(AssertionError("read the board")),
                )
        finally:
            config_path.unlink()
            real.rename(config_path)
        check(refused is None and "symlink" in " ".join(said),
              f"a configuration that is a link somewhere else is not one root trusts: {said}")
        check(code == 1 and "Nothing was changed" in "\n".join(command_said),
              f"so the command refuses before it reads the board: {command_said}")


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_prologue_cases()
        print(f"workflow_pane_rebind_test: privileged child ran {CHECKS} checks")
        return 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    main_board_cases()
    child = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
        text=True, capture_output=True, check=False,
    )
    if child.returncode != 0 or "privileged child ran" not in child.stdout:
        print(child.stdout + child.stderr)
        return 1
    print(child.stdout.strip())
    print(f"workflow_pane_rebind_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
