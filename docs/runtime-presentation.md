# Runtime presentation slots

`switchyard present` lets a project's director change which persistent role
sessions appear in a project window. Presentation changes do not change role
configuration, board ownership, permissions, worktrees, CLI resume records, or
worker tmux sessions.

The launcher config and its projected `RoleConfig` entries remain the only role
registry. A versioned `presentation.json` file in the project owner's
Switchyard state directory stores display data: fixed slot IDs, slot-to-role
references, focus, configured layouts, a revision, and a bounded action
history. An existing project gets the same defaults lazily from its configured
role slots. Fresh project configs include the default layout explicitly.

Each slot is a stable `<project>-display-<slot>` tmux session. Its sole pane is a
proxy client attached to the selected `<project>-<role>` session. `show`,
`swap`, and `hide` respawn that proxy pane. They never send keys to, respawn, or
kill the worker pane, so an unsubmitted composer and the worker process ID stay
in place. If a worker exits, the proxy remains as a recovery surface. The
project viewer and separate Konsole layout attach to display sessions rather
than worker sessions.

Bootstrap the persistent slots and the window that shows them once. The default
is the terminal's own layout; the viewer is the tmux fallback and opens no
window, so bootstrapping into it is refused (see below):

```text
switchyard present <project> bootstrap
switchyard present <project> bootstrap --layout separate
```

The director can then inspect and change the presentation while workers run:

```text
switchyard present <project> list [--json]
switchyard present <project> show <role> --slot <n>
switchyard present <project> swap <slot-a> <slot-b>
switchyard present <project> hide --slot <n>
switchyard present <project> focus --slot <n>
switchyard present <project> restore [--layout default]
switchyard present <project> recover <role>
```

Mapping changes require `TICKET_BOARD_CALLER_ROLE=director`. If
`TICKET_BOARD_PROJECT` is present, it must name the selected project. The
controller also requires every role session and target to use the exact
`<project>-<role>:0.0` namespace. These checks prevent one tenant from attaching
another tenant's tmux session. The ordinary commands run against the configured
project owner's tmux server.

An incompletely repatriated tenant can still declare a different
`run_as_user` for a role. For that compatibility topology the display proxy
uses the already-installed role-control interface to attach to that account's
tmux server, and locks the inner worker session's prefix, secondary prefix and
root key table before the nested client starts. A missing grant therefore
fails before a display slot is created, while a shared-account or
process-authority project takes the direct path and requires no compatibility
artifact.

All presentation worker, display-slot, and viewer targets use tmux exact-name
selection. A missing session therefore cannot prefix-match, attach to, relabel,
respawn, or stop a longer session name owned by another role.

## A window is not a client

The three layers each have clients, and only one of them is somebody looking at
the project. A viewer pane is itself a terminal, so it is a client of the slot
it observes; six of them can exist, every slot can report `session_attached=1`,
and `list` can report every worker and client connected, with nothing on any
screen at all. That is the state a rolled-back identity cutover and a
`bootstrap` from a tenant account both produced.

So the presentation distinguishes them. A **presentation window** is a client
attached to the viewer or to a display slot whose terminal is not one of
presentation's own panes: the tab of a desktop terminal. `list` reports it as
its own line, separately from the per-slot client state.

`bootstrap` opens that window and will not report success without one. It
defaults to the terminal's own layout — `--layout separate` — which is one
window of six tabs arranged two rows by three by the terminal itself, with its
native split and focus controls. The `viewer` layout is the tmux fallback for
hosts with no such terminal: it nests the slots inside another tmux session,
draws a second status row, and opens no window at all, so `bootstrap
--layout viewer` is refused rather than reporting a presentation nobody can
see.

For the native layout the proof is both halves: a client from outside
presentation, and the terminal process still running. A terminal handed a
layout file it cannot read aborts — status -6, empty log — after its tabs have
briefly existed, so client counts alone would call that a success. If the
window cannot be proved, `bootstrap` fails, takes down anything headless it
just built, and names the command that does work from where the caller is —
`switchyard <project>`, from an ordinary terminal in the desktop session that
owns the screen, or through that account's lifecycle control bridge, which
carries the desktop identity across.

Three things have to be true for that window to open at all, and each was
wrong:

- **The layout file must be readable by the account the terminal runs as.** The
  window is correctly dropped to the desktop user, while the generated layout
  was 0600 under the tenant's 0700 state directory. It is now written under the
  desktop account's own state directory, still 0600 in a 0700 directory, owned
  by the account that reads it. An invocation that cannot do that refuses
  instead of opening a terminal that will abort.
