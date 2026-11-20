# SYRD-838: Accept keeps the note typed in the UAT panel, and the Director is told it

## What failed

On SYRD-834 the User typed a note into the UAT panel's box ("plain Ctrl-V should paste text too") and pressed
**Accept**. The ticket was accepted, but the note was nowhere: no User comment, and nothing in the Director's
notice. The Director found out only because the User asked whether it had made it in.

`scripts/ticket_board/frontend_script_uat.py` called `performBoardUiAction(ticket, accept, {})`, so Accept never
read the box. Only Ask Director and Send to Director sent its text, and only they cleared the draft. The
placeholder, "Ask a question, or say what you saw.", did not say that Accept ignored it.

## The change

**Accept carries the note as the acceptance's own text.** The board already posts that text as the User's comment,
in the same transaction as the sign-off, on both kinds of board:
- `ticket_board.user_sign_off(id, comment_text)` on a legacy board;
- `ticket_board.perform_workflow_action_as`, for a declared approval with `text`, narrated as the User.

So the note and the acceptance are one write. If the comment cannot be written, nothing is accepted. No second
request is needed, and none can half-succeed.

**In the panel:**
- **With a note,** Accept asks "Accept this ticket? Your note will be posted as a comment for the Director." The
  confirmation lists the note as "Your note". An action that already asks for text (`require_reason`) gets the note
  as that text.
- **With an empty box,** Accept asks and does what it always did.
- **After a success** the draft is cleared, as for the other two decisions.
- **A refusal** of any of the three decisions is said in the panel, in a `role="alert"` line. It names the decision
  as the panel does, without the database's `CONTEXT:` trace, and it stays through the board's redraws. Nothing
  happened, so the box keeps what was typed. The next decision that goes through ends it.
- **The placeholder** says the box can carry a note with Accept. Past Ask Director's 4,000 characters, the limit
  line says "Ask Director takes 4,000 at most; Accept and Send to Director take all of it." There is no comment
  length limit, so that is the only limit to state.

**In the Director's notice** (`pgu1049`, `notify_ticket_state_transition`): when the move is an acceptance, the
User's comment written by that same transaction is appended to the transition notice: "PGU-1 -- … entered Final
Sign-Off: the User accepted it, with a note: …". This follows the precedent of an Inspector's recommendation, which
already rides in its kick-back notice. A move counts as an acceptance when it is:
- on a declared board, an `approve` transition out of a review stage the User owns, named by the action being
  performed (`ticket_board.workflow_action`);
- on a legacy board, `user_review` to `director_review` with `user_signoff` set.

So Send to Director's message is never read as a note, and neither is a User comment from before. A note longer
than 4,000 characters is cut in the notice, which says the whole note is the User's comment on the ticket. A long
note goes through directorctl's payload file, but the Director should not be handed a three-megabyte paste
(SYRD-741's case) as a notice.

## Not covered

On a held ticket (`manually_controlled`), a declared acceptance is deferred until the hold is released, and the move
happens in a later transaction. The note is still posted as the User's comment at once, but the notice that
releasing the hold eventually sends does not carry it. The Director is steering such a ticket by hand, and reads its
comments.

## Evidence

**`tests/uat_accept_note_browser_test.py`** drives the real page in Chromium against the real server, served as
production serves it (HTTP acts as the User only), on a declared board and a legacy one:
- **Main first** (`99328ed0`, from a git archive, in its own process): Accept with a note records the acceptance,
  and the note is posted nowhere and missing from the Director's notice.
- **A note:** the confirmation's question and summary, exactly one User comment with the note, the sign-off, the
  note at the end of the Director's notice, and the draft cleared.
- **An empty box:** the old question, nothing posted, and no note in the notice, not even the User's earlier comment
  on that ticket.
- **A failed comment write** (a test trigger refuses it): the ticket stays `user_review`, unaccepted; nothing is
  posted and the Director is told nothing. The panel says "Accept refused: the comment store is unavailable", keeps
  saying it through a redraw, and the box keeps the note. A question that then goes through ends the refusal, and
  Accept again posts the note and signs off.
- **A long note:** the limit line names Accept as it is typed. The comment keeps all of it, and the notice carries
  its first 4,000 characters and points at the comment.
- **Send to Director:** its message is not read as an acceptance's note.
- **Schema:** schema.sql's copy of the trigger is byte-identical to the migration's, and the migration is its newest
  definition.

`tests/uat_large_paste_browser_test.py` pins the new limit wording.

**Mutants:** each applied alone to a copy of this tree. All are killed by assertions except one, which is
equivalent: dropping the draft clear. A redraw recaptures the draft from the page, and after a successful Accept the
box is gone, because the ticket has left User Review or shows "Accepted ✓". The ticket comment lists them.
