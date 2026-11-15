# SYRD-781: a Librarian gets a display slot whenever the window has room; Router never

The User, creating test11 on 2026-11-15: "I chose to add the librarian, but it did not give that role a pane, and
started 5 instead of 6 slots". The Librarian was declared `slot: null`, which was by design then, and the only notice
was one easy-to-miss line, "it starts detached with the other roles". The User's rules:

- the Librarian "should only not get a slot if I have 6 filled already, but if I only have 5, there is room so why
  not";
- for Router: "no, don't show the router".

## The rule

- **The limit:** a project's window has six display slots. That is `documentation_gate.VISIBLE_SLOTS`, the same as
  workflow_config's visible-slot rule (0 to 5), the launcher's `MAX_VISIBLE_PANES_PER_WINDOW` and display-attach's
  `MAX_SLOTS`.
- **The free slot:** `documentation_gate.free_visible_slot(document)` is the lowest slot no active role holds, or
  None when all six are taken.
- **The Librarian:** `with_documentation_gate(document, librarian={..., "slot": n})` declares it in that slot. A
  missing slot means the background. It stays `ephemeral`, with a fresh conversation per ticket.
- **Router:** never given a slot.

## `switchyard new`

- **Composition:** `plan_with_documentation_gate` gives the Librarian `free_visible_slot` of the composed workflow,
  after its roles (in order, as `plan_document` slots them) and Router's (none). For test11's choices (designer,
  director, audit, main, ops and a Librarian) that is slot 5, so the window has six slots.
- **The summary before anything is created** says which:
  - "it has a pane of its own, display slot 5";
  - or "all six display slots are taken, so it runs in the background (`switchyard attach <project> librarian` shows
    it, or `switchyard present <project> show librarian --slot <n>` puts it in a slot)".
- **Router's summary** says "it runs in the background with no pane (`switchyard attach <project> router` shows it)".
- **A `--workflow-config` with its own slots:** if its slots put the Librarian elsewhere than that summary said, the
  composition says so again.

## Fresh sessions keep showing

An ephemeral role's next ticket starts with `/clear` typed into its own pane (`notification_session_clear`). That is
the same tmux session, which its display slot attaches to, so the slot keeps showing the Librarian between tickets.
There is never a new session that the slot would have to find. A role restart reconnects its slots as before
(SYRD-752).

## Existing projects

- **`switchyard documentation-gate <project> enable --runtime <cli>`** gives the Librarian the free slot too, and
  says: "the Librarian takes display slot N; it appears in the project window once the window is relaunched (as
  add-role --relayout's new slots do)". With all six taken, it says the Librarian runs in the background, and how to
  see it.
- **The layout:** a declared workflow's projection already writes the project's generated layout, so it gains the slot
  on the same write. A project with a hand-maintained layout has no declared workflow, so it cannot enable the gate
  through this path. Nothing else rewrites a layout.
- **The window:** a saved presentation grows to the new slot count when the window is next launched
  (`presentation_state_document._read_state`).

## test11

test11 declares its Librarian already, with `slot: null`. Once this is deployed, the Director gives it the slot:
1. Run `switchyard documentation-gate test11 disable`. The board refuses while a ticket is in Documentation.
2. Run `switchyard documentation-gate test11 enable --runtime claude`. It takes slot 5 and says so.
3. Relaunch test11's window.

Re-enabling starts the Librarian from its template role's launcher entry, so set its model again with
`switchyard set-role-runtime test11 librarian --model <model>` if it should not be the template's.

## Tests

`tests/librarian_display_slot_test.py` runs `switchyard new`'s real composition: a real provisioning plan, its
declared workflow, Router's and the gate's. It projects that into a real launcher configuration and layout, and loads
it as the launcher does. It shows:

- the six-slot limit is the window's;
- five roles and a Librarian: the Librarian in slot 5, ephemeral, not detached, with a six-pane layout, and the
  summary says it has a pane;
- six roles: the Librarian is in the background and detached, and the summary says so and how to see it;
- the Director's gate: no Librarian, and five panes;
- Router: never a slot, even with one free, and the Librarian takes the next one;
- the presentation's slot 5 shows the Librarian; a saved five-slot window grows to it when relaunched; each fresh
  session is a clear in the same pane;
- `documentation-gate enable`: the free slot; none named means the background; a taken slot is refused.

`router_onboarding_test` and `new_project_phases_boundary_test` pin the new Router summary.
