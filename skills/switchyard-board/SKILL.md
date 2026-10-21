---
name: switchyard-board
description: Read and write this project's Switchyard ticket board safely from any agent CLI - inspect tickets read-only, read a whole ticket before acting, take only the transitions your role is entitled to, attach honest commit provenance, set blockers, and record review sign-offs. Use whenever a ticket is mentioned, assigned, reviewed, or advanced.
---

# Switchyard ticket board

The board is the coordination record for this project. Every claim about state,
ownership, and provenance lives there, not in chat.

Nothing in this skill names a project, a port, a role, a stage, or a database.
All of that is per-project and is discovered at runtime. If you find yourself
typing a literal prefix or address, you are guessing.

**If `TICKET_BOARD_CALLER_ROLE` is `router`, stop here: this skill is not
yours.** Router's whole remit is its onboarding prompt - one read of the ticket
it was told about, then `router-assign` or `router-return` - and nothing below
applies to it.

**If `TICKET_BOARD_CALLER_ROLE` is `librarian`, load `switchyard-librarian`
too, and let it narrow this one:** the Librarian reads only the ticket it was
given and makes one of three documentation decisions; nothing below gives it
more. An implementer's ticket may pass through the Librarian's Documentation
stage after submission; if the Librarian signs off with a documentation commit,
that commit, with yours as its parent, is the ticket's candidate from then on.

## What the environment already tells you

Every Switchyard pane is launched with its board address and identity:

```bash
echo "$TICKET_BOARD_URL"            # HTTP root for reads
echo "$TICKET_BOARD_SOCKET"         # local write socket
echo "$TICKET_BOARD_CALLER_ROLE"    # the role you are acting as
echo "$TICKET_BOARD_TICKET_PREFIX"  # this project's ticket prefix
```

Use the variables. A pane can only reach its own project's board, which is what
keeps one project from writing to another's. `ticket-board-read` and
`ticket-board-write` are installed on `PATH` and default to these values, so you
rarely pass an address at all.

If `TICKET_BOARD_CALLER_ROLE` is empty you are not in a role pane. Find out who
you are before writing anything; do not assume a default.

Your pane also commits as your role: `git config --show-origin user.name` says
`command line`, and each commit you make names your role. Do not set
`user.name` or `user.email` with `git config`. Every worktree of a repository
shares that repository's config, so a name set there becomes every other
worker's name outside its pane.

## Reading

Reads are HTTP GET. They never register you, notify anyone, or change state.

```bash
ticket-board-read ticket "$TICKET_BOARD_TICKET_PREFIX-123"   # one full ticket
ticket-board-read ticket "$TICKET_BOARD_TICKET_PREFIX-123" --json
ticket-board-read comments "$TICKET_BOARD_TICKET_PREFIX-123"
ticket-board-read queue "$TICKET_BOARD_CALLER_ROLE"          # your active work
ticket-board-read board [--all]                              # every stage
ticket-board-read blockers "$TICKET_BOARD_TICKET_PREFIX-123"
ticket-board-read director                                   # needs director attention
```

`--json` works on all of them, and `curl -s "$TICKET_BOARD_URL/api/tickets/<id>"`
returns the same document if you would rather parse it directly.

**Never comment to have a look.** A comment is a real write: it is attributed to
you, it is permanent, and it notifies the assignee. Reading is free; peeking by
commenting is not.

**Read the whole ticket before you act on it.** Not the title, not the
notification line that woke you. The body, the `implementation` field, the
blockers, the flags, and every comment - the comments are usually where the
decision that changes your approach is recorded. A notification is a pointer to
the ticket, never a substitute for it.

## Which moves are legal for you

Do not memorise a stage sequence. The board publishes, per ticket, exactly which
operations exist right now:

```bash
ticket-board-read ticket "$TICKET_BOARD_TICKET_PREFIX-123" --json \
  | python3 -c 'import json,sys
t = json.load(sys.stdin)
for a in t["workflow_actions"]:
    print(a["action"], "->", a["to"], "| actors:", ",".join(a["actors"]),
          "| commit" if a["require_commit"] else "",
          "| reason" if a["require_reason"] else "",
          "| owner-scoped" if a["owner_scoped"] else "")'
```

