#!/usr/bin/env python3
"""SYRD-571: the board measures its trusted commit cache although another account owns it.

The board runs as its service account; the cache belongs to the tenant and
the board reaches it through one named ACL. Git's discovery refuses such a
repository ("dubious ownership"), and every size scan ran `git -C <cache>`:
SYRD-550 and SYRD-556 were both recorded as `scan_failed` with no measurement.

The cache here is really another account's: a bare repository chowned, inside
a user namespace over this account's subordinate ids, to a host uid that is
not this one, closed to everyone, and granted to this account by name, read-
only -- the live shape (owner switchyard-agent, `user:boardsvc:r-x`,
`other::---`). This account plays the board service, with a HOME that holds
no git config. The owner also writes what a hostile owner could: diff
programs, a textconv, an fsmonitor and a replace ref in the cache's config.

A production-built board (schema.sql, the real ticket-board-migrate, rbac.sql,
a declared workflow) in this process's cluster; each board is its own child
process, so a restart is a real one; the real write CLI on the real socket.
The before-run is main's code and SQL (a025c35b), on the same cache.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a025c35b"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
LISTENER = "tests/ticket_board_notify_listener_test.py"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES) or k.startswith("GIT_")]:
        os.environ.pop(key)
    return dict(os.environ)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def lines(n: int, word: str = "x") -> str:
    return "".join(f"{word}{i} = {i}\n" for i in range(n))


def as_owner(script: str, *, handback: bool = True, **env: str) -> None:
    """`sh` as the cache's owner side: root of a namespace over this account's subordinate ids.

    There the cache belongs to namespace uid 1 (a host uid that is not this
    one) and this account is uid 0. Whatever the owner writes is handed back to
    uid 1 and closed again, and only then is this account granted read by name
    -- chmod after setfacl would clip the grant through the mask.
    """
    grant = ('chown -R 1:1 "$CACHE" && chmod -R go-rwx "$CACHE" '
                '&& setfacl -R -m u:0:rX "$CACHE" && setfacl -R -d -m u:0:rX "$CACHE"')
    steps = f"{script}\n{grant}" if handback else script
    proc = subprocess.run(["unshare", "--map-auto", "--map-root-user", "sh", "-euc", steps],
                          capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, **env})
    assert proc.returncode == 0, f"owner step failed: {proc.stderr[-2000:]}"


def commit(work: Path, branch: str, start: str | None, files: dict[str, str]) -> str:
    """A commit on a new `branch` from `start`; with no start, the first commit, on main."""
    if start:
        git(work, "checkout", "-q", "-b", branch, start)
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
        git(work, "add", path)
    git(work, "commit", "-qm", branch)
    if not start:
        git(work, "branch", branch)
    return git(work, "rev-parse", "HEAD")


def build_fixture(tmp: Path) -> dict:
    """The SYRD-550 and SYRD-556 shapes, at their measured sizes, in a cache another account owns."""
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "config", "user.name", "t")
    base = {LISTENER: lines(3851), "scripts/ticket_board/pane_activity_gate.py": lines(1086),
            "scripts/ticket-board-pane-idle-hook": lines(871), "scripts/ticket_board/schema.sql": lines(13000, "s"),
            "scripts/small.py": lines(10)}
    shas = {"B": commit(work, "base", None, base)}
    # SYRD-550 (cc4bfbf0 on 2951c5c9): the listener test grew 3,851 -> 3,859; the gate 1,086 -> 1,098.
    shas["C550"] = commit(work, "c550", "base", {LISTENER: lines(3859), "scripts/ticket_board/pane_composer.py": lines(92),
                                                 "scripts/ticket_board/pane_activity_gate.py": lines(1098)})
    # SYRD-556 (8e90ab96 on cc4bfbf0): the idle hook 871 -> 990, a new 321-line test; nothing near 1,100.
    shas["C556"] = commit(work, "c556", "c550", {"scripts/ticket-board-pane-idle-hook": lines(990),
                                                 "tests/hermes_background_review_test.py": lines(321)})
    shas["C557"] = commit(work, "c557", "c550", {"scripts/small.py": lines(12)})  # never published to the cache
    shas["C559"] = commit(work, "c559", "c550", {"scripts/ticket_board/schema.sql": lines(13000, "s") + lines(40, "q")})
    shas["R559"] = commit(work, "r559", "c550", {"scripts/small.py": lines(11)})  # what a replace ref would show instead
    hook = tmp / "hook.sh"
    marker = tmp / "hook-ran"
    hook.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\nexit 1\n")
    hook.chmod(0o755)
    cache = tmp / "cache.git"
    subprocess.run(["git", "init", "-q", "--bare", str(cache)], check=True)
    subprocess.run(["git", f"--git-dir={cache}", "fetch", "-q", str(work), "base:refs/remotes/origin/main",
                    *(f"{b}:refs/heads/roles/main/{b}" for b in ("c550", "c556", "c559", "r559"))], check=True)
    # What the cache's owner can configure, and the board must never act on.
    as_owner(f"""
