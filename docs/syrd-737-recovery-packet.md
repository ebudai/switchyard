# SYRD-737: a fixed-schema recovery packet for a provider safety hold

A provider's safety refusal holds its whole account (SYRD-672). When the role refused is the Director, nothing may
then ask that model for anything, a handoff included. Instead, the board builds a recovery packet from its own
records. An alternate runtime reads it to see the coordination state:
- a replacement Director (SYRD-738);
- a one-operation break-glass role (SYRD-739).

Code: `scripts/ticket_board/recovery_packet.py`. Tests: `tests/recovery_packet_test.py` (the builder, schema and
consumer check on adversarial rows) and `tests/recovery_packet_live_test.py` (a production-built board, with the
Director refused first).

## Reading it

- `GET /api/recovery-packet[?hold=N]` returns the packet for the one current hold, or for hold N. It answers 400 in
  three cases: no account is held, more than one is held and none is named (the answer lists them), or N is not a
  current hold.
- `ticket-board-read recovery-packet [--hold N] [--json]` prints the packet as text, or as JSON with `--json`.
- `ticket-board-read recovery-packet --check FILE [--json]` verifies a saved packet against the board now. It exits 0
  when the board still reads as the packet says, and 1 when the packet is invalid, its hold is no longer current, or
  the board has changed.

The read changes nothing, contacts no provider and needs no model turn. The packet carries less than the board's
ticket reads already do, and no token, so having one grants nothing.

## What it holds

Version 1 of `switchyard.recovery-packet`. Every field is on an explicit allowlist, and every string must match a
fixed pattern: an identifier, a stage or role name, a runtime, a UTC timestamp, a commit hash, or a digest.

| Section | Contents | Cap |
|---|---|---|
| `hold` | id, runtime, kind, category, source, the refused role, its anchor ticket, `began_at`, `began_by`, refusals taken, the roles it holds, `director_held`, `director_notified` | — |
| `other_holds` | each other current hold: id, runtime, kind, `began_at`, roles | 8 |
| `affected` | the held roles, and the active tickets they own or wait on, or the hold names | — |
| `choices` | `inspect_manually`; `move_work` (`switchyard set-role-runtime`, the held roles, unheld runtimes); `clear_hold` (the exact command, `operator_confirms_account_safe`, the unheld roles that may clear) | fixed |
| `roles` | each active role: runtime (registered or declared), registration generation and time, `held_by`, `controls`, `may_clear` | 64 |
| `tickets` | each active ticket: stage, assignee, parent, blockers (with resolved), `blockers_omitted`, awaiting role and since, time in stage, `manually_controlled`, `parked`, candidate commit, `affected` | 100, blockers 8 each |
| `notifications` | each pending notice: id, ticket, kind, target, payload kind, attempts, created, next attempt, claimed, `held` | 50 |
| `events` | the newest window of `board_progress_events`, parsed back into fields by the grammar the board's triggers write | 50 |
| `omitted` | how many of each list a cap left out, and how many rows were unreadable | — |
| `provenance` | project, ticket prefix, board build, `generated_at`, the database snapshot's `db_xmax`, the newest event, notification and hold ids it saw, and `state_digest` | — |

The packet never holds any of these:
- a ticket's title, body, implementation notes, comments or blocked reason;
- a notice's message or error;
- a hold's or clear's free-text reason, or a provider's words;
- pane text, a pane target, a path, a process id, or any credential or token.

A row is dropped whole and counted in `omitted.malformed` when any of its fields fails its pattern. This covers refused
words written into an event, an unzoned time, or a wrong type. The row is never cleaned up and kept, so text outside
the allowlist has no field to ride in. An unreadable current hold builds no packet at all.

Which rows a cap keeps:
- **tickets:** the affected ones, then the owned ones, then the rest, each lowest number first;
- **notifications:** those waiting on a held account first, then the oldest;
- **events:** the newest.

The whole packet is at most 192 KiB (196,608 bytes). The fullest possible packet is 194,257 bytes:
- every list is at its cap, and every value is at its pattern's longest;
- every role is on the held account, so each is named three times (`hold`, `affected`, `move_work`) and every ticket
  is affected;
- the events are `moved` events between 64-character stages, the longest grammar;
- the held account has the longest runtime name, and every integer is a bigint's largest.

`tests/recovery_packet_test.py` measures it for every event grammar and checks this number.

## Determinism and provenance

- **Order and encoding.** Every list is in a fixed order, and the encoding is canonical JSON (sorted keys, no
  whitespace, ASCII).
- **`state_digest`.** The sha256 of everything except `provenance`. The same trusted state therefore gives the same
  digest whenever it is read, and in whatever order the rows arrive.
- **`packet_digest`.** Covers the whole packet, provenance included.
- **One snapshot.** All rows are read in one REPEATABLE READ, READ ONLY transaction. A change committed during the
  read is in neither the tickets nor the events.

## Consuming it

`recovery_packet.validate(packet)` checks a packet and raises `PacketError`, naming each problem, when it fails:
- each field, its pattern, each cap and each list's order;
- the event grammar;
- consistency: the held roles match the roles, the affected tickets match the tickets, and the choices are exactly the
  board's for this hold;
- the size bound and both digests.

`render(packet)` validates first, then formats the fields as text. It interprets nothing.

A consumer never trusts a packet it was handed on its own:
- `verify(packet, project=..., fresh=...)` compares it with a packet the consumer fetched itself just now, for the
  same hold. Both must validate and be the named project's, and the board must still answer for that hold. It returns
  `current` and the sections that `changed`.
- `fetch_verified(get, project=..., packet=None)` does the fetch through any GET transport.

Each consumer brings its own transport:
- a Director replacement, run with root's authority, reads over the board's proven Unix socket
  (`board_identity.board_get_for`);
- a break-glass role reads through `ticket-board-read`.

Authority stays with the transport and the board, never with the packet.
