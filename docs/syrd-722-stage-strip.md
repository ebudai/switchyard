# SYRD-722: every declared stage within reach when the strip is wider than the board

The User's board, on a 3840x2160 screen at 1.5 scale (a 2560x1440 page), cut off Final Sign-Off and the stage before
it at the window's edge, with no visible way across.

## Why the last stages could not be reached

The live board declares 15 stages; 12 show by default (Backlog, Done and Cancelled are behind the Show filters).
`syncBoardGridColumns` gives each lane at least 205px with 16px gaps, so the 12 need 2636px. Beside the 300px composer
and the strip's padding, a 2560px page leaves 2224px.

The strip was already its own horizontal scroller (`.board-scroll`, `overflow-x: auto`), but nothing bounded its
height. It grew to the tallest lane, the page scrolled instead, and the strip's horizontal scrollbar sat at the
strip's bottom, below the window. The top bar offered no other way across, and the strip could not take keyboard
focus.

Measured on main `ee27a35e` with the live stage list and cards shaped like the live board's:

| viewport | strip content / visible width | page height / window |
| --- | --- | --- |
| 3840x2160 | 3540 / 3540 (fits) | 2160 / 2160 |
| 2048x1152 | 2672 / 1748 | 2145 / 1152 |
| 1440x900 | 2672 / 1140 | 2145 / 900 |
| 1100x900 | 2672 / 800 | 2226 / 900 |
| 2560x1440 at 1.5 (the User's) | 2672 / 2260 | 2145 / 1440 |

Main also lost the User's place. A redraw kept the strip's pixel offset, so turning on Show Deferred, which adds
Backlog second from the left, moved the view one stage back. A refresh resets the Show filters, so the pixel offset
it restored could land on another stage.

## The change

`scripts/ticket_board/frontend_script_stage_strip.py`, added to the page by `frontend.py`:

- **The strip is bounded to the window** wherever the lanes are a horizontal strip: wider than 900px, where the
  composer sits beside the board. `.layout` is the window's height (`100vh`, or `100dvh` where supported), and the
  composer and the strip each scroll inside it. The strip's horizontal scrollbar is therefore at the window's bottom
  edge however tall a lane is, and the wheel scrolls the lanes vertically as it scrolled the page. At 900px and below
  the lanes stack in one column under the composer (`mobileSectionMedia`), nothing scrolls across, and the page keeps
  its own scroll, as before.
- **The composer stacks at 900px, not 980px** (`frontend_style.py`). Before, it stacked at 980px while the lanes
  stayed a horizontal strip down to 901px. Bounding the layout only above 980px left that band with the original
  defect, which Audit measured on the first candidate: a 3,823px page in a 900px window, and the strip's scrollbar at
  its foot. Keeping the composer beside the board through 901-980px gives that band the same layout as every wider
  page. Stacking the composer inside a window-height layout would leave the lanes a 258px strip under the form.
  The ticket-detail and lightbox rules at 980px are unchanged.
- **Earlier stages and Later stages** in the top bar, with "Stages a-b of n". They show only when the stages do not
  fit. Each moves a page of whole stages: Later brings the first stage cut off on the right to the left edge, and
  Earlier brings the last stage cut off on the left to the right edge. At an end the button is `aria-disabled` rather
  than `disabled`, so it keeps the keyboard focus that pressed it.
- **The strip takes focus** (`tabindex="0"`, `role="region"`, "Workflow stages") and shows it with an outline, so its
  arrow keys scroll it across.
- **A redraw keeps the stage the User was looking at.** `renderBoard` records the stage at the strip's left edge (each
  lane carries `data-stage`) and its offset, then puts that stage back at the same offset. If a filter just hid that
  stage, the offset is kept, which puts the next stage in its place. A refresh saves the same anchor beside the pixel
  offsets (`frontend_script_refresh.py`), because the Show filters reset on reload. When the strip gets shorter, the
  browser clamps it to its new end.

Unchanged:
- lane widths and the SYRD-104 containment rules;
- card order;
- every declared stage is shown, and none is hidden, merged or reordered;
- the Show filters, card clicks and the detail view;
- the phone layout.

## Evidence

