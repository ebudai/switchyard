# SYRD-622, 2026-10-24 recurrence: a role back on a runtime resumes what it left there

## What happened (syrd, read from Inspector's durable records)

Inspector ran agy on its own conversation `ac53bccd`. agy's
`cache/conversation_metadata.json` files that conversation under
`syrd-worktrees/inspector`.

1. **2026-10-21 05:14.** For SYRD-634's acceptance the Director moved
   Inspector to Copilot. The switch (`role_runtime._restart_worker`, then
   `stopped_project_model_repair.clear_role_records`) set the agy record aside
   as `.superseded`.
2. **05:15 to 22:05.** Copilot's records were set aside after it, three times.
   `clear_session_record_for_role` keeps two set-asides, `.superseded` and
   `.superseded.1`, so each one pushed the oldest off the end. At 22:06:35
   Inspector went back to agy. Both slots now held Copilot records (`c966c8b9`,
   `32eb3121`), and nothing in Inspector's store named `ac53bccd`.
3. **10-23 06:13 and 10-24 07:55.** Each start ran `agy_map_not_taken`, then
   `no_record`, then `fresh_started`. agy's map named `f4e8f4e1` for
   Inspector's worktree. That was left over from SYRD-638's incident, and agy
   files `f4e8f4e1` under `syrd-worktrees/audit`. SYRD-622 rightly declined it:
   the map was older than the set-aside, and the set-aside record came from
   another runtime.
4. **10-24 07:57:56.** The User resumed by hand. agy's log shows
   `Streaming conversation f4e8f4e1…`, which is Audit's conversation.

## The change: `scripts/runtime_return_records.py`

**On a runtime switch.** `clear_role_records` first keeps the record it is
about to set aside, as `<record>.runtime-<runtime>`. That file is the
conversation the role had on the runtime it is leaving. It is written only
when the record's own runtime (from its hook `source`) differs from the role's
new runtime, and it is replaced only by a later switch away from that same
runtime.
- A model change on the same runtime keeps nothing, so it still means a fresh
  start.
- Hermes keeps each role's sessions in its own home, and nothing is kept for
  it.
- As root for the owner, the record is read and the copy written by
  descriptor, the same way it is set aside (SYRD-612).

**At a start on that runtime** (`_start_role_session`, and `attach_role_to_slot`
for a stopped role):
- If the role wants a resume and has no record, the kept record becomes its
  record. Then the same checks as for any record decide: SYRD-638's
  provenance, then the resume preflight. If they refuse it, it is set aside
  with its reason. Because the kept file is gone, it is not tried again.
- Otherwise the kept record is dropped. That covers a role that already has a
  record of its own, a fresh start that was asked for, and a kept file that is
  not a regular file (a link is removed, never followed).

**attach-role starting fresh** (Audit's kick-back on `11bd8307`). For a
fresh-per-ticket role, `attach_role_to_slot` adopts the kept record and then
asks SYRD-626's recovery what this launch needs. If the board says fresh, the
pane started fresh, but the adopted record stayed current, so a later launch
could resume the conversation that policy had just rejected. When attach
starts fresh it now sets the record aside, as `_start_role_session` already
did. That also covers a record adopted by SYRD-569 or by agy's map before
recovery decided.

Every decision is kept in the pane's resume history: `kept_for_return`,
`adopted_return_record`, `return_record_dropped`. Adoption runs before
SYRD-569's stranded-record adoption and SYRD-622's agy-map adoption. Both of
those act only when there is no record.

`session_provenance` gains `role_runtime(role)` and `source_runtime(record)`,
the runtime naming `judge` already used.

## Tests: `tests/runtime_return_record_test.py`

The suite uses the real switch step (`_restart_worker` on a stopped role) and
the real starts, over provider stores in a test home. tmux is stood in for and
refuses anything that types into a pane. The before-run is main `d24a15cf`.

| case | main | this tree |
|---|---|---|
| syrd's sequence: agy, Copilot with three set-asides, agy; agy's map names Audit's conversation | fresh (`agy_map_not_taken`, `no_record`) | resumes `ac53bccd` (`adopted_return_record`), and again at the next start |
| Claude to agy and back | fresh | resumes the Claude conversation |
| attach-role after a round trip | fresh | resumes it |
| attach-role, fresh per ticket, board says fresh; then a launch told to resume what is recorded | n/a | fresh, no record left; the next launch takes nothing old |
| a model change on the same runtime | fresh | fresh, nothing kept |
| the kept conversation is now filed under another worktree | n/a | refused by SYRD-638 as `foreign-workspace`, fresh, not retried |
| back with a fresh start asked for | n/a | dropped, and not taken later |
| back with a record of its own | n/a | that record resumes; the kept one is dropped |
| a kept file that is a link | n/a | dropped, target untouched, fresh |
| as root for the owner | n/a | kept by descriptor, and set aside as before |

## Not covered, and the live tenant

- **The live tenant is not repaired by this change.** Inspector's agy record
  was lost before this code existed. Its pane now carries Audit's `f4e8f4e1`,
  resumed by hand at 07:57. After its first turn the hook will record that
  conversation, and the next start will refuse it as `foreign-workspace`
  (SYRD-638) and start fresh. Moving Inspector back onto `ac53bccd` is a
  Director/User decision, for example `/resume` of `ac53bccd` in the pane.
- **A kept record does not survive a role-identity cutover.** The cutover
  copies `""`, `.superseded` and `.resume_timeout`, not `.runtime-*`.
- **The residual SYRD-622 case is still open:** an in-pane agy `/resume` with
  no turn after it, on a role that already has a record.