An action is yours to take when `TICKET_BOARD_CALLER_ROLE` appears in its
`actors`, and - when `owner_scoped` is true - when the ticket is assigned to you.
Anything else belongs to another role.

Permissions are enforced in the database. A refusal naming a constraint is the
system working correctly; read the message and change your plan, do not look for
a way around it. In particular, do not re-run a refused write with a different
`--caller-role`: acting as a role you are not is impersonation, it is recorded
under that role's name, and it corrupts the one record everyone else trusts.
`--caller-role` exists to be explicit about the role you already are.

## Writing

```bash
ticket-board-write <action> "$TICKET_BOARD_TICKET_PREFIX-123" [options]
ticket-board-write workflow-action "$TICKET_BOARD_TICKET_PREFIX-123" <action>
```

The subcommand is the action name with dashes. `ticket-board-write --help` lists
what this board actually supports; `<action> --help` gives its required options.
Prefer that over remembering a command you saw once - operations are per-project
and they change.

Write comment bodies through a quoted heredoc, so backticks and `$` in the text
stay inert:

```bash
ticket-board-write add-comment "$TICKET_BOARD_TICKET_PREFIX-123" --text "$(cat <<'EOF'
Body with `backticks` and $variables, safely literal.
EOF
)"
```

## Commit provenance

An action whose `require_commit` is true takes a real commit hash, and the hash is
resolved by running git in your **current working directory**. Run the submission
from the checkout that holds the commit - for a ticket whose code lives in another
repository, that is that repository's checkout, not the pane's default directory.

```bash
git rev-parse HEAD
git merge-base --is-ancestor <commit> origin/main   # is it actually on main yet?
```

A `commit_hash` recorded on a ticket is a claim, not proof. Verify ancestry before
you rely on someone else's.

**Never borrow a hash.** Pointing the commit field at unrelated work is worse than
leaving it empty: it resolves, it looks authoritative, and it sends a reviewer into
somebody else's diff.

When work is genuinely finished and produced no commit - a spike whose deliverable
is the finding, an investigation that concluded "not reproduced", a decision
recorded on the ticket - use the no-commit submission the board offers
(`allow_no_code` in `workflow_actions`, typically
`submit-to-audit-without-commit`) and give a real `--reason`. The reason is posted
as an attributed comment and reviewed like any other submission. If instead the ask
itself needs rethinking, hand it back rather than submitting nothing.

## Blockers and waiting

A ticket that cannot proceed stays in its own stage with its own assignee. Do not
move it to backlog; that reads as abandoned.

```bash
ticket-board-write request-dependency <id> --role <role> --reason "what they have to do"
ticket-board-write set-blockers <id> --blocked-by <other-ticket-id> --blocked-reason "..."
ticket-board-write await-role <id> --role <role>
ticket-board-write clear-awaiting-role <id>
```

**Saying you are blocked is not the same as recording it.** A comment explaining
that you need something from another role leaves the board thinking the work is
yours and moving: the ticket stays in your stage with your name on it, nobody is
told, and it sits there. Use `request-dependency` - it writes your reason as the
comment you would have written AND sets the durable wait, in one action, so the
half that matters cannot be the half you forget. It does not reassign anything,
because the work is still yours; it is waiting.

`blocked_by` is for a dependency you can point at a ticket: one on this board
(`PREFIX-N`), work on another board (`project:PREFIX-N`), or a person whose
step has no ticket (`operator:<name>`). Only the director sets blockers. An external blocker never
clears by itself - the director releases it explicitly once the thing you need
is really there - and while it stands it silences your reminders and refuses
your submission, so you are not asked to act on work that cannot move. If you
are waiting on another board's work or on a person, ask the director once
(`request-dependency`) to record that blocker rather than re-opening the same
dependency; you are told once when it is released, with the result. `await-role` is the bare
wait without a reason - prefer `request-dependency`, which is that plus the
sentence the awaited role needs. Either way the wait clears when the awaited
role *acts*, not when they reply.

