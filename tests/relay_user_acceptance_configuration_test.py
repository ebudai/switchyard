#!/usr/bin/env python3
"""SYRD-535: a workflow declared after pgu953 can be given the User's acceptance relay, and only that.

MEFP's Director relayed a User's acceptance as the Director skill documents --
`workflow-action MEFP-649 relay_user_sign_off` -- and was refused: MEFP's
workflow (revision 66) declares no relay. pgu953 (SYRD-217) grants the relay by
migration to the document a board has when the migration runs, and MEFP
declared its workflow later; a migration does not run twice. Its User rejects
by `user_reopen`, which a relay cannot mirror (a relay only returns or
approves), so there is no rejection relay to add at all.

`ticket-board-write add-user-acceptance-relay` previews (by default) and, with
the revision the preview showed, applies exactly pgu953's transition, copied
from the User's own sign-off; nothing else in the document changes. It then
works as the relay always has: a no-code acceptance on a commit-exempt ticket
names no commit, a coded one names the recorded candidate, and earlier reviews
still gate it.

A production-built board (companion roles, schema.sql, the real
ticket-board-migrate, rbac.sql) with a declared workflow shaped like MEFP's --
the User signs off or reopens -- driven through the real CLI and HTTP server.
The reproduction runs main's code and SQL in a child, from a git worktree.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "b93c85824f14db327ce367cebc6f9d808775bec0"  # main before SYRD-535
COMMIT, OTHER = "6d4ee1aa99147e8118f59e637be02b660d62d064", "7e5ff2bb00258f9229ad8f748c16073f1d3a1e75"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def mefp_shaped(document: dict) -> dict:
    """The pre-relay workflow with MEFP's User stage: the User signs off, or reopens to analysis."""
    doc = copy.deepcopy(document)
    doc["transitions"] = [t for t in doc["transitions"] if t.get("action") != "user_kick_back"]
    # MEFP's own user_reopen at revision 66, field for field.
    doc["transitions"].append({"from": "user_review", "to": "analysis", "action": "user_reopen", "label": "Reopen",
                               "actors": ["user"], "primitive": "reopen", "owner_scoped": False,
                               "allow_no_code": False, "clear_signoffs": [], "require_commit": False,
                               "require_reason": False, "relays_decision_of": None})
    return doc


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        db = "relay"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        app._resolve_known_commit = lambda value: (COMMIT if value == COMMIT else OTHER, [])
        # Declared AFTER the migrations ran, as MEFP's was: pgu953 never saw it.
        doc = mefp_shaped(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        # Through the same apply MEFP's revisions took, so what is stored is the
        # normalized document a live board holds, not the raw era fixture.
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cli(*argv: str) -> tuple[int, str]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    code = write_cli.main(["--board-url", f"http://127.0.0.1:{server.server_port}", "--caller-role",
                                           "director", f"--write-token={server.write_token}", *argv])
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue()

        def workflow() -> dict:
            return app.workflow_configuration_payload() if hasattr(app, "workflow_configuration_payload") else {
                "revision": json.loads(t.psql(admin, "SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;").strip() or "0"),
                "document": json.loads(t.psql(admin, "SELECT document::text FROM ticket_board.workflow_configuration WHERE singleton;")),
            }

        def attempt(fn) -> str:
            try:
                fn()
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

        def ticket(tid: str, **flags) -> None:
            t.seed_postgres_ticket(admin, tid, title=tid, state="user_review", assignee="user", needs_user_signoff=True, **flags)

        def state(tid: str) -> dict:
            got = app.get_ticket(tid)
            return {k: got[k] for k in ("state", "assignee", "user_signoff", "commit_hash")} | {
                "relayed": any("relayed this decision from user" in c["text"] for c in got["comments"])}

        try:
            # MEFP-649's shape: a no-code coordination decision.
            ticket("PGU-1", commit_exempt=True, needs_audit=False)
            code, said = cli("workflow-action", "PGU-1", "relay_user_sign_off", "--payload-json",
                             json.dumps({"reason": "User accepted the small pilot"}))
            seen["absent"] = {"code": code, "said": said[-300:], "ticket": state("PGU-1")}
            before = workflow()
            code, said = cli("add-user-acceptance-relay")
            seen["preview"] = {"code": code, "said": said, "revision_after": workflow()["revision"], "revision": before["revision"]}
            code, said = cli("add-user-acceptance-relay", "--apply", "--expected-revision", str(before["revision"] - 1))
            seen["stale apply"] = {"code": code, "said": said[-300:], "revision_after": workflow()["revision"]}
            code, said = cli("add-user-acceptance-relay", "--apply", "--expected-revision", str(before["revision"]))
            after = workflow()
            seen["apply"] = {"code": code, "said": said, "revision_after": after["revision"],
                             "added": [x for x in after["document"]["transitions"] if x not in before["document"]["transitions"]],
                             "removed": [x for x in before["document"]["transitions"] if x not in after["document"]["transitions"]],
                             "rest_equal": {k: v for k, v in after["document"].items() if k != "transitions"}
                             == {k: v for k, v in before["document"].items() if k != "transitions"}}
            code, said = cli("add-user-acceptance-relay")
            seen["again"] = {"code": code, "said": said}
            # No-code acceptance, as the guide shows it: a reason and no commit_hash at all.
            code, said = cli("workflow-action", "PGU-1", "relay_user_sign_off", "--payload-json",
                             json.dumps({"reason": "User accepted the small pilot", "commit_hash": ""}))
            seen["no-code with an empty commit"] = {"code": code, "said": said, "ticket": state("PGU-1")}
            code, said = cli("workflow-action", "PGU-1", "relay_user_sign_off", "--payload-json",
                             json.dumps({"reason": "User accepted the small pilot"}))
            seen["no-code"] = {"code": code, "said": said[-300:], "ticket": state("PGU-1")}
            # A coded ticket names its recorded candidate, and earlier reviews still gate it.
            ticket("PGU-2", commit_hash=COMMIT, needs_audit=True, audit_signoff=True)
            seen["coded, wrong candidate"] = attempt(lambda: app.perform_workflow_action(
                "PGU-2", "relay_user_sign_off", {"reason": "accepted", "commit_hash": OTHER}, caller_role="director"))
            seen["coded, wrong candidate ticket"] = state("PGU-2")
            seen["coded"] = attempt(lambda: app.perform_workflow_action(
                "PGU-2", "relay_user_sign_off", {"reason": "accepted", "commit_hash": COMMIT}, caller_role="director"))
            seen["coded ticket"] = state("PGU-2")
            ticket("PGU-3", commit_hash=COMMIT, needs_audit=True, audit_signoff=False)
            seen["earlier review missing"] = attempt(lambda: app.perform_workflow_action(
                "PGU-3", "relay_user_sign_off", {"reason": "accepted", "commit_hash": COMMIT}, caller_role="director"))
            seen["earlier review ticket"] = state("PGU-3")
            # The User's reopen stays the User's, and there is no rejection relay.
            ticket("PGU-4", commit_exempt=True, needs_audit=False)
            seen["director reopens"] = attempt(lambda: app.perform_workflow_action(
                "PGU-4", "user_reopen", {"reason": "user said no"}, caller_role="director"))
            seen["rejection relay"] = attempt(lambda: app.perform_workflow_action(
                "PGU-4", "relay_user_kick_back", {"reason": "user said no"}, caller_role="director"))
            seen["rejected ticket"] = state("PGU-4")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return seen


def documented_commands() -> list[tuple[str, str]]:
    """Every `ticket-board-write` line in the relay guidance, joined across its backslashes."""
    found = []
    for doc in (ROOT / "skills/switchyard-director/SKILL.md", ROOT / "docs/onboarding/switchyard-director-guide.md"):
        text = doc.read_text(encoding="utf-8").replace("\\\n", " ")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ticket-board-write ") and ("relay_user" in line or "add-user-acceptance-relay" in line):
                found.append((doc.name, line.split("  #")[0].strip()))
    return found


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print("RESULT " + json.dumps(scenario(Path(sys.argv[2]), "syrd535b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd535-before.") as raw:
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), BEFORE], check=True)
        try:
            # The scenario is this file's; the code and SQL it drives are main's.
            (before_tree / "tests" / Path(__file__).name).write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(before_tree / "tests" / Path(__file__).name), "--scenario", str(before_tree)],
                                  text=True, capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        before = json.loads(line[len("RESULT "):])
    check(before["absent"]["code"] != 0 and before["absent"]["ticket"]["state"] == "user_review"
          and "relay_user_sign_off" in before["absent"]["said"],
          f"reproduced: on a workflow declared after pgu953 the acceptance relay is refused: {before['absent']}")
    check(before["preview"]["code"] != 0 and "invalid choice" in before["preview"]["said"],
          f"reproduced: and nothing a Director can run adds it: {before['preview']['said'][-300:]}")

    now = scenario(ROOT, "syrd535a-")
    check(now["absent"]["code"] != 0 and "director cannot call relay_user_sign_off" in now["absent"]["said"]
          and now["absent"]["ticket"]["state"] == "user_review" and not now["absent"]["ticket"]["user_signoff"],
          f"absent relay: refused, nothing recorded: {now['absent']}")
    def answer(said: str) -> dict:
        try:
            return json.loads(said)
        except ValueError:
            return {"refused": said.strip()[-300:]}

    preview = answer(now["preview"]["said"])
    check(now["preview"]["code"] == 0 and preview.get("applied") is False and preview.get("validated") is True
          and preview["added"]["action"] == "relay_user_sign_off" and preview["added"]["relays_decision_of"] == "user"
          and preview["added"]["to"] == "director_review" and preview["added"]["require_commit"] is True
          and preview["revision"] == now["preview"]["revision"] == now["preview"]["revision_after"]
          and preview["before_digest"] != preview["after_digest"]
          and f"--expected-revision {preview['revision']}" in preview["apply_with"],
          f"the preview shows the one transition, validates it on the board, and writes nothing: {preview}")
    check("user_reopen" in preview.get("rejection", "") and "only return or approve" in preview["rejection"],
          f"and says there is no rejection relay to add where the User reopens: {preview['rejection']}")
    check(now["stale apply"]["code"] != 0 and "not the" in now["stale apply"]["said"]
          and now["stale apply"]["revision_after"] == now["preview"]["revision"],
          f"an apply against a revision that was not the one reviewed is refused: {now['stale apply']}")
    applied = now["apply"]
    # Revisions are drawn from a sequence, so the dry runs before it consume
    # numbers too: what matters is that it moved and what moved with it.
    check(applied["code"] == 0 and applied["revision_after"] > now["preview"]["revision"]
          and applied["added"] == [preview["added"]] and applied["removed"] == [] and applied["rest_equal"],
          f"applying adds exactly that transition and changes nothing else: {applied}")
    again = answer(now["again"]["said"])
    check(again.get("added") is None and "already relayable" in again.get("reason", ""), f"and a second run adds nothing: {again}")
    empty = now["no-code with an empty commit"]
    check(empty["code"] != 0 and "invalid commit hash" in empty["said"] and empty["ticket"]["state"] == "user_review",
          f"a no-code acceptance with an empty commit_hash is refused: {empty}")
    nocode = now["no-code"]
    check(nocode["code"] == 0 and nocode["ticket"]["state"] == "director_review" and nocode["ticket"]["user_signoff"] is True
          and nocode["ticket"]["relayed"],
          f"a no-code acceptance with a reason and no commit_hash is the User's sign-off, entered by the Director: {nocode}")
    check("must name the recorded candidate" in now["coded, wrong candidate"]
          and now["coded, wrong candidate ticket"]["state"] == "user_review",
          f"a coded acceptance naming another commit is refused: {now['coded, wrong candidate']}")
    check(now["coded"] == "ok" and now["coded ticket"]["state"] == "director_review" and now["coded ticket"]["user_signoff"],
          f"naming the recorded candidate, it is accepted: {now['coded']} {now['coded ticket']}")
    check("earlier reviews" in now["earlier review missing"] and now["earlier review ticket"]["state"] == "user_review",
          f"earlier reviews still gate it: {now['earlier review missing']}")
    check(now["director reopens"] != "ok" and now["rejection relay"] != "ok" and now["rejected ticket"]["state"] == "user_review",
          f"the User's reopen stays the User's, and there is no rejection relay: {now['director reopens']} / {now['rejection relay']}")
    # The rule itself, where the board fixture cannot reach: each reason pgu953
    # declines is a reason this declines, and nothing is added.
    from scripts.ticket_board import workflow_relays as relays
    sys.path.insert(0, str(ROOT / "tests"))
    from workflow_document_eras import before_relaying

    base = mefp_shaped(before_relaying())
    granted, _, _ = relays.with_user_acceptance_relay(base)
    variants = {
        "two control roles": lambda d: d["roles"].append({**next(r for r in d["roles"] if r["name"] == "director"),
                                                           "name": "deputy", "target": "x:0.0"}),
        "the User has a pane": lambda d: next(r for r in d["roles"] if r["name"] == "user").update(target="p-user:0.0"),
        "the approval ends the ticket": lambda d: next(s for s in d["stages"] if s["name"] == "director_review").update(terminal=True),
        "no User approval": lambda d: d.update(transitions=[t for t in d["transitions"] if t.get("action") != "user_sign_off"]),
        "already granted": lambda d: d.update(transitions=granted["transitions"]),
        "the action name is taken": lambda d: d["transitions"].append({"from": "analysis", "to": "in_progress",
                                                                       "action": "relay_user_sign_off", "actors": ["director"]}),
    }
    because = {"two control roles": "2 active roles hold the control capabilities", "the User has a pane": "pane of its own",
               "the approval ends the ticket": "ends the ticket", "no User approval": "no approval in user_review",
               "already granted": "already relayable", "the action name is taken": "already names an action"}
    for label, change in variants.items():
        doc = copy.deepcopy(base)
        change(doc)
        updated, relay, reason = relays.with_user_acceptance_relay(doc)
        check(updated is None and relay is None and because[label] in reason, f"{label}: declined, for that reason: {reason}")
    returning = copy.deepcopy(before_relaying())
    check("could mirror that move" in relays.rejection_relay_finding(returning)
          and "only return or approve" in relays.rejection_relay_finding(base),
          "a User who returns work could have a rejection relay; one who reopens cannot")

    # The guidance is commands; each one parses with the real CLI, and the no-code
    # acceptance is the one this suite ran: a reason and no commit_hash at all.
    sys.path.insert(0, str(ROOT))
    import shlex

    from scripts.ticket_board import write_cli

    commands = documented_commands()
    check({name for name, _ in commands} == {"SKILL.md", "switchyard-director-guide.md"} and len(commands) >= 8,
          f"both documents carry the relay commands: {commands}")
    for name, line in commands:
        argv = shlex.split(line.replace("<revision from the preview>", "7").replace("<revision>", "7"))[1:]
        args = write_cli._build_parser().parse_args(argv)
        if args.command == "workflow-action":
            payload = json.loads(args.payload_json)
            check(payload.get("reason"), f"{name}: every relay carries the reason: {line}")
            if "no-code" in line or "commit_hash" not in payload:
                check("commit_hash" not in payload, f"{name}: a no-code acceptance names no commit: {line}")
    nocode = [line for _, line in commands if "relay_user_sign_off" in line and "commit_hash" not in line]
    check(len(nocode) == 2, f"each document shows the no-code acceptance: {nocode}")
    print(f"relay_user_acceptance_configuration_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
