# SYRD-703: a board's build is confirmed from a fixed-size answer

## What happened

SYRD-699 made every `board_from_unit` privileged action confirm the proven board's build: `board_identity.board_get_for` asked the board's socket for `/api/board` and compared its `build_id` with the effective unit's release commit.

`/api/board` is the board's whole snapshot, every ticket with its comments. `request()` caps every answer at 8 MiB (`MAX_ANSWER_BYTES`), and reported an answer over the cap as "answered /api/board with HTTP 200".

The sizes measured live on 2026-10-29 were: mefp 12.5 MB, syrd 24.1 MB, otto 5.3 MB. So once mefp's units were repaired to its exact release, every allow_active action on it was refused, among them `deploy-release`, `upgrade-tenant` and `repair-boundary`. syrd would have been refused the same way, and otto was 3 MB from it.

## The change

**The board says its build on its own** (`server.py`). `GET /api/build` answers `{"build_id": "<build>"}` and nothing else: no write token, no snapshot. It is served by the same handler as every other read, on the TCP port and on the Unix socket.

**The build check reads that** (`board_identity.py`).
- It asks `/api/build` over the same proven connection as before. The SO_PEERCRED user and main-process checks, and the effective unit's user, argv, environment and trusted-release checks, are unchanged.
- The answer has its own cap, `MAX_BUILD_ANSWER_BYTES` (4 KiB).
- The build id must be a string of 1 to 128 characters from `[0-9A-Za-z._-]`, and then equal the release commit.
- Each refusal says what it was:
  - HTTP failure: `answered /api/build with HTTP 500`;
  - oversize: `answered /api/build with more than 4096 bytes`;
  - not JSON: `answered /api/build with no JSON`;
  - malformed: `answered /api/build with no well-formed build id`;
  - wrong build: `reports build <b>, not its effective root release <commit>`.
- Every other answer read through `board_get_for` keeps its 8 MiB cap. One over it is now refused as `answered <path> with more than 8388608 bytes`, not as HTTP 200.

**Boards that predate `/api/build`.** The privileged helper is one root install for the host (`/usr/local/lib/switchyard`), while each tenant's board runs its own exact release. `deploy-release` and `upgrade-tenant` authorize against the board as it runs before the upgrade. Every exact release since SYRD-699, mefp's 18a7b84b among them, has no `/api/build`. Refusing their 404 outright would have kept mefp refused, and would have newly refused pie, sapi and otto, with no action left to upgrade them by. The Director chose this bounded sequence instead (comment on SYRD-703):

1. A 404 for `/api/build` is decided from root's side. `_release_serves_build` reads the board code in root's trusted release itself: `<release>/scripts/ticket_board/server.py`, opened without following a link, and required to be root-owned and writable by no one else. It looks for the route, not a mention of it. If that code serves `/api/build`, the 404 is refused; for this release and later, `/api/build` is mandatory.
2. For a release that predates it, the check asks `/api/client-config` over the same proven connection, capped at `MAX_COMPAT_ANSWER_BYTES` (16 KiB). Main's answer is a few short fields. Only `build_id` is taken, validated the same way, and compared.
3. That answer also carries the board's write token. The parsed answer is dropped as soon as its build is taken. Nothing of it is returned, kept in the returned reader, logged or put in an exception: refusals name only the path, the status, the size, "no JSON", or "no well-formed build id". A build from this answer that is well-formed but wrong is not repeated either (`reports a build other than its effective root release <commit> (from /api/client-config, not repeated here)`): the token is itself a well-formed build id (`secrets.token_urlsafe`), so a board could hand it back as its build. Audit found this on the first candidate. A wrong build from `/api/build`, which carries nothing else, is still named.
4. A pre-`/api/build` release that answers `/api/client-config` with a 404 is refused, and needs an explicit repair. Nothing falls back to `/api/board`.

`/api/board` is never asked by the build check, on either path.

**Unchanged:**
- which board is asked, and how its peer is proven;
- the explicit `main_pid` test seam, which has no build check;
- `release_alignment._live_board_build_id`, which reads `/api/board` over the board URL with no cap. It is not an authorization path, and could move to `/api/build` separately.

## Tests

`tests/board_build_identity_test.py` runs main first, from a git archive, then this tree. Each board answers over a Unix socket proven by SO_PEERCRED against an effective unit naming a release directory.

On main:
- the real handler (`TicketBoardUnixServer`) with a 12.1 MB snapshot is refused as "answered /api/board with HTTP 200", having built the snapshot; so is the helper's `authorizing_board` for `upgrade-tenant`;
- there is no `/api/build`;
- a pre-`/api/build` board over the cap is refused the same way;
- any other answer over the cap is reported as HTTP 200.

Here:
- this tree's real handler passes with its snapshot never built, and so does `authorizing_board`. `/api/build` answers exactly `{"build_id": …}`.
- a board whose release serves `/api/build`, answering it oversized, not JSON, with a non-string, too long or malformed build, a wrong build, HTTP 500, or 404, is refused with its own message, and is asked nothing else.
- main's real handler, run in a process of its own as a pre-`/api/build` release with a 12.1 MB snapshot, is proven from its `/api/client-config`. It is asked `/api/build` then `/api/client-config`, and its snapshot is never asked for or built.
- a pre-`/api/build` board whose client-config is oversized, not JSON, has no build, a non-string build, the token as its build (with or without its own `write_token`), a wrong build, HTTP 500 or 404 is refused as such, and never asked for its snapshot.
- every such client-config carries a sentinel write token. Two of them answer with that token, exactly, as their `build_id`. It appears in no refusal (`str`, `repr`, `args`, cause, context) and in nothing reachable from the returned reader's closures. A wrong build from the client-config is not repeated, whatever it is.
- a release whose board code is group-writable is refused before anything else is asked. A release whose code only mentions `/api/build` is still read as one that predates it.
- an oversized `/api/workflow` is refused as over 8388608 bytes.
- this tree's `server.py` carries the route marker that `_release_serves_build` looks for, and main's does not.
