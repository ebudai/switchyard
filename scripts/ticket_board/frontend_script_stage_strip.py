"""The board's stage strip: every declared stage within reach when they do not all fit (SYRD-722).

Wherever the lanes are a horizontal strip -- wider than 900px, where the composer sits beside the board -- the layout
is held to the window. The composer and the strip each scroll inside it, so the strip's own horizontal scrollbar sits
at the window's bottom edge however tall a lane is. Before, nothing bounded the strip's height, and its scrollbar was
a page's scroll below the window, under the longest lane. At 900px and below the lanes stack in one column under the
composer, nothing scrolls across, and the page keeps its own scroll.

When the stages do not fit, Earlier stages and Later stages in the top bar each move the strip a page of whole
stages, and say which stages are shown. Those buttons are always on screen and work from the keyboard; at an end the
button says so with aria-disabled and keeps its focus. The strip also takes focus, so its arrow keys scroll it.

A redraw (a ticket update, the Show filters, a reload) puts the stage at the strip's left edge back where it was. If
that stage was just hidden, the strip keeps its offset, which puts the next stage in its place. A strip made shorter
is clamped by the browser to its new end. No stage is hidden, merged or reordered.
"""

from __future__ import annotations

STYLE_STAGE_STRIP = """    .board-scroll:focus-visible {
      outline: 2px solid rgba(125, 211, 252, 0.5);
      outline-offset: -2px;
    }
    .stage-nav {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .stage-nav[hidden] { display: none; }
    .stage-nav button[aria-disabled="true"] {
      opacity: 0.45;
      cursor: default;
    }
    .stage-nav-status {
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }
    /* The strip already scrolls both ways (overflow-x: auto makes its overflow-y auto too); holding the layout to
       the window is what bounds its height. */
    @media (min-width: 901px) {
      .layout { height: 100vh; }
      aside, .shell { min-height: 0; }
    }
    @supports (height: 100dvh) {
      @media (min-width: 901px) {
        .layout { height: 100dvh; }
      }
    }
"""

SCRIPT_STAGE_STRIP = """    function stageColumnEls() {
      return [...boardEl.querySelectorAll(':scope > .column')];
    }

    function stripView(strip) {
      const left = strip.getBoundingClientRect().left;
      return { left, right: left + strip.clientWidth };
    }

    // Before a redraw: the stage at the strip's left edge, and how far from the edge its lane starts.
    function stripStageAnchor() {
      const strip = boardScrollElement();
      const view = stripView(strip);
      const columns = stageColumnEls();
      const index = columns.findIndex((column) => column.getBoundingClientRect().right > view.left + 1);
      if (index < 0) {  // no lanes yet
        return null;
      }
      return { key: columns[index].dataset.stage, offset: columns[index].getBoundingClientRect().left - view.left };
    }

    function restoreStripStageAnchor(anchor) {
      const strip = boardScrollElement();
      const column = anchor && stageColumnEls().find((item) => item.dataset.stage === anchor.key);
      if (column) {
        strip.scrollLeft += column.getBoundingClientRect().left - stripView(strip).left - anchor.offset;
      }
      syncStageNav();
    }

    function syncStageNav() {
      const strip = boardScrollElement();
      const columns = stageColumnEls();
      const end = strip.scrollWidth - strip.clientWidth;
      document.getElementById('stageNav').hidden = end <= 1;
      const view = stripView(strip);
      const shown = columns.map((column) => column.getBoundingClientRect())
        .map((rect, index) => (rect.right > view.left + 1 && rect.left < view.right - 1 ? index + 1 : 0))
        .filter(Boolean);
      document.getElementById('stageNavStatus').textContent = `Stages ${shown[0]}-${shown.at(-1)} of ${columns.length}`;
      // aria-disabled, not disabled: a disabled button drops the keyboard focus that just pressed it.
      document.getElementById('stageNavPrev').setAttribute('aria-disabled', String(strip.scrollLeft <= 1));
      document.getElementById('stageNavNext').setAttribute('aria-disabled', String(strip.scrollLeft >= end - 1));
    }

    // A page of whole stages: Later brings the first stage cut off on the right to the left edge, Earlier brings the
    // last stage cut off on the left to the right edge.
    function stepStages(direction) {
      const strip = boardScrollElement();
      const view = stripView(strip);
      const style = getComputedStyle(strip);
      const rects = stageColumnEls().map((column) => column.getBoundingClientRect());
      if (direction > 0) {
        const next = rects.find((rect) => rect.right > view.right + 1);
        strip.scrollLeft += next ? next.left - view.left - parseFloat(style.paddingLeft) : 0;
      } else {
        const previous = rects.filter((rect) => rect.left < view.left - 1).pop();
        strip.scrollLeft += previous ? previous.right - view.right + parseFloat(style.paddingRight) : 0;
      }
    }

    let stageNavFrame = 0;
    function scheduleStageNavSync() {
      if (!stageNavFrame) {
        stageNavFrame = window.requestAnimationFrame(() => {
          stageNavFrame = 0;
          syncStageNav();
        });
      }
    }

    document.getElementById('stageNavPrev').addEventListener('click', () => stepStages(-1));
    document.getElementById('stageNavNext').addEventListener('click', () => stepStages(1));
    boardScrollElement().addEventListener('scroll', scheduleStageNavSync, { passive: true });
    window.addEventListener('resize', scheduleStageNavSync);
"""
