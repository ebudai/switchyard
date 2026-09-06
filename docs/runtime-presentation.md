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

Bootstrap the persistent slots and the preferred client arrangement once:

```text
switchyard present <project> bootstrap --layout viewer
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
