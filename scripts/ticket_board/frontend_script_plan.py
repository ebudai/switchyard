"""The plan gate on the board page: each ticket's deliverable, the deliverable filter and the plan view (SYRD-850).

Only on a board whose declared workflow runs the plan gate: `GET /api/plan` answers `{"plan": null}` everywhere else,
and the page then shows nothing new -- the controls keep `hidden`, the filter lets every ticket through, and the ticket
view has no plan line.

- **The ticket view** says the deliverable the ticket serves, with its title and milestone, or "Unplanned" and the
  Director's reason (`plan_scope` in the ticket JSON). A ticket with neither, such as a plan ticket, says nothing.
- **The filter** beside the Show switches keeps the board to one deliverable's tickets, or to the unplanned ones.
- **The plan view** ("Plan") shows the approved commit and plan file, then each milestone and its deliverables with
  their tickets counted by stage, the unplanned tickets counted apart, and "Re-plan due" on a milestone whose
  tickets are all Done or cancelled. A deliverable's count opens the board filtered to it.

The board service's half is `plan_view.py`.
"""

from __future__ import annotations

STYLE_PLAN = """    .plan-controls {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      max-width: 100%;
    }
    .plan-controls[hidden] { display: none; }
    .plan-controls select.small { max-width: min(260px, 100%); }
    /* Its own classes: the ticket view's dialog is the page's one .detail-modal. */
    .plan-overlay[hidden] { display: none; }
    .plan-overlay {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: stretch;
      background: rgba(4, 6, 10, 0.72);
      padding: 18px;
      overflow: auto;
    }
    .plan-modal {
      width: min(100%, 900px);
      max-height: calc(100vh - 36px);
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--border);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(23, 26, 32, 0.98), rgba(15, 17, 21, 0.98));
      box-shadow: 0 30px 90px rgba(0, 0, 0, 0.42);
      overflow: hidden;
      min-height: 0;
    }
    .plan-modal-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      padding: 18px 22px;
      border-bottom: 1px solid var(--border);
    }
    .plan-modal-head h2 { margin: 0; font-size: 18px; }
    .plan-close {
      width: 40px;
      height: 40px;
      min-width: 40px;
      padding: 0;
      border-radius: 999px;
      font-size: 24px;
      line-height: 1;
    }
    .plan-modal-body { overflow: auto; min-height: 0; padding: 18px 22px; }
    @media (max-width: 980px) {
      .plan-overlay { padding: 0; }
      .plan-modal { width: 100%; max-height: 100vh; border-radius: 0; border-left: 0; border-right: 0; }
      .plan-modal-head, .plan-modal-body { padding-left: 16px; padding-right: 16px; }
    }
    .plan-view { display: grid; gap: 14px; font-size: 13px; overflow-wrap: anywhere; }
    .plan-approved code { font-size: 12px; }
    .plan-milestone {
      display: grid;
      gap: 6px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .plan-milestone-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
    .plan-milestone-head strong { font-size: 14px; }
    .plan-badge {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      color: var(--muted);
    }
    .plan-badge.plan-replan-due { color: var(--warn); border-color: var(--warn); }
    .plan-deliverable {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 10px;
      align-items: baseline;
      justify-content: space-between;
    }
    .plan-deliverable button.small { text-align: left; }
    .plan-counts { color: var(--muted); }
"""

MARKUP_PLAN_CONTROLS = """            <span id="planControls" class="plan-controls" hidden>
              <label class="check compact">
                <span>Deliverable</span>
                <select id="planFilterInput" class="small" aria-label="Show the tickets of one deliverable"></select>
              </label>
              <button id="planViewBtn" class="small" type="button" aria-controls="planOverlay">Plan</button>
            </span>
"""

