# SYRD-677: a reporting tenant follows up on its own upstream report

## What happened

On 2026-10-25 the pie Director filed SYRD-673 and SYRD-674 on the Switchyard
board. Within the hour, each needed a follow-up from the reporter: on one, a
different approach the operator now preferred; on the other, a caveat. The
report credential could only create, and the board guide said so as policy.
Every correction would have been a new report for the upstream Director to
reconcile by hand.

## Who is reporting

The upstream board could not tell its tenants apart.

- **One shared secret.** Provisioning mints one report token per board
  (`TICKET_BOARD_TENANT_REPORT_TOKEN`), and `refresh_upstream_report_credential`
  copied that value verbatim to every tenant.
- **A claimed project.** The reporting project was whatever the payload's
  `origin_project` said. A token holder could name any project; only the digest
  stopped a mismatch with the preview.
- **No usable author record.** A ticket has no author column, and the origin
  fields can be edited by the Director.

So "a report it filed itself" needs two new facts.

### A credential bound to a project

`report_follow_up.bound_token(binding_key, project)` is
`syrdr1.<project>.<HMAC-SHA256(binding_key, "syrd-677 report credential:<project>")>`.

**The binding key is the board's own.** It is `TICKET_BOARD_REPORT_BINDING_KEY`,
kept in the board's environment file beside its report secret. Only two things
read it: the board's server, and the credential refresh, which runs as root and
reads that file by descriptor.

**It is never the report secret.** Audit found the first candidate's flaw: that
version derived credentials from the report secret, and every tenant was handed
that secret before this change. So any tenant that kept it could mint any
project's credential, and no refresh could revoke what it already knew.

**So:**
- a credential derived from the report secret is refused;
- a binding key equal to the report secret is no key: the board then advertises
  only shared credentials;
- rotating the binding key revokes every bound credential.

The board verifies a credential with no registry. The project is read from the
credential and checked in constant time; it never comes from the request.

**Where the key comes from:**
- a new board gets it at provision, with its report secret;
- a board provisioned before this change gets it from root's
  `sudo switchyard upgrade <board> --only accept-reports`. That writes it once,
  0600, keeping the rest of the file. It takes effect when the board next starts.

| Credential | `file_report` | Follow-up |
|---|---|---|
| The board's shared secret | Files, as before | Refused |
| A credential bound to a project | Files, only with that project as `origin_project` | Accepted, for that project's reports only |

`refresh_upstream_report_credential` runs on every upgrade and on
`--only upstream-report`. It gives a tenant a credential bound to the tenant's own
project, derived from the binding key. It does this only when the upstream
board advertises `report_credentials: "bound"` and its key is readable. A board
that doesn't answer, an older board, or one without a key, is given its secret
as before, so a tenant's `file-report` never breaks ahead of its board's deploy.
The next refresh asks again.

The advertisement is in the board's public `/api/runtime-assignments` answer,
which a connection already reads to know the board is itself (SYRD-674).
`upstream_report_sinks.board_reading` returns that answer and `connect` hands it
to the refresh, so a new tenant's board is still asked once.
`upstream_report_connect_test` now patches `board_reading`, which `board_answer`
also goes through.

The probe is a parameter (`takes_bound`). `upstream_report_credential_test`
answers it with a stub, because its `UPSTREAM` is the live board's port.

### Provenance that does not move

`ticket_board.report_provenance` records which project filed a report and its
source reference:

- an AFTER INSERT trigger on `tickets` writes it, for any ticket created with an
  origin (only `file_report` creates those);
- a BEFORE UPDATE trigger refuses any change;
- no role is granted write access.

A Director's edit of a ticket's origin fields therefore moves no authority. The
migration backfills it once from existing tickets with an origin
(`recorded_from = 'backfill'`), without rewriting the tickets, so existing
reports become eligible on upgrade.

## The follow-up

`POST /api/tickets/actions/report_follow_up` accepts the report credential only.
`server.py` now routes both report actions to `report_follow_up.handle`, in
fewer lines than before.

**The payload** is `ticket_id`, `kind` and `text`, plus `confirmation`, the
preview's digest:

- the kinds are `comment`, `correction`, `withdrawal_request` and
  `supersession`;
- the text is at most 8000 characters;
- any other key, including a project, is refused.

**The digest.** As for `file_report` (SYRD-602), nothing is written without an
exact confirmation. The digest is SHA-256 over canonical JSON of:

- the destination board's project and ticket prefix;
- the ticket id;
- the credential's project;
- the report's source reference, from provenance;
- the kind and the literal text.

The board recomputes it from what it holds.

**The order of checks:**

