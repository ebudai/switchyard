#!/usr/bin/env python3
"""SYRD-280: withdrawing an approval is a reasoned Director edit, not a field edit.

MEFP-8 (release 49abeb4): Audit approved commit df71cf4, the Director integrated
it, then routed director_review -> in_progress/ops for a README-only correction.
The route kept audit_signoff=true. Wanting the next submission reviewed, the
Director tried `edit-fields` to clear it, which the database refused; the
supported `director-edit`, with a reason, cleared it and kept needs_audit.

The Director skill and guide now say which is which. This suite pins what they
say, on a board carrying MEFP's own composed workflow, through the real
`ticket-board-write` as each role:
- a route back for rework keeps the approval of the unchanged commit;
- `edit-fields` cannot change a sign-off, in either direction;
- `director-edit` withdraws one with a recorded reason, keeps the review gate,
  and cannot create one;
- resubmitting the same commit carries an approval that stands, and returns to
  review once it is withdrawn;
- submitting a different commit clears every carried approval by itself
  (SYRD-271), with nothing to withdraw;
- the commands the docs print are the ones run here, verbatim.

It runs on main, from a git archive in a child process, and on this tree.
"""

from __future__ import annotations

import io
import json
import re
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a1bcdade8a18bb0bf56e6bb58f01a45e3b1983fe"  # main before SYRD-280
DOCS = ("skills/switchyard-director/SKILL.md", "docs/onboarding/switchyard-director-guide.md")
REVOKE = 'ticket-board-write director-edit <id> --set audit_signoff=false --reason "<why the approval no longer holds>"'
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def scenario(root: Path) -> dict:
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
        plan, canonical=lw.load_canonical(root), stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan), panes=equivalence.mefp_panes(),
    ).document
    seen: dict = {"docs": {doc: " ".join((root / doc).read_text().split()) for doc in DOCS}}  # prose wraps
    with temporary_cluster(prefix="syrd280-", shutdown="immediate") as cluster:
        # The changed-commit case gets a board of its own: an implementer's slot is held until its ticket is done.
        boards = {name: equivalence.Board(cluster, name) for name in ("declared", "changed", "legacy")}
        try:
            for name in ("declared", "changed"):
                t.psql(boards[name].admin, pv.render_workflow_sql(plan))
                with boards[name].app._pg_connect() as conn:
                    boards[name].app._pg_set_caller_role(conn, "director")
                    conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(declared),))
            board = boards["declared"]
            repo = cluster.root / "project"
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            commits = []
            for message in ("approved", "correction"):
                subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], check=True)
                commits.append(subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                                              check=True).stdout.strip())
            approved, correction = commits
            # Ops submits from a clone whose origin holds both commits, as the client requires.
            subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(cluster.root / "origin.git")], check=True)
            subprocess.run([*git, "remote", "add", "origin", str(cluster.root / "origin.git")], check=True)
            subprocess.run([*git, "fetch", "-q", "origin"], check=True)
            for b in boards.values():
                b.app.commit_git_dirs = (repo / ".git",)
            seen["route"] = next(tr for tr in declared["transitions"]
                                 if tr["from"] == "director_review" and tr["to"] == "in_progress")

            def cli(b, role: str, *args: str) -> str:
                env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8",
                       "TICKET_BOARD_URL": b.base, "TICKET_BOARD_WRITE_TOKEN": b.server.write_token,
                       "TICKET_BOARD_CALLER_ROLE": role}
                done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                      cwd=str(repo), capture_output=True, text=True, timeout=120, check=False)
                # A refusal's PL/pgSQL line is not part of the answer; dropping it first keeps the cut where it was.
                said = re.sub(r"\bline \d+ at\b", "line N at", " ".join((done.stdout + done.stderr).split()))
                return "ok" if done.returncode == 0 else said[:300]

            def row(b, ticket_id: str) -> dict:
                return json.loads(t.psql(b.admin, "SELECT json_build_object('state', state, 'assignee', assignee, "
                                                  "'audit_signoff', audit_signoff, 'needs_audit', needs_audit, "
                                                  "'commit_hash', commit_hash)::text FROM ticket_board.tickets "
                                                  f"WHERE id = '{ticket_id}';"))

            def last_comment(b, ticket_id: str) -> str:
                return t.psql(b.admin, f"SELECT who || ': ' || text FROM ticket_board.ticket_comments "
                                       f"WHERE ticket_id = '{ticket_id}' ORDER BY id DESC LIMIT 1;").strip()

            approved_review = {"state": "director_review", "assignee": "director", "audit_signoff": True,
                               "commit_hash": approved}
            for ticket_id in ("MEFP-8", "MEFP-18"):
                equivalence.seed(board, ticket_id, approved_review)
            equivalence.seed(boards["changed"], "MEFP-19", approved_review)
            out: dict = {}

            def route(b, ticket_id: str, implementer: str) -> None:
                """MEFP's own route back for rework: director_review -> in_progress, a plain move."""
                out[f"{ticket_id}/routed"] = cli(b, "director", "workflow-action", ticket_id, seen["route"]["action"],
                                                 "--payload-json", json.dumps({"assignee": implementer, "target": "in_progress"}))
                out[f"{ticket_id}/after_route"] = row(b, ticket_id)

            route(board, "MEFP-8", "ops")
            route(board, "MEFP-18", "main")
            route(boards["changed"], "MEFP-19", "ops")
            out["edit_fields_clear"] = cli(board, "director", "edit-fields", "MEFP-8", "--json", '{"audit_signoff": false}')
            out["edit_fields_raise"] = cli(board, "director", "edit-fields", "MEFP-18", "--json", '{"audit_signoff": true}')
            out["director_edit_raise"] = cli(board, "director", "director-edit", "MEFP-18", "--set", "audit_signoff=true",
                                             "--reason", "Approving it myself.")
            unreasoned = board.act("MEFP-8", "director_edit", "director", patch={"audit_signoff": False}, reason="  ")
            out["director_edit_unreasoned"] = "ok" if unreasoned[0] else " ".join(str(unreasoned[1]).split())[:300]
            out["ops_revokes"] = cli(board, "ops", "director-edit", "MEFP-8", "--set", "audit_signoff=false",
                                     "--reason", "Ops withdrawing Audit's approval.")
            out["after_refusals"] = row(board, "MEFP-8")
            # The documented command, verbatim, with its placeholders filled.
            documented = shlex.split(REVOKE.replace("<id>", "MEFP-8").replace(
                "<why the approval no longer holds>", "The README correction must be reviewed before integration."))
            out["revoked"] = cli(board, "director", *documented[1:])
            out["after_revoke"] = row(board, "MEFP-8")
            out["revoke_comment"] = last_comment(board, "MEFP-8")
            # Ops resubmits: the same commit after the revocation, the same commit with the approval
            # standing, and a different commit with it standing.
            out["MEFP-8/submitted"] = cli(board, "ops", "submit-to-audit", "MEFP-8", "--commit", approved)
            out["MEFP-18/submitted"] = cli(board, "main", "submit-to-audit", "MEFP-18", "--commit", approved)
            out["MEFP-19/submitted"] = cli(boards["changed"], "ops", "submit-to-audit", "MEFP-19", "--commit", correction)
            for ticket_id in ("MEFP-8", "MEFP-18"):
                out[f"{ticket_id}/after_submit"] = row(board, ticket_id)
            out["MEFP-19/after_submit"] = row(boards["changed"], "MEFP-19")
            seen["declared"] = out

            # A board with no declared workflow.
            legacy = boards["legacy"]
            equivalence.seed(legacy, "MEFP-8", approved_review)
            equivalence.seed(legacy, "MEFP-9", approved_review)
            seen["legacy"] = {
                "edit_fields_clear": cli(legacy, "director", "edit-fields", "MEFP-8", "--json", '{"audit_signoff": false}'),
                "after_edit_fields": row(legacy, "MEFP-8"),
                "ops_edit_fields_clear": None,
                "director_edit_raise": cli(legacy, "director", "director-edit", "MEFP-8", "--set", "audit_signoff=true",
                                           "--reason", "Approving it myself."),
                "revoked": cli(legacy, "director", *documented[1:]),
                "after_revoke": row(legacy, "MEFP-8"),
            }
            seen["legacy"]["ops_edit_fields_clear"] = cli(legacy, "ops", "edit-fields", "MEFP-9", "--json",
                                                          '{"audit_signoff": false}')
            seen["legacy"]["after_ops_edit_fields"] = row(legacy, "MEFP-9")
        finally:
            for b in boards.values():
                b.close()
    return seen


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd280.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    after = scenario(ROOT)

    def shape(run: dict) -> dict:
        """A run's outcomes with its own scratch commit hashes named by role, so main's and this tree's compare."""
        text = json.dumps({k: run[k] for k in ("declared", "legacy")})
        # Where in a PL/pgSQL body a refusal is raised is not an answer: any edit to that
        # function moves it, and the refusal is the same (SYRD-599 moved this one).
        text = re.sub(r"\bline \d+ at\b", "line N at", text)
        hashes = sorted({row["commit_hash"] for section in ("declared",) for row in run[section].values()
                         if isinstance(row, dict) and row.get("commit_hash")}, key=text.index)
        for name, value in zip(("<approved>", "<correction>"), hashes):
            text = text.replace(value, name)
        # A refusal's CONTEXT names the line in its PL/pgSQL function, which moves when a function above it grows.
        return json.loads(re.sub(r"line \d+ at", "line N at", text))

    d, legacy = after["declared"], after["legacy"]
    refused_flag = "flag change requires authorized workflow action"
    for doc in DOCS:
        check(REVOKE not in before["docs"][doc] and REVOKE in after["docs"][doc] and refused_flag in after["docs"][doc],
              f"{doc}: main does not say how to withdraw an approval; this tree prints the command this suite runs, "
              f"and the refusal edit-fields meets")
    check(shape(before) == shape(after),
          "the board answers every step identically on main and on this tree: the change is what the docs say")
    check(after["route"]["primitive"] == "move" and after["route"]["clear_signoffs"] == []
          and all(d[f"{t}/routed"] == "ok" and d[f"{t}/after_route"]["audit_signoff"] for t in ("MEFP-8", "MEFP-18", "MEFP-19")),
          f"MEFP's route back for rework is a plain move and keeps the approval of the unchanged commit: {d['MEFP-8/after_route']}")
    check(d["edit_fields_clear"].startswith(refused_flag) and d["edit_fields_raise"] == "audit_signoff=true requires audit_sign_off",
          f"edit-fields cannot change a sign-off on a declared board, either way: {d['edit_fields_clear'][:80]}")
    check(d["director_edit_raise"].startswith("director_edit cannot create sign-off audit_signoff")
          and "director_edit requires a reason" in d["director_edit_unreasoned"]
          and d["ops_revokes"] == "ops cannot call director_edit" and d["after_refusals"]["audit_signoff"] is True,
          f"director-edit never creates a sign-off, needs a reason, and is the Director's alone: "
          f"{d['director_edit_unreasoned'][-120:]}")
    check(d["revoked"] == "ok" and d["after_revoke"] == {**d["after_refusals"], "audit_signoff": False}
          and d["after_revoke"]["needs_audit"] is True
          and d["revoke_comment"] == "director: Director edit: The README correction must be reviewed before integration.",
          f"the documented command, verbatim, withdraws the approval, keeps the gate and records why: {d['revoke_comment']}")
    check(d["MEFP-8/submitted"] == "ok" and d["MEFP-8/after_submit"]["state"] == "audit",
          f"the same commit, resubmitted after the withdrawal, goes to Audit: {d['MEFP-8/after_submit']}")
    check(d["MEFP-18/submitted"] == "ok" and d["MEFP-18/after_submit"]["state"] == "director_review"
          and d["MEFP-18/after_submit"]["audit_signoff"] is True,
          f"the same commit, resubmitted with its approval standing, skips the review it passed, as the docs warn: "
          f"{d['MEFP-18/after_submit']}")
    check(d["MEFP-19/submitted"] == "ok" and d["MEFP-19/after_submit"]["state"] == "audit"
          and d["MEFP-19/after_submit"]["audit_signoff"] is False
          and d["MEFP-19/after_submit"]["commit_hash"] != d["MEFP-19/after_route"]["commit_hash"],
          f"a different commit clears the carried approval by itself, with nothing withdrawn: {d['MEFP-19/after_submit']}")
    check(legacy["edit_fields_clear"] == legacy["ops_edit_fields_clear"] == "ok"
          and legacy["after_edit_fields"]["audit_signoff"] is False and legacy["after_ops_edit_fields"]["audit_signoff"] is False
          and legacy["director_edit_raise"].startswith("director_edit cannot create sign-off")
          and legacy["revoked"] == "ok" and legacy["after_revoke"]["needs_audit"] is True,
          f"on a board with no declared workflow edit-fields still lowers a sign-off, unreasoned, as the docs say; the "
          f"documented command works there too: {legacy}")
    print(f"director_approval_revocation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