**If you finish, transition.** A comment saying the work is done leaves the
ticket exactly where it was. When the work produced no commit, the no-code
submission is the action that ends your turn - see `allow_no_code` in
`workflow_actions`. The board will eventually tell the director that an active
ticket's owner went quiet without advancing it, which is a backstop and not a
workflow: it means somebody has to come and find out what you meant. Your own
submission is owner-scoped, so nobody else can take it for you - a director can
only recover the ticket to its next required gate, and that is a repair, not the
path.

## Reporting to another project's board

`ticket-board-write file-report` creates a ticket on **another project's** board. Use it
only for a Switchyard platform defect or a deliberate escalation to that project; your own
project's work goes on your own board with `create-ticket`. It is two steps, every time:
the command alone is a preview that files nothing and prints both projects, the report
and a digest; only the same command with `--submit-upstream --expect <digest>` files it,
and only if nothing changed since the preview. No configured token, URL or flag is
standing permission.

## A ticket that is not yours

When a project runs a Router, an implementer can be handed work that belongs to
another implementation specialty, or that needs a Director decision before
anyone builds it. Do not start it because misroutes are rare. Hand it back:

```bash
ticket-board-write implementer-kick-back <id> --reason "<whose it is, or what needs deciding, in a sentence>"
```

It goes to the Director's triage, never back through Router, and the board
records it as a Router misroute. You do not choose who gets it next or change
anything else about it; the reason is what the Director reads. It is listed in
the ticket's `workflow_actions` where the workflow declares it.

## Background commands

A command you leave running under your pane can hold back your next ticket: the
board does not type into a pane whose children are still working. Give every
background command an end, and finish it before you move the ticket on.

- **Wait on the exact process you started, never on a search.** A loop like
  `while pgrep -f "sweep.sh my-worktree"; do sleep 20; done` carries that
  pattern in its own command line, so it always finds itself and never ends.
  One ran for eighteen hours and held the next ticket back the whole time.
- **Always set a deadline, and stop only what you started when it passes.**
  Check that it really stopped.

```bash
long-command & pid=$!                      # the one process to wait for
deadline=$((SECONDS + 1800))               # always a deadline
while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do sleep 20; done
if kill -0 "$pid" 2>/dev/null; then        # past it: stop only that process
  kill -TERM "$pid"; sleep 5
  kill -0 "$pid" 2>/dev/null && echo "still running: $pid" >&2
fi
```

Your CLI's own background-task tools work too, if they tell you when the
command exits. The board names a ticket's leftover processes to the director
with their pid, and does not stop them itself.

**Do not poll to watch progress.** A loop that keeps re-reading an unchanged
ticket, the board, or another role's pane spends your model quota and tells you
nothing the board will not. The board notifies you when a ticket moves, when a
handoff names you, and when a stall needs you. Wait for that. When you genuinely
must check again, name what you are waiting for, set a deadline and a stop rule,
check the one thing concerned, and stop.

## Review sign-offs

Review stages are gates, not implementation stages. A reviewer signs off or kicks
back with a reason, and does not take the work over or reassign it. Kick-backs
require a reason for the same purpose the commit hash serves: the next person has
to be able to reconstruct the decision from the ticket alone.

Sign off on what you actually verified. "Tests pass" is only true if you ran them
and read the output; say what you ran and what it printed.

## When you are told to act

1. Read the whole ticket.
2. Confirm the action you intend is in `workflow_actions` for your role.
3. Do the work.
4. Record what you did in the ticket - the implementation field or a comment - so
   the next role does not have to reconstruct it.
5. Take the transition yourself. Nobody submits your work forward for you.

Never announce a transition you have not performed, and never perform one on
another role's behalf.

## More detail

This skill is the operating manual for the board tools. The project's own
onboarding packet (`docs/onboarding/` in the project directory) carries the full
mechanics: stage table, per-role permissions, `directorctl`, and the conventions
that surprise people. Read it when you need the *why*, or when an operation here
does not match what this board offers.
