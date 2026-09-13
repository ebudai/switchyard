#!/usr/bin/env python3
"""SYRD-118: a published verdict has to be something the board saw.

Publication request 17 was recorded published while the ref it named was on
neither GitHub nor the trusted commit cache, so the implementer was told to
submit a commit the board would then refuse. The record and the submittable
commit disagreed because `resolve_publication` accepted 'published' on the
control role's word alone.

What counts as proof here is narrow on purpose. The trusted publication path
writes `refs/remotes/origin/<ref>` into the tenant's commit cache, and it does
that only after the privileged publisher has pushed and read the exact commit
back from the public remote. `refs/heads/<ref>` in the same repository is not
proof of anything: `switchyard-request-publication` creates it locally, before
anyone has decided, so accepting it would prove only that the implementer asked.

Real cluster, real HTTP handler, real git repositories, and a git shim that
fails the test if the check ever reaches for a network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from publication_cache_fixture import GIT as REAL_GIT
from publication_cache_fixture import build_cache, claim, commit_file, publish
from scripts.ticket_board.app import PUBLISHED_REF_NAMESPACE
from scripts.ticket_board.workflow_config import validate
from temporary_cluster import temporary_cluster

BUNDLE = "/home/agent/.local/state/switchyard/publish-outbox/syrd/PGU-1.bundle"
GIT_LOG_ENV = "SYRD118_GIT_LOG"
NETWORK_SUBCOMMANDS = ("ls-remote", "fetch", "push", "clone", "remote")

GIT_SHIM = """#!/usr/bin/env python3
import os, subprocess, sys

argv = sys.argv[1:]
with open(os.environ["{log_env}"], "a", encoding="utf-8") as handle:
    handle.write("\\x1f".join(argv) + "\\n")
