"""Where this board's project files reports, shown read-only on the board page (SYRD-721).

The page files no report: a report crosses to another project's board only through
`ticket-board-write file-report`'s preview and confirmation (SYRD-602). It shows the
same answer `ticket-board-read report-ancestors` gives -- the report parent and every
ancestor, nearest first, by name and ticket prefix, whether each board answered as
itself, and whether root issued this project a credential for it -- from
`GET /api/report-ancestry`. No URL or token is shown.
"""

from __future__ import annotations

SCRIPT_REPORTS = """    async function refreshReportAncestry() {
      const target = document.getElementById('reportAncestry');
      if (!target) return;
      try {
        const response = await fetch('/api/report-ancestry', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        const entries = Array.isArray(payload.ancestors) ? payload.ancestors : [];
        target.replaceChildren();
        if (!entries.length) {
          target.textContent = payload.problem ? `none usable: ${payload.problem}` : 'no report parent';
          return;
        }
        entries.forEach((entry, index) => {
          const item = document.createElement('span');
          item.className = 'report-ancestor';
          item.dataset.project = String(entry.project || '');
          const board = entry.identity_verified ? 'verified' : (entry.reachable ? 'NOT VERIFIED' : 'unreachable');
          // Root's record of issuing it: the board service reads no tenant file, so usability is report-ancestors'.
          const credential = entry.credential && entry.credential.issued ? 'credential issued' : 'no credential issued';
          const arrow = index ? ' \\u2192 ' : '';
          item.textContent = `${arrow}${entry.name} (${entry.ticket_prefix || '?'}): ${board}, ${credential}`;
          item.title = [entry.problem, entry.credential && entry.credential.state].filter(Boolean).join('; ');
          target.appendChild(item);
        });
        if (payload.problem) target.appendChild(document.createTextNode(` (stops: ${payload.problem})`));
        // Root's issuance record only (SYRD-721, Director's decision A): whether a pane's credential is usable now is
        // the pane's own live check.
        const live = document.createElement('span');
        live.className = 'report-ancestry-live';
        live.textContent = ' \u2014 is a credential usable now? run ticket-board-read report-ancestors in a pane';
        target.appendChild(live);
      } catch (error) {
        target.textContent = `unknown (${error.message})`;
      }
    }
    refreshReportAncestry();
"""
