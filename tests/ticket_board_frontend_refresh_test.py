#!/usr/bin/env python3
"""SYRD-508: the refresh and scroll-restoration script, where SCRIPT_CORE splices it, and what it does."""

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
from scripts.ticket_board import frontend_script_refresh as owner  # noqa: E402

REFRESH = (
    "normalizeBuildId", "refreshIdleMs", "markUserActivity", "boardScrollElement", "scrollPositionStorageKey",
    "legacyScrollPositionStorageKey", "rememberScrollPositionForRefresh", "restoreScrollPositionAfterRefresh",
    "detailHasDirtyDraft", "createFormHasDraft", "refreshUnsafeReason", "syncRefreshUpdateBanner",
    "scheduleAutoRefreshCheck", "performSmartRefresh", "maybeAutoRefresh", "updateRefreshRequired",
)


def test_script_core_splices_refresh_in_place() -> None:
    text = owner.SCRIPT_REFRESH
    assert core.SCRIPT_REFRESH is text
    assert re.findall(r"^    function (\w+)\(", text, re.M) == list(REFRESH)
    assert text.startswith("    function normalizeBuildId(value) {\n") and text.endswith("    }\n\n")
    assert core.SCRIPT_CORE.count(text) == 1 and frontend.render_html().count(text) == 1
    before, after = core.SCRIPT_CORE.split(text)
    assert before.endswith("      createStatus.style.color = isError ? '#fecdd3' : 'var(--text)';\n    }\n\n")
    assert after.startswith("    function stateLabel(key) {\n")
    for name in REFRESH:
        assert f"function {name}(" not in before + after, name
    # Shared state, DOM lookups and event wiring stay where they were.
    for shared in ("loadedBuildId: String(window.TICKET_BOARD_BUILD_ID", "autoRefreshHandledBuildIds: new Set(),",
                   "const refreshUpdateBannerEl = document.getElementById('refreshUpdateBanner');",
                   "const LEGACY_PGU_STORAGE_NAMESPACE = 'pgu-ticket-board';"):
        assert shared in before and shared not in text, shared
    tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    value = next(n.value for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SCRIPT_CORE")
    parts: list[ast.expr] = []
    while isinstance(value, ast.BinOp):
        parts.insert(0, value.right)
        value = value.left
    parts.insert(0, value)
    names = [type(p).__name__ if isinstance(p, ast.Constant) else p.id for p in parts]
    assert all(isinstance(p, (ast.Constant, ast.Name)) for p in parts), names
    assert names.count("SCRIPT_REFRESH") == 1, names
    at = names.index("SCRIPT_REFRESH")
    assert names[at - 1] == names[at + 1] == "Constant", names
    assert at < names.index("DEFAULT_STATE_LABELS_JSON"), names
    owner_tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    assert [type(n).__name__ for n in owner_tree.body] == ["Expr", "ImportFrom", "Assign"]


HARNESS = r'''const fs = require('fs');
const vm = require('vm');
const chunk = fs.readFileSync(process.argv[2], 'utf8');
const NAMES = ['normalizeBuildId','refreshIdleMs','markUserActivity','boardScrollElement','scrollPositionStorageKey','legacyScrollPositionStorageKey',
  'rememberScrollPositionForRefresh','restoreScrollPositionAfterRefresh','detailHasDirtyDraft','createFormHasDraft','refreshUnsafeReason',
  'syncRefreshUpdateBanner','scheduleAutoRefreshCheck','performSmartRefresh','maybeAutoRefresh','updateRefreshRequired'];
function world(legacy, storageMode = 'ok') {
  const log = [];
  const clock = { now: 1000000 };
  const timers = new Map(); let nextTimer = 1;
  const store = new Map();
  const storage = {
    getItem(k) { if (storageMode === 'throw') throw new Error('denied'); log.push(['get', k]); return store.has(k) ? store.get(k) : null; },
    setItem(k, v) { if (storageMode !== 'ok') throw new Error('denied'); log.push(['set', k, v]); store.set(k, v); },
    removeItem(k) { log.push(['remove', k]); store.delete(k); },
  };
  const board = { scrollLeft: 7, scrollTop: 9 };
  const flags = { detail: false, lightbox: false, board: true, syncFn: true };
  const window = {
    scrollX: 11, scrollY: 22, sessionStorage: storage,
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, { fn, delay }); log.push(['setTimeout', id, delay]); return id; },
    clearTimeout(id) { log.push(['clearTimeout', id]); timers.delete(id); },
    scrollTo(x, y) { log.push(['scrollTo', x, y]); },
    location: { reload() { log.push(['reload']); } },
  };
  const state = { lastActivityAt: clock.now, serverBuildId: 'b1', loadedBuildId: 'b1', refreshRequired: false, pendingRefreshBuildId: '',
    autoRefreshTimer: null, autoRefreshHandledBuildIds: new Set(), detailDraft: null, pendingCreateScreenshots: [] };
  const ctx = {
    window, state, console,
    Date: { now: () => clock.now },
    document: { querySelector: (sel) => (sel === '.board-scroll' && flags.board ? board : null) },
    BOARD_STORAGE_NAMESPACE: 'ticket-board:demo:DEMO', LEGACY_PGU_STORAGE_NAMESPACE: 'pgu-ticket-board', IS_LEGACY_PGU_BOARD: legacy,
    titleInput: { value: '' }, bodyInput: { value: '' },
    refreshUpdateBannerEl: { hidden: true },
    rememberDetailDraft: () => log.push(['rememberDetailDraft']),
    detailModalIsOpen: () => flags.detail, imageLightboxIsOpen: () => flags.lightbox,
  };
  vm.createContext(ctx);
  vm.runInContext(`${chunk};globalThis.f = {${NAMES.join(',')}};`, ctx);
  Object.defineProperty(ctx, 'syncBodyModalState', { configurable: true, get: () => (flags.syncFn ? () => log.push(['syncBodyModalState']) : undefined) });
  const fire = () => { const [[id, t]] = timers; timers.delete(id); log.push(['fire', id]); t.fn(); };
  return { ctx, f: ctx.f, log, clock, timers, store, board, flags, window, state, fire };
}
const out = {};
{ const w = world(false);
  out.normalize = [undefined, null, '  abc  ', 0, 42].map(w.f.normalizeBuildId);
  const idle = [];
  for (const [t, p] of [[undefined, undefined], [25, 60000], [undefined, 60000], [null, 70], [0, 60000], [-1, 5], ['abc', 5], [Infinity, 5], ['12', 5]]) {
    if (t === undefined) delete w.window.TICKET_BOARD_REFRESH_IDLE_MS; else w.window.TICKET_BOARD_REFRESH_IDLE_MS = t;
    if (p === undefined) delete w.window.PGU_TICKET_BOARD_REFRESH_IDLE_MS; else w.window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = p;
    idle.push(w.f.refreshIdleMs());
  }
  out.idle = idle;
  out.keys = [w.f.scrollPositionStorageKey('b9'), w.f.scrollPositionStorageKey(''), w.f.legacyScrollPositionStorageKey('b9'), w.f.legacyScrollPositionStorageKey(null)];
}
{ const w = world(false);
  delete w.window.TICKET_BOARD_REFRESH_IDLE_MS;
  w.window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 300;
  w.f.updateRefreshRequired('b1');
  w.f.updateRefreshRequired('  ');
  const s1 = [w.state.serverBuildId, w.state.refreshRequired, w.ctx.refreshUpdateBannerEl.hidden];
  w.f.updateRefreshRequired(' b2 ');
  const s2 = [w.state.serverBuildId, w.state.refreshRequired, w.state.pendingRefreshBuildId, w.ctx.refreshUpdateBannerEl.hidden, w.state.autoRefreshTimer];
  w.titleInput = null;
  w.clock.now += 1000;
  w.ctx.titleInput.value = ' draft ';
  w.fire(); const r1 = [w.f.refreshUnsafeReason(), w.ctx.refreshUpdateBannerEl.hidden];
  w.ctx.titleInput.value = ''; w.state.pendingCreateScreenshots = ['/x.png']; const r2 = w.f.refreshUnsafeReason();
  w.state.pendingCreateScreenshots = []; w.ctx.bodyInput.value = 'b'; const r3 = w.f.refreshUnsafeReason();
  w.ctx.bodyInput.value = ''; w.flags.detail = true; const r4 = w.f.refreshUnsafeReason();
  w.flags.detail = false; w.flags.lightbox = true; const r5 = w.f.refreshUnsafeReason();
  w.flags.lightbox = false; w.state.detailDraft = { fields: { body: 'x' } }; const r6 = w.f.refreshUnsafeReason();
  w.state.detailDraft = { fields: {} }; const r7 = w.f.refreshUnsafeReason();
  w.state.detailDraft = { fields: { body: 'x' } };
  w.fire();
  w.state.detailDraft = null;
  w.f.markUserActivity();
  const activity = [w.state.lastActivityAt === w.clock.now, [...w.timers.values()].map((t) => t.delay)];
  w.clock.now += 5000;
  w.fire();
  out.flow = { s1, s2, r1, reasons: [r2, r3, r4, r5, r6, r7], activity, handled: [...w.state.autoRefreshHandledBuildIds], store: [...w.store], log: w.log };
  w.f.updateRefreshRequired('b2');
  out.afterHandled = [w.state.refreshRequired, w.state.pendingRefreshBuildId, w.ctx.refreshUpdateBannerEl.hidden];
  const quiet = world(false); quiet.f.scheduleAutoRefreshCheck(); quiet.f.maybeAutoRefresh(); out.quiet = quiet.log;
  const nosync = world(false); nosync.flags.syncFn = false; nosync.state.refreshRequired = true; nosync.f.syncRefreshUpdateBanner('x');
  out.nosync = [nosync.ctx.refreshUpdateBannerEl.hidden, nosync.log];
  const zero = world(false); zero.window.TICKET_BOARD_REFRESH_IDLE_MS = 0; zero.state.lastActivityAt -= 10; zero.f.updateRefreshRequired('b3'); out.zeroIdle = zero.log;
}
for (const legacy of [false, true]) {
  const w = world(legacy);
  w.store.set('ticket-board:demo:DEMO:scroll:b1', JSON.stringify({ windowX: 5, windowY: '6', boardLeft: 'x', boardTop: 8 }));
  w.store.set('pgu-ticket-board:scroll:b1', JSON.stringify({ windowX: 1, windowY: 2, boardLeft: 3, boardTop: 4 }));
  w.f.restoreScrollPositionAfterRefresh();
  const first = [[...w.store.keys()], w.board.scrollLeft, w.board.scrollTop];
  w.f.restoreScrollPositionAfterRefresh();
  const second = [[...w.store.keys()], w.board.scrollLeft, w.board.scrollTop];
  w.store.set('ticket-board:demo:DEMO:scroll:b1', '{broken');
  w.f.restoreScrollPositionAfterRefresh();
  w.flags.board = false;
  w.store.set('ticket-board:demo:DEMO:scroll:b1', JSON.stringify({ windowX: 9 }));
  w.f.restoreScrollPositionAfterRefresh();
  w.state.serverBuildId = 'b7'; w.f.rememberScrollPositionForRefresh();
  out[`restore legacy=${legacy}`] = { first, second, store: [...w.store], log: w.log };
}
for (const mode of ['throw', 'readonly']) {
  const w = world(true, mode);
  let error = null;
  try { w.f.rememberScrollPositionForRefresh(); w.f.restoreScrollPositionAfterRefresh(); } catch (e) { error = String(e); }
  out[`storage ${mode}`] = { error, log: w.log };
}
{
  const both = world(false);
  both.state.refreshRequired = true; both.state.lastActivityAt -= 100000;
  const pri = [];
  both.ctx.titleInput.value = 'x'; both.flags.detail = true; pri.push(both.f.refreshUnsafeReason());
  both.ctx.titleInput.value = ''; both.flags.lightbox = true; pri.push(both.f.refreshUnsafeReason());
  both.flags.detail = false; both.state.detailDraft = { fields: { a: 1 } }; pri.push(both.f.refreshUnsafeReason());
  out.priority = pri;
  const exact = world(false); exact.window.TICKET_BOARD_REFRESH_IDLE_MS = 0; exact.state.refreshRequired = true;
  out.exactIdle = exact.f.refreshUnsafeReason();
  const banner = world(false); banner.ctx.refreshUpdateBannerEl.hidden = false; banner.f.syncRefreshUpdateBanner('reason');
  out.bannerNotRequired = banner.ctx.refreshUpdateBannerEl.hidden;
  const small = world(false); small.window.TICKET_BOARD_REFRESH_IDLE_MS = 25; small.f.updateRefreshRequired('b5');
  out.smallIdle = small.log.filter((e) => e[0] === 'setTimeout');
  const unloaded = world(false); unloaded.state.loadedBuildId = ''; unloaded.f.updateRefreshRequired('b6');
  out.unloaded = [unloaded.state.refreshRequired, unloaded.state.serverBuildId, unloaded.log];
  const pending = world(false); pending.state.refreshRequired = true; pending.state.pendingRefreshBuildId = '';
  pending.state.lastActivityAt -= 100000; pending.f.maybeAutoRefresh();
  out.noPending = [pending.log, pending.ctx.refreshUpdateBannerEl.hidden];
}
console.log(JSON.stringify(out));
'''


def run_refresh() -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd508-refresh.") as tmp:
        chunk = Path(tmp) / "refresh.js"
        chunk.write_text(owner.SCRIPT_REFRESH, encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        done = subprocess.run(["node", str(harness), str(chunk)], capture_output=True, text=True, check=True,
                              env={"PATH": "/usr/bin:/bin"}, timeout=60)
    return json.loads(done.stdout)


EXPECTED = json.loads(r'''{"normalize":["","","abc","","42"],"idle":[2500,25,60000,70,0,2500,2500,2500,12],"keys":["ticket-board:demo:DEMO:scroll:b9","ticket-board:demo:DEMO:scroll:latest","pgu-ticket-board:scroll:b9","pgu-ticket-board:scroll:latest"],"flow":{"s1":["b1",false,true],"s2":["b2",true,"b2",false,1],"r1":["new ticket draft",false],"reasons":["new ticket draft","new ticket draft","detail open","attachment preview open","unsaved detail edits",""],"activity":[true,[250]],"handled":["b2"],"store":[["ticket-board:demo:DEMO:scroll:b2","{\"windowX\":11,\"windowY\":22,\"boardLeft\":7,\"boardTop\":9}"]],"log":[["syncBodyModalState"],["syncBodyModalState"],["syncBodyModalState"],["setTimeout",1,300],["fire",1],["syncBodyModalState"],["setTimeout",2,500],["rememberDetailDraft"],["rememberDetailDraft"],["fire",2],["rememberDetailDraft"],["syncBodyModalState"],["setTimeout",3,500],["clearTimeout",3],["setTimeout",4,250],["fire",4],["rememberDetailDraft"],["syncBodyModalState"],["rememberDetailDraft"],["set","ticket-board:demo:DEMO:scroll:b2","{\"windowX\":11,\"windowY\":22,\"boardLeft\":7,\"boardTop\":9}"],["reload"],["syncBodyModalState"]]},"afterHandled":[false,"",true],"quiet":[],"nosync":[false,[]],"zeroIdle":[["rememberDetailDraft"],["syncBodyModalState"],["rememberDetailDraft"],["set","ticket-board:demo:DEMO:scroll:b3","{\"windowX\":11,\"windowY\":22,\"boardLeft\":7,\"boardTop\":9}"],["reload"]],"restore legacy=false":{"first":[["pgu-ticket-board:scroll:b1"],0,8],"second":[["pgu-ticket-board:scroll:b1"],0,8],"store":[["pgu-ticket-board:scroll:b1","{\"windowX\":1,\"windowY\":2,\"boardLeft\":3,\"boardTop\":4}"],["ticket-board:demo:DEMO:scroll:b7","{\"windowX\":11,\"windowY\":22,\"boardLeft\":0,\"boardTop\":0}"]],"log":[["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["scrollTo",5,6],["get","ticket-board:demo:DEMO:scroll:b1"],["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["scrollTo",9,0],["set","ticket-board:demo:DEMO:scroll:b7","{\"windowX\":11,\"windowY\":22,\"boardLeft\":0,\"boardTop\":0}"]]},"restore legacy=true":{"first":[["pgu-ticket-board:scroll:b1"],0,8],"second":[[],3,4],"store":[["ticket-board:demo:DEMO:scroll:b7","{\"windowX\":11,\"windowY\":22,\"boardLeft\":0,\"boardTop\":0}"]],"log":[["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["scrollTo",5,6],["get","ticket-board:demo:DEMO:scroll:b1"],["get","pgu-ticket-board:scroll:b1"],["remove","pgu-ticket-board:scroll:b1"],["scrollTo",1,2],["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["get","ticket-board:demo:DEMO:scroll:b1"],["remove","ticket-board:demo:DEMO:scroll:b1"],["scrollTo",9,0],["set","ticket-board:demo:DEMO:scroll:b7","{\"windowX\":11,\"windowY\":22,\"boardLeft\":0,\"boardTop\":0}"]]},"storage throw":{"error":null,"log":[]},"storage readonly":{"error":null,"log":[["get","ticket-board:demo:DEMO:scroll:b1"],["get","pgu-ticket-board:scroll:b1"]]},"priority":["new ticket draft","detail open","attachment preview open"],"exactIdle":"","bannerNotRequired":true,"smallIdle":[["setTimeout",1,100]],"unloaded":[false,"b6",[["syncBodyModalState"]]],"noPending":[[],true]}''')


def test_refresh_and_scroll_behaviour() -> None:
    if shutil.which("node") is None:
        print("ticket_board_frontend_refresh_test: node unavailable; refresh behaviour NOT checked")
        return
    out = run_refresh()
    assert out["normalize"] == ["", "", "abc", "", "42"]
    assert out["idle"] == [2500, 25, 60000, 70, 0, 2500, 2500, 2500, 12]
    assert out["keys"] == ["ticket-board:demo:DEMO:scroll:b9", "ticket-board:demo:DEMO:scroll:latest",
                           "pgu-ticket-board:scroll:b9", "pgu-ticket-board:scroll:latest"]
    flow = out["flow"]
    assert flow["s1"] == ["b1", False, True] and flow["s2"] == ["b2", True, "b2", False, 1]
    assert flow["r1"] == ["new ticket draft", False]
    assert flow["reasons"] == ["new ticket draft", "new ticket draft", "detail open", "attachment preview open",
                               "unsaved detail edits", ""]
    assert flow["activity"] == [True, [250]] and flow["handled"] == ["b2"]
    assert flow["store"] == [["ticket-board:demo:DEMO:scroll:b2", '{"windowX":11,"windowY":22,"boardLeft":7,"boardTop":9}']]
    assert [e for e in flow["log"] if e[0] in ("setTimeout", "clearTimeout", "fire", "reload")] == [
        ["setTimeout", 1, 300], ["fire", 1], ["setTimeout", 2, 500], ["fire", 2], ["setTimeout", 3, 500],
        ["clearTimeout", 3], ["setTimeout", 4, 250], ["fire", 4], ["reload"]]
    assert flow["log"][-4:] == [["rememberDetailDraft"], ["set", "ticket-board:demo:DEMO:scroll:b2",
                                '{"windowX":11,"windowY":22,"boardLeft":7,"boardTop":9}'], ["reload"], ["syncBodyModalState"]]
    assert out["afterHandled"] == [False, "", True]
    assert out["quiet"] == [] and out["nosync"] == [False, []]
    assert out["zeroIdle"][-1] == ["reload"] and not any(e[0] == "setTimeout" for e in out["zeroIdle"])
    plain, legacy = out["restore legacy=false"], out["restore legacy=true"]
    assert plain["first"] == [["pgu-ticket-board:scroll:b1"], 0, 8] and plain["second"] == plain["first"]
    assert legacy["first"] == [["pgu-ticket-board:scroll:b1"], 0, 8] and legacy["second"] == [[], 3, 4]
    assert ["get", "pgu-ticket-board:scroll:b1"] not in plain["log"] and ["scrollTo", 1, 2] in legacy["log"]
    assert plain["log"][:3] == [["get", "ticket-board:demo:DEMO:scroll:b1"], ["remove", "ticket-board:demo:DEMO:scroll:b1"], ["scrollTo", 5, 6]]
    assert ["scrollTo", 9, 0] in plain["log"]
    assert plain["store"][-1] == ["ticket-board:demo:DEMO:scroll:b7", '{"windowX":11,"windowY":22,"boardLeft":0,"boardTop":0}']
    assert out["storage throw"] == {"error": None, "log": []}
    assert out["storage readonly"] == {"error": None, "log": [["get", "ticket-board:demo:DEMO:scroll:b1"], ["get", "pgu-ticket-board:scroll:b1"]]}
    assert out["priority"] == ["new ticket draft", "detail open", "attachment preview open"]
    assert out["exactIdle"] == "" and out["bannerNotRequired"] is True
    assert out["smallIdle"] == [["setTimeout", 1, 100]]
    assert out["unloaded"] == [False, "b6", [["syncBodyModalState"]]]
    assert out["noPending"] == [[], True]
    assert out == EXPECTED


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_frontend_refresh_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
