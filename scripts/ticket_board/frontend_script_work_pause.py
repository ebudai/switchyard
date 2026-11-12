"""The Director's pause switch on the board page: its banner, and the User's two switches and resume (SYRD-756).

`GET /api/work-pause` says whether work is paused, in which mode, set by whom, why, since when, and what waits for
whom. While a pause stands the page shows a banner saying so. The panel under it is the User's: a reason, then
"Drain" (no notifications; a turn already running finishes), "Stop now" (that, and running turns are interrupted)
or "Resume work". Each is a recorded board action, sent as the User (`pause_work`, `resume_work`); the database
admits only the Director and the User. The Director's pane is exempt whoever sets it.
"""

from __future__ import annotations

STYLE_WORK_PAUSE = """    .work-pause {
      display: grid;
      gap: 8px;
      margin-bottom: 10px;
    }
    .work-pause-panel {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      font-size: 12px;
    }
    .work-pause-panel input {
      flex: 1 1 220px;
      min-width: 0;
    }
    .work-pause-status {
      flex-basis: 100%;
      opacity: 0.85;
    }
"""

SCRIPT_WORK_PAUSE = """    const WORK_PAUSE_MODES = {
      drain: 'Drain: no notifications go out; a turn already running finishes, then its pane stays idle.',
      stop: 'Stop now: running turns are interrupted with each runtime\\'s own interrupt; each role is told once ' +
        'to record where it is, and nothing new starts.',
    };

    function workPauseLine(pause) {
      const held = Object.entries(pause.held || {}).map(([role, count]) => `${role} (${count})`).join(', ');
      return `Set by ${pause.began_by} at ${pause.began_at}: "${pause.reason}". ${WORK_PAUSE_MODES[pause.mode] || ''} ` +
        `Waiting: ${held || 'nothing yet'}. The Director\\'s pane is exempt; the Publisher keeps publishing.`;
    }

    async function refreshWorkPause() {
      const banner = document.getElementById('workPauseBanner');
      const panel = document.getElementById('workPausePanel');
      if (!banner || !panel) return;
      let status;
      try {
        const response = await fetch('/api/work-pause', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        status = await response.json();
      } catch (error) {
        return;  // An unreadable answer is not a pause: the page keeps what it last showed.
      }
      const pause = status.paused ? status.pause : null;
      if (pause) {
        const title = document.createElement('strong');
        title.textContent = pause.mode === 'stop' ? 'Work paused: stop now' : 'Work paused: drain';
        const body = document.createElement('div');
        body.textContent = workPauseLine(pause);
        banner.replaceChildren(title, body);
        banner.hidden = false;
      } else {
        banner.hidden = true;
        banner.replaceChildren();
      }
      panel.dataset.mode = pause ? pause.mode : '';
      panel.querySelector('[data-pause-mode="drain"]').disabled = Boolean(pause && pause.mode === 'drain');
      panel.querySelector('[data-pause-mode="stop"]').disabled = Boolean(pause && pause.mode === 'stop');
      panel.querySelector('[data-pause-resume]').disabled = !pause;
    }

    async function sendWorkPause(operation, payload) {
      const panel = document.getElementById('workPausePanel');
      const status = panel.querySelector('.work-pause-status');
      const reason = panel.querySelector('[data-pause-reason]');
      if (!reason.value.trim()) {
        status.textContent = 'Say why first: the reason is recorded with the switch.';
        reason.focus();
        return;
      }
      status.textContent = 'Sending...';
      try {
        await sendBoardWrite(`/api/tickets/actions/${operation}`, { ...payload, reason: reason.value.trim() });
        reason.value = '';
        status.textContent = operation === 'resume_work' ? 'Work resumed.' : 'Recorded.';
      } catch (error) {
        status.textContent = `The board refused this: ${error.message || error}`;
      }
      await refreshWorkPause();
    }

    function setupWorkPause() {
      const panel = document.getElementById('workPausePanel');
      if (!panel) return;
      panel.querySelector('[data-pause-mode="drain"]').addEventListener('click', () => sendWorkPause('pause_work', { mode: 'drain' }));
      panel.querySelector('[data-pause-mode="stop"]').addEventListener('click', () => sendWorkPause('pause_work', { mode: 'stop' }));
      panel.querySelector('[data-pause-resume]').addEventListener('click', () => sendWorkPause('resume_work', {}));
      refreshWorkPause();
      setInterval(refreshWorkPause, 15000);
    }
    setupWorkPause();
"""

MARKUP_WORK_PAUSE = """        <div class="work-pause">
          <div id="workPauseBanner" class="alert alert-blocked" role="alert" hidden></div>
          <div id="workPausePanel" class="work-pause-panel" aria-label="Pause all work">
            <strong>Pause all work</strong>
            <input data-pause-reason type="text" maxlength="500" placeholder="Why (recorded)" aria-label="Reason">
            <button type="button" class="small" data-pause-mode="drain" title="No notifications go out; a turn already running finishes, then stays idle">Drain</button>
            <button type="button" class="small" data-pause-mode="stop" title="Interrupt every running turn now; each role is told once to record where it is">Stop now</button>
            <button type="button" class="small" data-pause-resume disabled title="End the pause, whichever mode">Resume work</button>
            <span class="work-pause-status" role="status"></span>
          </div>
        </div>
"""
