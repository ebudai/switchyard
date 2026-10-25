# Switchyard Librarian Guide

The onboarding document for the **Librarian**, the optional role that keeps a
project's documentation consistent with its work. Read it if you are the
Librarian, if you are a Director deciding whether a ticket needs documentation
review, or if you are an implementer whose ticket went to the Documentation
stage.

It is project-agnostic. Examples use `PGU-123`; your project has its own prefix.
The Librarian's working instructions in compact form are the
`switchyard-librarian` skill, installed for every agent CLI beside
`switchyard-board`; this guide is the same rules with the reasons behind them.

---

## Why the role exists

A ticket that adds, changes or retires a capability changes what people need to
read: the command's `--help`, the operator and Director guides, the onboarding a
new project gets, the README, the examples. Implementers and reviewers look at
whether the work is right. Nobody's job was to look at whether every page a
person reads still tells the truth about it, so stale instructions accumulated:
a retired flag still in a guide, two documents disagreeing, an example the new
validator refuses.

The Librarian owns exactly that, for the tickets the Director sends it, and
nothing else.

## Where it sits

```
Implementation ──submit──▶ Documentation ──▶ Inspection ──▶ Audit ──▶ ...
                           (only when needs_documentation is on)
```

- **The gate.** Each ticket has a `needs_documentation` gate, **off** by
  default. Only the Director turns it on, at triage or at any time before the
  work leaves Implementation:
  `ticket-board-write require-documentation PGU-123` (and `--off` to undo it).
- **The stage.** When the implementer submits, a ticket with the gate on enters
  the **Documentation** stage, assigned to the Librarian, who is told about it
  once. A ticket with the gate off skips the stage and goes where submissions
  always went. Nothing else about the pipeline changes.
- **Before review, never after.** The stage is directly after Implementation, so
  whatever the Librarian changes is in the exact commit Inspection and Audit
  review. Documentation is never amended after a review while that review's
  sign-off is kept.

### Projects without a Librarian

The role is optional, and so is the gate. A project chooses one of three when
it is created (`switchyard new` asks, default off) or later
(`switchyard documentation-gate <project> enable|disable`):

| Choice | What exists | Who decides |
|---|---|---|
| off | no gate, no stage, no role | the Director, as part of its ordinary review |
| Director-owned gate | the gate and the stage | the Director, through the same three decisions; no worker, no extra cost |
| Librarian | the gate, the stage and a `librarian` role | the Librarian, one more model session |

Enabling never starts a model session silently: the Director-owned gate creates
no role at all, and the Librarian is a role the operator chose, with a runtime
and model the operator chose. Existing projects keep their workflow until they
opt in.

## What the Librarian receives

Only what one decision needs:

- **the current ticket** - its body, implementation notes and comments;
- **its candidate** - the submitted commit and its diff;
- **the documentation surfaces that diff affects**, read in its own worktree;
- **three board decisions and `add-comment`**, and nothing else.

Each ticket starts the Librarian on a fresh conversation, so nothing from the
previous ticket carries over. It does not read other tickets, board queues or
other panes, and it does not poll; the board tells it when there is work.

## The surfaces it owns

- **CLI help and usage**: `--help` output, usage lines, subcommand lists, option
  help strings, and messages that tell a person what to run next.
- **Guides**: operator guides, the Director guide and skill, the board guide and
  shared skill, role prompts and skills.
- **Onboarding**: `docs/onboarding/` and everything `switchyard new` copies into
  a project.
- **Project documentation**: the README, `docs/`, and design notes that
  describe current behaviour.
- **Examples**: sample configurations, example workflows and commands.
- **Removal of stale instructions**: what the change retired has to leave every
  surface above, not merely be joined by what replaced it.

The Librarian reviews every surface above but **edits documents only**. The
board refuses a documentation commit that changes anything except Markdown,
reStructuredText or AsciiDoc files outside test data (`tests/`, `fixtures/`,
`testdata/` and the like), and plain text or images under a top-level `docs/` or
`doc/` -- each added, edited or removed as an ordinary, non-executable file.
`switchyard-publish-candidate` refuses the same commit before it pushes
anything. So a help string in code, an example workflow or configuration a
program reads, a test, a default or any behaviour is the implementer's: kick
back with the exact text it should say.

## Checklist: finding stale or contradictory instructions

1. **Inventory the visible change.** From the ticket and the diff, list what is
   added, changed and retired that a person can see: commands and subcommands,
   flags and defaults, environment variables, paths, stage, action and role
   names, messages, limits and numbers, and the order of steps.
2. **Search for every old and new name** across `docs/`, `skills/`,
   `examples/`, the README and help strings (`git grep -n -- '<name>'`). An old
   name that remains is stale; a new one that appears nowhere a person would look
   is undocumented.
3. **Run the changed commands' `--help`** and compare it with what the
   documents say: names, defaults, required options.
4. **Look for contradictions**: two documents describing the same thing
   differently; a document describing behaviour the diff removed; an example the
   changed parser or validator would refuse; a link to a renamed or deleted file.
5. **Hold the documents to the ticket**, not the other way round. The ticket's
   words are the requirement. If the product does something else, that is a
   finding, not a page to rewrite.

## The three decisions

Every decision is one board write. Each is recorded on the ticket under the
Librarian's name, and each is refused by the board if the ticket is not in the
Documentation stage assigned to the caller.

### 1. No documentation change

```bash
ticket-board-write documentation-no-change PGU-123 --reason "Checked --help for x and y, the board guide's Who may do what, examples/z.json: all already describe the new flag."
```

