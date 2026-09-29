#!/usr/bin/env python3
"""SYRD-509: the ticket-reference and linked-ticket script, where SCRIPT_CORE splices it, and what it does."""

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
from scripts.ticket_board import frontend_script_core as core  # noqa: E402
from scripts.ticket_board import frontend_script_ticket_links as owner  # noqa: E402

LINKS = (
    "ticketById", "buildTicketReference", "buildChildTicketList", "appendLinkedTicketText",
    "linkedTextBlock", "linkedPreview", "linkedTicketRow",
)
PATTERN = r"const TICKET_REF_PATTERN = /\b(?:[a-z0-9_]+:)?([A-Z][A-Z0-9]*-\d+)\b/ig;"


def test_script_core_splices_ticket_links_in_place() -> None:
    text = owner.SCRIPT_TICKET_LINKS
    assert core.SCRIPT_TICKET_LINKS is text
    assert re.findall(r"^    function (\w+)\(", text, re.M) == list(LINKS)
    assert text.startswith("    function ticketById(ticketId) {\n") and text.endswith("    }\n\n")
    assert core.SCRIPT_CORE.count(text) == 1 and frontend.render_html().count(text) == 1
    before, after = core.SCRIPT_CORE.split(text)
    assert before.endswith("      span.textContent = text;\n      return span;\n    }\n\n")
    assert after.startswith("    function ticketNumber(ticketId) {\n")
    for name in LINKS:
        assert f"function {name}(" not in before + after, name
    # The shared pattern, openDetail and stateLabel stay in SCRIPT_CORE; the owner only uses them.
    assert core.SCRIPT_CORE.startswith("    " + PATTERN + "\n") and "const TICKET_REF_PATTERN" not in text
    for name in ("openDetail", "stateLabel"):
        assert f"function {name}(" in before and f"function {name}(" not in text and f"{name}(" in text, name
    tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    value = next(n.value for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SCRIPT_CORE")
    parts: list[ast.expr] = []
    while isinstance(value, ast.BinOp):
        parts.insert(0, value.right)
        value = value.left
    parts.insert(0, value)
    names = [type(p).__name__ if isinstance(p, ast.Constant) else p.id for p in parts]
    assert all(isinstance(p, (ast.Constant, ast.Name)) for p in parts), names
    assert names.count("SCRIPT_TICKET_LINKS") == 1, names
    at = names.index("SCRIPT_TICKET_LINKS")
    assert names[at - 1] == names[at + 1] == "Constant", names
    assert names.index("SCRIPT_ATTACHMENTS") < at, names
    owner_tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    assert [type(n).__name__ for n in owner_tree.body] == ["Expr", "ImportFrom", "Assign"]


HARNESS = r'''const fs = require('fs');
const vm = require('vm');
const chunk = fs.readFileSync(process.argv[2], 'utf8');
const patternSource = fs.readFileSync(process.argv[3], 'utf8');
function el(tag) {
  const node = { tag, className: '', children: [], listeners: {}, textContent: '', classes: [],
    addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
  };
  node.classList = { add: (c) => node.classes.push(c) };
  return node;
}
const opened = [];
const state = { selectedId: 'PGU-2', tickets: [
  { id: 'PGU-1', title: 'One', state: 'backlog' }, { id: 'PGU-2', title: 'Two', state: 'in_progress' }, { id: 'MEFP-7', title: 'Seven', state: 'done' }] };
const ctx = { state, console,
  document: { createElement: el, createTextNode: (text) => ({ tag: '#text', text }) },
  openDetail: (id) => opened.push(id), stateLabel: (key) => `label:${key}` };
vm.createContext(ctx);
vm.runInContext(`${patternSource};\n${chunk};globalThis.f = { ticketById, buildTicketReference, buildChildTicketList, appendLinkedTicketText, linkedTextBlock, linkedPreview, linkedTicketRow };`, ctx);
const f = ctx.f;
const ser = (n) => n.tag === '#text' ? ['text', n.text] : { tag: n.tag, className: n.className, classes: n.classes, text: n.textContent,
  type: n.type, disabled: n.disabled, title: n.title, listeners: Object.keys(n.listeners), children: n.children.map(ser) };
const click = (node) => { const log = []; node.listeners.click[0]({ preventDefault: () => log.push('prevent'), stopPropagation: () => log.push('stop') }); return log; };
const out = {};
out.byId = ['pgu-1', 'PGU-1', 'PGU-9', '', null, undefined, 'mefp-7'].map((id) => (f.ticketById(id) || {}).id || null);
const ref = f.buildTicketReference('pgu-1');
out.ref = [ser(ref), click(ref), [...opened]];
out.labelled = ser(f.buildTicketReference('PGU-2', 'see PGU-2'));
out.missing = ser(f.buildTicketReference('PGU-404'));
out.blankLabel = ser(f.buildTicketReference('pgu-1', ''));
const texts = ['Fixes PGU-1 and pgu-2.', 'Blocks other:PGU-1\r\nthen MEFP-7,PGU-404', 'PGU-1PGU-2 x', 'no refs here', '', 'a\n\nb', 'PGU-1'];
out.linked = texts.map((text) => { const c = el('div'); f.appendLinkedTicketText(c, text); return c.children.map(ser); });
const again = el('div'); f.appendLinkedTicketText(again, 'PGU-1 PGU-2'); f.appendLinkedTicketText(again, 'PGU-1'); out.again = again.children.map(ser);
out.staleIndex = (() => { vm.runInContext('TICKET_REF_PATTERN.lastIndex = 5;', ctx); const c = el('div'); f.appendLinkedTicketText(c, 'PGU-1 x'); return c.children.map(ser); })();
out.nullText = (() => { const c = el('div'); f.appendLinkedTicketText(c, null); return c.children.length; })();
out.blocks = [ser(f.linkedTextBlock('see PGU-1')), ser(f.linkedTextBlock('')), ser(f.linkedTextBlock(null, '(no body)'))];
out.preview = ser(f.linkedPreview('Rendered Preview', '', '(no implementation yet)'));
out.rows = [ser(f.linkedTicketRow([])), ser(f.linkedTicketRow(['PGU-1', 'PGU-404']))];
const children = f.buildChildTicketList([{ id: 'PGU-1', title: 'One', state: 'backlog' }, { id: 'PGU-2', title: 'Two', state: 'in_progress' }]);
out.children = ser(children);
out.childClick = [click(children.children[1].children[1]), opened.slice(-1)];
out.childCompact = ser(f.buildChildTicketList([{ id: 'PGU-1', title: 'One', state: 'backlog' }], { compact: true }));
out.opened = opened;
console.log(JSON.stringify(out));
'''
EXPECTED = json.loads(r'''{"byId":["PGU-1","PGU-1",null,null,null,null,"MEFP-7"],"ref":[{"tag":"button","className":"ticket-ref","classes":[],"text":"pgu-1","type":"button","listeners":["click"],"children":[]},["prevent","stop"],["PGU-1"]],"labelled":{"tag":"button","className":"ticket-ref","classes":[],"text":"see PGU-2","type":"button","listeners":["click"],"children":[]},"missing":{"tag":"button","className":"ticket-ref","classes":["ticket-ref-missing"],"text":"PGU-404","type":"button","disabled":true,"title":"Ticket not found in the current board snapshot.","listeners":[],"children":[]},"blankLabel":{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]},"linked":[[["text","Fixes "],{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]},["text"," and "],{"tag":"button","className":"ticket-ref","classes":[],"text":"pgu-2","type":"button","listeners":["click"],"children":[]},["text","."]],[["text","Blocks "],{"tag":"button","className":"ticket-ref","classes":[],"text":"other:PGU-1","type":"button","listeners":["click"],"children":[]},{"tag":"br","className":"","classes":[],"text":"","listeners":[],"children":[]},["text","then "],{"tag":"button","className":"ticket-ref","classes":[],"text":"MEFP-7","type":"button","listeners":["click"],"children":[]},["text",","],{"tag":"button","className":"ticket-ref","classes":["ticket-ref-missing"],"text":"PGU-404","type":"button","disabled":true,"title":"Ticket not found in the current board snapshot.","listeners":[],"children":[]}],[["text","PGU-1PGU-2 x"]],[["text","no refs here"]],[],[["text","a"],{"tag":"br","className":"","classes":[],"text":"","listeners":[],"children":[]},{"tag":"br","className":"","classes":[],"text":"","listeners":[],"children":[]},["text","b"]],[{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]}]],"again":[{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]},["text"," "],{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-2","type":"button","listeners":["click"],"children":[]},{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]}],"staleIndex":[{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]},["text"," x"]],"nullText":0,"blocks":[{"tag":"div","className":"body-text linked-text","classes":[],"text":"","listeners":[],"children":[["text","see "],{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]}]},{"tag":"div","className":"body-text linked-text","classes":[],"text":"","listeners":[],"children":[["text","(none)"]]},{"tag":"div","className":"body-text linked-text","classes":[],"text":"","listeners":[],"children":[["text","(no body)"]]}],"preview":{"tag":"div","className":"field-preview","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"field-preview-label","classes":[],"text":"Rendered Preview","listeners":[],"children":[]},{"tag":"div","className":"body-text linked-text","classes":[],"text":"","listeners":[],"children":[["text","(no implementation yet)"]]}]},"rows":[{"tag":"div","className":"ticket-ref-row","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"soft-note","classes":[],"text":"No linked tickets.","listeners":[],"children":[]}]},{"tag":"div","className":"ticket-ref-row","classes":[],"text":"","listeners":[],"children":[{"tag":"button","className":"ticket-ref","classes":[],"text":"PGU-1","type":"button","listeners":["click"],"children":[]},{"tag":"button","className":"ticket-ref","classes":["ticket-ref-missing"],"text":"PGU-404","type":"button","disabled":true,"title":"Ticket not found in the current board snapshot.","listeners":[],"children":[]}]}],"children":{"tag":"div","className":"child-ticket-list","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"child-ticket-head","classes":[],"text":"2 linked children","listeners":[],"children":[]},{"tag":"div","className":"child-ticket-items","classes":[],"text":"","listeners":[],"children":[{"tag":"button","className":"child-ticket-item","classes":[],"text":"","type":"button","listeners":["click"],"children":[{"tag":"div","className":"child-ticket-text","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"child-ticket-id","classes":[],"text":"PGU-1","listeners":[],"children":[]},{"tag":"div","className":"child-ticket-title","classes":[],"text":"One","listeners":[],"children":[]}]},{"tag":"span","className":"tag child-ticket-state","classes":[],"text":"label:backlog","listeners":[],"children":[]}]},{"tag":"button","className":"child-ticket-item","classes":["selected"],"text":"","type":"button","listeners":["click"],"children":[{"tag":"div","className":"child-ticket-text","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"child-ticket-id","classes":[],"text":"PGU-2","listeners":[],"children":[]},{"tag":"div","className":"child-ticket-title","classes":[],"text":"Two","listeners":[],"children":[]}]},{"tag":"span","className":"tag child-ticket-state","classes":[],"text":"label:in_progress","listeners":[],"children":[]}]}]}]},"childClick":[["prevent","stop"],["PGU-2"]],"childCompact":{"tag":"div","className":"child-ticket-list child-ticket-list-compact","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"child-ticket-head","classes":[],"text":"1 linked child","listeners":[],"children":[]},{"tag":"div","className":"child-ticket-items","classes":[],"text":"","listeners":[],"children":[{"tag":"button","className":"child-ticket-item","classes":[],"text":"","type":"button","listeners":["click"],"children":[{"tag":"div","className":"child-ticket-text","classes":[],"text":"","listeners":[],"children":[{"tag":"div","className":"child-ticket-id","classes":[],"text":"PGU-1","listeners":[],"children":[]},{"tag":"div","className":"child-ticket-title","classes":[],"text":"One","listeners":[],"children":[]}]},{"tag":"span","className":"tag child-ticket-state","classes":[],"text":"label:backlog","listeners":[],"children":[]}]}]}]},"opened":["PGU-1","PGU-2"]}''')


def run_links() -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd509-links.") as tmp:
        chunk, pattern, harness = Path(tmp) / "links.js", Path(tmp) / "pattern.js", Path(tmp) / "harness.js"
        chunk.write_text(owner.SCRIPT_TICKET_LINKS, encoding="utf-8")
        pattern.write_text(core.SCRIPT_CORE.splitlines()[0].strip(), encoding="utf-8")
        harness.write_text(HARNESS, encoding="utf-8")
        done = subprocess.run(["node", str(harness), str(chunk), str(pattern)], capture_output=True, text=True,
                              check=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    return json.loads(done.stdout)


def texts(nodes: list) -> list:
    return [n if isinstance(n, list) else [n["tag"], n["text"], n.get("disabled", False)] for n in nodes]


def test_linked_ticket_behaviour() -> None:
    if shutil.which("node") is None:
        print("ticket_board_frontend_ticket_links_test: node unavailable; link behaviour NOT checked")
        return
    out = run_links()
    assert out["byId"] == ["PGU-1", "PGU-1", None, None, None, None, "MEFP-7"]
    ref, clicks, opened = out["ref"]
    # The label keeps the ID as written; the lookup and openDetail use it upper-cased.
    assert (ref["className"], ref["type"], ref["text"], ref["listeners"], ref["classes"]) == ("ticket-ref", "button", "pgu-1", ["click"], [])
    assert "disabled" not in ref and clicks == ["prevent", "stop"] and opened == ["PGU-1"]
    assert out["labelled"]["text"] == "see PGU-2" and out["blankLabel"]["text"] == "PGU-1"
    missing = out["missing"]
    assert (missing["text"], missing["disabled"], missing["classes"], missing["listeners"]) == ("PGU-404", True, ["ticket-ref-missing"], [])
    assert missing["title"] == "Ticket not found in the current board snapshot."
    fixes, blocks, adjacent, plain, empty, blank_line, only = out["linked"]
    assert texts(fixes) == [["text", "Fixes "], ["button", "PGU-1", False], ["text", " and "], ["button", "pgu-2", False], ["text", "."]]
    # Recorded, not endorsed: a qualified foreign reference resolves to a local ticket with the same ID (SYRD-509).
    assert texts(blocks) == [["text", "Blocks "], ["button", "other:PGU-1", False], ["br", "", False], ["text", "then "],
                             ["button", "MEFP-7", False], ["text", ","], ["button", "PGU-404", True]]
    assert texts(adjacent) == [["text", "PGU-1PGU-2 x"]] and texts(plain) == [["text", "no refs here"]] and empty == []
    assert texts(blank_line) == [["text", "a"], ["br", "", False], ["br", "", False], ["text", "b"]]
    assert texts(only) == [["button", "PGU-1", False]]
    assert texts(out["again"]) == [["button", "PGU-1", False], ["text", " "], ["button", "PGU-2", False], ["button", "PGU-1", False]]
    # Another user of the shared /g pattern can leave lastIndex mid-string; each line starts from 0.
    assert texts(out["staleIndex"]) == [["button", "PGU-1", False], ["text", " x"]]
    assert out["nullText"] == 0
    assert [(b["className"], texts(b["children"])) for b in out["blocks"]] == [
        ("body-text linked-text", [["text", "see "], ["button", "PGU-1", False]]),
        ("body-text linked-text", [["text", "(none)"]]),
        ("body-text linked-text", [["text", "(no body)"]])]
    preview = out["preview"]
    assert preview["className"] == "field-preview" and [c["className"] for c in preview["children"]] == ["field-preview-label", "body-text linked-text"]
    assert preview["children"][0]["text"] == "Rendered Preview" and texts(preview["children"][1]["children"]) == [["text", "(no implementation yet)"]]
    empty_row, row = out["rows"]
    assert (empty_row["className"], [(c["className"], c["text"]) for c in empty_row["children"]]) == ("ticket-ref-row", [("soft-note", "No linked tickets.")])
    assert [(c["text"], c.get("disabled", False)) for c in row["children"]] == [("PGU-1", False), ("PGU-404", True)]
    children = out["children"]
    assert children["className"] == "child-ticket-list" and children["children"][0]["text"] == "2 linked children"
    items = children["children"][1]["children"]
    assert [(i["classes"], i["type"], i["children"][1]["text"]) for i in items] == [
        ([], "button", "label:backlog"), (["selected"], "button", "label:in_progress")]
    assert [c["text"] for c in items[0]["children"][0]["children"]] == ["PGU-1", "One"]
    assert out["childClick"] == [["prevent", "stop"], ["PGU-2"]]
    compact = out["childCompact"]
    assert compact["className"] == "child-ticket-list child-ticket-list-compact" and compact["children"][0]["text"] == "1 linked child"
    assert out["opened"] == ["PGU-1", "PGU-2"]
    assert out == EXPECTED


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_frontend_ticket_links_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
