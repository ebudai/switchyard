#!/usr/bin/env python3
"""SYRD-507: the attachment-gallery script, where SCRIPT_CORE splices it, and what it does."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import frontend  # noqa: E402
from scripts.ticket_board import frontend_script_attachments as owner  # noqa: E402
from scripts.ticket_board import frontend_script_core as core  # noqa: E402

GALLERY = (
    "previewUrlFor", "thumbnailUrlFor", "ticketScreenshotEntries", "ticketScreenshotPaths", "uniquePaths",
    "screenshotLabelFor", "screenshotEntriesForPaths", "attachmentSetLabelSlug", "parseAttachmentSet",
    "groupAttachmentEntries", "renderAttachmentGallery", "renderAttachmentSetGroups",
)


def test_script_core_splices_the_gallery_in_place() -> None:
    text = owner.SCRIPT_ATTACHMENTS
    assert core.SCRIPT_ATTACHMENTS is text
    assert re.findall(r"^    function (\w+)\(", text, re.M) == list(GALLERY)
    assert text.startswith("    function previewUrlFor(path) {\n") and text.endswith("    }\n\n")
    assert core.SCRIPT_CORE.count(text) == 1
    before, after = core.SCRIPT_CORE.split(text)
    assert before.endswith("      select.appendChild(option);\n    }\n\n")
    assert after.startswith("    function renderCreatePreview() {\n")
    for name in GALLERY:
        assert f"function {name}(" not in before + after, name
    assert "function cropMetadataCaption(entry)" in before and "function cropMetadataCaption" not in text
    assert frontend.HTML.count(core.SCRIPT_CORE) == 1 and frontend.render_html().count(text) == 1
    # The source keeps one SCRIPT_CORE expression: literal, labels, literal, gallery, literal.
    tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    value = next(n.value for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SCRIPT_CORE")
    parts: list[ast.expr] = []
    while isinstance(value, ast.BinOp):
        parts.insert(0, value.right)
        value = value.left
    parts.insert(0, value)
    assert [type(p).__name__ if isinstance(p, ast.Constant) else p.id for p in parts] == [
        "Constant", "DEFAULT_STATE_LABELS_JSON", "Constant", "SCRIPT_ATTACHMENTS", "Constant",
    ]
    owner_tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    assert [type(n).__name__ for n in owner_tree.body] == ["Expr", "ImportFrom", "Assign"]


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
function el(tag) {
  const node = { tag, className: '', children: [], attrs: {}, listeners: {}, textContent: '', classes: [],
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    set innerHTML(v) { this.children = []; },
  };
  node.classList = { add: (c) => node.classes.push(c) };
  return node;
}
const state = { screenshots: [{ path: '/a/known.png', name: 'known.png', modified: '2026-01-01' }],
  tickets: [{ screenshots_info: [{ path: '/a/known.png', available: false }] }, { screenshots: ['/a/other.png'] }] };
const ctx = { document: { createElement: el }, state, cropMetadataCaption: (e) => (e.metadata || {}).kind === 'crop' ? 'crop of s.png' : '' };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + ';globalThis.g = {' + process.argv[3] + '};', ctx);
const g = ctx.g;
const ser = (n) => ({ tag: n.tag, className: n.className, classes: n.classes, attrs: n.attrs, text: n.textContent, title: n.title,
  src: n.src, loading: n.loading, decoding: n.decoding, tabIndex: n.tabIndex, open: n.open, children: n.children.map(ser) });
const out = {};
out.urls = [g.previewUrlFor('dir a/b#c?.png'), g.thumbnailUrlFor('dir a/b#c?.png')];
out.entries = [
  g.ticketScreenshotEntries({ screenshots_info: [{ path: 'i', available: false }], screenshots: ['x'], screenshot: 'y' }),
  g.ticketScreenshotEntries({ screenshots_info: [], screenshots: ['x'] }),
  g.ticketScreenshotEntries({ screenshot: 'y', screenshot_available: 0 }),
  g.ticketScreenshotEntries({}), g.ticketScreenshotPaths({ screenshots: ['p', 'q'] })];
out.unique = g.uniquePaths(['a', 'b', 'a', '', null, 'c']);
out.labels = [g.screenshotLabelFor('/a/known.png'), g.screenshotLabelFor('/z/dir/file.png')];
out.forPaths = g.screenshotEntriesForPaths(['/a/known.png', '/a/other.png', '/n/new.png', '/a/known.png', '']);
out.slugs = ['first-pass2x_ab', '', 'a--b__c', 'v2', 'long word here'].map(g.attachmentSetLabelSlug);
const paths = ['/x/target__hero.png', '/x/attempt-001-first-pass__a.png', '/x/attempt-12__b.png', '/x/ATTEMPT-3-final__c.png',
  '/x/feedback-3-check__d.png', '/x/feedback-10__e.png', '/x/reference-set__f.png', '/x/plain.png', '/x/-bad__i.png'];
out.parsed = paths.map(g.parseAttachmentSet).map((p) => [p.key, p.label, p.order, p.itemLabel]);
const mk = (list) => list.map((path, i) => ({ path, available: i % 4 !== 3, label: `L${i}`, metadata: i === 1 ? { kind: 'crop' } : {} }));
out.groups = [paths, ['/x/feedback-2__a.png', '/x/feedback-9-late__b.png', '/x/note__c.png'], ['/x/only.png'], ['/x/a.png', '/x/b__c.png']]
  .map((list) => g.groupAttachmentEntries(mk(list)).map((grp) => [grp.label, grp.open, grp.entries.map((e) => e.label)]));
out.caseFold = g.groupAttachmentEntries(mk(['/x/Ref__a.png', '/x/ref__b.png'])).map((grp) => [grp.key, grp.entries.length]);
const pairs = [['/x/target__a.png', '/x/attempt-1__b.png'], ['/x/attempt-1__a.png', '/x/feedback-1__b.png'],
  ['/x/feedback-1__a.png', '/x/named__b.png'], ['/x/named__a.png', '/x/plain.png']];
out.pairOrder = pairs.flatMap((pair) => [pair, [pair[1], pair[0]]])
  .map((list) => g.groupAttachmentEntries(mk(list)).map((grp) => grp.type));
const calls = [];
const gallery = el('div');
g.renderAttachmentGallery(gallery, mk(['/x/attempt-1__a.png', '/x/b.png', '/x/c.png', '/x/d.png']), 'Remove it',
  async (p) => calls.push(['remove', p]), (e) => calls.push(['open', e.path]));
const card = gallery.children[0];
card.listeners.click[0]();
for (const [target, key] of [[card, 'Enter'], [card, ' '], [card, 'x'], [{}, 'Enter']]) {
  card.listeners.keydown[0]({ target, key, preventDefault: () => calls.push(['prevent', key]) });
}
card.children[0].listeners.click[0]({ preventDefault: () => calls.push(['rm-prevent']), stopPropagation: () => calls.push(['rm-stop']) });
out.gallery = ser(gallery);
const bare = el('div');
g.renderAttachmentGallery(bare, mk(['/x/q.png']), 'x', null, null);
out.bare = ser(bare);
const sets = el('div');
g.renderAttachmentSetGroups(sets, mk(paths.slice(0, 7)), 'Remove attachment', null, null);
out.sets = sets.children.map((d) => [d.className, d.open, d.children[0].textContent, d.children[1].className, d.children[1].children.length]);
setTimeout(() => { out.calls = calls; console.log(JSON.stringify(out)); }, 10);
"""


