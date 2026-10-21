#!/usr/bin/env python3
"""SYRD-624: the optional documentation gate, decided by a Librarian or the Director, on real boards.

Production-built boards (schema.sql, companion roles, rbac.sql, the real
ticket-board-migrate, rbac.sql again), the real HTTP server and the real notify
listener with an injected sender, and a real git repository as the board's
trusted commit cache, so a documentation commit's parent and published ref are
read by git, not stubbed:

* a Librarian board: the shipped example with `with_documentation_gate` and a
  Librarian;
* a Director-owned board: the same gate with no Librarian;
* the example without the gate, which must behave exactly as before;
* `without_documentation_gate` applied to the Librarian board.

Every authority refusal the Librarian meets over HTTP is asked again in
PostgreSQL as the service with caller role `librarian`.

The before-run (skipped with SYRD624_SKIP_BEFORE=1) runs this file's board
probe against main's tree at BEFORE, in a subprocess, with the same composed
document: main's board takes the declared gate as it is -- no migration was
needed -- and accepts a "documentation" sign-off naming a commit that is not the
candidate's child, published nowhere, which this tree refuses.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from; the before-run's tree.
BEFORE = "f22638f5ff2f72ad4b4e301e7c102d77aebad6bb"


def _tree_root() -> Path:
    """This tree, or the one `--tree` names for the before-run probe."""
    if "--tree" in sys.argv:
        return Path(sys.argv[sys.argv.index("--tree") + 1]).resolve()
    return HERE


ROOT = _tree_root()
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster, to  # noqa: E402

CHECKS = 0
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Store:
    """A git repository standing in for the board's trusted commit cache."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q")
        self.index = path.parent / f"{path.name}.index"

    def git(self, *args: str, stdin: str | None = None) -> str:
        env = {**os.environ, **GIT_ENV, "GIT_INDEX_FILE": str(self.index)} if hasattr(self, "index") else \
            {**os.environ, **GIT_ENV}
        done = subprocess.run(["git", "-C", str(self.path), *args], input=stdin, capture_output=True, text=True,
                              env=env, check=True)
        return done.stdout.strip()

    def commit(self, parents: list[str], files: dict[str, str | tuple[str, str] | None], message: str) -> str:
        """A commit on `parents` writing each file (text, or (mode, text)), or removing it (None)."""
        if parents:
            self.git("read-tree", parents[0])
        else:
            self.git("read-tree", "--empty")
        for name, content in files.items():
            if content is None:
                self.git("update-index", "--force-remove", name)
                continue
            mode, text = content if isinstance(content, tuple) else ("100644", content)
            blob = self.git("hash-object", "-w", "--stdin", stdin=text)
            self.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}")
        tree = self.git("write-tree")
        return self.git("commit-tree", tree, *[a for p in parents for a in ("-p", p)], "-m", message)

    def publish(self, ref: str, commit: str) -> None:
        self.git("update-ref", f"refs/remotes/origin/{ref}", commit)


def example() -> dict:
    """The shipped example as the Router suite uses it, plus MEFP-4's plain route back from final review."""
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    cfg["transitions"].append({
        "from": "director_review", "to": "in_progress", "action": "route", "label": "Route", "actors": ["director"],
        "primitive": "move", "owner_scoped": False, "require_commit": False, "require_reason": False,
        "clear_signoffs": [], "allow_no_code": False})
    return cfg


def open_board(cluster, name: str, document: dict, store: Store) -> Board:
    board = Board(cluster, name, document)
    board.app.commit_git_dirs = (store.path,)
    board.app.commit_git_dir = store.path
    return board


def short(answer: dict) -> str:
    text = answer.get("refused", "")
    return "ok" if "ok" in answer else "refused: " + (text if len(text) <= 700 else text[:400] + " ... " + text[-300:])


