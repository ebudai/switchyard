#!/usr/bin/env python3
"""SYRD-489: a blank commit hash closed an audited no-code ticket on one path and not on the other.

SYRD-488 was an audited, commit-exempt design ticket with no commit, in Final
Sign-Off, under a declared `mark_done` that requires a commit. On 2026-09-29,
`ticket-board-write mark-done SYRD-488` failed with `invalid commit hash`, while
`ticket-board-write workflow-action SYRD-488 mark_done --payload-json '{}'`
closed it.

The installed client was release b097e679, from 2026-09-20. SYRD-267's client
change, which omits a blank hash, is 63fa402 (2026-09-25) and was not in it, so
the client sent `{"commit_hash": ""}`.

On a board with a declared workflow, the server hands `mark_done` to the
declared executor, as it does the generic action. The app validated the blank
hash to `""` and passed it on, and the executor refuses any `commit_hash` key
that is not hex. On a board with no declared workflow, the legacy state=done
path reads `""` as no hash, and the ticket's exemption decides. The web UI still
sends `{commit_hash: ''}` for a ticket with no hash, so the declared path fails
for it today.

A blank hash now names no commit on every path: the app drops it. Everything
else is unchanged, and each part is checked:
- an ordinary ticket without a recorded commit is refused by the commit
  requirement;
- a malformed hash is refused for its form;
- a verified exact commit closes an ordinary ticket on it;
- only the Director may close.

Driven through real clients against a real board carrying MEFP's declared
workflow, where `mark_done` requires a commit:
- the b097e679 `ticket-board-write`, from a git archive (the one SYRD-488 met);
- this tree's `ticket-board-write`, through both `mark-done` and
  `workflow-action`;
- the web UI's request, over the board's HTTP API.

It runs on main, from a git archive in a child process, and on this tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "0bc0e235385cd3d5a6eee1e080acff2aedca5ebf"  # main before SYRD-489
SYRD_488_CLIENT = "b097e679a801fc56cdc5345d48590214d06d2339"  # the live release on 2026-09-29
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def scenario(root: Path, old_client: Path) -> dict:
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from ticket_board_pane_env import strip_ticket_board_pane_env

    strip_ticket_board_pane_env(os.environ)
    import legacy_workflow_equivalence_test as equivalence
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster
    from ticket_board import legacy_workflow as lw
    from ticket_board import project_provision as pv

    plan = equivalence.mefp_plan()
    declared = lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(root),
        stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan),
        panes=equivalence.mefp_panes(),
    ).document
    seen: dict = {"mark_done": next(tr for tr in declared["transitions"] if tr["action"] == "mark_done")}
    with temporary_cluster(prefix="syrd489-", shutdown="immediate") as cluster:
        board = equivalence.Board(cluster, "close")
        try:
            t.psql(board.admin, pv.render_workflow_sql(plan))
            final = {"state": "director_review", "assignee": "director", "audit_signoff": True}

            def state(ticket_id: str) -> list:
                return json.loads(t.psql(board.admin, "SELECT json_build_array(state, commit_hash)::text "
                                                      f"FROM ticket_board.tickets WHERE id = '{ticket_id}';"))

            def cli(program: Path, *args: str, role: str = "director") -> str:
                env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8",
                       "TICKET_BOARD_URL": board.base, "TICKET_BOARD_WRITE_TOKEN": board.server.write_token,
                       "TICKET_BOARD_CALLER_ROLE": role}
                done = subprocess.run([sys.executable, str(program), *args], env=env, cwd=str(cluster.root),
                                      capture_output=True, text=True, timeout=120, check=False)
                return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())

            old = old_client / "scripts" / "ticket-board-write"
            new = root / "scripts" / "ticket-board-write"

            # No declared workflow yet: the legacy state=done path.
            equivalence.seed(board, "MEFP-10", {**final, "commit_exempt": True})
            seen["legacy_board"] = board.app.workflow_configuration() is None
            seen["legacy_old_client"] = [cli(old, "mark-done", "MEFP-10"), state("MEFP-10")]

            with board.app._pg_connect() as conn:
                board.app._pg_set_caller_role(conn, "director")
                conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(declared),))
            seen["declared_board"] = board.app.workflow_configuration() is not None

            exempt = {"MEFP-1": "the SYRD-488 client", "MEFP-2": "the web UI", "MEFP-3": "workflow-action, blank",
                      "MEFP-4": "this tree's mark-done", "MEFP-5": "a malformed hash", "MEFP-6": "not the Director"}
            for ticket_id in exempt:
                equivalence.seed(board, ticket_id, {**final, "commit_exempt": True})
            for ticket_id in ("MEFP-7", "MEFP-8"):
                equivalence.seed(board, ticket_id, final)
            # The board's own copy of the project repository: the audited
            # candidate, and the integration commit the Director closes on.
            repo = cluster.root / "project"
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            commits = []
            for message in ("candidate", "integration"):
                subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], check=True)
                commits.append(subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                                              check=True).stdout.strip())
            candidate, commit = commits
            board.app.commit_git_dirs = (repo / ".git",)
            equivalence.seed(board, "MEFP-9", {**final, "commit_hash": candidate})

            seen["exempt"] = {
                "MEFP-1": cli(old, "mark-done", "MEFP-1"),
                "MEFP-2": "ok" if board.act("MEFP-2", "mark_done", "director", commit_hash="")[0]
                          else board.act("MEFP-2", "mark_done", "director", commit_hash="")[1],
                "MEFP-3": cli(new, "workflow-action", "MEFP-3", "mark_done", "--payload-json", '{"commit_hash": ""}'),
                "MEFP-4": cli(new, "mark-done", "MEFP-4"),
                "MEFP-5": cli(old, "mark-done", "MEFP-5", "--commit-hash", "not-a-hash"),
                "MEFP-6": cli(old, "mark-done", "MEFP-6", role="audit"),
            }
            seen["ordinary"] = {
                "MEFP-7": cli(old, "mark-done", "MEFP-7"),
                "MEFP-8": cli(new, "mark-done", "MEFP-8"),
                "MEFP-9": cli(new, "mark-done", "MEFP-9", "--commit-hash", commit),
            }
            seen["states"] = {ticket_id: state(ticket_id) for ticket_id in (*exempt, "MEFP-7", "MEFP-8", "MEFP-9")}
            seen["commit"] = commit
        finally:
            board.close()
    return seen


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]))))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd489.") as tmp:
        old_client = tree_at(SYRD_488_CLIENT, Path(tmp) / "client")
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root), str(old_client)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, old_client)
    check(after["mark_done"]["require_commit"] is True and after["declared_board"] and after["legacy_board"],
          f"MEFP's declared close requires a commit, and the legacy board is undeclared: {after['mark_done']}")
    check(all("invalid commit hash" in before["exempt"][ticket_id] for ticket_id in ("MEFP-1", "MEFP-2", "MEFP-3"))
          and before["states"]["MEFP-1"][0] == "director_review",
          f"main: the SYRD-488 client, the web UI and a blank workflow-action are refused 'invalid commit hash': "
          f"{before['exempt']}")
    check(before["legacy_old_client"] == ["ok", ["done", ""]] and before["exempt"]["MEFP-4"] == "ok",
          f"main: the legacy path and this tree's mark-done close it: {before['legacy_old_client']} {before['exempt']['MEFP-4']}")

    for ticket_id, how in (("MEFP-1", "the SYRD-488 client"), ("MEFP-2", "the web UI"),
                           ("MEFP-3", "a blank workflow-action"), ("MEFP-4", "this tree's mark-done")):
        check(after["exempt"][ticket_id] == "ok" and after["states"][ticket_id] == ["done", ""],
              f"{how} closes the audited no-code ticket with no commit recorded: "
              f"{after['exempt'][ticket_id]} {after['states'][ticket_id]}")
    check(after["legacy_old_client"] == ["ok", ["done", ""]],
          f"the legacy path is unchanged: {after['legacy_old_client']}")
    check("7-40 character hex" in after["exempt"]["MEFP-5"] and after["states"]["MEFP-5"][0] == "director_review",
          f"a malformed hash is still refused for its form, exemption or not: {after['exempt']['MEFP-5']}")
    check(after["exempt"]["MEFP-6"] != "ok" and after["states"]["MEFP-6"][0] == "director_review",
          f"only the Director may close it: {after['exempt']['MEFP-6']}")
    for ticket_id in ("MEFP-7", "MEFP-8"):
        said = after["ordinary"][ticket_id]
        check(said != "ok" and "invalid commit hash" not in said and "commit" in said.lower()
              and after["states"][ticket_id][0] == "director_review",
              f"an ordinary ticket with no commit is refused by the commit requirement, whichever client: {said}")
    check(after["ordinary"]["MEFP-9"] == "ok" and after["states"]["MEFP-9"] == ["done", after["commit"]],
          f"an ordinary ticket closes on the verified exact commit the Director names: "
          f"{after['ordinary']['MEFP-9']} {after['states']['MEFP-9']}")
    print(f"mark_done_blank_hash_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
