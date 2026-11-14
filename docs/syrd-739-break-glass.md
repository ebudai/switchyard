# SYRD-739: a one-operation break-glass role for a provider safety hold

A safety refusal holds a provider account (SYRD-672). Sometimes the work it refused must be isolated now, and the
Director that would isolate it is held, or otherwise not usable. The historical fix was a temporary role on another
provider: it made the one board change that was needed, then handed control back. This makes that a bounded,
journalled operation whose limits the board enforces, not the role's prompt.

```bash
pkexec switchyard break-glass <project> --cli codex --hold <N> --allow isolate,record --reason-file why.txt  # preview
pkexec switchyard break-glass <project> --cli codex --hold <N> --allow isolate,record --reason-file why.txt \
    --apply --expect sha256:...                                                                             # run it
```

`--ttl S` sets how long the grant lasts: 60 to 3600 seconds, 900 by default.

Code:
- `scripts/break_glass_role.py`: the operator's command;
- `scripts/ticket_board/break_glass.py`: the board's route and the role's client;
- migration `pgu1040` (`schema.sql` identical, grants in `rbac.sql`).

Tests: `tests/break_glass_test.py` and `tests/break_glass_live_test.py`.

## The grant is the authority

`break_glass_grants`, one row per grant, holds:
- the hold, the operator, the runtime and the reason;
- the **sha256 of a random token** (the token itself is never stored);
- the **operations** it may choose from;
- **`expires_at`**, by the database clock;
- the recovery packet's provenance;
- what it **attempted** and what came of it (**completed**);
- its **state**;
- the **hand-back**.

Only one grant stands at a time.

| operation | what it does, through the board's ordinary path |
|---|---|
| `record` | one comment on a ticket (`add_comment`), prefixed with the grant's number |
| `isolate` | one ticket out of automation (`set_manually_controlled`) |
| `reassign` | one ticket to another role, with a reason (`reassign`) |
| `unblock` | one named blocker off one ticket (`set_blockers`) |
| `abort` | always allowed: ends the grant with no change |

`POST /api/break-glass` takes `{token, operation, args}` and nothing else. It is checked before any caller role, so
the temporary role needs no pane registration and lends no role's identity.
1. **Claim.** The database claims the grant (`claim_break_glass`). The token must be known, the grant open and not
   expired, and the operation one it allows. Otherwise the grant ends: expired, refused, or aborted for `abort`. The
   claim is **committed as `operating` before anything is done**, so neither a second request nor a crash mid-way
   can reach a second operation.
2. **Check.** The arguments must be exactly the operation's shape, with ticket, role and blocker identifiers matching
   their patterns.
3. **Perform.** The one operation runs as the Director operation it is (`update_ticket`, `reassign_ticket`). Every
   invariant, trigger and notice of that path applies. The board's records name the operation's role. The grant row
   names the grant, and a comment or a reason carries `Break-glass grant N`.
4. **Finish.** `finish_break_glass` records `completed`, with the ticket as it now stands, or `refused`, with the
   board's reason. A refusal ends the grant like a completion.

`GET /api/break-glass` lists the newest grants, never a token or its hash.

A claim does not ask again whether the hold still stands. A grant opened for a hold that is cleared before the role acts still allows its one operation: the operator opened it for that hold, and it lapses at its deadline either way.

The service and the listener may read every column of a grant except the token's hash; the status read omits it too.

Root's functions are granted to no role: `open_break_glass`, `end_break_glass` and `hand_back_break_glass`. Only the
board service may claim or finish.

## The command

**Who.** Root, elevated by pkexec for a named person, as `rebind-director` and `rebind-workflow-panes`. The tenant
configuration is root's verified one. The board is read over root's kernel-proven socket, and written through the
database owner.

**Preview.** It reads, and touches nothing:
- the SYRD-737 recovery packet for the hold, validated and verified against a second read;
- the runtime. It must not be under a hold (a held account is never even probed), must be installed and signed in for
  the owner, and must be one whose fresh session takes its first instruction on the command line: `claude` or `codex`.

