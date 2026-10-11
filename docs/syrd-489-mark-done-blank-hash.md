# SYRD-489: a blank commit hash is no commit, on every path

## What was seen

SYRD-488 was an audited design ticket with no code, closed on 2026-09-29. At
Final Sign-Off it stood like this:
- `director_review`, with `audit_signoff=true`;
- `commit_exempt=true` and `commit_hash=""`;
- a declared `mark_done` that requires a commit.

Two commands on it behaved differently:
- `ticket-board-write mark-done SYRD-488` failed with `invalid commit hash`;
- `ticket-board-write workflow-action SYRD-488 mark_done --payload-json '{}'`
  closed it.

## The request path, measured

- **The installed client was older than SYRD-267's change.** On 2026-09-29 the
  live board ran release b097e679, installed on 2026-09-20. SYRD-267's client
  change is 63fa402, from 2026-09-25: it omits a blank hash instead of sending
  one, and it was not in that release. Its `mark-done` sent
  `{"commit_hash": ""}`. The release installed since, 2951c5c9, does have it.
- **The server routes both commands the same way.** On a board with a declared
  workflow, the server hands any declared action to the declared executor, the
  generic `workflow-action` and `mark_done` alike (`server.handle_ticket_action`).
  `app.perform_workflow_action` validated the blank hash to `""` and passed it
  on, and the executor refuses any `commit_hash` key that is not hex
  (`perform_workflow_action_as`).
- **The legacy path never failed.** On a board with no declared workflow,
  state=done reaches SQL `mark_done`. That reads `""` as no hash, and the
  ticket's exemption decides.
- **The web UI still sends a blank hash.** It sends `{commit_hash: ''}` for a
  ticket with no hash (`frontend_script_app.py`). So on a declared board it
  fails today, and so does any older pinned client in a tenant pane.

## The change

For `mark_done`, `app.perform_workflow_action` drops a `commit_hash` that
validates to blank (three lines in `app.py`, which is over the review ceiling), so a blank hash and an omitted field are the same request.
The rest of the path is untouched:
- **A malformed hash** is still refused for its form, by the same validation,
  before anything is dropped.
- **An ordinary ticket** with no commit recorded and none named is still
  refused by the commit requirement in the update trigger (a `require_commit`
  transition with a blank resulting hash on a ticket that is not exempt).
- **A named hash** still closes on that commit once it is resolved, which is how
  the Director closes on an integration commit.
- **Authorization** is unchanged: only the actors the transition names may
  close.
- **Other actions keep their rule.** In particular, a relayed User acceptance
  takes no `commit_hash`, not even an empty one. SYRD-535 measured that, taught
  the Director's guide to omit the field, and tests it. Dropping a blank on
  every action was tried first, and it reddened that test. Whether a blank
  should mean "none" there too is a decision for the Director; it is left
  as it was.

## Tests

`tests/mark_done_blank_hash_test.py`, 13 checks. It runs on a real board with
MEFP's declared workflow, where `mark_done` requires a commit, and the board
has its own scratch copy of a project repository. The clients are real:
- **The SYRD-488 client:** the b097e679 `ticket-board-write`, run from a git
  archive.
- **This tree's `ticket-board-write`:** through both `mark-done` and
  `workflow-action`.
- **The web UI's request:** sent over the board's HTTP API.

**Main 0bc0e23** (child process, from a git archive): the SYRD-488 client, the
web UI and a blank `workflow-action` are refused with `invalid commit hash`.
The legacy path, and this tree's `mark-done`, close the ticket.

**This tree:**
- **Every path closes the ticket:** the SYRD-488 client, the web UI, a blank
  `workflow-action`, `mark-done`, and the legacy path. Each closes the audited
  no-code ticket with no commit recorded.
- **A malformed hash** is refused for its form.
- **A non-Director** is refused.
- **An ordinary ticket with no commit** is refused by the commit requirement,
  from the old client and the new alike.
- **An ordinary ticket** closes on the verified integration commit the Director
  names, rather than on its recorded candidate.

## Evidence

- **`tests/mark_done_blank_hash_test.py`:** 13 checks pass on this tree. Its
  child run of main 0bc0e23 reproduces the refusal.
- **Mutation:** four mutants, run through `tests/bounded_run.py mutate` with
  this test and `relay_user_acceptance_configuration_test`. All four were
  killed, each read on its failing assertion:
  - **"A blank hash is passed on":** the SYRD-488 client is refused.
  - **"Every hash is dropped":** the ticket closes on its recorded candidate,
    not the integration commit named.
  - **"A supplied hash is not validated":** a malformed hash is refused as
    `invalid commit hash`, not for its form.
  - **"A blank hash is dropped on every action":** SYRD-535's empty-hash relay
    check fails.
- **The sweep:** 110 related suites were run serially on this tree and on main
  c87409d, with provider stubs that refuse to start. The 28 red on both have
  identical normalised output, most of them browser suites that cannot run
  here. Nothing is red here that is green on main. Main has since moved to
  0bc0e23 (SYRD-441). That commit changes only the rollout log and its tests,
  and none of those suites is in this sweep.
- **SYRD-267's test still passes:** `no_code_mark_done_test`, 9 checks.
