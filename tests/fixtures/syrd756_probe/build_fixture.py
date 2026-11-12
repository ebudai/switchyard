"""SYRD-756: write tests/fixtures/syrd756_interrupt_screens.json from probe.py's runs.

    python3 build_fixture.py RUNS_DIR OUT_JSON

RUNS_DIR holds one probe.py output directory per run: claude (a turn interrupted before it said anything),
claude-partial, codex, copilot (Esc Esc, the installed 1.0.101 loader's own package), copilot-102 (Esc Esc, the 1.0.102
package this account's Copilot runs, with --copilot-pkg), hermes and agy (each a turn that had already said something). Each screen
keeps its rows and cursor as tmux captured them, with the probe's own scratch paths taken out; each runtime keeps
what the run measured (summary.json).
"""
import json
import re
import sys
from pathlib import Path

RUNS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
PRIVATE = ("claude-1006", "scratchpad", "switchyard-agent", "91e6cd24", "TICKET_BOARD_", "SSL_CERT_FILE", "HTTPS_PROXY")


def screen(run: str, name: str) -> dict:
    rows = (RUNS / run / f"screen_{name}.txt").read_text().split("\n")
    if rows and rows[-1] == "":
        rows = rows[:-1]
    clean = []
    for row in rows:
        row = re.sub(r"/tmp/\S*/(p756f?)/[\w-]+/work", "/probe/work", row)
        clean.append("" if any(word in row for word in PRIVATE) else row)
    x, y, _pid = (RUNS / run / f"screen_{name}.cursor").read_text().split()
    return {"rows": clean, "cursor": [int(x), int(y)]}


def measured(run: str) -> dict:
    s = json.loads((RUNS / run / "summary.json").read_text())
    return {
        "version": (s.get("versions") or "").splitlines()[0] if s.get("versions") else None, "interrupt_keys": s.get("interrupt_keys"), "partial_answer_first": s.get("partial"),
        "cli_survived": s.get("cli_survived"), "pid_before": s.get("pid_before"),
        "pid_after_interrupt": s.get("pid_after_interrupt"),
        "hook_events_after_interrupt": [h.get("event", "").split(" --state-dir")[0] for h in s.get("hooks_after_interrupt") or []],
        "pane_state_after_interrupt": {k: s.get("state_after_interrupt", {}).get(k) for k in ("state", "source")},
        "next_prompt_words": s.get("after_request_words"), "resumed_by": "session id" if s.get("session") else None,
        "resumed_prompt_words": s.get("resumed_request_words"),
    }


fixture = {
    "source": "tests/fixtures/syrd756_probe/probe.py, run offline with `unshare -rn` on 2026-11-12; see its docstring",
    "screens": {
        "claude": {"idle": screen("claude-partial", "ready"), "busy": screen("claude-partial", "busy"),
                   "interrupted": screen("claude-partial", "interrupted_2"),
                   "returned": screen("claude", "interrupted_2")},
        "codex": {"idle": screen("codex", "ready"), "busy": screen("codex", "busy"),
                  "interrupted": screen("codex", "interrupted_2")},
        "copilot": {"idle": screen("copilot-102", "ready"), "busy": screen("copilot-102", "busy"),
                    "interrupted": screen("copilot-102", "interrupted_2")},
        "agy": {"idle": screen("agy", "ready"), "busy": screen("agy", "busy"),
                "interrupted": screen("agy", "interrupted_2")},
        "hermes": {"idle": screen("hermes", "ready"), "busy": screen("hermes", "busy"),
                   "after_message": screen("hermes", "after")},
    },
    "measured": {run: measured(run) for run in ("claude", "claude-partial", "codex", "copilot", "copilot-102", "hermes",
                                                "agy")},
    "notes": {
        "claude": "interrupted before it said anything: Claude put the prompt back in its composer, and PROBE-AFTER was "
                  "typed after it, so the next prompt carried both as one message",
        "hermes": "Esc does nothing while Hermes works; its own interrupt is a message (msg=interrupt): PROBE-AFTER typed "
                  "while busy ended the turn ('Interrupted during API call', then 'Sending after interrupt') and was "
                  "merged into the interrupted prompt. The saved conversation kept the prompt and the reply but not the "
                  "merged message, so the resumed prompt carries PROBE-BUSY, PROBE-REPLY and PROBE-RESUMED only",
        "agy": "no hook ran offline (agy logged that it loaded the hook file), so there is no pane state; it was resumed "
               "by the conversation id it prints as it quits",
    },
}
OUT.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
print(f"wrote {OUT}")
