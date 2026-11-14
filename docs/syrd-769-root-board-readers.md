# SYRD-769: root's readers of a tenant board ask the board root's unit names

SYRD-766 made root's `switchyard status` ask the board root's own unit names (`board_identity.board_get_for`). The
other root-run readers still took the board's address from the tenant's configuration, which the tenant can point at
a socket or URL it controls, and so choose the answer root acts on or records:

| reader | reached as root by | read |
|---|---|---|
| `board_authority_preflight.process_authority_board_compatibility` | `start` | `/api/runtime-assignments` |
| `board_authority_preflight.board_socket_answer` | `resume-provision`, `deploy-release` | `/api/runtime-assignments` |
| `board_workflow_readers.read_board_declared_workflow`, `read_board_workflow_state` | `adopt-workflow`, `migrate-workflow` | `/api/workflow` |
| `provider_runtime_state.read_runtime_assignments`, `read_runtime_assignment_details` | `resume-provision` | `/api/runtime-assignments` |
| `presentation_runtime_assignments`, `presentation_role_recovery` | `replace-window`, `present … recover` | `/api/runtime-assignments`, `/api/workflow` |
| `release_alignment._live_board_build_id` | `release-status --close`, `deploy-release` | `/api/board` (its build id goes into root's journal) |

## As root, the board root's unit names

`board_identity.unit_connection(project)`:
- reads `/etc/systemd/system/<project>-ticket-board.service` as root's (the same descriptor walk as SYRD-694), for
  the one `--unix-socket` its `ExecStart=` names and its `User=`;
- connects there, and asks the kernel (`SO_PEERCRED`) who answered: it must be the unit's service account **and** the
  unit's main process as systemd reports it. Every tenant board runs as `boardsvc`, so the account alone would not
  tell one tenant's board from another's.

Each reader chooses through `board_connection_factory(config, given)` (socket readers) or `board_opener(config, given)`
(readers written against `urlopen`):
- a connection the caller names is used, root or not. No production caller names one, except recovery's replay of an
  answer it already read, which opens nothing;
- as root (effective uid 0), `unit_connection`. The configured `board_socket`, or the host and port of the
  configured `board_url`, are never connected to. The opener asks exactly one route from a fixed list,
  `ROOT_READ_ROUTES` (`/api/board`, `/api/workflow`, `/api/runtime-assignments`): the one the reader appended to the
  URL. Nothing the tenant's `board_url` says before it, whether a path, a query or a fragment, is asked, and a URL
  ending with no listed route is refused;
- otherwise, as before, the configured socket or URL.

What a reader does with the answer is unchanged: the same project, authority and document checks.

**Root must choose the route as well as the board.** A board's answers can be trusted only per route.
`/api/image/<path>` serves, unchanged, the bytes of an image file under the frame directory, which the tenant's owner
writes. So a proven connection to the right board is not enough if the tenant chooses the question: a `board_url`
ending `/api/image/<frames>/b.png?` once made root's live build id whatever `b.png` held (Audit of the first
candidate).

- **Nothing answering** on the unit's socket is an `OSError`, as for any socket: `board_socket_answer` reports it as
  silence, and resume keeps waiting within its bound.
- **Something other than the board answering**, or a unit that cannot be read as root's, is a `BoardIdentityError`:
  `board_socket_answer` refuses at once rather than waiting.

## A board's raw HTTP words, shown inert

`http.client` puts the raw status line in `BadStatusLine`, and `urllib` puts the server's reason phrase in
`HTTPError`. Every reader above, and `board_supervisor_migration.default_probe`, now:
- shows an exception's words through `phone_text.clean_text`, which leaves no control, format or bidirectional
  character;
- catches `http.client.HTTPException` where only `OSError` and `URLError` were caught, so a malformed status line
  becomes the reader's refusal, not a traceback that prints it raw.

As root, the opener says a malformed answer in its own words with the exception's class, and a non-200 answer with
its status alone, never its reason phrase.

## Publication request fields

`switchyard-publish-ref` already refused a `requested_by`, `ref` or `bundle_path` holding a control character
(SYRD-766). Each field now has its own test, so dropping any one of them from the check is caught.

## Not done here

A board run by the board supervisor (SYRD-586) is not the unit's main process, so root refuses to read it this way,
as `board_get_for` already did. No tenant runs under the supervisor today.

Tests: `tests/root_board_readers_test.py`.
