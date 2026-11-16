"""look.py <out> <tree>: run by Claude Code's Bash tool -- what role_restart.other_work finds in this pane, and the
session this command is in (SYRD-801)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from scripts import role_restart  # noqa: E402

proc = Path("/proc")
chain, pid = [], os.getpid()
while pid > 1:
    chain.append(pid)
    stat = (proc / str(pid) / "stat").read_text()
    pid = int(stat[stat.rindex(")") + 2:].split()[1])


def comm(pid: int) -> str:
    stat = (proc / str(pid) / "stat").read_text()
    return stat[stat.index("(") + 1:stat.rindex(")")]


pane = next(p for p in chain if comm(role_restart._table(proc)[p][0]).startswith("tmux"))  # tmux's child: the pane
session = role_restart._session(os.getpid(), proc)
line = sorted(p for p in role_restart.pane_tree(pane, proc) if role_restart._session(p, proc) == session and p not in chain)
Path(sys.argv[1]).write_text(json.dumps({"pane": pane, "cli": comm(pane), "session": session, "chain": chain,
                                         "beside": [[p, comm(p)] for p in line],
                                         "other_work": role_restart.other_work(pane, proc)}))