def row(board: Board, ticket: str) -> dict:
    return json.loads(board.sql(
        "SELECT json_build_object('state', state, 'assignee', assignee, 'commit', commit_hash, "
        "'audit', audit_signoff, 'inspector', inspector_signoff, 'flags', workflow_flags)::text "
        f"FROM ticket_board.tickets WHERE id = '{ticket}';"))


def last_comment(board: Board, ticket: str) -> str:
    return board.sql("SELECT who || '|' || text FROM ticket_board.ticket_comments "
                     f"WHERE ticket_id = '{ticket}' ORDER BY id DESC LIMIT 1;")


def as_role_sql(board: Board, role: str, statement: str) -> str:
    try:
        with psycopg.connect(board.service, autocommit=False) as conn:
            conn.execute("SELECT set_config('ticket_board.caller_role', %s, true)", (role,))
            conn.execute(statement)
            conn.commit()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - a refusal is an answer
        return "refused: " + str(exc).splitlines()[0]


def start(board: Board, store: Store, ticket: str, implementer: str, *, gate: bool, base: str) -> str:
    """A ticket in triage, routed, gated as asked, and submitted with a fresh candidate. Returns the candidate."""
    fixture.seed_postgres_ticket(board.admin, ticket, title=f"Fixture {ticket}", state="analysis", assignee="director")
    check(short(board.act(ticket, "route", "director", state="in_progress", assignee=implementer)) == "ok",
          f"{ticket} routed to {implementer}")
    if gate:
        check(short(board.act(ticket, "set_workflow_flags", "director", needs_documentation=True)) == "ok",
              f"the Director turns {ticket}'s documentation gate on")
    candidate = store.commit([base], {f"src/{ticket}.py": f"print('{ticket}')\n"}, f"{ticket}: the work")
    store.publish(f"roles/{implementer}/{ticket.lower()}", candidate)
    answer = board.act(ticket, "submit_to_audit", implementer, commit_hash=candidate)
    check(short(answer) == "ok", f"{implementer} submits {ticket}: {short(answer)}")
    return candidate


def finish(board: Board, ticket: str) -> None:
    """Defer a ticket so its implementer is free for the next one (cancelling a review needs its sign-off)."""
    answer = board.act(ticket, "defer", "director")
    check(short(answer) == "ok", f"{ticket} deferred to free its implementer: {short(answer)}")


def documentation_ref(owner: str, ticket: str, candidate: str) -> str:
    return f"roles/{owner}/{ticket.lower()}-{candidate[:12]}"


def before_probe(cluster, store: Store, document: dict, base: str) -> dict:
    """What a board answers to a documentation sign-off naming an unrelated, unpublished commit.

    Runs in whichever tree ROOT is. It needs nothing from documentation_gate: the
    document arrives composed.
    """
    board = open_board(cluster, "probe", document, store)
    try:
        ticket = "PGU-990"
        candidate = start(board, store, ticket, "app", gate=True, base=base)
        entered = row(board, ticket)
        unrelated = store.commit([base], {"docs/other.md": "unrelated\n"}, "not the candidate's child")
        answer = board.act(ticket, "documentation_sign_off", "librarian", commit_hash=unrelated)
        return {"entered": entered, "candidate": candidate, "unrelated": unrelated, "answer": short(answer),
                "after": row(board, ticket)}
    finally:
        board.close()


def run_before(document: dict) -> dict:
    """The probe in main's tree at BEFORE, in its own process."""
    with tempfile.TemporaryDirectory(prefix="syrd624-before.") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        archive = subprocess.run(["git", "-C", str(HERE), "archive", BEFORE], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        check(not (tree / "scripts/ticket_board/documentation_gate.py").exists(), "main has no documentation gate")
        doc = Path(tmp) / "document.json"
        doc.write_text(json.dumps(document))
        out = Path(tmp) / "out.json"
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--tree", str(tree), "--probe",
                               str(doc), str(out)], capture_output=True, text=True, timeout=900)
        check(done.returncode == 0, f"the before probe ran in main's tree: {done.stderr[-2000:]}")
        return json.loads(out.read_text())


