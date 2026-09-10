#!/usr/bin/env python3
"""SYRD-93 acceptance: the SYRD-92 flow, with nobody typing a git push.

What happened on SYRD-92: an implementer finished a commit, the board correctly
refused `submit_to_audit` because the commit was in no public ref and no trusted
cache, and the only publisher on the host still expected a per-role account to
sudo as the project owner -- which one shared Unix account (SYRD-69) made
impossible. The Director unpicked it by hand: import the bundle, push the ref,
refresh the cache, hand submission back.

This drives the replacement end to end, with the real programs: a real cluster
and board, the real request CLI, the real Director-side driver, and the real
privileged publisher pushing to a real remote. Nothing here runs as root and
nothing types a push.
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
os.environ["TICKET_BOARD_PROCESS_AUTHORITY"] = "1"

import ticket_board_write_api_test as t  # noqa: E402
import switchyard_publish_ref_test as publish_fixture  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

publisher = publish_fixture.publisher
write_proc = publish_fixture.write_proc
TMUX_PID, DIRECTOR_PANE, OPS_PANE = 910001, 910002, 910003
DIRECTOR_START, OPS_START = 6161, 7272

#: Stamp this process into the fake process table, then become the real program.
#: Each link claims its actual parent, so the chain the publisher walks is the
#: chain that really exists -- only its top is fictional, standing in for the
#: pane tmux would have started.
STAMP = (
    "import os, sys\n"
    "root, ppid = sys.argv[1], int(sys.argv[2])\n"
    "ppid = os.getppid() if ppid == 0 else ppid\n"
    "d = os.path.join(root, str(os.getpid()))\n"
    "os.makedirs(d, exist_ok=True)\n"
    "fields = ['S', str(ppid)] + ['0'] * 17 + ['11']\n"
    "open(os.path.join(d, 'stat'), 'w').write('%d (python3) ' % os.getpid() + ' '.join(fields) + '\\n')\n"
    "open(os.path.join(d, 'status'), 'w').write('Uid:\\t%d\\t%d\\t%d\\t%d\\n' % ((os.getuid(),) * 4))\n"
    "os.execv(sys.executable, [sys.executable] + sys.argv[3:])\n"
)


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def main() -> int:
    with temporary_cluster(prefix="publication-e2e-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "publication_handoff_end_to_end_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(admin, "PGU-1", title="Defer the backlog", state="in_progress", assignee="ops")

        # The host's root-owned data, redirected into the temporary tree, and a
        # real remote and trusted cache.
        owner_home = root / "owner"
        repository = owner_home / "Projects" / "cerulean"
        registry = root / "etc" / "switchyard" / "projects"
        units = root / "etc" / "systemd" / "system"
        grants = root / "etc" / "switchyard" / "publish"
        staging = root / "var" / "lib" / "switchyard" / "publish"
        proc = root / "proc"
        cache = root / "source-cache.git"
        remote = root / "remote.git"
        worktree = root / "ops-worktree"
        for directory in (repository, registry, units, grants, staging, proc):
            directory.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "-q", str(remote))
        git("init", "--bare", "-q", str(cache))
        git("--git-dir", str(cache), "remote", "add", "origin", str(remote))

        publish_fixture.write_json(repository / ".switchyard/provision/cerulean.json", {
            "project": "cerulean", "repository": str(repository), "worktree_remote": "origin",
        })
        publish_fixture.write_json(registry / "cerulean.json", {
            "schema": publisher.PROJECT_REGISTRY_SCHEMA, "slug": "cerulean",
            "name": "Cerulean", "config_path": str(repository / ".switchyard/provision/cerulean.json"),
        })
        identity = grants / "cerulean-publish-key"
        identity.write_text("root-only key material\n", encoding="utf-8")
        identity.chmod(0o600)
        publish_fixture.write_json(grants / "cerulean.json", {
            "schema": publisher.PUBLISH_GRANT_SCHEMA, "project": "cerulean",
            "identity_file": str(identity), "remote": str(remote),
        })

        uid = os.getuid()
        write_proc(proc, TMUX_PID, ppid=1, start=1, comm="tmux: server", uid=uid)
        write_proc(proc, DIRECTOR_PANE, ppid=TMUX_PID, start=DIRECTOR_START, comm="bash", uid=uid)
        write_proc(proc, OPS_PANE, ppid=TMUX_PID, start=OPS_START, comm="bash", uid=uid)

        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
            commit_git_dir=cache,
        )
        cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        (units / "cerulean-ticket-board.service").write_text(
            "[Service]\n"
            f"ExecStart=/usr/bin/python3 /board/ticket-board.py --host 127.0.0.1 "
            f"--port {server.server_port}\n"
            f"Environment=TICKET_BOARD_COMMIT_GIT_DIR={cache}\n",
            encoding="utf-8",
        )
        try:
            t.post_json(
                base,
                "/api/tickets/actions/configure_workflow",
                {"document": cfg, "expected_revision": 0},
                caller="director",
            )
            # The launcher's own registration: which process is which role.
            for role, runtime, pane, start in (
                ("director", "claude", DIRECTOR_PANE, DIRECTOR_START),
                ("ops", "claude", OPS_PANE, OPS_START),
            ):
                app.register_runtime_assignment(
                    role=role,
                    runtime=runtime,
                    target=f"cerulean-{role}:0.0",
                    worktree=str(worktree),
                    session_dir=str(root / "sessions" / role),
                    process_pid=pane,
                    process_start_time=start,
                    process_uid=uid,
                    expected_generation=0,
                )

            # ---- the implementer commits, in its own worktree, with no key ----
            git("init", "-q", "-b", "trunk", str(worktree))
            git("config", "user.email", "ops@example.invalid", cwd=worktree)
            git("config", "user.name", "Ops", cwd=worktree)
            (worktree / "backlog.md").write_text("deferred\n", encoding="utf-8")
            git("add", "backlog.md", cwd=worktree)
            git("commit", "-q", "-m", "Defer the backlog", cwd=worktree)
            # The role's checkout knows where the project lives; what it does
            # not have, and must not need, is anything that could write there.
            git("remote", "add", "origin", str(remote), cwd=worktree)
            commit = git("rev-parse", "HEAD", cwd=worktree)

            # Deliberately no TICKET_BOARD_URL: every program here is told
            # which board to use on its command line, so nothing can fall back
            # to the socket of whatever board this host happens to be running.
            board_env = {
                **os.environ,
                "TICKET_BOARD_WRITE_TOKEN": server.write_token,
                # No socket: this board is the disposable one above, reached
                # over its own loopback port.
                "TICKET_BOARD_SOCKET": "",
                "HOME": str(owner_home),
            }
            write_client = str(ROOT / "scripts" / "ticket-board-write")

            # Submission is correctly refused while the commit is nowhere the
            # board can see it. This is where SYRD-92 stopped.
            refused = subprocess.run(
                [sys.executable, write_client, "--board-url", base,
                 "submit-to-audit", "PGU-1", "--commit", commit],
                text=True, capture_output=True, cwd=str(worktree),
                env={**board_env, "TICKET_BOARD_CALLER_ROLE": "ops"},
            )
            assert refused.returncode != 0, refused.stdout
            complaint = (refused.stdout + refused.stderr).lower()
            assert commit[:12] in complaint, complaint
            assert "not" in complaint and "origin" in complaint, complaint

            # So the implementer asks, with the real CLI and no credential.
            asked = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "switchyard-request-publication"),
                    "PGU-1", "--worktree", str(worktree), "--role", "ops",
                    "--project", "cerulean", "--write-client", write_client,
                    "--board-url", base,
                ],
                text=True, capture_output=True,
                env={**board_env, "TICKET_BOARD_CALLER_ROLE": "ops"},
            )
            assert asked.returncode == 0, asked.stdout + asked.stderr
            assert "asked to publish roles/ops/trunk" in asked.stdout, asked.stdout

            waiting = app.publication_requests(state="requested")
            assert len(waiting) == 1, waiting
            request = waiting[0]
            assert request["requested_by"] == "ops" and request["commit_hash"] == commit, request
            assert t.psql(
                admin,
                "SELECT awaiting_role FROM ticket_board.ticket_notification_state "
                "WHERE ticket_id = 'PGU-1';",
            ) == "director"

            # ---- the control role publishes, from its own pane ----
            publisher_env = {
                publisher.REGISTRY_TEST_ROOT_ENV: str(registry),
                publisher.BOARD_UNIT_TEST_DIR_ENV: str(units),
                publisher.PUBLISH_GRANT_TEST_DIR_ENV: str(grants),
                publisher.STAGING_TEST_ROOT_ENV: str(staging),
                publisher.PROC_ROOT_ENV: str(proc),
                publisher.OWNER_TEST_HOME_ENV: str(owner_home),
            }
            wrapped_publisher = root / "publisher-shim"
            wrapped_publisher.write_text(
                "#!/bin/sh\n"
                f'exec {sys.executable} -c "$(cat {root / "stamp.py"})" {proc} 0 '
                f'{ROOT / "scripts" / "switchyard-publish-ref"} "$@"\n',
                encoding="utf-8",
            )
            (root / "stamp.py").write_text(STAMP, encoding="utf-8")
            wrapped_publisher.chmod(0o755)

            published = subprocess.run(
                [
                    sys.executable, "-c", STAMP, str(proc), str(DIRECTOR_PANE),
                    str(ROOT / "scripts" / "switchyard-publish"), "PGU-1",
                    "--project", "cerulean", "--board-url", base,
                    "--publisher", str(wrapped_publisher), "--sudo", "",
                    "--write-client", write_client,
                ],
                text=True, capture_output=True,
                env={**board_env, **publisher_env, "TICKET_BOARD_CALLER_ROLE": "director"},
            )
            assert published.returncode == 0, published.stdout + published.stderr
            assert "published roles/ops/trunk" in published.stdout, published.stdout

            # The ref is on the remote, exactly once and exactly as asked.
            listed = git("--git-dir", str(remote), "for-each-ref", "--format=%(refname) %(objectname)")
            assert listed.split() == [f"refs/heads/roles/ops/trunk", commit], listed
            # And in the cache the board verifies against.
            assert git("--git-dir", str(cache), "rev-parse",
                       "refs/remotes/origin/roles/ops/trunk^{commit}") == commit

            # The board says what happened, the wait is over, and ops was told.
            decided = app.publication_requests(ticket_id="PGU-1")[0]
            assert decided["state"] == "published" and decided["decided_by"] == "director", decided
            assert t.psql(
                admin,
                "SELECT awaiting_role FROM ticket_board.ticket_notification_state "
                "WHERE ticket_id = 'PGU-1';",
            ) == ""
            notified = t.psql(
                admin,
                "SELECT coalesce(jsonb_agg(message), '[]'::jsonb) "
                "FROM ticket_board.ticket_notification_queue "
                "WHERE target_role = 'ops' AND kind = 'publication';",
            )
            assert "is published at" in notified, notified

            # The publication did not submit anything: the ticket is still the
            # implementer's, in the implementer's stage.
            still = app.get_ticket("PGU-1")
            assert still["state"] == "in_progress" and still["assignee"] == "ops", still

            # ---- and now the implementer submits its own work ----
            submitted = subprocess.run(
                [sys.executable, write_client, "--board-url", base,
                 "submit-to-audit", "PGU-1", "--commit", commit],
                text=True, capture_output=True, cwd=str(worktree),
                env={**board_env, "TICKET_BOARD_CALLER_ROLE": "ops"},
            )
            assert submitted.returncode == 0, submitted.stdout + submitted.stderr
            after = app.get_ticket("PGU-1")
            assert after["state"] != "in_progress", after
            assert after["commit_hash"] == commit, after

            # Nothing in this reproduction had a push credential except the
            # publisher, and the publisher's is unreadable by this account.
            assert identity.stat().st_mode & 0o077 == 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("publication_handoff_end_to_end_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
