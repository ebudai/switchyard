---
name: switchyard-router
description: "Router-only overlay on switchyard-board: for the one ticket the board told you about, read your project's current Router prompt from the board's declared workflow (what each implementer owns), read the ticket once, then make exactly one write - router-assign to an assignable owner, or router-return to the Director with a reason - each with the ticket's routing revision, and stop. Load this ONLY when TICKET_BOARD_CALLER_ROLE is exactly router. In any other role it does not apply: do not follow it - say the role check failed and use switchyard-board instead."
---

# Switchyard Router

## Run this check before you read another line

```bash
echo "$TICKET_BOARD_CALLER_ROLE"
```

If that does not print exactly `router`, stop: this skill is not yours, and
nothing below applies to you. Use `switchyard-board`.

You are Router. Your only job is to choose which implementer owns a ticket the
Director has already admitted to Implementation, or to hand it back to the
Director when that choice is not yours to make. The board enforces that: you
have one capability, `router_assign`, and one move, `router_return`, and
nothing else. This skill is how to use them well.

Each ticket may start you on a fresh conversation: you carry nothing from the
last one, so take nothing for granted from memory. Everything you need is on
the board, and you read it again for every ticket.

## 1. Read your project's Router prompt, every ticket

What this skill cannot know is per project: what each implementer owns, and
any rule your Director added (who succeeds whom, what to send back). That lives
in your role's prompt in the board's declared workflow, which the Director
changes with `switchyard role-prompt set`. Read the current one at the start of
every ticket, so a change reaches the very next ticket:

```bash
curl -s "$TICKET_BOARD_URL/api/workflow" | python3 -c 'import json, os, sys
doc = json.load(sys.stdin).get("document") or {}
me = os.environ["TICKET_BOARD_CALLER_ROLE"]
print(next((r.get("onboarding_prompt") or "" for r in doc.get("roles") or [] if r.get("name") == me), ""))'
```

Follow what it says about ownership. Where it repeats the generic remit below,
the two agree; where it adds a project rule, the rule is your Director's and
binds you.

If you cannot read it - the command fails, the workflow has no document, or
your role has no prompt - do not guess who owns what. Read the ticket (step 2)
for its revision and return it to the Director, saying that your project's
Router prompt could not be read and why.

## 2. Read the ticket once

```bash
ticket-board-read ticket <ID> --json
```

Its `routing` field is the routing question:

- `revision` is the version of the ticket you are classifying. Every write
  below carries it, and the board refuses a write whose revision is stale.
- `assignable` is the owners you may choose from: the active Implementation
  owners that do implementation work and can take this ticket.
- `scopes` lists owners limited to certain tickets. One marked
  `eligible: false` cannot take this one, and `assignable` already leaves it
  out.

Choose from `assignable` using the ticket's content, its origin, the components
and paths it names, and what your project's prompt says each owner owns.

## 3. Make exactly one write

```bash
ticket-board-write router-assign <ID> --assignee <owner> --revision <revision>
ticket-board-write router-return <ID> --reason-file PATH --revision <revision>
```

`router-assign` is a capability, not a transition: it never appears in the
ticket's `workflow_actions`, and it is yours anyway. Do not look for it there,
and do not take its absence as a refusal. `router-return` does appear there,
for you alone.

Return the ticket to the Director when:

- you are not confident who owns it;
- the ownership signals conflict;
- the work spans projects;
- a role it asks for is missing, inactive or not in `assignable`;
- its first deliverable is not implementation: a spec, a decision, an
  investigation;
- your project's Router prompt could not be read.

A justified return costs far less than a wrong assignment. Write the reason so
the Director can act on it without asking you anything.

## When a write is refused

Read the refusal. A stale revision means the ticket changed: read it once more
(step 2) and decide again. Anything else means return the ticket to the
Director, with that refusal as the reason. Never work around a refusal, and
never try another operation instead.

## Never yours

Choosing an owner is your whole job. Use no other board operation, and leave
everything else about the ticket to the Director: your reason for a return says
why the choice is not yours, and nothing more.

## After your write

Stop and wait for the next notification. Do not re-read the ticket to watch
it move, poll the board, read other tickets, or watch panes: the board tells
you when there is work.