def librarian_board(cluster, store: Store, base: str) -> None:
    from scripts.ticket_board.documentation_gate import with_documentation_gate, without_documentation_gate

    document = with_documentation_gate(example(), librarian={"runtime": "claude"})
    board = open_board(cluster, "librarian", document, store)
    try:
        board.one_pass()
        board.sent.clear()
        board.busy.clear()

        # 1. The gate is off by default: the ticket skips Documentation and goes where it always went.
        t = "PGU-901"
        start(board, store, t, "main", gate=False, base=base)
        check(row(board, t)["state"] == "audit", f"a ticket whose gate is off skips Documentation: {row(board, t)}")
        check(not to(board.one_pass(), "librarian"), "the Librarian hears nothing about it")
        board.busy.clear()
        finish(board, t)

        # 2. Only the Director turns the gate on.
        t = "PGU-902"
        fixture.seed_postgres_ticket(board.admin, t, title=t, state="analysis", assignee="director")
        board.act(t, "route", "director", state="in_progress", assignee="app")
        for role in ("app", "librarian", "audit"):
            check("only director may set gates" in short(board.act(t, "set_workflow_flags", role,
                                                                  needs_documentation=True)),
                  f"{role} cannot turn the gate on")
        finish(board, t)

        # 3. Gate on: the ticket waits in Documentation, the Librarian's, and is told once.
        t = "PGU-903"
        candidate = start(board, store, t, "app", gate=True, base=base)
        entered = row(board, t)
        check(entered["state"] == "documentation" and entered["assignee"] == "librarian",
              f"a gated ticket waits in Documentation for the Librarian: {entered}")
        notices = to(board.one_pass(), "librarian")
        check(len(notices) == 2 and notices[0] == "/clear" and t in notices[1],
              f"the Librarian starts the ticket on a cleared conversation and is told once: {notices}")
        board.busy.clear()
        actions = sorted(a["action"] for a in board.ticket(t)["workflow_actions"] if "librarian" in a["actors"])
        check(actions == ["documentation_kick_back", "documentation_no_change", "documentation_sign_off"],
              f"the Librarian is offered its three decisions and nothing else: {actions}")

        # 4. Nobody else decides, and the Librarian may do nothing else -- over HTTP and in PostgreSQL.
        check("app cannot call documentation_sign_off" in short(board.act(t, "documentation_sign_off", "app",
                                                                          commit_hash=candidate)),
              "the implementer cannot sign its own documentation off")
        for role in ("director", "audit"):
            check(short(board.act(t, "documentation_no_change", role, reason="x")).startswith("refused"),
                  f"{role} cannot decide the Librarian's documentation")
        before = board.snapshot(t)
        http = {
            "route": board.act(t, "route", "librarian", state="analysis", assignee="director"),
            "reassign": board.act(t, "reassign", "librarian", assignee="main", reason="x"),
            "edit_fields": board.act(t, "edit_fields", "librarian", body="rewritten"),
            "set_blockers": board.act(t, "set_blockers", "librarian", blocked_by=["PGU-901"], blocked_reason="x"),
            "hold": board.act(t, "set_manually_controlled", "librarian", manually_controlled=True),
            "request_publication": board.act(t, "request_publication", "librarian", ref="roles/librarian/x",
                                             commit=candidate),
            "resolve_publication": board.act(t, "resolve_publication", "librarian", request_id=1, outcome="published"),
            "merge": board.act(t, "merge", "librarian", into="PGU-901"),
            "defer": board.act(t, "defer", "librarian"),
            "cancel": board.act(t, "cancel", "librarian"),
            "audit_sign_off": board.act(t, "audit_sign_off", "librarian"),
            "director_edit": board.act(t, "director_edit", "librarian", set={"needs_audit": False}, reason="x"),
            "force_move": board.act(t, "force_move", "librarian", state="done", assignee="director"),
            "file_bug": board.act(t, "file_bug", "librarian", title="x", body="x"),
        }
        for name, answer in http.items():
            check("refused" in answer and "403" in answer["refused"],
                  f"the Librarian is not allowed to {name} over HTTP: {short(answer)}")
        q = lambda s: s.replace("'", "''")  # noqa: E731
        pg = {
            "reassign": f"SELECT ticket_board.reassign('{t}', 'main', 'x');",
            "route": f"SELECT ticket_board.route('{t}', 'analysis', 'director');",
            "edit_fields": f"SELECT ticket_board.edit_fields('{t}', '{q(json.dumps({'body': 'x'}))}'::jsonb);",
            "set_blockers": f"SELECT ticket_board.set_blockers('{t}', ARRAY['PGU-901'], 'x');",
            "cancel": f"SELECT ticket_board.perform_workflow_action('{t}', 'cancel', '{{}}'::jsonb);",
            "defer": f"SELECT ticket_board.perform_workflow_action('{t}', 'defer', '{{}}'::jsonb);",
            "update": f"UPDATE ticket_board.tickets SET state = 'audit', assignee = 'audit' WHERE id = '{t}';",
        }
        for name, statement in pg.items():
            check(as_role_sql(board, "librarian", statement).startswith("refused"),
                  f"the Librarian cannot {name} in PostgreSQL")
        check(board.snapshot(t) == before, "and none of that changed the ticket")

        # 5. A sign-off the candidate's history does not support moves nothing.
        ref = documentation_ref("librarian", t, candidate)
        # Every kind of documentation surface at once: a guide, a top-level README, a skill, an image in docs/
        # and plain text in docs/ (removals are documentation_gate.outside_documentation's, tested by the command suite).
        doc_commit = store.commit([candidate], {"docs/guide.md": f"How to use {t}\n", "README.md": "Read me.\n",
                                                "skills/demo/SKILL.md": "---\nname: demo\n---\n",
                                                "docs/img/flow.svg": "<svg/>\n", "docs/notes.txt": "notes\n"},
                                  f"{t}: documentation")
        sibling = store.commit([base], {"docs/guide.md": "unrelated\n"}, "a sibling, not on the candidate")
        other = store.commit([base], {"docs/x.md": "x\n"}, "another line of work")
        merged = store.commit([candidate, other], {"docs/guide.md": "merged\n"}, "a merge onto the candidate")
        store.publish(documentation_ref("librarian", t, sibling), sibling)
        store.publish(documentation_ref("librarian", t, candidate).replace(t.lower(), "pgu-999"), doc_commit)
        refusals = {
            "no ref": ({"commit_hash": doc_commit}, "not an unnamed ref"),
            "another ticket's ref": ({"commit_hash": doc_commit, "ref": f"roles/librarian/pgu-999-{candidate[:12]}"},
                                     f"is published at {ref}"),
            "the implementer's ref": ({"commit_hash": doc_commit, "ref": f"roles/app/{t.lower()}"},
                                      f"is published at {ref}"),
            "unpublished": ({"commit_hash": doc_commit, "ref": ref}, "is not published"),
            "the candidate itself": ({"commit_hash": candidate, "ref": ref}, "nothing was edited"),
            "with a reason": ({"commit_hash": doc_commit, "ref": ref, "reason": "x"}, "records its commit"),
            "unknown commit": ({"commit_hash": "0" * 40, "ref": ref}, "unknown commit_hash"),
        }
        for name, (payload, words) in refusals.items():
            answer = short(board.act(t, "documentation_sign_off", "librarian", **payload))
            check(words in answer, f"a sign-off with {name} is refused naming why: {answer}")
        store.publish(ref, sibling)
        answer = short(board.act(t, "documentation_sign_off", "librarian", commit_hash=sibling, ref=ref))
        check("a documentation commit has exactly one parent, the submitted candidate" in answer,
              f"a commit that is not the candidate's child is refused: {answer}")
        store.publish(ref, merged)
        answer = short(board.act(t, "documentation_sign_off", "librarian", commit_hash=merged, ref=ref))
        check("a documentation commit has exactly one parent" in answer and other[:12] in answer,
              f"a merge onto the candidate is refused, naming both parents: {answer}")
        check(board.snapshot(t) == before, "none of those refusals changed the ticket")

        # 5b. Audit's finding: a commit that changes anything but documentation never becomes the candidate,
        # however correctly it is parented and published -- refused before the ticket changes at all.
        beyond = {
            "a product file": ({"docs/guide.md": "ok\n", "scripts/tool.py": "print('changed')\n"}, "scripts/tool.py"),
            "a test fixture in Markdown": ({"tests/fixtures/screen.md": "changed\n"}, "tests/fixtures/screen.md"),
            "configuration in plain text": ({"requirements.txt": "psycopg\n"}, "requirements.txt"),
            "an executable document": ({"docs/run.md": ("100755", "#!/bin/sh\n")}, "docs/run.md (mode"),
            "a document that is a link": ({"docs/link.md": ("120000", "../scripts/tool.py")}, "docs/link.md (mode"),
            "a removed product file": ({f"src/{t}.py": None}, f"src/{t}.py"),
            "a product file renamed into docs": ({f"src/{t}.py": None, "docs/moved.md": f"print('{t}')\n"},
                                                 f"src/{t}.py"),
        }
        # Audit's second finding: an empty commit edits nothing, so it is no-change without its reason.
        empty = store.commit([candidate], {}, f"{t}: nothing")
        store.publish(ref, empty)
        answer = short(board.act(t, "documentation_sign_off", "librarian", commit_hash=empty, ref=ref))
        check("changes nothing against the candidate" in answer and "documentation_no_change with a reason" in answer,
              f"an empty documentation commit is refused, pointing at no-change: {answer}")
        check(board.snapshot(t) == before and row(board, t)["commit"] == candidate,
              f"and the ticket and its candidate are untouched: {row(board, t)}")
        for name, (files, words) in beyond.items():
            wide = store.commit([candidate], files, f"{t}: {name}")
            store.publish(ref, wide)
            answer = short(board.act(t, "documentation_sign_off", "librarian", commit_hash=wide, ref=ref))
            check("changes more than documentation" in answer and words in answer and "documentation_kick_back" in answer,
                  f"a documentation commit with {name} is refused, naming it: {answer}")
            check(board.snapshot(t) == before and row(board, t)["commit"] == candidate,
                  f"and the ticket and its candidate are untouched: {row(board, t)}")

        # 6. The documentation commit, published at the ticket's ref, becomes the candidate.
        store.publish(ref, doc_commit)
        answer = short(board.act(t, "documentation_sign_off", "librarian", commit_hash=doc_commit, ref=ref,
                                 text="Audit: only docs/guide.md changed."))
        check(answer == "ok", f"the Librarian signs off with its documentation commit: {answer}")
        after = row(board, t)
        check(after["commit"] == doc_commit and after["state"] == "audit" and after["assignee"] == "audit"
              and after["flags"].get("documentation_signoff") is True and after["audit"] is False,
              f"its commit replaced the candidate and Audit reviews it: {after}")
        record = last_comment(board, t)
        check(record.startswith("librarian|") and doc_commit in record and candidate in record and ref in record
              and "docs/guide.md" in record and "sole parent" in record and "Audit: only docs/guide.md" in record,
              f"the record names both SHAs, the ref, the paths and the note: {record}")
        finish(board, t)

        # 7. No change: a reason is required, and the candidate stays exactly as submitted.
        t = "PGU-904"
        candidate = start(board, store, t, "app", gate=True, base=base)
        before = board.snapshot(t)
        for name, payload, words in (
                ("no reason", {}, "reason required"),
                ("a blank reason", {"reason": "   "}, "reason required"),
                ("a commit", {"reason": "x", "commit_hash": candidate}, "takes no commit or ref"),
                ("a ref", {"reason": "x", "ref": documentation_ref("librarian", t, candidate)}, "takes no commit or ref"),
                ("an unbounded reason", {"reason": "x" * 4001}, "limited to 4000 characters")):
            answer = short(board.act(t, "documentation_no_change", "librarian", **payload))
            check(words in answer, f"no-change with {name} is refused: {answer}")
        check(board.snapshot(t) == before, "and changed nothing")
        answer = short(board.act(t, "documentation_no_change", "librarian",
                                 reason="Internal refactor: no command, flag or message changes."))
        check(answer == "ok", f"no-change with a reason is taken: {answer}")
        after = row(board, t)
        check(after["commit"] == candidate and after["state"] == "audit", f"the candidate is unchanged: {after}")
        record = last_comment(board, t)
        check(record.startswith("librarian|No documentation change: the candidate stays " + candidate)
              and "Internal refactor" in record, f"the decision is attributed and says so: {record}")
        finish(board, t)

        # 8. A kick-back returns the ticket with findings; the next submission comes back through Documentation.
        t = "PGU-905"
        candidate = start(board, store, t, "app", gate=True, base=base)
        answer = short(board.act(t, "documentation_kick_back", "librarian", reason="x", commit_hash=candidate))
        check("takes no commit or ref" in answer, f"a kick-back names no commit: {answer}")
        answer = short(board.act(t, "documentation_kick_back", "librarian",
                                 reason="docs/guide.md still documents --old; the change needs a code fix."))
        check(answer == "ok", f"the Librarian kicks it back with findings: {answer}")
        check(row(board, t)["state"] == "in_progress" and row(board, t)["assignee"] == "app",
              f"back to its implementer: {row(board, t)}")
        check("librarian|docs/guide.md still documents --old" in last_comment(board, t), "with the findings")
        again = store.commit([candidate], {f"src/{t}.py": "fixed\n"}, f"{t}: fixed")
        store.publish(f"roles/app/{t.lower()}", again)
        board.act(t, "submit_to_audit", "app", commit_hash=again)
        check(row(board, t)["state"] == "documentation", f"the new candidate comes back through Documentation: {row(board, t)}")
        finish(board, t)

        # 9. Documentation is never amended after a review that keeps its verdict: Audit signs off, the
        # Director routes it back with a plain move that clears nothing (MEFP-4's route), the same commit is
        # resubmitted with the gate on, and the Librarian's commit then clears Audit's sign-off.
        t = "PGU-906"
        candidate = start(board, store, t, "ops", gate=False, base=base)
        check(short(board.act(t, "audit_sign_off", "audit")) == "ok", "Audit signs off the candidate")
        check(row(board, t)["state"] == "director_review", f"it reaches final review: {row(board, t)}")
        board.act(t, "set_workflow_flags", "director", needs_documentation=True)
        check(short(board.act(t, "route", "director", state="in_progress", assignee="ops")) == "ok",
              "the Director routes it back with a plain move")
        board.act(t, "submit_to_audit", "ops", commit_hash=candidate)
        entered = row(board, t)
        check(entered["state"] == "documentation" and entered["audit"] is True,
              f"it waits for documentation still carrying Audit's sign-off on {candidate[:12]}: {entered}")
        ref = documentation_ref("librarian", t, candidate)
        doc_commit = store.commit([candidate], {"docs/guide.md": "updated\n"}, f"{t}: docs")
        store.publish(ref, doc_commit)
        check(short(board.act(t, "documentation_sign_off", "librarian", commit_hash=doc_commit, ref=ref)) == "ok",
              "the Librarian signs off with a documentation commit")
        after = row(board, t)
        check(after["state"] == "audit" and after["audit"] is False and after["commit"] == doc_commit,
              f"Audit's old sign-off is cleared and Audit reviews the documentation commit: {after}")
        finish(board, t)

        # 10. Turning the gate off: refused while a ticket waits in the stage, then the old graph returns.
        t = "PGU-907"
        start(board, store, t, "app", gate=True, base=base)
        current = board.app.workflow_configuration()
        revision = int(board.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
        try:
            board.app.apply_workflow(without_documentation_gate(current), expected_revision=revision, dry_run=False,
                                     caller_role="director")
            occupied = "ok"
        except Exception as exc:  # noqa: BLE001
            occupied = "refused: " + str(exc).splitlines()[0]
        check(occupied.startswith("refused"), f"the gate cannot be removed while a ticket waits in it: {occupied}")
        board.act(t, "documentation_no_change", "librarian", reason="nothing user-visible")
        finish(board, t)
        try:
            board.app.apply_workflow(without_documentation_gate(board.app.workflow_configuration()),
                                     expected_revision=revision, dry_run=False, caller_role="director")
            owed = "ok"
        except Exception as exc:  # noqa: BLE001
            owed = "refused: " + str(exc).splitlines()[0]
        check("outstanding review obligation" in owed,
              f"nor while a live ticket still requires documentation review (PGU-905, deferred): {owed}")
        check(short(board.act("PGU-905", "set_workflow_flags", "director", needs_documentation=False)) == "ok",
              "the Director deliberately turns that ticket's gate off")
        board.app.apply_workflow(without_documentation_gate(board.app.workflow_configuration()),
                                 expected_revision=revision, dry_run=False, caller_role="director")
        t = "PGU-908"
        fixture.seed_postgres_ticket(board.admin, t, title=t, state="analysis", assignee="director")
        board.act(t, "route", "director", state="in_progress", assignee="app")
        check("refused" in board.act(t, "set_workflow_flags", "director", needs_documentation=True),
              "with the gate removed, needs_documentation is no longer a flag")
        later = store.commit([base], {"src/later.py": "x\n"}, "later")
        store.publish("roles/app/pgu-908", later)
        board.act(t, "submit_to_audit", "app", commit_hash=later)
        check(row(board, t)["state"] == "audit", f"submissions go where they went before: {row(board, t)}")
        roles = {r["name"]: r for r in board.app.workflow_configuration()["roles"]}
        check(roles["librarian"]["active"] is False, "the Librarian stays declared, inactive")
    finally:
        board.close()


def director_board(cluster, store: Store, base: str) -> None:
    from scripts.ticket_board.documentation_gate import with_documentation_gate

    document = with_documentation_gate(example())
    check(not any(r["name"] == "librarian" for r in document["roles"]), "a Director-owned gate creates no role")
    board = open_board(cluster, "director", document, store)
    try:
        check(board.sql("SELECT count(*) FROM ticket_board.role_runtime_assignments WHERE role = 'librarian';") == "0",
              "and no runtime or session for one")
        t = "PGU-921"
        candidate = start(board, store, t, "app", gate=True, base=base)
        check(row(board, t)["state"] == "documentation" and row(board, t)["assignee"] == "director",
              f"the Director owns the decision: {row(board, t)}")
        actions = sorted(a["action"] for a in board.ticket(t)["workflow_actions"]
                         if a["action"].startswith("documentation_"))
        check(actions == ["documentation_kick_back", "documentation_no_change", "documentation_sign_off"],
              f"through the same three decisions: {actions}")
        check(all(a["primitive"] != "approve" for a in board.ticket(t)["workflow_actions"]),
              "none of which is an approval: the Director holds no sign-off")
        check(short(board.act(t, "documentation_no_change", "app", reason="x")).startswith("refused"),
              "the implementer cannot decide it")
        ref = documentation_ref("director", t, candidate)
        doc_commit = store.commit([candidate], {"docs/guide.md": "director docs\n"}, f"{t}: docs")
        store.publish(ref, doc_commit)
        answer = short(board.act(t, "documentation_sign_off", "director", commit_hash=doc_commit,
                                 ref=documentation_ref("librarian", t, candidate)))
        check(f"is published at {ref}" in answer, f"the Director's documentation ref is the Director's: {answer}")
        check(short(board.act(t, "documentation_sign_off", "director", commit_hash=doc_commit, ref=ref)) == "ok",
              "the Director signs off with a documentation commit on the candidate")
        after = row(board, t)
        check(after["commit"] == doc_commit and after["state"] == "audit", f"it is the candidate Audit reviews: {after}")
        finish(board, t)
        t = "PGU-922"
        candidate = start(board, store, t, "app", gate=True, base=base)
        check(short(board.act(t, "documentation_no_change", "director", reason="Nothing user-visible.")) == "ok",
              "the Director approves no change, with a reason")
        check(row(board, t)["commit"] == candidate and row(board, t)["state"] == "audit", "the candidate is unchanged")
        finish(board, t)
        t = "PGU-923"
        start(board, store, t, "main", gate=False, base=base)
        check(row(board, t)["state"] == "audit", "with the gate off the Director-owned stage is skipped too")
    finally:
        board.close()


def plain_board(cluster, store: Store, base: str) -> None:
    from scripts.ticket_board.workflow_config import validate

    document = example()
    check("documentation" not in [s["name"] for s in validate(json.loads(json.dumps(document)))["stages"]],
          "an example without the gate has no documentation stage")
    board = open_board(cluster, "plain", document, store)
    try:
        t = "PGU-931"
        start(board, store, t, "app", gate=False, base=base)
        check(row(board, t)["state"] == "audit", f"a board without the gate submits as before: {row(board, t)}")
        check(not any(a["action"].startswith("documentation_") for a in board.ticket(t)["workflow_actions"]),
              "and offers no documentation decision")
        check("refused" in board.act(t, "set_workflow_flags", "director", needs_documentation=True),
              "and has no needs_documentation gate to turn on")
    finally:
        board.close()


def main() -> int:
    if "--probe" in sys.argv:  # the before-run, inside main's tree
        at = sys.argv.index("--probe")
        document, out = json.loads(Path(sys.argv[at + 1]).read_text()), Path(sys.argv[at + 2])
        with tempfile.TemporaryDirectory(prefix="syrd624-probe.") as tmp, \
                temporary_cluster(prefix="syrd624-before-", shutdown="immediate") as cluster:
            store = Store(Path(tmp) / "commits")
            base = store.commit([], {"README.md": "base\n"}, "base")
            out.write_text(json.dumps(before_probe(cluster, store, document, base)))
        return 0
    from scripts.ticket_board.documentation_gate import with_documentation_gate

    with tempfile.TemporaryDirectory(prefix="syrd624.") as tmp, \
            temporary_cluster(prefix="syrd624-", shutdown="immediate") as cluster:
        store = Store(Path(tmp) / "commits")
        base = store.commit([], {"README.md": "base\n"}, "base")
        document = with_documentation_gate(example(), librarian={"runtime": "claude"})
        if not os.environ.get("SYRD624_SKIP_BEFORE"):
            before = run_before(document)
            check(before["entered"]["state"] == "documentation",
                  f"main's board takes the declared gate as it is, with no migration: {before['entered']}")
            check(before["answer"] == "ok" and before["after"]["commit"] == before["unrelated"],
                  f"main accepts a sign-off naming a commit that is not the candidate's child: {before}")
        here = before_probe(cluster, store, document, base)
        check("not an unnamed ref" in here["answer"] and here["after"] == here["entered"],
              f"this tree refuses the same sign-off and moves nothing: {here}")
        librarian_board(cluster, store, base)
        director_board(cluster, store, base)
        plain_board(cluster, store, base)
    print(f"documentation_gate_board_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