It prints a review digest; `--apply --expect DIGEST` runs exactly that.

**Run.**
1. The board opens the grant with the token's hash.
2. A scratch directory is built, `break-glass-<grant>/` beside the project's layout files, owned by the project
   account:
   - `work/` holds only the validated `recovery-packet.json` and `INSTRUCTIONS.md`, the exact client command for each
     allowed operation;
   - `home/` holds only the chosen runtime's own credential file (`role_credentials`' allowlist) and the entries that
     open it at a prompt in the sandbox's work directory;
   - `token` is a 0600 file.
3. `<project>-break-glass` starts as the project account, inside a **bubblewrap sandbox**. Its pane opens the token
   file as fd 9, deletes it, and execs `bwrap`, which builds the role's filesystem from an empty root:
   - `/usr`, `/bin`, `/sbin`, `/lib*`, `/etc` and `/opt`, read-only;
   - the CLI's own install, read-only, as the project account's login shell finds it: its npm package directory when
     it sits inside a `node_modules` tree, otherwise only its own file, and node's file for a node script. A
     `package.json` anywhere else marks nothing, and no program's directory is bound. A bind that would be, or hold,
     the project account's home, `/home` or `/` is refused at preview ("cannot confine"). The preview lists the binds,
     and the digest covers them;
   - fresh `/proc`, `/dev`, and tmpfs `/tmp`, `/var/tmp`, `/run` and `/home`;
   - the scratch home read-write at `/home/break-glass`, and `work/` read-only at `/work`;
   - the client at `/run/break-glass/break_glass.py`, and the token copied from fd 9 to `/run/break-glass/token` on
     the sandbox's own tmpfs.

   It runs in its own pid, ipc and uts namespaces, with a cleared environment: HOME, PATH, TERM, LANG, the board URL
   and the token file's path, nothing else. The network is shared, for the provider and the board. The CLI gets one
   fixed first instruction.

   So the following are not in the role's world at all, whatever path it tries:
   - the project account's home and state;
   - the Director's session, worktree and transcripts (the refused content);
   - any other provider's credentials;
   - the board's write token;
   - any other grant's token.

   There is also no root. After the start the token exists nowhere on disk. Inside the sandbox, the path down to the
   CLI's install tree is visible, holding only that tree: a home's name, when the CLI is installed in it.
4. It waits until the grant ends, or its time runs out, or the session ends by itself. Ctrl-C or SIGTERM aborts.
5. Whatever happened, the session is stopped, the whole scratch directory (the credential copy, the packet copy and
   any token) is removed as the project account, and the grant ended on the board:
   - expired, if its time ran out;
   - aborted, for any other end without a result;
   - ended, if the board left it operating;
   - a grant the board already finished stays as it is.
6. **Hand-back** (`hand_back_break_glass`):
   - **to the Director**, only when its account is not held now: one fixed notice, payload `break_glass_handback`,
     naming the grant and its one request. The listener delivers it only while the Director's account stays unheld;
   - **otherwise** nothing is sent. The hold stays, and the operator is given the full replacement:
     `pkexec switchyard rebind-director <project> --cli <runtime> --hold N` (SYRD-738).

Root's rollout journal records the plan, the grant, the start, the end (attempted and completed) and the hand-back.
The grant row keeps the same.

## Not done here

- A role's mutation is recorded on the board as the Director operation it is; the grant row, not the board's actor
  field, names the break-glass grant.
- The sandbox keeps the role from the project account's files. It does not hide the role from the account's other
  processes, which run as the same user and could, under the host's ptrace policy, reach a running process.
  A dedicated account per grant would close that, at the cost of provisioning one.
- `agy`, `hermes` and `copilot` are not offered: no verified way is known here to give their fresh interactive session
  its first instruction on the command line.