raise SystemExit(subprocess.run(["{real_git}", *argv]).returncode)
"""


def install_git_shim(root: Path) -> Path:
    log = root / "git-argv.log"
    shim_dir = root / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(GIT_SHIM.format(log_env=GIT_LOG_ENV, real_git=REAL_GIT), encoding="utf-8")
    shim.chmod(0o755)
    log.write_text("", encoding="utf-8")
    os.environ[GIT_LOG_ENV] = str(log)
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"
    return log


def git_calls(log: Path) -> list[list[str]]:
    return [line.split("\x1f") for line in log.read_text(encoding="utf-8").splitlines() if line]


def requests_for(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) "
        f"FROM ticket_board.publication_requests r WHERE ticket_id = '{ticket}';",
    )
    return json.loads(raw)


def open_request(admin: str, ticket: str) -> dict:
    rows = [row for row in requests_for(admin, ticket) if row["state"] == "requested"]
    assert len(rows) == 1, rows
    return rows[0]


def queued_for(admin: str, role: str) -> list[str]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(message ORDER BY id), '[]'::jsonb) "
        f"FROM ticket_board.ticket_notification_queue WHERE target_role = '{role}' "
        "AND kind = 'publication';",
    )
    return json.loads(raw)


def main() -> int:
    with temporary_cluster(prefix="publication-proof-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "publication_proof_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        for ticket in ("PGU-1", "PGU-2", "PGU-3", "PGU-4"):
            t.seed_postgres_ticket(admin, ticket, title=f"{ticket} work", state="in_progress", assignee="main")
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)

        cache, work = build_cache(root)
        first = commit_file(work, "first")
        second = commit_file(work, "second")
        log = install_git_shim(root)

        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            commit_git_dir=str(cache),
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            t.post_json(
                base,
                "/api/tickets/actions/configure_workflow",
                {"document": cfg, "expected_revision": 0},
                caller="director",
            )

            def ask(ticket: str, ref: str, commit: str) -> dict:
                t.post_json(
                    base,
                    f"/api/tickets/{ticket}/actions/request_publication",
                    {"ref": ref, "commit": commit, "bundle": BUNDLE},
                    caller="main",
                )
                return open_request(admin, ticket)

            def record(ticket: str, request: dict, outcome: str, detail: str, expect: int | None = None):
                return t.post_json(
                    base,
                    f"/api/tickets/{ticket}/actions/resolve_publication",
                    {"request_id": request["id"], "outcome": outcome, "detail": detail},
                    caller="director",
                    **({"expect": expect} if expect else {}),
                )

            # 1. Request 17's shape: nothing was pushed, so the cache has never
            #    heard of the ref. The verdict is refused and the ask survives.
            absent_ref = "roles/main/syrd-118-absent"
            absent = ask("PGU-1", absent_ref, second)
            refused = record("PGU-1", absent, "published", "pushed", expect=400)
            assert absent_ref in str(refused), refused
            assert "not published" in str(refused).lower() or "no published" in str(refused).lower(), refused
            assert open_request(admin, "PGU-1")["id"] == absent["id"]
            assert queued_for(admin, "main") == [], queued_for(admin, "main")

            # 2. A ref that exists but points somewhere else is not this
            #    request's publication.
            wrong_ref = "roles/main/syrd-118-wrong"
            wrong = ask("PGU-2", wrong_ref, second)
            publish(cache, work, wrong_ref, first)
            mismatched = record("PGU-2", wrong, "published", "pushed", expect=400)
            assert first[:12] in str(mismatched) and second[:12] in str(mismatched), mismatched
            assert open_request(admin, "PGU-2")["id"] == wrong["id"]

            # 3. The implementer's own local branch is not evidence that
            #    anything was published.
            claimed_ref = "roles/main/syrd-118-claimed"
            claimed = ask("PGU-3", claimed_ref, second)
            claim(cache, work, claimed_ref, second)
            unproven = record("PGU-3", claimed, "published", "pushed", expect=400)
            assert claimed_ref in str(unproven), unproven
            assert open_request(admin, "PGU-3")["id"] == claimed["id"]

            # 4. A rejection is unaffected: it needs no push to be true.
            rejected = record("PGU-3", claimed, "rejected", "bundle does not verify")
            assert rejected["request"]["state"] == "rejected", rejected
            assert any("was rejected" in m for m in queued_for(admin, "main")), queued_for(admin, "main")

            # 5. The retry is safe: publish for real, ask again, and the same
            #    verdict is now accepted and carries what the board saw.
            publish(cache, work, absent_ref, second)
            accepted = record("PGU-1", absent, "published", "pushed")
            assert accepted["request"]["state"] == "published", accepted
            assert accepted["request"]["verified_commit"] == second, accepted
            assert any(f"{absent_ref} is published" in m for m in queued_for(admin, "main")), queued_for(admin, "main")
            # Re-recording after the verdict landed stays safe.
            assert record("PGU-1", absent, "published", "pushed")["request"]["state"] == "published"

            # 6. Nothing in any of that contacted a remote. The proof is
            #    read with local git, from the repositories the tenant's root-
            #    owned unit names, so a retry is safe and no caller can point
            #    the check at a remote of its own choosing.
            calls = git_calls(log)
            assert calls, "the verification never ran git at all"
            for call in calls:
                assert not any(word in call for word in NETWORK_SUBCOMMANDS), call
                assert not any("://" in arg or arg.startswith("git@") for arg in call), call
            for ref in (absent_ref, wrong_ref, claimed_ref):
                looked = [call for call in calls if any(ref in arg for arg in call)]
                assert looked, f"nothing was resolved for {ref}"
                for call in looked:
                    assert call[0] == f"--git-dir={cache}", call
                    assert "rev-parse" in call, call
                assert any(
                    f"{PUBLISHED_REF_NAMESPACE}/{ref}" in arg for call in looked for arg in call
                ), looked

            # 6b. A board whose commit cache is gone proves nothing, and says
            #     that rather than accusing the publisher of not pushing.
            configured = app.commit_git_dirs
            app.commit_git_dirs = (root / "no-such-cache.git",)
            try:
                blind = ask("PGU-4", "roles/main/syrd-118-blind", second)
                unknowable = record("PGU-4", blind, "published", "pushed", expect=400)
                assert "cannot be proven published" in str(unknowable), unknowable
                assert open_request(admin, "PGU-4")["id"] == blind["id"]
                record("PGU-4", blind, "rejected", "cache is gone")
            finally:
                app.commit_git_dirs = configured

            # 7. The database does not take the claim on trust either: the
            #    proof it is handed has to be the commit that was asked for,
            #    checked where a caller holding the board's own credential
            #    still cannot talk its way past it.
            standalone = ask("PGU-4", "roles/main/syrd-118-direct", second)
            for proof in ("", first, "a" * 40):
                try:
                    with app._pg_connect() as conn:
                        app._pg_set_caller_role(conn, "director")
                        conn.execute(
                            "SELECT ticket_board.resolve_publication(%s::bigint, 'published', 'pushed', %s);",
                            (standalone["id"], proof),
                        )
                        conn.commit()
                except Exception as exc:  # noqa: BLE001
                    assert "proven" in str(exc), (proof, exc)
                else:
                    raise AssertionError(f"the database accepted {proof!r} as proof of publication")
            assert open_request(admin, "PGU-4")["id"] == standalone["id"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("publication_proof_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