`tests/board_stage_strip_browser_test.py`, 80 checks. The fixture is a board with the live SYRD board's 15 declared
stages, read from its `/api/board` on 2026-11-01, and cards shaped like its own: a tall Implementation lane with long
blocked reasons, a long UAT lane, two cards in Final Sign-Off, 13 deferred and 60 done. It is served by the real
server and opened in Chromium. Main runs first from a git archive, then this tree.

- On main, on the User's screen:
  - the last stage is cut off;
  - the strip's scrollbar is below the window;
  - there is no other control;
  - Show Deferred moves the view off the stage being read.
- Here, at 3840x2160, 2048x1152, 1440x900, 1100x900 and the User's 2560x1440 at 1.5:
  - the page is no larger than the window, while a lane is taller than the strip;
  - the first stage, and after scrolling to the end the last stage (Final Sign-Off), is wholly in view: lane, header
    and cards, inside the window and clear of the composer;
  - the last lane's lowest card scrolls into view;
  - at the tallest lane's end the strip and its scrollbar are still in the window;
  - the stage controls show exactly when the stages do not fit, name the stages in view, and mark the end reached.
- Either side of both breakpoints, at 900, 901, 980 and 981px wide (900px tall), reading the tallest lane to its end:
  - on main, at 901, 980 and 981px, the strip scrolls across but its scrollbar is at the foot of a page taller than
    the window;
  - here, at 900px, the lanes stack in one column with nothing to scroll across and no stage controls;
  - here, at 901, 980 and 981px, the page stays in the window, the strip's scrollbar and Later stages are on screen,
    and the composer sits beside the board, which keeps the lanes most of the window's height.
- At 1440x900:
  - **Pointer.** The wheel scrolls the lanes vertically and only vertically, and a sideways swipe scrolls them across.
  - **Keyboard.** Tab reaches Later stages, and one press brings the stage cut off on the right to the left edge.
    Pressing on reaches the last stage, wholly in view, and focus stays on the button. Shift+Tab and Earlier stages go
    back to the first stage. The strip itself takes focus, shows the outline, and its arrow keys scroll it across.
  - **Resize.** Widening the window until every stage fits hides the controls, and narrowing it shows them again.
  - **Keeping the place.**
    - A ticket update keeps the stage and the lane's vertical position.
    - Show Deferred, Show Done and Show Cancelled keep the stage.
    - Hiding the stage at the left edge puts the next one there.
    - A refresh, whose filters reset, keeps the stage where a pixel restore would have shown the next one.
    - A strip made shorter at its end is clamped to its new end, with Final Sign-Off in view.
  - **Detail view.** A card in the last stage opens its detail, and closing it leaves the strip where it was.

`tests/ticket_board_frontend_refresh_test.py` pins the refresh script's behaviour under node, including the exact JSON
it stores. Its pinned values were edited by rule:
- each stored value gains `"boardStage":null`, from a stub of the stage-strip script in its harness;
- one new case shows that a saved stage is put back after the offsets, and a malformed one is ignored.

Mutants: 31, applied one at a time and each killed. They cover:
- the window bound, and the breakpoints: the first candidate's 981px bound, either breakpoint moved back alone;
- the focus outline;
- the controls' visibility, status text, end states and styling;
- both stepping directions;
- both listeners;
- `tabindex`;
- the anchor's capture, key, offset and restore, and the refresh's save and restore.

Lines a mutant showed to have no effect were removed rather than kept: the strip's explicit `overflow`, its
`min-height`, `overscroll-behavior`, a second `min-height` on the composer, and a fallback to later stages that
offset retention already gives.

The neighbour sweep ran 84 suites, on the final tree, that touch the board page on main and on this tree. The SYRD-104 containment, UAT
panel, delegation, write-token rotation and phone (SYRD-718) browser suites pass on both. Four suites fail identically
on both sides, at the same line: `create_ticket_stays_on_board_test`, `provision_workflow_projection_boundary_test`,
`ticket_board_ui_audit_test` and `ticket_board_write_api_test` (`temporary_cluster`).

Nothing live was changed. The User's check is to open the board on the same screen and reach Final Sign-Off.
