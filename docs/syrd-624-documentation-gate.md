# SYRD-624: an optional documentation gate before Audit, decided by a Librarian or the Director

## The ask

Tickets that add, change or retire something a person reads about (CLI help,
operator and Director guides, onboarding, project docs, examples) can be held
after Implementation until their documentation is consistent. The edit must be
in the exact commit Audit reviews, and documentation is never amended after a
review while that review's sign-off is kept. The Librarian role is optional;
without it the Director keeps the decision, at no extra cost. Existing tenants
are unchanged until they opt in. The Librarian gets only the ticket, its
candidate, the affected surfaces and the narrow operations its decisions need,
and no publication authority.

The Director's decision of 2026-10-18 (Option A): the Librarian may publish
its documentation commit, with the submitted candidate as its sole parent, to a
ticket-scoped candidate ref. The action records both SHAs and the ref. The
Librarian cannot touch `main`, force-push, rebase, merge, choose a ref or
remote, or publish a release. Inability to form or publish that commit is a
kick-back.

## No migration

`pgu999` is the last migration slot (SYRD-617 is in backlog), and none was
needed. The declarative workflow (SYRD-11) already has every piece:

- a review stage gated by a custom flag, skipped when the flag is off;
- `approve`, which sets the stage's sign-off;
- `clear_signoffs`, which the executor applies on any primitive;
- a payload `commit_hash` that replaces the candidate;
- `return` to the recorded implementer.

The before-run applies the composed document to main's board unchanged. The
command suite asks the board's own `validate_declared_workflow` on a
`schema.sql` board and on a migrated one.

## The shape (`scripts/ticket_board/documentation_gate.py`)

`with_documentation_gate(document, librarian=None|{runtime, template_role})` adds:

- **Flags.** `needs_documentation` (a gate, default off) and
  `documentation_signoff`.
- **The `documentation` stage,** of kind `review`, directly after
  Implementation. Every submission out of Implementation enters it, and with the
  gate off it skips to wherever those submissions went before (Inspection or
  Audit). Because it comes before every review, Inspection and Audit see the
  final documentation, and an edit invalidates no earlier verdict.
- **Its three owner-scoped decisions:**
  - `documentation_sign_off`: an approve (a move when the Director owns the
    stage). It requires a commit, and clears `inspector_signoff`,
    `audit_signoff` and `user_signoff`.
  - `documentation_no_change`: the same primitive, requiring a reason.
  - `documentation_kick_back`: a return to Implementation, requiring findings,
    and clearing every sign-off.
- **The Director's defer** (and any other Director-only exit to parking or a
  terminal stage), cloned from the stage the submissions went to.
- **With a Librarian:** a `librarian` role of kind `reviewer`. It holds
  `add_comment` only, is detached (`slot: null`), `ephemeral` (its conversation
  is cleared before each ticket), never `serial`, and gets `LIBRARIAN_PROMPT` as
  its onboarding prompt. Its model is its template's, under the project account.
- **Without one:** the Director owns the stage. The workflow never gives the
  Director an `approve` (SYRD-82), so the stage carries no sign-off and the
  three decisions are Director moves. No role, pane or session is created.