def run_gallery() -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd507-gallery.") as tmp:
        chunk = Path(tmp) / "gallery.js"
        chunk.write_text(owner.SCRIPT_ATTACHMENTS, encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        done = subprocess.run(["node", str(harness), str(chunk), ", ".join(GALLERY)], capture_output=True, text=True,
                              check=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    return json.loads(done.stdout)


def test_gallery_behaviour() -> None:
    if shutil.which("node") is None:
        print("ticket_board_frontend_attachments_test: node unavailable; gallery behaviour NOT checked")
        return
    out = run_gallery()
    assert out["urls"] == ["/api/image/dir%20a%2Fb%23c%3F.png", "/api/thumb/dir%20a%2Fb%23c%3F.png?w=512"]
    assert out["entries"] == [[{"path": "i", "available": False}], [{"path": "x", "available": True}],
                              [{"path": "y", "available": False}], [], ["p", "q"]]
    assert out["unique"] == ["a", "b", "c"] and out["labels"] == ["known.png - 2026-01-01", "file.png"]
    assert [(e["path"], e["available"], e["label"]) for e in out["forPaths"]] == [
        ("/a/known.png", False, "known.png - 2026-01-01"), ("/a/other.png", True, "other.png"), ("/n/new.png", True, "new.png")]
    assert out["slugs"] == ["First Pass 2X AB", "", "A B C", "V 2", "Long Word Here"]
    assert out["parsed"] == [
        ["target", "Target", -1000000, "hero.png"],
        ["attempt-001-first-pass", "Render Attempt 001 - First Pass", 1, "a.png"],
        ["attempt-12-", "Render Attempt 12", 12, "b.png"],
        ["attempt-3-final", "Render Attempt 3 - Final", 3, "c.png"],
        ["feedback-3-check", "Feedback #3 - Check", 3, "d.png"],
        ["feedback-10-", "Feedback #10", 10, "e.png"],
        ["named-reference-set", "Reference SET", 500000, "f.png"],
        ["ungrouped", "Ungrouped", 1000000, "plain.png"],
        ["ungrouped", "Ungrouped", 1000000, "-bad__i.png"],
    ]
    mixed, feedback, single, named = out["groups"]
    assert [(label, is_open) for label, is_open, _ in mixed] == [
        ("Target", False), ("Render Attempt 12", True), ("Render Attempt 3 - Final", False),
        ("Render Attempt 001 - First Pass", False), ("Feedback #10", False), ("Feedback #3 - Check", False),
        ("Reference SET", False), ("Ungrouped", False)]
    assert mixed[-1][2] == ["plain.png", "-bad__i.png"]
    assert [(label, is_open) for label, is_open, _ in feedback] == [("Feedback #9 - Late", True), ("Feedback #2", False), ("Note", False)]
    assert single == [["Ungrouped", True, ["only.png"]]]
    assert [(label, is_open) for label, is_open, _ in named] == [("B", False), ("Ungrouped", False)]
    assert out["caseFold"] == [["named-ref", 2]]
    assert out["pairOrder"] == [
        ["target", "attempt"], ["target", "attempt"], ["attempt", "feedback"], ["attempt", "feedback"],
        ["feedback", "named"], ["feedback", "named"], ["named", "ungrouped"], ["named", "ungrouped"]]
    cards = out["gallery"]["children"]
    first = cards[0]
    assert first["classes"] == ["attachment-card-clickable"] and first["tabIndex"] == 0
    assert first["attrs"] == {"role": "button", "aria-haspopup": "dialog", "aria-label": "Open attachment full size: L0"}
    remove, thumb, meta = first["children"]
    assert (remove["className"], remove["text"], remove["title"]) == ("attachment-remove", "×", "Remove it")
    assert (thumb["className"], thumb["src"], thumb["loading"], thumb["decoding"]) == (
        "attachment-thumb", "/api/thumb/%2Fx%2Fattempt-1__a.png?w=512", "lazy", "async")
    assert (meta["className"], meta["text"]) == ("attachment-meta", "L0")
    assert [c["className"] for c in cards[1]["children"]] == ["attachment-remove", "attachment-thumb", "attachment-meta", "attachment-provenance"]
    assert cards[1]["children"][3]["text"] == "crop of s.png"
    assert cards[3]["classes"] == [] and [c["className"] for c in cards[3]["children"]] == ["attachment-remove", "attachment-missing", "attachment-meta"]
    assert cards[3]["children"][1]["text"] == "image unavailable"
    assert [c["className"] for c in out["bare"]["children"][0]["children"]] == ["attachment-thumb", "attachment-meta"]
    assert out["bare"]["children"][0]["classes"] == []
    assert out["calls"] == [
        ["open", "/x/attempt-1__a.png"], ["prevent", "Enter"], ["open", "/x/attempt-1__a.png"],
        ["prevent", " "], ["open", "/x/attempt-1__a.png"], ["rm-prevent"], ["rm-stop"], ["remove", "/x/attempt-1__a.png"]]
    assert out["sets"] == [
        ["attachment-set attachment-set-target", False, "Target (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-attempt", True, "Render Attempt 12 (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-attempt", False, "Render Attempt 3 - Final (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-attempt", False, "Render Attempt 001 - First Pass (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-feedback", False, "Feedback #10 (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-feedback", False, "Feedback #3 - Check (1 image)", "attachment-gallery", 1],
        ["attachment-set attachment-set-named", False, "Reference SET (1 image)", "attachment-gallery", 1],
    ]


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_frontend_attachments_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