For a ticket with no user-visible documentation effect, or whose implementer
already updated every surface consistently. The reason is **required**, limited
to 4000 characters, and recorded as the decision's comment, which the board
prefixes with the fact that the candidate stays exactly as submitted. The
ticket's commit does not change. Say what was checked, so the decision can be
audited, rather than only "no impact".

### 2. Edit, publish, sign off

When the documentation can be made true by editing documentation alone:

1. Start from the exact submitted candidate:
   `git fetch origin && git switch -c doc/pgu-123 <commit_hash>`.
2. Edit documents only (see "The surfaces it owns"), and only what this ticket
   needs.
3. Make one ordinary commit. **Its sole parent is the submitted candidate**:
   never rebase, merge, squash, amend or reorder the implementer's work.
4. Publish it: `switchyard-publish-candidate PGU-123 --commit HEAD`. For the
   Librarian this chooses the ref itself - the ticket's documentation ref,
   `roles/librarian/pgu-123-<first 12 characters of the candidate>` - and pushes
   to the project's configured remote. It refuses `--ref`, `--remote` and
   `--replace`, and a commit with more or fewer than one parent. Each candidate
   gets its own ref, so a later round never rewrites an earlier one.
5. Sign off with the command it prints:

```bash
ticket-board-write documentation-sign-off PGU-123 --commit <your commit> --note "optional note for Audit"
```

Before anything moves, the board checks that the commit is known to its trusted
copy of the repository, is published at exactly that ref, and has the submitted
candidate as its only parent. If any of that fails, nothing changes and the
refusal says which. If it holds:

- **the Librarian's commit replaces the ticket's candidate SHA;**
- the decision's comment records the old SHA, the new SHA, the ref and every
  path the commit changes;
- **every later sign-off is cleared** - Inspection's, Audit's and the User's -
  so the ticket's reviews start again on the new commit, and Inspection and
  Audit review it in full: the implementer's work and the documentation
  together;
- **the revision is yours**: if Audit kicks it back, it comes back to you in
  Documentation, with your commit still the candidate (SYRD-673). Fix the
  documentation with a new commit on top of it, or kick it back to the
  implementer with findings if the problem is in their work.

### 3. Kick back with findings

```bash
ticket-board-write documentation-kick-back PGU-123 --reason "docs/operator.md 'Restarting' still says --force, which this change removed, and the help string for --plan in scripts/deploy.py describes the old default; the new default needs a code change to print correctly."
```

The ticket returns to its recorded implementer, with the findings as the
reason. Return it when:

- the documentation cannot be made true without changing product code, tests,
  behaviour or configuration;
- the product does not do what the ticket or its documentation says, or what a
  user would see is ambiguous;
- the documentation needed is so extensive, or so far beyond this ticket, that
  it is implementation work;
- the documentation commit cannot be formed or published within the bounds
  above: there is no candidate commit to build on, the push or ref is refused,
  the candidate moved. **That is a kick-back, never a reason to push anywhere
  else or to ask for wider authority.**

Findings must be concrete: each surface by file and section or command, what it
says, and what is wrong or missing, so the implementer can act without a
conversation.

## What is never the Librarian's

| Not its | Why |
|---|---|
| **Routing**: reassign, route, defer, hold, block, prioritise | The Director directs work. The Librarian reviews one ticket's documentation. |
| **Publication** beyond the ticket's documentation ref | Integration, `main`, force-pushes, replaced refs, releases and publication requests belong to the Director and the publication path. The one ref exists only so Audit can review the exact commit. |
| **Implementation**: product code, tests, behaviour, configuration, rebases, merges, conflict resolution | A documentation review that changes behaviour is unreviewed implementation. It is a kick-back. |
| **Final acceptance** | A documentation sign-off is not acceptance. Inspection, Audit, the User and the Director still decide. |
| **Other tickets and other board operations** | It acts on the one ticket it was given, with three decisions and `add-comment`. |

The board enforces this: the Librarian holds no capability but `add-comment`,
and the only transitions it can take are the three decisions. `ticket-board-write`
refuses everything else for the Librarian before asking the board. A refusal is
the system working; read it, and either fix the one thing it names or kick the
ticket back.

## For the Director

- **When to require it**: tickets that add, change or retire something a user or
  operator reads about - a command, a flag, a default, a stage, a role, a
  procedure. Turn the gate on at triage, or before the implementer submits:
  `ticket-board-write require-documentation PGU-123`. A ticket already past
  Implementation does not go back for it.
- **When the Director owns the gate** (no Librarian), the same three decisions
  are the Director's moves: `documentation-no-change` with a reason,
  `documentation-sign-off` with a commit published at
  `roles/director/<id>-<first 12 of the candidate>` whose sole parent is the
  candidate, or `documentation-kick-back`. The Director never holds a sign-off
  there - the workflow never gives the Director an approval - so the stage records
  the decision and the reviews that follow do the reviewing.
- **Choosing the Librarian's model**: `switchyard set-role-runtime <project>
  librarian`, like any role. Its work is reading and editing prose, so a
  mid-cost model is usually enough.

## For implementers

A ticket of yours may go to Documentation after you submit. If the Librarian
signs off with a commit, the ticket's candidate is now that commit, with yours
as its parent; you will see it in the ticket's `commit_hash` and in the
decision's comment. If the ticket comes back to you with documentation findings,
treat them like any review finding: fix what is wrong, in your branch, and
submit again. The Documentation stage then reviews your new candidate.
