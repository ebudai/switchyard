# SYRD-741: a large paste in the UAT box leaves every control reachable

## What was reported

While reviewing a ticket in User Review, the User pasted the output of a failed `switchyard new` into the UAT panel's
questions box. The output was very large. The panel seemed to grow past the window, Accept could not be reached, and
the page could not be used again until it was reloaded.

## What main does

Measured in Chromium on main, with 3 MB of `switchyard new` output pasted through the clipboard:

- **The box itself holds.** A textarea does not grow with its text. After the paste, Ask Director, Accept and Send to
  Director were where they had been.
- **The confirmation does not.** Send to Director, and Ask Director, open the board's confirmation dialog
  (`openBoardUiDialog`), which lists every value it will send, the whole message included. The dialog had no height
  limit and sat centred in a fixed overlay. With the message in it, it was 643,258 px tall on the laptop and desktop
  (1,168,992 px on a phone). Its top was 320,000 px above the window, and Confirm and Cancel were the same distance
  below it. Nothing scrolled, and the overlay covered the page.
- **Escape made it permanent.** The page's Escape handler closes the open ticket. Pressed with the dialog open, it closed
  the ticket underneath, which threw away the pasted text, and left the dialog and overlay in place. Only a reload
  removed them.
- **Ask Director could never succeed.** `ticket_board.ask_director` refuses a question over 4000 characters, but the
  page found that out only after the dialog was confirmed, and said so only in the board's status line.
- **Sent text was drawn whole.** After Send to Director, the panel shows "Your message" in full: 541,028 px for the
  3 MB message on the laptop. The User's question in the thread is drawn the same way.
- **Dragging the box's grip had no limit.** The box grew to 4,121 px, pushing the controls that far down.

So the panel only looked as if it had grown: the dialog over it had.

## The change

**The confirmation dialog** (`frontend_script_delegation.py`), used by every board UI action:
- It is at most the window's height less 2rem, and scrolls inside itself.
- Each value line is at most 12em tall and scrolls inside itself. It keeps its line breaks, and wraps long unbroken
  lines.
- The button row is pinned to the bottom of the dialog, so Confirm and Cancel stay in view however much is above
  them.
- While the dialog is open, Escape is Cancel and goes no further, so the ticket behind it stays open with its text. The
  key handler is removed when the dialog closes, so Escape closes the ticket again as it always has.

**The UAT panel** (`frontend_script_uat.py`):
- The questions box can be resized by its grip, as before, up to half the window. The board's global
  `resize: vertical` already keeps it from getting wider.
- The sent message and the thread's question and answers are each at most 40% of the window tall, and scroll inside
  themselves.
- A note under the box says, as text is typed or pasted, when it is longer than Ask Director takes:
  "This is 3,000,000 characters. Ask Director takes 4,000 at most; Send to Director takes all of it." The note is
  `aria-live="polite"`. While it shows, Ask Director sends nothing, and the text stays in the box. A draft restored by a
  redraw is measured too.

The limit, 4000, is `ask_director`'s, copied into the page beside the Ask Director operation, which the page defines
itself. The test sends a question of exactly 4000 characters, and asserts the page does not call that too long.

**Unchanged:**
- what is stored: Send to Director stores the whole message, once;
- the draft kept across redraws;
- the operations, their payloads and their confirmations' wording;
- server limits.

**Rejected: pinning the panel's action row.** A pinned row is 150 px of buttons, which are full width on the board. On
a phone turned sideways, that is half the window. Chromium also scrolled the page itself to show the caret behind the
row, moving the open ticket up and uncovering the board beneath. Instead, the bounds above keep the controls a short
scroll below the box: 171 to 191 px below it on the viewports tested, whatever was pasted.

## Tests

`tests/uat_large_paste_browser_test.py` uses a production-built board with the shipped workflow and the real server,
which acts as the User only, as in production. Chromium runs at four viewports:
- the User's desktop, 2560x1440 CSS px at scale 1.5;
- a 1280x800 laptop;
- a 390x844 phone;
- the same phone turned sideways, 844x390.

The pasted text is 3 MB of step lines and tracebacks, with unbroken lines of thousands of characters. It is pasted
through the clipboard. Main runs first, from a git archive.

| On every viewport | Main | This tree |
|---|---|---|
| Send to Director's Confirm and Cancel | off the window, 320,000 px away | in view, and they take the click |
| Escape in that dialog | closes the ticket, discards the text, leaves the dialog | Cancel; the ticket and the text stay |
| The dialog's last words, scrolled to | (not reached) | in view, above the buttons |
| Box dragged 4000 px down | 4,121 px | half the window, never wider |
| "Your message" after sending 3 MB | 541,028 px (laptop) | 40% of the window, scrolling |
| A question of 3 MB | dialog, then refused out of sight | said at once, nothing sent, text kept |

On this tree, also on every viewport:
- the paste lands whole, sends nothing, and survives a redraw;
- the open ticket stays where it was drawn;
- the page answers within 3 s;
- Ask Director, Accept and Send to Director can be reached and take a click;
- nothing is wider than the window;
- a question of exactly 4000 characters is asked, and shown in a bounded block;
- Accept, with 3 MB still in the box, accepts;
- with no dialog open, Escape closes the ticket.

## Mutation

Seventeen mutants, all killed by an assertion:
- the dialog's height limit, its scrolling, or its pinned buttons removed;
- the value lines' height limit, wrapping, or line breaks removed;
- the Escape handler not added, letting Escape through, or never removed;
- the box's height limit removed;
- the sent message's bound, or the thread's bound, removed;
- the note shown at exactly 4000;
- Ask Director sending past the note;
- the note not updated on input, or on a redraw;
- the note without `aria-live`.
