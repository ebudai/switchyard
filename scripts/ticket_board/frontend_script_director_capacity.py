"""The Director's capacity alert, shown on the board page (SYRD-728).

When the Director's Codex pane stopped on "Selected model is at capacity" and the board's automatic retries did not
get through, nothing more is typed into it, and the alert must not wait in that same pane: the board page shows it
here, from `GET /api/director-capacity`, until a Director turn ends normally again. The phone bridge carries the same
event where it is configured. What to do is the manual recovery the Director guide describes.
"""

from __future__ import annotations

SCRIPT_DIRECTOR_CAPACITY = """    async function refreshDirectorCapacity() {
      const target = document.getElementById('directorCapacityAlert');
      if (!target) return;
      try {
        const response = await fetch('/api/director-capacity', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const alert = (await response.json()).alert;
        if (!alert) {
          target.hidden = true;
          target.replaceChildren();
          return;
        }
        const title = document.createElement('strong');
        title.textContent = 'Director stopped: its model is at capacity';
        const body = document.createElement('div');
        body.textContent = `The Director's Codex pane stopped on "${alert.error}" at ${alert.since}, and ` +
          `${alert.attempts} automatic retries did not get through; nothing more is sent to it. Attach to its pane, ` +
          'check what its last turn already did, then tell it to carry on once the model answers, or switch its ' +
          'model. This clears when a Director turn ends normally.';
        target.replaceChildren(title, body);
        target.hidden = false;
      } catch (error) {
        // An unreadable answer is not an alert: the banner keeps what it last showed.
      }
    }
    refreshDirectorCapacity();
    setInterval(refreshDirectorCapacity, 60000);
"""
