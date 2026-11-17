"""look.py <out> <tree>: run as Codex's shell tool command -- the pane's process tree (pid, ppid, session, name),
this command's ancestry, and what role_restart.other_work finds (SYRD-833)."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from scripts import role_restart  # noqa: E402
proc = Path("/proc")
def stat(pid):
    s = (proc / str(pid) / "stat").read_text()
    f = s[s.rindex(")") + 2:].split()
    return {"pid": pid, "ppid": int(f[1]), "sid": int(f[3]), "pgid": int(f[2]), "comm": s[s.index("(") + 1:s.rindex(")")],
            "cmdline": (proc / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:160]}
chain, pid = [], os.getpid()
while pid > 1:
    chain.append(stat(pid)); pid = chain[-1]["ppid"]
pane = next(c["pid"] for c in chain if stat(c["ppid"])["comm"].startswith("tmux"))
tree = role_restart.pane_tree(pane, proc)
Path(sys.argv[1]).write_text(json.dumps({"pane": pane, "me": os.getpid(), "chain": chain,
                                         "tree": [stat(p) for p in sorted(tree)],
                                         "other_work": role_restart.other_work(pane, proc)}, indent=1))