`without_documentation_gate` removes the stage, its moves and flags, puts the
submissions back, names the stage in `remove_stages`, and leaves an inactive
Librarian declared. The board refuses it while a ticket waits in the stage, or
while a live ticket still requires documentation review ("outstanding review
obligation"); the Director turns that ticket's gate off or lets it pass.

`validate_documentation_gate`, run by `workflow_config.validate` on every
document, holds the shape:
- the Librarian is a reviewer with nothing but `add_comment`, owns only the
  stage, and moves only by the three decisions;
- the stage is the Librarian's or the Director's, gated, and notifies its owner,
  with a sign-off only for the Librarian;
- the gate is off by default;
- every submission enters the stage;
- the three decisions are exactly as composed.

## What git must say (`scripts/ticket_board/documentation_candidate.py`)

The database cannot read git, so the board service checks the commit before
the transition, from `extension_operations.before_transition`, for whoever owns
the stage. Roles reach the database only through the service.

- **`documentation_sign_off`** needs a submitted candidate, and a different
  commit that the trusted commit cache knows. That commit's parents must be
  exactly `[candidate]` (`commit_cache.commit_parents`), and it must be
  published at `candidate_ref(actor, ticket, candidate)` =
  `roles/<actor>/<ticket>-<first 12 of candidate>`, as the cache resolves it,
  after one refresh. **Every change it makes must be to a documentation file**
  (below). The comment records both SHAs, the ref, the changed paths and an
  optional note. A reason is refused: a sign-off records its commit.
- **`documentation_no_change`** takes no commit or ref. Its comment says the
  candidate stays as submitted, with the reason. Reasons, findings and notes are
  limited to 4000 characters.

One ref per candidate means a later round never rewrites an earlier ref, so no
force is ever needed.

### Documentation only (Audit's kick-back of a53e264)

The first candidate listed the changed paths in the record but accepted any of
them, so a correctly parented and published commit could carry code into the
candidate. The board now reads every change with `git diff --raw --no-renames`
(`commit_cache.changed_entries`). A rename counts as a removal and an addition,
so both paths are checked. Before anything moves, it refuses the sign-off unless
`documentation_gate.outside_documentation` finds nothing, and the refusal names
each offending path:
- **Documents anywhere:** a path is documentation if it ends in `.md`,
  `.markdown`, `.rst`, `.adoc` or `.asciidoc`.
- **More under docs/:** under a top-level `docs/` or `doc/`, plain text and
  images (`.txt`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`) also count.
- **Never test data:** nothing under a test-data directory (`test`, `tests`,
  `testdata`, `fixtures`, `__snapshots__`, `golden(s)`) counts, whatever its
  suffix. Those files are compared by tests.
- **Ordinary files only:** each change must be to a non-executable file (mode
  `100644`, or `000000` for an addition's old side and a removal's new side).
  An executable bit, a symlink or a submodule is refused.
- **Fails closed:** a diff the board cannot read is also a refusal.
- **Never empty** (Audit's second kick-back, of 7ba5c60): a commit that
  changes nothing edits nothing. Taking it would be `documentation_no_change`
  without the reason that decision requires, so the board refuses it and
  points at `documentation_no_change`.

CLI help lives in code and example workflows are read by programs, so the
Librarian still *reviews* both but cannot *edit* them. When they need to change,
it kicks back with the exact wording; the guide, the skill and the stored prompt
say so.

## Publishing (`scripts/switchyard-publish-candidate`)

Run as the Librarian, the publisher:
- needs a ticket;
- refuses `--ref`, `--replace` and any remote but `origin`;
- refuses a commit without exactly one parent;
- refuses a commit that changes more than documentation, by the same
  `outside_documentation`, or that changes nothing, before it pushes anything;
- derives the ref with the same `candidate_ref` the board checks;
- prints the `documentation-sign-off` command to run next.

It is a guard rail, like the rest of that program; the board's check is the
boundary.

## Surfaces

- **`ticket-board-write`:**
  - `documentation-sign-off <id> --commit <sha> [--ref] [--note]`. The ref
    defaults to the one derived from the ticket's current commit.
  - `documentation-no-change <id> --reason`, `documentation-kick-back <id> --reason`.
  - `require-documentation <id> [--off]`, for the Director.
  - Run as the Librarian, it refuses everything but these three and
    `add-comment`.
- **`switchyard documentation-gate <project> enable [--runtime <cli>
  [--template-role R]] | disable [--dry-run]`.** This goes through
  workflow_manage's apply path: a board dry-run, the expected revision, a
  journal and the projection. A Librarian's launcher entry is projected
  detached, carrying its prompt. Like every other workflow write since
  SYRD-623, `enable-documentation-gate` and `disable-documentation-gate` are in
  `workflow_manage.TENANT_WRITES`, so root is refused before any tenant path or
  the board is touched (workflow_manage_root_writes_test runs both, and their
  dry runs, as namespace root).
- **`switchyard new`:** after Router, the menu "Documentation gate (optional)"
  offers off / director / librarian, default off. A Librarian's runtime, model
  and effort are asked the guided way, inside the owner-catalog guard.
  - It is recorded as `project.documentation`, read back by `--from`, and set
    by `team-launcher design --documentation-gate/--librarian-*`.
  - A Librarian's runtime goes through both agent CLI checks.
  - It is applied by `documentation_onboarding.plan_with_documentation_gate`,
    after Router, on the plan's own declared document or the one its rows
    describe (`router_onboarding.plan_document`, now shared).
  - Upgrades never read the artifact.
- **Board UI:** no change was needed. SYRD-598's delegation lends every
  Director transition that is neither an approval nor commit-carrying, so with
  a Director-owned gate the UI offers no-change and kick-back. A commit
  sign-off is CLI-only, like every commit-carrying move (SYRD-541).
- **Onboarding:**
  - `skills/switchyard-librarian/SKILL.md`, the third canonical skill. It is
    installed into every CLI's tree (Claude, Codex, agy, Hermes and, since
    SYRD-634, Copilot), and
    `skills_for_role` points only the Librarian at it.
  - `docs/onboarding/switchyard-librarian-guide.md`, which `switchyard new` now
    copies into every project.
  - The Librarian's stored prompt, which the SessionStart path injects for every
    provider.
  - The board guide, Director guide, both skills, `docs/board-skill.md`,
    `docs/team-launcher.md` and the README.

## Tests

- **`tests/documentation_gate_board_test.py`.** Production-built boards, the
  real HTTP server and listener, and a real git repository as the commit cache.
  - **Gate:**
    - with the gate off, Documentation is skipped and the Librarian is not told;
    - only the Director sets the gate;
    - with it on, the ticket waits for the Librarian, who is cleared then told once.
  - **Librarian authority:** refused 14 other operations over HTTP (403) and 7
    in PostgreSQL. It is offered only its three decisions.
  - **Sign-off:**
    - refused for each unsupported shape: no ref, another ticket's ref, the
      implementer's ref, unpublished, the candidate itself, a reason, an
      unknown commit, a sibling, a merge; none changes the ticket;
    - refused, correctly parented and published, for a commit that changes more
      than documentation: a product file beside a guide, a Markdown test
      fixture, `requirements.txt`, an executable document, a document that is a
      symlink, a removed product file, a product file renamed into `docs/`.
      Each refusal names the path, and the ticket and its candidate are
      untouched;
    - refused for an empty commit, pointing at `documentation_no_change`,
      with the ticket and its candidate untouched;
    - accepted for a real child of the candidate that touches a guide, the
      README, a skill, an image and plain text under `docs/`: it becomes the
      candidate, Audit is next, and the record is complete.
  - **No change:** reason rules, unchanged SHA, an attributed record.
  - **Kick-back:** returns the ticket with findings; the next candidate comes
    back through Documentation.
  - **MEFP-4's route-back:** Audit's sign-off is carried into Documentation and
    cleared by the Librarian's commit.
  - **Disabling:** refused while occupied, and while an obligation is
    outstanding.
  - **Director-owned board:** its moves, its own ref namespace, no Librarian
    runtime.
  - **Board without the gate:** unchanged.
  - **Before-run on main:** the gate is accepted as declared, and a sign-off
    naming an unrelated, unpublished commit is accepted. This tree refuses it.
- **`tests/documentation_gate_command_test.py`.**
  - Composition, and each broken shape refused by `workflow_config`.
  - The board's SQL validator on both build paths.
  - `switchyard documentation-gate` against a board and a real projection.
  - The Librarian's whole path with real programs: a bare remote, the
    Librarian's clone, a mirror as the commit cache, the real publisher
    (refusals, the derived ref, `main` untouched, a refused merge, and refused
    commits that change code or nothing, with nothing pushed) and the real
    `documentation-sign-off`.
  - The documentation-surface policy as a table, plus mode and removal cases
    of `outside_documentation`.
  - CLI refusals for the Librarian.
  - The skill installed into every CLI's tree by the real installer, and the
    guide/skill/prompt content.
  - The `switchyard new` record, role-plan entry, prompt, summary and artifact
    payload.
