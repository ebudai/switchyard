# SYRD-730: a hidden Director-capacity banner takes no room

After SYRD-728, the live board, on release `25a99757`, showed an empty dark-red strip above its stages, although
`GET /api/director-capacity` answered `{"open":null,"alert":null}`.

The banner is `<div id="directorCapacityAlert" class="alert alert-blocked" role="alert" hidden>`. Its script hides it
correctly when there is no alert: it keeps `hidden` and empties the banner. But `hidden` only works through the user
agent's `[hidden] { display: none }`, and any author rule that sets `display` beats it. The board's shared `.alert`
rule sets `display: grid`, so the hidden banner was an empty styled box. SYRD-728's banner check ran in node, with no
stylesheet, so it saw only the attribute.

## The change

`frontend_style.py` gains `.alert[hidden] { display: none; }`, next to `.alert`. It outranks every alert class
(`.alert`, `.alert-blocked`, `.card-alert`, `.card-alert-blocked`), whatever their order, so no alert carrying
`hidden` takes room. This follows the page's existing `.x[hidden]` rules. The endpoint, the script and a real alert
are unchanged.

## Evidence

`tests/director_capacity_banner_browser_test.py`, 33 checks:
- **Setup:** a production-built board, the real server and the real endpoint, with Chromium driving the page.
- **Sizes:** desktop (1280x800) and phone (390x844, touch).
- **Order:** main (`79f0b1b0`) runs first, from a git archive.

**Main:** with no alert, the endpoint answers null and the banner is hidden and empty. It still has `display: grid`
and takes room above the board, on both sizes.

**Here, on both sizes:**
- **No alert:** the endpoint answers exactly `{"alert": null, "open": null}`. The banner is hidden and empty, with
  `display: none` and no width or height. The board starts where the banner would be.
- **Every alert class**, on a hidden element in the banner's place, takes no room.
- **A real alert** (an exhausted episode, as escalation leaves it, then a reload): the banner shows its title, "3
  automatic retries did not get through", the recovery steps and "This clears when a Director turn ends normally",
  inside the window's width.
- **Cleared,** as a normal Director turn clears it: the page's own refresh hides the banner again and leaves no strip.
- **No page errors.**

**Mutants:** 7, each killed by a failed check:
- the rule removed;
- the rule limited to the banner's id, or to `.alert-blocked`;
- `visibility: hidden` in place of `display: none`;
- the script never showing the banner, never hiding it, or not emptying it.

Nothing live was changed.
