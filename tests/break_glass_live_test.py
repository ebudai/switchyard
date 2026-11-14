#!/usr/bin/env python3
"""SYRD-739 on a production-built board: one break-glass operation, and no second, on every outcome.

SYRD-672's rig (the real listener, gate and hook; the board built from this tree's migrations), with the Director refused
first. Grants are opened, ended and handed back through the board's root-only functions as the database owner, as
`pkexec switchyard break-glass` does through psql; the temporary role's requests go to the board's own HTTP route with
nothing but the token. The last scenario runs the operator command's own `run()`: a stand-in for the temporary role
reads the token from the scratch directory the command built, makes its one operation, and is refused a second.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import director_rebind_live_test as d738  # noqa: E402
import provider_account_hold_test as s672  # noqa: E402
from role_runtime_test import RuntimeRunner, _write_config  # noqa: E402
from scripts import break_glass_role as bgr, team_launcher  # noqa: E402
from scripts.ticket_board import break_glass as bg  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
REASON = "isolate the refused task while the Director's claude account is held"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Rig(d738.Rig):
    def post(self, body: Any) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + bg.PATH, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body or "{}")
            except ValueError:  # a board error page: an answer this suite judges, never a crash
                return exc.code, {"refused": f"HTTP {exc.code}: {body[:300]}"}

    def grant(self, grant: int) -> dict:
        return self.json(f"SELECT row_to_json(g)::text FROM ticket_board.break_glass_grants g WHERE id = {grant};") or {}

    def ticket(self, ticket: str) -> dict:
        return self.get(f"/api/tickets/{ticket}")


def packet_of(rig: Rig, hold: int) -> dict:
    return {"hold": hold, "state_digest": "sha256:" + "0" * 64}


def open_grant(rig: Rig, hold: int, allow: list[str], *, runtime: str = "codex", ttl: int = 600) -> tuple[int, str]:
    token = os.urandom(24).hex()
    grant = d738.RigAdmin(rig).call("ticket_board.open_break_glass", (hold, "bigint"), ("eric", "text"),
                                    (REASON, "text"), (runtime, "text"), ("{" + ",".join(allow) + "}", "text[]"),
                                    (ttl, "integer"), (bg.digest(token), "text"), (packet_of(rig, hold), "jsonb"))
    return int(grant), token


def scenario_only_root_opens_ends_and_hands_back(rig: Rig, hold: int) -> None:
    def refused(statement: str) -> str:
        return d738.refuse(rig, statement)
    token = "x" * 64
    service_open = refused("SET ROLE ticket_board_service;\nSELECT ticket_board.open_break_glass("
                           f"{hold}, 'eric', 'x', 'codex', '{{record}}', 600, '{token}', '{{\"hold\": {hold}}}');")
    service_end = refused("SET ROLE ticket_board_service;\nSELECT ticket_board.end_break_glass(1, 'aborted', 'x');")
    service_back = refused("SET ROLE ticket_board_service;\nSELECT ticket_board.hand_back_break_glass(1);")
    listener_claim = refused("SET ROLE ticket_board_listener;\nSELECT ticket_board.claim_break_glass('x', 'record', '{}');")
    check(all("permission denied" in said for said in (service_open, service_end, service_back, listener_claim)),
          f"opening, ending and handing back are root's; claiming is the service's alone: "
          f"{[s[:80] for s in (service_open, service_end, service_back, listener_claim)]}")
    admin = d738.RigAdmin(rig)
    for args, said in ((("claude", 600, hold), "under a safety hold"), (("codex", 30, hold), "60 to 3600 seconds"),
                       (("codex", 600, hold + 9), "packet is for hold")):
        try:
            admin.call("ticket_board.open_break_glass", (hold, "bigint"), ("eric", "text"), (REASON, "text"),
                       (args[0], "text"), ("{record}", "text[]"), (args[1], "integer"), ("f" * 64, "text"),
                       ({"hold": args[2]}, "jsonb"))
            got = ""
        except RuntimeError as exc:
            got = str(exc)
        check(said in got, f"{args}: the database refuses: {got[:160]}")
    bad_vocabulary = refused(f"SELECT ticket_board.open_break_glass({hold}, 'eric', 'x', 'codex', '{{merge}}', 600, "
                             f"'{'e' * 64}', '{{\"hold\": {hold}}}');")
    check("violates check constraint" in bad_vocabulary, f"no operation outside the vocabulary: {bad_vocabulary[:120]}")


def scenario_one_operation_and_no_second(rig: Rig, hold: int) -> None:
    grant, token = open_grant(rig, hold, ["record", "reassign"])
    try:
        open_grant(rig, hold, ["record"])
        second = "opened"
    except RuntimeError as exc:
        second = str(exc)
    check("another break-glass grant stands" in str(second), f"one grant at a time: {second}")
    status, wrong = rig.post({"token": "y" * 48, "operation": "record", "args": {"ticket": "PGU-2", "text": "x"}})
    check(status == 409 and "no break-glass grant has this token" in wrong["refused"]
          and rig.grant(grant)["state"] == "open", f"a wrong token is refused and spends nothing: {status} {wrong}")
    before = rig.ticket("PGU-2")
    mark = time.monotonic()
    status, done = rig.post({"token": token, "operation": "record",
                             "args": {"ticket": "PGU-2", "text": "the refused request is isolated on PGU-2"}})
    after = rig.ticket("PGU-2")
    row = rig.grant(grant)
    check(status == 200 and done["state"] == "completed" and row["state"] == "completed"
          and row["attempted"]["operation"] == "record" and row["attempted"]["args"]["ticket"] == "PGU-2"
          and row["completed"]["ticket"] == "PGU-2" and row["ended_by"] == "board"
          and any(f"Break-glass grant {grant}: the refused request is isolated" in c["text"]
                  for c in after.get("comments") or []),
          f"one authorized operation completes, through the board's ordinary path, and is journalled: {done} {row}")
    status, again = rig.post({"token": token, "operation": "reassign",
                              "args": {"ticket": "PGU-2", "assignee": "main", "reason": "move it"}})
    check(status == 409 and "it allows one operation" in again["refused"]
          and rig.ticket("PGU-2")["assignee"] == before["assignee"] and rig.grant(grant)["state"] == "completed",
          f"no second operation, whatever it is: {status} {again}")
    shown = json.dumps(rig.get(bg.PATH))
    check(token not in shown and bg.digest(token) not in shown and f'"grant": {grant}' in shown,
          "the status read shows the grant and never its token or hash")
    back = d738.RigAdmin(rig).call("ticket_board.hand_back_break_glass", (grant, "bigint"))
    rig.passes(1.5)
    check(back["handed_back"] is False and back["route"] == "rebind-director" and back["director_hold"] == hold
          and rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                      "WHERE payload->>'kind' = 'break_glass_handback';") == "0"
          and not rig.to("director", mark) and not rig.to("app", mark) and not rig.to("ops", mark),
          f"with the Director's account held, nothing is handed back and nothing reaches the held account: {back}")


def scenario_every_other_end(rig: Rig, hold: int) -> None:
    grant, token = open_grant(rig, hold, ["isolate"])
    status, said = rig.post({"token": token, "operation": "reassign",
                             "args": {"ticket": "PGU-2", "assignee": "main", "reason": "x"}})
    status2, said2 = rig.post({"token": token, "operation": "isolate", "args": {"ticket": "PGU-2"}})
    check(status == 409 and "does not allow reassign" in said["refused"] and rig.grant(grant)["state"] == "refused"
          and status2 == 409 and rig.ticket("PGU-2")["manually_controlled"] is False,
          f"an operation the grant does not allow is refused, and the grant is spent: {said} {said2}")
    grant, token = open_grant(rig, hold, ["reassign"])
    status, said = rig.post({"token": token, "operation": "reassign",
                             "args": {"ticket": "PGU-2", "assignee": "nosuchrole", "reason": "x"}})
    row = rig.grant(grant)
    check(status == 409 and row["state"] == "refused" and row["completed"]["refused"]
          and rig.ticket("PGU-2")["assignee"] == "ops",
          f"the board's own invariants refuse, and the refusal ends the grant: {said} {row['completed']}")
    grant, token = open_grant(rig, hold, ["unblock"])
    status, said = rig.post({"token": token, "operation": "unblock", "args": {"ticket": "PGU-2"}})
    check(status == 409 and "takes exactly blocker, ticket" in said["refused"] and rig.grant(grant)["state"] == "refused",
          f"arguments of the wrong shape are refused, and end the grant: {said}")
    grant, token = open_grant(rig, hold, ["record"])
    status, said = rig.post({"token": token, "operation": "abort", "args": {}})
    check(status == 200 and said.get("aborted") and rig.grant(grant)["state"] == "aborted"
          and rig.grant(grant)["ended_by"] == "role", f"the role may abort: {said}")
    grant, token = open_grant(rig, hold, ["record"], ttl=60)
    rig.sql(f"UPDATE ticket_board.break_glass_grants SET opened_at = opened_at - interval '2 minutes', "
            f"expires_at = expires_at - interval '2 minutes' WHERE id = {grant};")
    status, said = rig.post({"token": token, "operation": "record", "args": {"ticket": "PGU-2", "text": "late"}})
    check(status == 409 and "expired" in said["refused"] and rig.grant(grant)["state"] == "expired",
          f"an expired grant is refused by the database clock, and closed: {said}")
    grant, token = open_grant(rig, hold, ["record"])
    ended = d738.RigAdmin(rig).call("ticket_board.end_break_glass", (grant, "bigint"), ("expired", "text"),
                                    ("expiry", "text"))
    status, said = rig.post({"token": token, "operation": "record", "args": {"ticket": "PGU-2", "text": "after"}})
    check(ended["state"] == "expired" and status == 409, f"root's end closes an unused grant: {ended} {said}")
    grant, token = open_grant(rig, hold, ["record"])
    rig.sql(f"UPDATE ticket_board.break_glass_grants SET state = 'operating' WHERE id = {grant};")  # a board that died
    ended = d738.RigAdmin(rig).call("ticket_board.end_break_glass", (grant, "bigint"), ("aborted", "text"),
                                    ("operator", "text"))
    check(ended["state"] == "ended", f"a claim the board never finished is ended, its outcome unknown: {ended}")


class RoleTmux(RuntimeRunner):
    """The role's tmux: new-session runs the real argv start_command built -- bash, bwrap, the stand-in CLI --
    detached; has-session asks whether it still runs; kill-session ends it and keeps its report before the scratch goes."""

    def __init__(self, keep: Path) -> None:
        super().__init__(live=set())
        self.keep, self.proc, self.argv, self.home = keep, None, [], None

    def __call__(self, args: list[str], **kwargs: Any):
        import subprocess as sp

        command = [str(a) for a in args]
        if command[:1] == ["rm"]:
            return sp.run(command, capture_output=True, text=True)
        if command[:2] == ["tmux", "new-session"]:
            self.argv = command
            self.home = Path(command[command.index("--bind") + 1])
            self.proc = sp.Popen(command[command.index("/bin/bash"):], stdin=sp.DEVNULL, stdout=sp.DEVNULL,
                                 stderr=sp.PIPE, start_new_session=True)
            return sp.CompletedProcess(command, 0)
        if command[:2] == ["tmux", "has-session"]:
            return sp.CompletedProcess(command, 0 if self.proc and self.proc.poll() is None else 1)
        if command[:2] == ["tmux", "kill-session"]:
            self.calls.append(command)
            if self.proc is not None:
                if self.proc.poll() is None:
                    self.proc.terminate()
                self.proc.wait(timeout=30)
                report = self.home / "report.json"
                if report.exists():
                    (self.keep / "report.json").write_text(report.read_text())
            return sp.CompletedProcess(command, 0)
        return super().__call__(args, **kwargs)


def scenario_the_operator_command_end_to_end(rig: Rig, tmp: Path, hold: int, *, handed_back: bool) -> None:
    import break_glass_test as unit

    owner_home = tmp / "owner-home"
    planted = {"held_credential": owner_home / ".claude/.credentials.json",
               "director_transcript": owner_home / ".claude/projects/-home-o-worktrees-director/held.jsonl",
               "other_grant_token": tmp / "scratch" / "break-glass-999" / "token"}
    for relative, body in ((".codex/auth.json", '{"codex": "token"}'),):
        (owner_home / relative).parent.mkdir(parents=True, exist_ok=True)
        (owner_home / relative).write_text(body)
    for name, path in planted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"SECRET-{name}")
    cli = tmp / "install" / "codex-pkg" / "bin" / "codex"
    unit.standin_cli(cli, {name: str(p) for name, p in planted.items()},
                     operations=[["isolate", "--ticket", "PGU-1"], ["record", "--ticket", "PGU-1", "--text", "a second"]])
    config = team_launcher.load_project_config("pgu", _write_config(tmp, project="pgu"))
    real_home = team_launcher._owner_home_for_auth
    team_launcher._owner_home_for_auth = lambda _owner, *a, **k: owner_home  # type: ignore[assignment]
    import subprocess as sp
    try:
        p = bgr.plan(config, get=rig.get, hold=hold, runtime="codex", allow=["isolate"], ttl=120, runner=sp.run,
                     problems_for=lambda *a, **k: [], tools_for=lambda c, r: bgr.resolve_tools(c, str(cli), runner=sp.run))
        tmux = RoleTmux(tmp)
        printed: list[str] = []
        events: list[dict] = []
        mark = time.monotonic()
        status = bgr.run(config, p, operator="eric", reason=REASON, admin=d738.RigAdmin(rig), get=rig.get,
                         tmux_runner=tmux, scratch_root=tmp / "scratch", board_url=rig.base,
                         client=ROOT / "scripts/ticket_board/break_glass.py", journal=events.append,
                         print_func=printed.append, sleep=lambda _s: time.sleep(0.2))
    finally:
        team_launcher._owner_home_for_auth = real_home  # type: ignore[assignment]
    grant = next(e["opened"] for e in events if "opened" in e)
    row = rig.grant(grant)
    report = json.loads((tmp / "report.json").read_text()) if (tmp / "report.json").exists() else {}
    check(status == 0 and row["state"] == "completed" and row["attempted"]["operation"] == "isolate"
          and rig.ticket("PGU-1")["manually_controlled"] is True,
          f"the role, in its sandbox, makes its one operation through the client: {row} {report.get('ops')} "
          f"{printed[-3:]} {tmux.proc.stderr.read()[-300:] if tmux.proc else ''}")
    ops = report.get("ops") or [{}, {}]
    check(len(ops) == 2 and ops[0].get("rc") == 0 and ops[1].get("rc") == 1 and "it allows one operation" in ops[1].get("out", ""),
          f"and is refused a second, by the board: {ops}")
    check(all(said.startswith("DENIED") for said in report.get("probes", {"x": "READ"}).values())
          and report["own"]["token"].startswith("READ ") and report["listing"]["/home"] == ["break-glass"],
          f"from inside, the held credential, the Director's transcript and another grant's token do not exist; its "
          f"own token does: {report.get('probes')} {report.get('own', {}).get('token', '')[:12]}")
    check(not (tmp / "scratch" / f"break-glass-{grant}").exists() and "pgu-break-glass" in tmux.sessions_killed()
          and "pgu-director" not in " ".join(" ".join(c) for c in tmux.calls),
          f"the session is stopped and its scratch, credential copy and token, removed; the Director's untouched")
    rig.passes(2.0)
    told = rig.to("director", mark)
    if handed_back:
        check(row["handback"]["handed_back"] is True and any(f"break-glass grant {grant}" in m for m in told)
              and "handed back" in "\n".join(printed),
              f"with the Director's account eligible, it is handed back with one notice: {row['handback']} {told}")
    else:
        check(row["handback"]["handed_back"] is False and not told and "rebind-director pgu" in "\n".join(printed),
              f"with the Director's account held, the operator is routed to a full replacement: {printed[-1]}")
    marks = [e for e in events if "opened" in e or "started" in e or "ended" in e or "handback" in e]
    check(len(marks) == 4 and marks[2]["ended"]["state"] == "completed" and marks[2]["scratch_removed"] is True,
          f"root's journal has the grant, its start, its end and the hand-back: {events}")


def test_break_glass_on_a_production_board() -> None:
    with temporary_cluster(prefix="s739", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd739.") as tmp:
        state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
        state_dir.mkdir(), session_dir.mkdir()
        rig = Rig(cluster, state_dir, session_dir)
        try:
            hold = d738.director_first_refusal(rig)["id"]
            scenario_only_root_opens_ends_and_hands_back(rig, hold)
            scenario_one_operation_and_no_second(rig, hold)
            scenario_every_other_end(rig, hold)
            (Path(tmp) / "held").mkdir()
            scenario_the_operator_command_end_to_end(rig, Path(tmp) / "held", hold, handed_back=False)
            rig.op("clear_provider_account_hold", caller="user", runtime="claude", reason="operator confirmed it safe")
            rig.op("hold_provider_account", caller="user", runtime="agy", reason="a second account, held by hand")
            agy = rig.holds()[-1]["id"]
            rig.act("PGU-1", "set_manually_controlled", manually_controlled=False)
            (Path(tmp) / "eligible").mkdir()
            scenario_the_operator_command_end_to_end(rig, Path(tmp) / "eligible", agy, handed_back=True)
        finally:
            rig.close()


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"break_glass_live_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
