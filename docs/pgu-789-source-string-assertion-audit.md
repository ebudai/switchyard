# PGU-789 Source-String Assertion Audit

PGU-787 made hidden frontend suites runnable and repointed several stale
source-string assertions. This pass re-checked each changed assertion against
the behavior its test name claims.

Verdicts:

- `tests/audit_signoff_comment_frontend_test.py`: real coverage. The exact
  detail-modal `Audit signoff` locator prevents the metadata `Needs audit`
  checkbox from satisfying the test. The `#createStatus` wait and unchecked
  wait cover the visible error and revert behavior. Killing mutations: make
  the locator broad again, render the backend error outside `#createStatus`,
  remove `input.checked = previousChecked`, or let the failed action reach
  `update_ticket`.
- `tests/board_grid_visible_columns_test.py`: real coverage. PGU-787's 8/11
  counts match the current default workflow: 8 visible columns, plus backlog,
  done, and cancelled after toggles. PGU-789 adds the rendered visible order,
  including Draft before Triage, because the old draft source-order anchor was
  fake. Killing mutations: omit DAT/UAT from fallback rendering, stop syncing
  the grid template to visible columns, hide/show the wrong terminal columns,
  or render Draft after Triage.
- `tests/cancelled_state_frontend_test.py`: PGU-787/788's label-map assertion
  was a false anchor and is removed. It only tracked the representation of a
  generated fallback object, not cancelled-column behavior. Retained assertions
  are real static wiring checks. Killing mutations: remove the cancelled
  visibility toggle/count, stop hiding cancelled by default, remove the
  non-empty cancellation reason requirement, stop calling `cancelTicket`, or
  stop sending the director caller.
- `tests/deferred_column_hidden_default_test.py`: PGU-787/788's label-map
  assertion was a false anchor and is removed. It only tracked fallback-object
  representation, not backlog visibility. Retained assertions are real static
  wiring checks. Killing mutations: remove the deferred toggle/count, stop
  hiding backlog by default, stop updating the count, or stop wiring the toggle
  change handler.
- `tests/detail_modal_overlay_test.py`: real coverage. The Escape handler was
  centralized after the original assertion was written; asserting the Escape
  guard plus `state.detailOpen` branch still matches the file's overlay/close
  claim. Killing mutations: remove the overlay element, stop toggling
  `detailOverlayEl.hidden`, remove close-button or backdrop handlers, remove
  Escape handling, or stop setting `body.detail-open`.
- `tests/draft_stage_frontend_test.py`: PGU-787's object-order assertion was a
  false anchor and PGU-788 removed it. PGU-789 also removes the remaining
  label-map literals from this test. Retained assertions are real draft UI and
  release wiring checks. Killing mutations: remove the draft checkbox, stop
  creating draft tickets from it, stop clearing it after create, remove
  draft-to-analysis default advance, remove `release_draft`, or remove the
  draft detail-stage class.
- `tests/multi_attachment_frontend_test.py`: real coverage. The
  `openImageLightbox(entry, ticket)` assertion follows the current callback
  signature and still proves detail attachments open through the lightbox path
  with ticket context. Killing mutations: remove lightbox elements, remove
  clickable attachment cards, stop passing the ticket to `openImageLightbox`,
  break set grouping/parsing, or reintroduce the retired Available Frame UI.
- `tests/state_transition_error_feedback_test.py`: PGU-787's
  `assigneeSelect.addEventListener` anchor was misnamed coverage. PGU-789
  rewrites the file as a browser test for the current workflow transition path:
  click `Advance -> Implementation`, force the backend `start_work` action to
  fail, assert the error is shown, and assert the ticket remains in Triage.
  Killing mutations: stop wiring the advance button to `advanceTicket`, swallow
  backend action errors, render errors outside `#createStatus`, or mutate the
  ticket despite the rejected transition.
- `tests/ticket_board_workflow_config_equivalence_test.py`: real coverage. The
  DEFAULT_STATE_LABELS map is generated from `schema.sql`; the guard compares
  the emitted frontend map against the schema seed and has explicit negative
  checks for label divergence and missing stages. PGU-789 changes the synthetic
  divergent label to `ZZ-NOT-A-LABEL` so it cannot collide with a plausible
  real label. Killing mutations: hand-maintain the fallback map again, remove
  the schema comparison, drop a stage from the map, or change a generated label
  independently of the schema.