git -c safe.directory='*' --git-dir="$CACHE" config core.fsmonitor {hook}
git -c safe.directory='*' --git-dir="$CACHE" config diff.external {hook}
git -c safe.directory='*' --git-dir="$CACHE" config diff.evil.textconv {hook}
git -c safe.directory='*' --git-dir="$CACHE" config diff.evil.command {hook}
mkdir -p "$CACHE/info" && echo '* diff=evil' > "$CACHE/info/attributes"
git -c safe.directory='*' --git-dir="$CACHE" replace {shas['C559']} {shas['R559']}
git -c safe.directory='*' --git-dir="$CACHE" config core.repositoryformatversion 1
git -c safe.directory='*' --git-dir="$CACHE" config extensions.partialClone evil
git -c safe.directory='*' --git-dir="$CACHE" config remote.evil.url {work}
git -c safe.directory='*' --git-dir="$CACHE" config remote.evil.promisor true
git -c safe.directory='*' --git-dir="$CACHE" config remote.evil.uploadpack {hook}
""", CACHE=str(cache))
    decoy = tmp / "decoy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(decoy)], check=True)
    home = tmp / "board-home"
    home.mkdir()
    return {"tmp": str(tmp), "work": str(work), "cache": str(cache), "hook": str(hook), "marker": str(marker),
            "decoy": str(decoy), "home": str(home), "shas": shas}


def board_git_env(fixture: dict) -> dict[str, str]:
    return {**os.environ, "HOME": fixture["home"]}


def tree_at(commit_id: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit_id, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def build_board(root: Path, cluster, db: str) -> str:
    import ticket_board_write_api_test as t

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
    with contextlib.suppress(AssertionError):
        t.create_roles(admin)
    t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    migrate = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                             env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, capture_output=True, text=True)
    assert migrate.returncode == 0, migrate.stderr
    t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
    return admin


def phase(root: Path, state: dict, number: int) -> dict:
    """One board process: started, driven through the real CLI, stopped."""
    for extra in (str(root / "tests"), str(root)):
        if extra in sys.path:
            sys.path.remove(extra)
        sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from scripts.ticket_board.board_notifications import TicketBoardEventHub
    from scripts.ticket_board.server import TicketBoardUnixServer
    from workflow_document_eras import before_relaying

    fixture, run = state["fixture"], state["run"]
    shas, admin = fixture["shas"], run["admin"]
    app = t.TicketBoardApp(Path(run["dir"]) / "frames", Path(run["dir"]) / "assets", project="cerulean",
                           ticket_prefix="PGU", commit_git_dir=fixture["cache"], database_url=run["service"])
    if number == 1:
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
    sock = Path(run["dir"]) / "board.sock"
    servers: dict = {}

    def serve(role: str) -> None:
        if role not in servers:
            for srv, events in servers.values():
                srv.shutdown()
                srv.server_close()
                events.close()
            servers.clear()
            events = TicketBoardEventHub(app)
            srv = TicketBoardUnixServer(sock, app, events=events, director_notifier=t.QuietNotifier(),
                                        role_authority=t.local_role_authority_as(role))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers[role] = (srv, events)

    def cli(role: str, *args: str) -> list:
        serve(role)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = write_cli.main(["--socket", str(sock), "--caller-role", role, *args])
            except SystemExit as exc:
                code = int(exc.code or 0)
        return [code, out.getvalue(), err.getvalue()]

    def scan(tid: str) -> dict:
        row = t.psql(admin, f"SELECT row_to_json(s)::text FROM ticket_board.size_scans s WHERE ticket_id = '{tid}';").strip()
        return json.loads(row) if row else {}

    def findings(tid: str) -> list:
        return json.loads(t.psql(admin, (
            "SELECT coalesce(jsonb_agg(jsonb_build_array(path, state, reason, before_lines, after_lines) ORDER BY path), "
            f"'[]')::text FROM ticket_board.size_findings WHERE ticket_id = '{tid}';")).strip())

    def audited(tid: str, sha: str) -> dict:
        """Seed `tid` at Audit with `sha` and approve it: what the board measured and decided."""
        t.seed_postgres_ticket(admin, tid, title=tid, state="audit", assignee="audit", commit_hash=sha)
        return approve(tid)

    def approve(tid: str) -> dict:
        code, _out, err = cli("audit", "audit-sign-off", tid, "--text", "reviewed")
        state_now = t.psql(admin, f"SELECT state FROM ticket_board.tickets WHERE id = '{tid}';").strip()
        return {"code": code, "err": err[-1500:], "state": state_now, "scan": scan(tid), "findings": findings(tid)}

    seen: dict = {}
    try:
        if number == 1:
            seen["p550"] = audited("PGU-550", shas["C550"])
            seen["enable"] = cli("director", "enable-size-review", "--apply")
            seen["p551"] = audited("PGU-551", shas["C550"])
            return seen
        seen["p551_durable"] = (scan("PGU-551"), findings("PGU-551"))
        seen["p556"] = audited("PGU-556", shas["C556"])
        if run["name"] == "before":
            return seen
        seen["remeasure_551"] = cli("director", "measure-size", "PGU-551", "--commit", shas["C550"])
        seen["p551_remeasured"] = (scan("PGU-551"), findings("PGU-551"))
        cache = fixture["cache"]
        # Missing object: refused with the reason and the repair, then measured once the owner has it.
        seen["p557_missing"] = audited("PGU-557", shas["C557"])
        as_owner(f'git -c safe.directory="*" --git-dir="$CACHE" fetch -q {fixture["work"]} '
                 'c557:refs/heads/roles/main/c557', CACHE=cache)
        seen["p557_published"] = approve("PGU-557")
        # The board's account refused: the reason names the supported repair; the grant restored, measured.
        as_owner('setfacl -x u:0 "$CACHE"', handback=False, CACHE=cache)
        seen["p558_refused"] = audited("PGU-558", shas["C556"])
        as_owner(":", CACHE=cache)
        seen["p558_repaired"] = approve("PGU-558")
        as_owner('setfacl -x u:0 "$CACHE/objects"', handback=False, CACHE=cache)
        seen["p564_refused"] = audited("PGU-564", shas["C556"])
        as_owner(":", CACHE=cache)
        # One object the board may not read is not a missing commit: git's reason, and the repair.
        as_owner(f'setfacl -x u:0 "$CACHE/objects/{shas["C556"][:2]}"', handback=False, CACHE=cache)
        seen["p565_refused"] = audited("PGU-565", shas["C556"])
        as_owner(":", CACHE=cache)
        # The owner's diff programs, fsmonitor and replace ref, under the board's measurement.
        seen["p559"] = audited("PGU-559", shas["C559"])
        # And an environment pointing git somewhere else, as an owner-written EnvironmentFile could.
        hostile = {"GIT_DIR": fixture["decoy"], "GIT_OBJECT_DIRECTORY": fixture["decoy"] + "/objects",
                   "GIT_EXTERNAL_DIFF": fixture["hook"], "GIT_CONFIG_PARAMETERS": f"'diff.external'='{fixture['hook']}'",
                   "GIT_REPLACE_REF_BASE": "refs/replace/"}
        os.environ.update(hostile)
        try:
            seen["p560_env"] = audited("PGU-560", shas["C559"])
        finally:
            for key in hostile:
                os.environ.pop(key)
        # A close measured on the base an earlier scan recorded, which the cache no longer has.
        t.seed_postgres_ticket(admin, "PGU-563", title="PGU-563", state="audit", assignee="audit", commit_hash=shas["C556"])
        t.psql(admin, "INSERT INTO ticket_board.size_scans (ticket_id, candidate, base) "
                      f"VALUES ('PGU-563', '{shas['C556']}', '{'ab' * 20}');")
        seen["base_missing"] = cli("director", "measure-size", "PGU-563", "--commit", shas["C556"])
        seen["base_missing_scan"] = scan("PGU-563")
        # Ticket input that is not a commit id never reaches git.
        before = scan("PGU-556")
        out_file = Path(fixture["tmp"]) / "written-by-git"
        seen["hostile_commits"] = [cli("director", "measure-size", "PGU-556", "--commit", raw)[0] for raw in (
            f"--output={out_file}", "HEAD:../../etc/passwd", fixture["decoy"], "roles/main/c550", f"{shas['C556']}:scripts")]
        seen["hostile_untouched"] = scan("PGU-556") == before and not out_file.exists()
        # The configured cache swapped for a symlink to a repository that has the commit: not followed.
        real = Path(cache).with_name("cache.real")
        Path(cache).rename(real)
        Path(cache).symlink_to(Path(fixture["work"]) / ".git")
        try:
            seen["symlink"] = cli("director", "measure-size", "PGU-556", "--commit", shas["C556"])
            seen["symlink_scan"] = scan("PGU-556")
        finally:
            Path(cache).unlink()
            real.rename(cache)
        return seen
    finally:
        for srv, events in servers.values():
            srv.shutdown()
            srv.server_close()
            events.close()


def board_process(root: Path, state: dict, number: int, fixture: dict) -> dict:
    """A board process of its own: whatever one run kept in memory, the next does not have."""
    path = Path(state["run"]["dir"]) / f"state-{number}.json"
    path.write_text(json.dumps(state))
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", str(number), str(root), str(path)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**board_git_env(fixture), "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--phase":
        clean_env()
        print(json.dumps(phase(Path(sys.argv[3]), json.loads(Path(sys.argv[4]).read_text()), int(sys.argv[2]))))
        return 0
    clean_env()
    for extra in (str(ROOT), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from temporary_cluster import temporary_cluster

    tmp = Path(tempfile.mkdtemp(prefix="syrd571."))
    try:
        fixture = build_fixture(tmp)
        shas, cache = fixture["shas"], Path(fixture["cache"])
        env = board_git_env(fixture)
        info = cache.stat()
        acl = subprocess.run(["getfacl", "-p", "-n", str(cache)], check=True, capture_output=True, text=True).stdout
        discovered = subprocess.run(["git", "-C", str(cache), "rev-parse", "HEAD"], capture_output=True, text=True, env=env)
        check(info.st_uid != os.geteuid() and info.st_mode & 0o007 == 0 and f"user:{os.geteuid()}:r-x" in acl
              and "dubious ownership" in discovered.stderr,
              f"the fixture is the live shape: another account's cache, closed, granted to the board by name, and "
              f"refused by git's discovery: uid {info.st_uid} mode {oct(info.st_mode)} {acl} {discovered.stderr}")
        live = subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={cache}", "diff", "-U0", shas["B"], shas["C550"]],
                              capture_output=True, text=True, env=env)
        ran = Path(fixture["marker"]).read_text() if Path(fixture["marker"]).exists() else ""
        replaced = subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={cache}", "rev-parse", f"{shas['C559']}^{{tree}}"],
                                  capture_output=True, text=True, env=env).stdout.strip()
        check(f"{fixture['hook']} scripts/ticket_board/pane_activity_gate.py" in ran and replaced == git(Path(fixture["work"]), "rev-parse", f"{shas['R559']}^{{tree}}"),
              f"the owner's attacks are live for a git that trusts the cache: its diff program ran, and C559 reads as "
              f"R559: {ran} {replaced}")
        Path(fixture["marker"]).unlink()

        with temporary_cluster(prefix="syrd571-board.", shutdown="immediate") as cluster:
            import ticket_board_write_api_test as t

            runs = {}
            for name, root in (("before", tree_at(BEFORE, tmp / "before-tree")), ("after", ROOT)):
                run_dir = tmp / f"run-{name}"
                run_dir.mkdir()
                runs[name] = {"name": name, "root": str(root), "dir": str(run_dir),
                              "admin": build_board(root, cluster, f"board_{name}"),
                              "service": t.conninfo(cluster.socket_dir, cluster.port, f"board_{name}", t.SERVICE_ROLE)}
            seen = {name: {} for name in runs}
            for name, run in runs.items():
                seen[name].update(board_process(Path(run["root"]), {"fixture": fixture, "run": run}, 1, fixture))
            home_after_enable = sorted(p.name for p in Path(fixture["home"]).iterdir())
            # The owner's cache moves on: SYRD-550 has landed, and SYRD-556 is measured against it.
            as_owner(f'git -c safe.directory="*" --git-dir="$CACHE" update-ref refs/remotes/origin/main {shas["C550"]}',
                     CACHE=str(cache))
            seen["before"].update(board_process(Path(runs["before"]["root"]), {"fixture": fixture, "run": runs["before"]},
                                                2, fixture))
            # The board's HOME is the owner's home (`Environment=HOME=`), and the owner breaks its git config there.
            (Path(fixture["home"]) / ".gitconfig").write_text("[[[ not a git config\n")
            seen["after"].update(board_process(ROOT, {"fixture": fixture, "run": runs["after"]}, 2, fixture))
        b, a = seen["before"], seen["after"]

        # Before: both shapes fail to measure on ownership, and the review cannot be enabled.
        for key in ("p550", "p556"):
            check("dubious ownership" in (b[key]["scan"].get("error") or "") and b[key]["scan"]["report"] == []
                  and b[key]["findings"] == [["*", "open", "scan_failed", None, None]],
                  f"before: {key} is a failed scan on the cache's ownership, with no measurement: {b[key]}")
        check(b["enable"][0] != 0 and "dubious ownership" in b["enable"][2],
              f"before: enabling the review fails on the cache's ownership: {b['enable']}")

        # After: SYRD-550's shape is measured, against main, with its real sizes.
        p550 = a["p550"]["scan"]
        check(p550.get("error") is None and p550["candidate"] == shas["C550"] and p550["base"] == shas["B"]
              and [(f["path"], f["before"], f["after"], f["band"], f["finding"]) for f in p550["report"]]
              == [(LISTENER, 3851, 3859, "review", "growth")]
              and a["p550"]["findings"] == [[LISTENER, "open", "growth", 3851, 3859]],
              f"SYRD-550's shape is measured, not scan_failed: the listener test 3,851 -> 3,859 is growth: {a['p550']}")
        check(a["p550"]["code"] == 0 and a["p550"]["state"] != "audit",
              f"with the review not enabled, nothing is gated: defaults are unchanged: {a['p550']['state']}")
        enabled = json.loads(a["enable"][1]) if a["enable"][0] == 0 else {}
        check(enabled.get("applied") is True and enabled.get("baseline") == shas["B"]
              and {"path": LISTENER, "lines": 3851} in enabled.get("inventory", []),
              f"the review is enabled on this board as provisioned, with no git config anywhere: {a['enable']}")
        check(home_after_enable == [], f"and nothing wrote a global git config for it: {home_after_enable}")
        p551 = a["p551"]
        check(p551["code"] != 0 and p551["state"] == "audit" and f"{LISTENER} (growth: 3851 -> 3859 lines)" in p551["err"]
              and p551["findings"] == [[LISTENER, "open", "growth", 3851, 3859]],
              f"enabled, the same growth is a valid finding that refuses the approval, naming the file: {p551}")

        # A restart: the next board process finds the measurement where the last one left it, and measures again.
        check(a["p551_durable"][0]["report"] == p551["scan"]["report"] and a["p551_durable"][1] == p551["findings"],
              f"the measurement and the finding survive the board's restart: {a['p551_durable']}")
        check(a["remeasure_551"][0] == 0 and a["p551_remeasured"][0].get("error") is None
              and a["p551_remeasured"][0]["base"] == shas["B"] and a["p551_remeasured"][1] == p551["findings"],
              f"and the restarted board measures again, with nothing re-applied: {a['remeasure_551']} {a['p551_remeasured']}")
        p556 = a["p556"]["scan"]
        # From here the board's HOME holds a git config git cannot parse: the board never reads it.
        check(a["p556"]["code"] == 0 and a["p556"]["state"] != "audit" and p556.get("error") is None
              and p556["candidate"] == shas["C556"] and p556["base"] == shas["C550"] and p556["report"] == []
              and a["p556"]["findings"] == [],
              f"SYRD-556's shape, on the restarted board with the review enabled: measured against main (cc4bfbf0's "
              f"stand-in), nothing at or over 1,100 lines, approved: {a['p556']}")

        # A commit the cache does not have: the reason, and the supported repair.
        missing = a["p557_missing"]
        error = missing["scan"].get("error") or ""
        check(missing["code"] != 0 and missing["findings"] == [["*", "open", "scan_failed", None, None]]
              and f"the candidate {shas['C557']} is not in the board's trusted commit cache {cache}" in error
              and "switchyard-publish-candidate" in error
              and f"ticket-board-write measure-size PGU-557 --commit {shas['C557']}" in error,
              f"a missing candidate is a failed scan that says where it is missing and how to bring it: {missing}")
        published = a["p557_published"]
        check(published["code"] == 0 and published["scan"].get("error") is None and published["scan"]["base"] == shas["C550"]
              and published["findings"] == [["*", "resolved", "scan_failed", None, None]],
              f"once the owner has it, the next approval measures and the failed scan resolves: {published}")

        # The board's account refused by the cache: the reason, and the supported repair.
        refused = a["p558_refused"]
        error = refused["scan"].get("error") or ""
        check(refused["code"] != 0 and f"the board cannot read its trusted commit cache {cache}" in error
              and "Repair: pkexec switchyard repair-boundary cerulean --apply" in error,
              f"without its grant the board says so and names the repair: {refused}")
        refused = a["p564_refused"]
        error = refused["scan"].get("error") or ""
        check(refused["code"] != 0 and f"the board cannot read its trusted commit cache {cache}: no read access to {cache}/objects"
              in error and "Repair: pkexec switchyard repair-boundary cerulean --apply" in error,
              f"nor when the grant is gone from the objects alone: {refused}")
        refused = a["p565_refused"]
        error = refused["scan"].get("error") or ""
        check(refused["code"] != 0 and "Permission denied" in error and "is not in the board's trusted commit cache" not in error
              and "Repair: pkexec switchyard repair-boundary cerulean --apply" in error,
              f"and an object it may not read is git's own reason with the repair, never called missing: {refused}")
        repaired = a["p558_repaired"]
        check(repaired["code"] == 0 and repaired["scan"].get("error") is None and repaired["scan"]["base"] == shas["C550"],
              f"the grant restored, it measures: {repaired}")

        # Nothing the owner configured, and nothing in the board's environment, runs or redirects the scan.
        for key in ("p559", "p560_env"):
            got = a[key]
            check(got["scan"].get("error") is None and got["scan"]["candidate"] == shas["C559"]
                  and got["findings"] == [["scripts/ticket_board/schema.sql", "open", "growth", 13000, 13040]],
                  f"{key}: the exact candidate is measured, not what the replace ref or the environment names: {got}")
        check(not Path(fixture["marker"]).exists(),
              f"and no diff program, textconv, fsmonitor or promisor fetch of the owner's ran: {Path(fixture['marker']).read_text() if Path(fixture['marker']).exists() else ''}")
        check(a["base_missing"][0] == 0 and f"the base {'ab' * 20} is not in the board's trusted commit cache {cache}"
              in (a["base_missing_scan"].get("error") or ""),
              f"a recorded base the cache no longer has is a failed scan that says so: {a['base_missing_scan']}")
        check(a["hostile_untouched"],
              f"an option, a path, a ref the cache has, or a tree path, given as the commit, is never measured or followed: {a['hostile_commits']}")
        check(a["symlink"][0] == 0 and "is a symlink; the board reads only the repository it names" in (a["symlink_scan"].get("error") or ""),
              f"a symlink in place of the configured cache is not followed: {a['symlink_scan']}")
        # The boundary itself: reads only, and reasons for a cache that cannot be one.
        from scripts.ticket_board.commit_cache_reader import cache_reader

        reader = cache_reader(cache)
        refusals = []
        for args in (["fetch", "origin"], ["update-ref", "refs/heads/x", shas["B"]], ["gc"], [],
                     ["diff", "--no-ext-diff", shas["B"], shas["C550"]], ["diff", "--no-textconv", shas["B"], shas["C550"]]):
            try:
                reader.git_command(args)
            except Exception as exc:
                refusals.append(f"{type(exc).__name__}: {exc}")
        check(len(refusals) == 6 and all(r.startswith("ValueError: ") for r in refusals)
              and reader.git_command(["cat-file", "-t", shas["B"]])[0][-3:] == ["cat-file", "-t", shas["B"]],
              f"the reader runs reads only, and no diff that could run the cache's diff programs: {refusals}")
        (tmp / "not-a-repo").mkdir()
        reasons = []
        for path in (tmp / "absent.git", tmp / "not-a-repo"):
            try:
                cache_reader(path)
            except Exception as exc:
                reasons.append(f"{type(exc).__name__}: {exc}")
        check(reasons == [f"ValueError: the trusted commit cache {tmp / 'absent.git'} does not exist",
                          f"ValueError: the trusted commit cache {tmp / 'not-a-repo'} is not a git repository: it has no objects"],
              f"a cache that is missing, or is no repository, is said to be so: {reasons}")
        after = subprocess.run(["git", "-C", str(cache), "rev-parse", "HEAD"], capture_output=True, text=True,
                               env={**env, "GIT_CONFIG_GLOBAL": os.devnull})
        check(cache.stat().st_uid != os.geteuid() and "dubious ownership" in after.stderr,
              "the cache was another account's, and refused to discovery, throughout")
    finally:
        as_owner(f"python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1])' {tmp}", handback=False)
    print(f"size_review_cache_ownership_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
