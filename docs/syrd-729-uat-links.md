# SYRD-729: a Markdown link in the UAT panel is a link

SYRD-690 reached User Review on board release `25a99757`. Its **Do this** said:

```
[canary report](https://github.com/ebudai/switchyard/blob/25a99757a21bcd1804816bf524469ffebfcd5b4e/docs/syrd-693-local-qwen-canary.md)
```

The panel showed exactly that, as text. `uatRichText` (`scripts/ticket_board/frontend_script_uat.py`) rendered inline
and fenced code, and appended everything else as a text node.

## The change

Text outside code now goes through `uatLinkedText`, which makes a Markdown link `[label](target)` an anchor when its
target is safe:
- **Safe:** an absolute URL whose scheme is `http:` or `https:`, as the browser's own URL parser reads it. The target
  may hold balanced parentheses (`.../Otto_(tenant)`), but no whitespace.
- **The anchor:**
  - its `href` is the target exactly as written, set as an attribute;
  - its text is the label, set as `textContent`, so a label written as markup shows as that markup's text;
  - it opens in a new tab, with `rel="noopener noreferrer"`.
- **Left as the text written:**
  - any other link: `javascript:`, `data:`, a relative or scheme-relative target, a target with a space;
  - a malformed one: no label, or not closed;
  - a link inside inline or fenced code, since code is split out first;
  - a Markdown title (`[label](url "title")`), which is not supported.

No `innerHTML` is involved. The stored steps are unchanged, as is the three-part layout. Links are drawn in the
panel's accent colour. The same renderer serves the panel's other text: the User's question, the Director's answers
under it, a legacy UAT request and the User's message.

## Evidence

`tests/uat_link_browser_test.py`, 31 checks:
- **Setup:** a production-built board with the shipped workflow and the real server, served as production serves it,
  with Chromium driving the page.
- **Order:** main runs first, from a git archive of `25a99757`.
- **Sizes:** each case runs at desktop size (1280x800) and at a phone's (390x844, touch).
- **The steps:** SYRD-690's own Do this text, plus links that must stay text.
- **Nothing leaves this host:** a stand-in answers for github.com and example.com.

**Main:** the canary link is literal text on both sizes, and there is nothing to open.

**Here, on both sizes:**
- **Exactly three anchors:** the canary link, a markup label with a parenthesised target, and a link in **You should
  see**. Each has its exact target, opens in a new tab and has `noopener noreferrer`.
- **No markup is rendered.**
- **Text stays text:**
  - the `javascript:`, `data:`, relative, spaced and unclosed links read as written;
  - the text around each link is kept;
  - the links in inline and fenced code stay literal.
- **Layout:** the three parts are in order, nothing is wider than the panel, and the links are in the accent colour.

**Clicking:**
- On the desktop, clicking the link opens its exact target. On the phone, tapping it does too.
- Clicking where each inert link is written opens nothing, and no dialog appears.

**The rest of the panel:**
- The steps are stored byte for byte as the Director wrote them.
- The User's question with a link, and the Director's answer with one, show both as anchors.
- Send to Director still moves the ticket to User Feedback.
- On the phone, Accept still records the sign-off.
- There are no page errors.

**Mutants:** 18, each killed by a failed check:
- **The scheme check:** none, `http:` only, or a base URL that admits relative targets.
- **The label:** set as `innerHTML`, or as the whole Markdown.
- **The anchor:** no new tab, no `rel`, or the href from the label.
- **Where links are made:** not made at all, or made inside code.
- **The pattern:** no balanced parentheses, whitespace allowed, or brackets allowed in the label.
- **The text:** the text before or after a link dropped, or the position not advanced.
- **An unsafe link stopping the rest:** `break` in place of `continue`.
- **The accent rule** removed.

Nothing live was changed.
