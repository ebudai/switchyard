#!/usr/bin/env python3
"""SYRD-712: a database no root record accounts for is accepted only while the cluster refuses every connection to it.

SYRD-707 could not preview the cluster's hardening: `pgu_syrd705_failed_20261030205539`, SYRD-705's preserved
evidence, has no root record, and the planner refused every such database -- though `datallowconn = false` means no
account reaches it now, and none will after. Reopening it to drop or dump it would break SYRD-705's preservation.

Against `database_auth_transition_test`'s host -- each probe decided by the files last reloaded, each database's
`datallowconn` its own, and anything done to a sealed database recorded -- main first from a git archive, then this
tree:

- a recordless, sealed database is named in the plan, its digest and the preview's opening section, and granted no
  rule; a recordless database that accepts connections is still refused;
- the accepted digest stops holding when the sealed database reopens, or another database appears, sealed or not;
- apply and its rollback never connect to it, dump, drop, rename or reopen it, and prove it still sealed after both;
  one that reopens while the transition runs fails it, and the files and jobs come back;
- registered databases and `postgres` are as before.
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
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "0c3107dc0a1ea382a3704f8affaaacb914d77e2b"
SEALED = "pgu_syrd705_failed_20261030205539"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(ROOT / "tests")]
    import database_auth_transition_test as fixture

    sys.path[:] = [str(tree)] + [p for p in sys.path if p and Path(p).resolve() != ROOT.resolve()]
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        del sys.modules[name]
    from scripts import database_auth_transition as t

    assert Path(t.__file__).resolve().is_relative_to(tree.resolve()), t.__file__
    t.pwd.getpwnam = lambda name: name  # every owner is an account on this host
    me = os.getuid()
    tenants = {"alpha": ("alice", "alpha_ticket_board"), "beta": ("bob", "beta_ticket_board")}
    registered = {"alpha_ticket_board": True, "beta_ticket_board": True}
    out: dict = {}

    def setting(databases: dict):
        tmp = Path(tempfile.mkdtemp(prefix="syrd712-t."))
        host = fixture.Host(tmp, databases=databases)
        provision, registry = fixture.records(tmp, tenants, {"alpha": "alice"})
        plan = lambda: t.build_plan(runner=host, provision_root=provision, registry=registry, owner_uid=me)  # noqa: E731
        apply = lambda digest, said: t.apply(digest, runner=host, provision_root=provision,  # noqa: E731
                                             registry=registry, owner_uid=me, backup_root=tmp / "backups",
                                             print_func=said.append, clock=lambda: 0)
        return tmp, host, plan, apply

    def state(plan) -> dict:
        return {"problems": plan.problems, "sealed": getattr(plan, "sealed", None), "digest": plan.digest,
                "tenants": [t_.database for t_ in plan.tenants], "hba_after": plan.hba_after,
                "ident_after": plan.ident_after}

    # -- preview: sealed, and without it ------------------------------------------------------------------------------
    tmp, host, plan, apply = setting({**registered, SEALED: False})
    sealed = plan()
    out["sealed"] = state(sealed)
    out["preview"] = t.preview(sealed)
    _tmp, _host, plain_plan, _apply = setting(dict(registered))
    out["without"] = state(plain_plan())
    _tmp, _host, open_plan, _apply = setting({**registered, SEALED: True})
    out["open"] = state(open_plan())

    # -- apply, then the way back -------------------------------------------------------------------------------------
    said: list[str] = []
    out["applied"] = {"code": apply(sealed.digest, said), "said": said, "log": list(host.log),
                      "hba": host.hba.read_text(), "ident": host.ident.read_text()}
    backup = tmp / "backups" / "19700101T000000Z"
    out["record"] = json.loads((backup / "record.json").read_text()) if (backup / "record.json").exists() else {}
    mark = len(host.log)
    out["restored"] = t.restore(backup, runner=host)
    out["restore_log"] = host.log[mark:]
    out["violations"] = list(host.violations)
    out["still_sealed"] = host.databases[SEALED]

    # -- drift between the preview and the apply ------------------------------------------------------------------
    for name, change in (("reopened", lambda h: h.databases.__setitem__(SEALED, True)),
                         ("another_sealed", lambda h: h.databases.__setitem__("pgu_syrd705_failed_2", False)),
                         ("another_open", lambda h: h.databases.__setitem__("gamma_ticket_board", True)),
                         ("dropped", lambda h: h.databases.pop(SEALED))):
        tmp, host, plan, apply = setting({**registered, SEALED: False})
        digest = plan().digest
        change(host)
        said = []
        out[name] = {"code": apply(digest, said), "said": said, "backups": (tmp / "backups").exists(),
                     "hba_unchanged": host.hba.read_text() == fixture.TRUSTING}

    # -- reopened while the transition runs, and after the way back ----------------------------------------------------
    tmp, host, plan, apply = setting({**registered, SEALED: False})
    digest = plan().digest
    host.flip_on_reload = SEALED
    said = []
    out["reopened_during"] = {"code": apply(digest, said), "said": said,
                              "hba_back": host.hba.read_text() == fixture.TRUSTING,
                              "ident_back": host.ident.read_text() == fixture.IDENT,
                              "loaded_back": host.loaded[0] == fixture.TRUSTING, "violations": list(host.violations)}
    tmp, host, plan, apply = setting({**registered, SEALED: False})
    digest = plan().digest
    host.drop_on_reload = SEALED
    said = []
    out["dropped_during"] = {"code": apply(digest, said), "said": said,
                             "hba_back": host.hba.read_text() == fixture.TRUSTING}
    return out


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PG"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenarios", str(tree)], capture_output=True,
                           text=True, timeout=300, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL)
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


REFUSED = f"{SEALED} is a database no root record accounts for"


def common(got: dict) -> None:
    check(got["without"]["problems"] == [] and got["without"]["tenants"] == ["alpha_ticket_board", "beta_ticket_board"],
          f"registered databases are the tenants, as before: {got['without']}")
    check(any(p.startswith(REFUSED) for p in got["open"]["problems"]),
          f"a recordless database that accepts connections is refused: {got['open']['problems']}")
    check(got["another_open"]["code"] == 1 and not got["another_open"]["backups"] and got["another_open"]["hba_unchanged"],
          f"a connectable database appearing after the preview refuses the apply: {got['another_open']}")
    check(got["violations"] == [], f"nothing connects to, changes or dumps the sealed database: {got['violations']}")


def before(got: dict) -> None:
    common(got)
    check(any(p.startswith(REFUSED) for p in got["sealed"]["problems"]) and got["sealed"]["sealed"] is None,
          f"main: the sealed evidence database is refused like a live one, so nothing can be hardened: "
          f"{got['sealed']['problems']}")
    check(got["applied"]["code"] == 1, f"main: and the apply refuses: {got['applied']['said']}")


def candidate(got: dict) -> None:
    common(got)
    sealed = got["sealed"]
    check(sealed["problems"] == [] and sealed["sealed"] == [SEALED]
          and sealed["tenants"] == ["alpha_ticket_board", "beta_ticket_board"],
          f"a recordless database the cluster refuses every connection to is sealed offline, not a refusal: {sealed}")
    check(sealed["digest"] != got["without"]["digest"],
          f"and it is part of the digest: the same host without it is another plan: {sealed['digest']}")
    check(SEALED not in sealed["hba_after"] and SEALED not in sealed["ident_after"]
          and sealed["hba_after"] == got["without"]["hba_after"] and sealed["ident_after"] == got["without"]["ident_after"],
          f"no rule is granted for it: the files are those of the host without it: {sealed['hba_after']}")
    preview = got["preview"].splitlines()
    at = next((i for i, line in enumerate(preview) if line.startswith("SEALED OFFLINE")), -1)
    check(0 < at < next(i for i, line in enumerate(preview) if line.startswith("tenants (root's records)"))
          and preview[at + 1] == f"  - {SEALED}" and "datallowconn = false" in preview[at],
          f"the preview names it before anything else it does: {preview[:8]}")
    applied = got["applied"]
    reads = [i for i, line in enumerate(applied["log"]) if line == "read databases"]
    probes = [i for i, line in enumerate(applied["log"]) if line.startswith("probe")]
    check(applied["code"] == 0 and reads and probes and reads[-1] > probes[-1],
          f"applied, and its state read again after the probes: still sealed: {applied['code']} {applied['log']}")
    check(got["record"].get("sealed") == [SEALED], f"root's backup records it: {got['record']}")
    check(got["restored"] == [] and "read databases" in got["restore_log"] and got["still_sealed"] is False,
          f"the way back proves it still sealed too, and leaves it so: {got['restored']} {got['restore_log']}")
    for name in ("reopened", "another_sealed", "dropped"):
        r = got[name]
        check(r["code"] == 1 and not r["backups"] and r["hba_unchanged"] and "not the" in " ".join(r["said"]),
              f"{name} after the preview: the accepted digest no longer holds, nothing is written: {r}")
    check(any(REFUSED in line for line in got["reopened"]["said"]) or "preview again" in " ".join(got["reopened"]["said"]),
          f"and says to preview again: {got['reopened']['said']}")
    during = got["reopened_during"]
    check(during["code"] == 1 and during["hba_back"] and during["ident_back"] and during["loaded_back"]
          and any(f"{SEALED} is no longer sealed offline (it accepts connections)" in line for line in during["said"]),
          f"reopened while the transition runs: it fails, says so, and everything comes back: {during}")
    check(during["violations"] == [], f"and even then nothing touched it: {during['violations']}")
    gone = got["dropped_during"]
    check(gone["code"] == 1 and gone["hba_back"]
          and any(f"{SEALED} is no longer sealed offline (gone)" in line for line in gone["said"]),
          f"dropped while the transition runs: the plan no longer holds, and everything comes back: {gone}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenarios":
        print(json.dumps(scenarios(Path(sys.argv[2])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd712-before.", dir="/tmp") as tmp:
        if os.environ.get("SYRD712_MUTATION_SKIP_BEFORE") != "1":
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
        candidate(run(ROOT))
    print(f"sealed_offline_database_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