1. The credential.
2. The payload.
3. The ticket exists.
4. It was filed as a report.
5. By this project.
6. The digest.

Authority is decided before the digest, so another tenant's report is refused as
such whatever the request confirms. A digest made with a source reference edited
after filing is named as exactly that, with both values.

**The database.** `append_report_follow_up` is service-only, as `file_report`
is, and rechecks everything:

- the ticket, its provenance and the project;
- the source reference.

It then:

- records the confirmation in `report_follow_ups`, keyed by the digest. A second
  submission, or a simultaneous one, returns the comment already added and adds
  nothing;
- appends one comment as `reporter:<project>`. A correction, a withdrawal
  request or a supersession is prefixed so it reads as what it is;
- enqueues one Director notice: queue kind `ticket_update`, payload kind
  `report_follow_up`, deduplicated by the digest.

**What it never changes:** state, assignee, blockers, waits, flags, sign-offs,
commit data, parentage, or the ticket's `updated`. The test compares the whole
ticket row before and after.

**Finished reports.** A follow-up is accepted on a report in any state, a
finished one included. Its notice is current for the Director whatever the
ticket's state; `report_follow_up` is a Director-only notice kind.

## The client

`ticket-board-write report-follow-up --ticket <ID> --kind <kind> --text <text>`
(or `--text-file -`) is a preview:

- it reads the destination's identity and the ticket from the upstream board;
- it refuses early when the ticket was not filed from this project;
- it prints the board, the ticket, its state, the project, the kind, the
  attribution, the literal text and the digest;
- it sends nothing.

The same command with `--submit-upstream --expect <digest>` sends exactly that
follow-up.

The project is `named_project` of the credential. `--origin-project` and
`TICKET_BOARD_REPORT_ORIGIN_PROJECT` play no part. The shared token is refused
with the narrow repair command. No token is ever printed.

## Where it shows

| Where | How a follow-up appears |
|---|---|
| Board page | `Reporter (<project>, external)`, marked `data-comment-origin="external-reporter"`, with a dashed accent |
| `ticket-board-read` | `reporter:<project> (external reporter):` |

The board guide and the board skill describe the operation.

## Tests

`tests/upstream_report_follow_up_test.py` uses production-built `syrd` (upstream)
and `pie` (tenant) boards, the real CLI as a pie pane runs it, and direct HTTP.
Main runs first from a git archive.

**Main:**

- the shared token files reports;
- there is no follow-up command or endpoint;
- a bound credential is refused.

**Filing:**

- the bound credential files from pie, and is refused as otto;
- the shared token still files.

**The follow-up:**

- the preview sends nothing;
- the submission adds one literal `reporter:pie` comment and one Director
  notice, and leaves the ticket row unchanged;
- a replay adds nothing;
- three at once give `[200, 200, 201]` and one comment;
- a changed text is refused.

**Refusals, each with its own message:**

- a ticket that doesn't exist: 404;
- a local ticket: 403;
- another tenant's report, and the other tenant's credential, whatever it
  confirms: 403;
- a source reference edited after filing: 403, naming both values;
- the shared token, no credential, a forged credential, a rotated binding key: 403;
- a credential minted from the shared secret every tenant once held (Audit's
  case), for its own project and for another's: 403;
- a field, or a named `origin_project`: 403;
- a wrong confirmation, and text over 8000 characters: 400.

**The database refuses on its own:**

- another project's follow-up;
- any writer but the service.

**Everything else:**

- provenance is fixed;
- a finished report takes a withdrawal request, nothing moves, and its notice is
  the Director's;
- a project named in the pane's environment is ignored;
- a report filed on main's schema is followed up after the upgrade;
- the board page (in Chromium) and the read CLI label the reporter;
- the refresh writes a credential derived from the binding key (containing
  neither the key nor the secret) once, for a board that reads one and has a
  key, and the secret otherwise;
- root's `--only accept-reports` gives a board its binding key once, keeping its
  file, 0600;
- a binding key equal to the report secret opens nothing, and such a board
  advertises only shared credentials;
- no token appears in any output.

### Neighbour suites

- `ticket_board_project_workflow_provision_test` removes the real workflow seed
  INSERT to prove the projection then fails loudly. Its regex ran on to the next
  line *starting* `ON CONFLICT`, which the backfill provided, so that clause is
  indented, like the migration's others.
- `tests/field_audit_grants_test.py` (SYRD-668) compared every board object's
  grants with its pre-ticket base. It went red on main when SYRD-675 dropped
  `note_role_stall`'s four-argument form. It now compares the objects both sides
  have: neither a later ticket's additions nor its replacements are its to judge.