MARKUP_PLAN_OVERLAY = """  <div id="planOverlay" class="plan-overlay" hidden>
    <section class="plan-modal" role="dialog" aria-modal="true" aria-labelledby="planModalTitle">
      <div class="plan-modal-head">
        <div>
          <div class="subtle">Plan gate</div>
          <h2 id="planModalTitle">Plan</h2>
        </div>
        <button id="planCloseBtn" class="plan-close" type="button" aria-label="Close the plan view">×</button>
      </div>
      <div class="plan-modal-body">
        <div id="planContent" class="plan-view"></div>
      </div>
    </section>
  </div>
"""

SCRIPT_PLAN = """    // SYRD-850. Reads only `state`, so the board's first render may call it before this block has run.
    function planTicketShown(ticket) {
      const filter = state.plan ? String(state.planFilter || '') : '';
      if (!filter) return true;
      const scope = ticket.plan_scope || {};
      return filter === 'unplanned' ? Boolean(scope.unplanned_reason) : scope.deliverable === filter;
    }

    function planDeliverables() {
      const found = new Map();
      ((state.plan && state.plan.milestones) || []).forEach((milestone) => {
        (milestone.deliverables || []).forEach((deliverable) => found.set(deliverable.id, { ...deliverable, milestone }));
      });
      return found;
    }

    function planScopeText(ticket) {
      const scope = ticket.plan_scope;
      if (!scope) return '';
      if (scope.unplanned_reason) return `Unplanned: ${scope.unplanned_reason}`;
      const known = planDeliverables().get(scope.deliverable);
      if (!known) return `Deliverable: ${scope.deliverable}`;
      return `Deliverable: ${scope.deliverable} ${known.title}, of milestone ${known.milestone.id} (${known.milestone.title})`;
    }

    function planCountsText(tickets) {
      const counts = tickets || {};
      const order = boardColumns().map((column) => column.key);
      const keys = Object.keys(counts).sort((a, b) => {
        const left = order.indexOf(a);
        const right = order.indexOf(b);
        return (left < 0 ? order.length : left) - (right < 0 ? order.length : right) || a.localeCompare(b);
      });
      if (!keys.length) return 'no tickets';
      const label = (key) => (boardColumns().find((column) => column.key === key) || { label: key }).label;
      return keys.map((key) => `${label(key)} ${counts[key]}`).join(' · ');
    }

    function setPlanFilter(value) {
      state.planFilter = value;
      const input = document.getElementById('planFilterInput');
      if (input) input.value = value;
      renderStateVisibilityToggles();
      renderBoard();
    }

    function renderPlanControls() {
      const controls = document.getElementById('planControls');
      const input = document.getElementById('planFilterInput');
      if (!controls || !input) return;
      const deliverables = planDeliverables();
      if (state.planFilter && state.planFilter !== 'unplanned' && !deliverables.has(state.planFilter)) {
        state.planFilter = '';
      }
      controls.hidden = !state.plan;
      const options = [new Option('All tickets', '')];
      ((state.plan && state.plan.milestones) || []).forEach((milestone) => {
        const group = document.createElement('optgroup');
        group.label = `${milestone.id} ${milestone.title}`;
        (milestone.deliverables || []).forEach((deliverable) => {
          group.appendChild(new Option(`${deliverable.id} ${deliverable.title}`, deliverable.id));
        });
        options.push(group);
      });
      options.push(new Option('Unplanned', 'unplanned'));
      input.replaceChildren(...options);
      input.value = state.plan ? String(state.planFilter || '') : '';
    }

    function planElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    }

    function renderPlanView() {
      const content = document.getElementById('planContent');
      if (!content) return;
      const plan = state.plan;
      if (!plan) {
        content.replaceChildren(planElement('div', 'meta', 'This board does not run the plan gate.'));
        return;
      }
      const parts = [];
      const approved = plan.approved;
      const head = planElement('div', 'plan-approved');
      if (approved) {
        const file = planElement('code', '', approved.plan_file);
        const commit = planElement('code', '', approved.commit);
        head.append('Approved plan: ', file, ` (${approved.plan_lines} lines) at commit `, commit,
                    `, approved ${approved.approved_at}` + (approved.ticket ? ` by closing ${approved.ticket}.` : '.'));
      } else {
        head.textContent = 'No plan has been approved yet: a plan ticket closed with the User\\'s acceptance approves one.';
      }
      parts.push(head);
      (plan.milestones || []).forEach((milestone) => {
        const box = planElement('section', 'plan-milestone');
        box.dataset.milestone = milestone.id;
        const title = planElement('div', 'plan-milestone-head');
        title.appendChild(planElement('strong', '', `${milestone.id} ${milestone.title}`));
        if (!milestone.detailed) title.appendChild(planElement('span', 'plan-badge', 'Not detailed yet'));
        if (milestone.replan_due) {
          const due = planElement('span', 'plan-badge plan-replan-due', 'Re-plan due');
          due.title = 'Every ticket of this milestone is Done or cancelled: the plan owner and the Director were told.';
          title.appendChild(due);
        }
        box.appendChild(title);
        (milestone.deliverables || []).forEach((deliverable) => {
          const row = planElement('div', 'plan-deliverable');
          row.dataset.deliverable = deliverable.id;
          const show = planElement('button', 'small', `${deliverable.id} ${deliverable.title}`);
          show.type = 'button';
          show.title = 'Show these tickets on the board';
          show.addEventListener('click', () => { closePlanView(); setPlanFilter(deliverable.id); });
          row.append(show, planElement('span', 'plan-counts', planCountsText(deliverable.tickets)));
          box.appendChild(row);
        });
        parts.push(box);
      });
      const unplanned = planElement('section', 'plan-milestone plan-unplanned');
      const row = planElement('div', 'plan-deliverable');
      const show = planElement('button', 'small', 'Unplanned');
      show.type = 'button';
      show.title = 'Show the unplanned tickets on the board';
      show.addEventListener('click', () => { closePlanView(); setPlanFilter('unplanned'); });
      row.append(show, planElement('span', 'plan-counts', planCountsText(plan.unplanned)));
      unplanned.appendChild(row);
      parts.push(unplanned);
      content.replaceChildren(...parts);
    }

    function planViewIsOpen() {
      const overlay = document.getElementById('planOverlay');
      return Boolean(overlay && !overlay.hidden);
    }

    function closePlanView() {
      const overlay = document.getElementById('planOverlay');
      if (overlay) overlay.hidden = true;
      document.body.classList.toggle('detail-open', detailModalIsOpen() || imageLightboxIsOpen());
    }

    async function refreshPlan() {
      let answer;
      try {
        const response = await fetch('/api/plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        answer = await response.json();
      } catch (error) {
        return;  // An unreadable answer changes nothing: the page keeps what it last showed.
      }
      const before = JSON.stringify([state.plan || null, state.planFilter || '']);
      state.plan = answer && answer.plan && typeof answer.plan === 'object' ? answer.plan : null;
      renderPlanControls();
      if (planViewIsOpen()) renderPlanView();
      if (JSON.stringify([state.plan || null, state.planFilter || '']) !== before) {
        renderStateVisibilityToggles();
        renderBoard();
      }
    }

    async function openPlanView() {
      await refreshPlan();
      renderPlanView();
      document.getElementById('planOverlay').hidden = false;
      document.body.classList.add('detail-open');
    }

    function setupPlan() {
      const input = document.getElementById('planFilterInput');
      if (!input) return;
      input.addEventListener('change', () => setPlanFilter(input.value));
      document.getElementById('planViewBtn').addEventListener('click', () => { void openPlanView(); });
      document.getElementById('planCloseBtn').addEventListener('click', closePlanView);
      const overlay = document.getElementById('planOverlay');
      overlay.addEventListener('click', (event) => { if (event.target === overlay) closePlanView(); });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && planViewIsOpen() && !state.detailOpen && !imageLightboxIsOpen()) closePlanView();
      });
      refreshPlan();
      setInterval(refreshPlan, 15000);
    }
    setupPlan();
"""