- **The tab's working directory must be one that account can enter.** It was
  the launching process's own home, which under `sudo` is `/root`.
- **Each tab must reach its display session without a password.** The sessions
  belong to the tenant owner and the window to a person, so something crosses
  that line once per tab. `sudo -u <owner> tmux attach` asks for a password in
  each of six tabs as the window opens, and the way to avoid that is a blanket
  grant on the owner account. Instead each tab runs
  `/usr/local/lib/switchyard/<project>/switchyard-display-attach <project>
  <slot>`: root-owned, reached through one `NOPASSWD` entry naming only that
  program, taking a slot number as its only input, and attaching by exact name
  to a display session of that project. It can name no worker session, no other
  tenant, and no command to run inside one.

Choosing which session is attached is only half of that last one. **An attached
tmux client inherits the session's key tables**, so exact-name selection
constrains the initial attach and nothing the client does afterwards. With the
owner's default prefix still live, a desktop user handed a display slot could
press `prefix c` for a shell in the owner's account, `prefix :` for the tmux
command prompt, or `prefix s` / `prefix )` to reach any other session on that
server — another project's included, where two share an owner.

So the transport is locked before the attach, with three session options and
all three are needed:

| option | value | what it closes |
| --- | --- | --- |
| `prefix` | `None` | the prefix table, and with it every default binding |
| `prefix2` | `None` | the secondary prefix, which would otherwise still reach it |
| `key-table` | `switchyard-display` | the root table, which is consulted with no prefix at all — an owner whose `tmux.conf` carries any `bind -n` would keep exactly that binding live. The named table is never given any bindings, so every lookup in it misses. |

What is left is a client whose keys all fall through to the pane — the proxy
attached to the worker — so ordinary typing still reaches the role while no
keystroke reaches tmux. `switchyard-display-attach` applies the lock itself,
as the owner, before it attaches, and refuses if it cannot: the guarantee is
the bridge's own and does not depend on whatever configured the slot, so a
session left by an earlier release is locked too. Every slot is also locked as
it is configured, because a client can attach the moment a session exists.

The identity cutover stops only the worker sessions. The slots are long-lived
and are re-pointed in place, so the window the tenant was looking at is never
taken down, on either the success or the rollback path; if it is gone anyway,
the transaction says so instead of reporting a presentation that reconnected.

## Client sizing and status bars

A presentation client stack has three layers: the worker session, the display
slot that proxies it, and — in viewer mode — the viewer that aggregates the
slots. Each layer is a tmux session, so each one could draw a status line and
each one could resize the layer below it.

Only the worker draws a status line. Display slots and the viewer both set
`status off`; the slot label lives in the display session's window title and
its `@switchyard_slot`/`@switchyard_role` pane options, and the viewer repeats
it on the pane borders. A client therefore shows one status bar per visible
worker rather than one per layer.

Sizing follows the window an operator is actually looking at:

- Viewer panes attach to their display slot with the `ignore-size` client flag.
  A viewer that is smaller than a separate window showing the same slot no
  longer shrinks that slot, and when the viewer pane is a slot's only client
  tmux still sizes the slot from it.
- A display slot attaches to its worker as that worker's sizing client, so the
  worker follows the presentation window as it is resized. When the worker is
  already attached to a client of its own — an ordinary project window that is
  still open, for instance — the slot attaches with `ignore-size` instead and
  leaves the geometry that window is showing alone.

Reconciliation applies both rules, so `show`, `swap`, `hide`, `restore` and
`recover` repair a running presentation instead of leaving the policy to the
next bootstrap. Reconciliation also re-applies `ignore-size` to the viewer's
live clients, which repairs a viewer that an earlier build started.

`list` distinguishes missing or dead workers from disconnected display
clients, reports whether a resume record exists, and lists configured roles
that are hidden. `recover` is the only presentation operation that may make one
bounded call into the existing launcher start/resume path. It rechecks the
configured desktop readiness and uses the prepared role environment, without
repeating interactive first-run consent. Display slots do not make background
model calls.

During an ordinary presentation-enabled launch, one worker start failure does
not prevent other independent workers or the display clients from starting.
Each failed visible role's slot shows its recovery status, while the launcher still
returns the first worker failure code for automation and operator reporting.

State mutations hold an advisory lock, preflight the full mapping, update every
proxy, then atomically write the next revision. A partial tmux failure reapplies
the previous mapping and leaves the prior state revision on disk. Named layouts
come from the config's `presentation.layouts` object and therefore survive
reconnects and relaunches without introducing another role definition source.
