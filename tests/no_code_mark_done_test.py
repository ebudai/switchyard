#!/usr/bin/env python3
"""SYRD-267: an audited no-code ticket can be closed, and nothing invents its provenance.

MEFP-1 was a no-code migration: Ops took the declared no-code submission and
Audit signed it off. At Final Sign-Off it was `director_review`,
`audit_signoff=true`, `commit_exempt=true`, `commit_hash=""` -- and
`ticket-board-write mark-done MEFP-1` refused it with `invalid commit hash`.

The board validates any commit hash it is GIVEN, before it consults the
ticket's commit exemption, and the client always gave one: an empty string.
So the only way through was to supply an old HEAD, which is false
provenance. The client now omits the field when there is no hash; the
board's commit requirement still refuses an ordinary ticket without one.

Driven through the real `ticket-board-write` program against a real board
carrying MEFP's declared workflow, where `mark_done` requires a commit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)

import legacy_workflow_equivalence_test as equivalence  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from ticket_board import legacy_workflow as lw  # noqa: E402
from ticket_board import project_provision as pv  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
WRITE = ROOT / "scripts" / "ticket-board-write"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def ticket(board, ticket_id: str) -> dict:
    import json

    return json.loads(t.psql(
        board.admin,
        "SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash) "
        f"FROM ticket_board.tickets WHERE id='{ticket_id}';",
    ))


def main() -> int:
    plan = equivalence.mefp_plan()
    declared = lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(ROOT),
        stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan),
        panes=equivalence.mefp_panes(),
    ).document
    mark_done = next(tr for tr in declared["transitions"] if tr["action"] == "mark_done")
    check(mark_done["require_commit"] is True and mark_done["allow_no_code"] is False,
          f"MEFP's close requires a commit and declares no no-code variant: {mark_done}")

    with temporary_cluster(prefix="syrd267-", shutdown="immediate") as cluster:
        board = equivalence.Board(cluster, "close")
        try:
            t.psql(board.admin, pv.render_workflow_sql(plan))
            import json

            with board.app._pg_connect() as conn:
                board.app._pg_set_caller_role(conn, "director")
                conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(declared),))

            final = {"state": "director_review", "assignee": "director", "audit_signoff": True}
            equivalence.seed(board, "MEFP-1", {**final, "commit_exempt": True})
            equivalence.seed(board, "MEFP-2", final)
            equivalence.seed(board, "MEFP-3", {**final, "commit_exempt": True})
            equivalence.seed(board, "MEFP-4", {**final, "commit_exempt": True})

            env = {
                "PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8",
                "TICKET_BOARD_URL": board.base,
                "TICKET_BOARD_WRITE_TOKEN": board.server.write_token,
                "TICKET_BOARD_CALLER_ROLE": "director",
            }

            def cli(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, str(WRITE), *args], env=env,
                                      capture_output=True, text=True, timeout=120, check=False)

            # The live report, exactly: the Director's plain mark-done.
            done = cli("mark-done", "MEFP-1")
            check(done.returncode == 0, f"the audited no-code ticket closes: {done.stdout}{done.stderr}")
            check(ticket(board, "MEFP-1") == {"state": "done", "commit_hash": ""},
                  f"with no commit recorded, because none was made: {ticket(board, 'MEFP-1')}")

            # The gate that must not move: a code ticket still needs its commit.
            refused = cli("mark-done", "MEFP-2")
            check(refused.returncode != 0, f"an ordinary ticket without a commit is refused: {refused.stdout}")
            said = refused.stdout + refused.stderr
            check("invalid commit hash" not in said and "commit" in said.lower(),
                  f"by the commit requirement itself, not a malformed field: {said}")
            check(ticket(board, "MEFP-2")["state"] == "director_review", "and it stays in Final Sign-Off")

            # Nor does the exemption let a bad hash through when one is given.
            bogus = cli("mark-done", "MEFP-3", "--commit-hash", "not-a-hash")
            said = bogus.stdout + bogus.stderr
            # Refused for its form -- by the board's own check before the
            # database's, whichever comes first -- not accepted on the strength
            # of the exemption.
            check(bogus.returncode != 0 and ("7-40 character hex" in said or "invalid commit hash" in said),
                  f"a malformed hash is still refused: {said}")
            check(ticket(board, "MEFP-3")["state"] == "director_review", "and nothing moved")

            # Blank is none, not a hash.
            blank = cli("mark-done", "MEFP-4", "--commit-hash", "   ")
            check(blank.returncode == 0 and ticket(board, "MEFP-4") == {"state": "done", "commit_hash": ""},
                  f"a blank hash is no hash: {blank.stdout}{blank.stderr} {ticket(board, 'MEFP-4')}")
        finally:
            board.close()
    print(f"no_code_mark_done_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
