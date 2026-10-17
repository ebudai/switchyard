"""The board UI's confirmed actions (SYRD-598).

The page asserts no role: every request it makes is the User's. What it may
offer for a ticket is described by the server from the live workflow and the
database (POST /api/ui/delegations {describe}), so the page keeps no table of
transitions of its own. The User's own declared actions are posted directly. A
Director operation is the User's decision, made in two steps -- the fields, then
a confirmation that shows the ticket as it is now, the action, and every value --
and done under a single-use grant the server mints for exactly that action. Any
refusal is said plainly and the board is reloaded, so a stale view is replaced.
"""

SCRIPT_DELEGATION = r"""
    const BOARD_UI_STYLE = `
      .board-ui-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex;
        align-items: center; justify-content: center; z-index: 2000; }
      .board-ui-dialog { background: var(--panel, #fff); color: inherit; max-width: 34rem; width: calc(100% - 2rem);
        border-radius: 8px; padding: 1rem 1.25rem; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
      .board-ui-dialog h3 { margin: 0 0 0.5rem; font-size: 1.05rem; }
      .board-ui-dialog label { display: block; margin: 0.5rem 0; }
      .board-ui-dialog input, .board-ui-dialog select, .board-ui-dialog textarea { width: 100%; box-sizing: border-box; }
      .board-ui-summary { margin: 0.5rem 0; padding: 0; list-style: none; }
      .board-ui-summary li { margin: 0.2rem 0; }
      .board-ui-authority { font-size: 0.9rem; opacity: 0.8; margin: 0.5rem 0; }
      .board-ui-error { color: #b00020; margin: 0.5rem 0; white-space: pre-wrap; }
      .board-ui-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.4rem 0; }
    `;
    (function installBoardUiStyle() {
      const style = document.createElement('style');
      style.textContent = BOARD_UI_STYLE;
      document.head.appendChild(style);
    })();

    async function boardUiRequest(body) {
      const response = await fetch('/api/ui/delegations', {
        method: 'POST',
        headers: ticketWriteHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify(body),
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(text || `The board refused this (${response.status}).`);
      }
      return JSON.parse(text);
    }

    async function describeBoardUiActions(ticketId) {
      return boardUiRequest({ describe: true, ticket_id: ticketId });
    }

    function boardUiFieldValue(field, input) {
      if (field.kind === 'fixed') return field.value;
      if (field.kind === 'list') {
        return String(input.value || '').split(/[\s,]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
      }
      if (field.kind === 'ticket') return String(input.value || '').trim().toUpperCase();
      return String(input.value || '').trim();
    }

    function boardUiValueText(value) {
      if (Array.isArray(value)) return value.length ? value.join(', ') : '(none)';
      if (value === true) return 'yes';
      if (value === false) return 'no';
      return value === '' || value === null || value === undefined ? '(none)' : String(value);
    }

    // The dialog: step one asks for what the operation needs, step two shows exactly what will be done.
    function openBoardUiDialog(ticket, op, presets = {}) {
      return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'board-ui-overlay';
        const dialog = document.createElement('div');
        dialog.className = 'board-ui-dialog';
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('data-board-ui-dialog', op.operation);
        overlay.appendChild(dialog);
        const close = (result) => { overlay.remove(); resolve(result); };
        const error = document.createElement('div');
        error.className = 'board-ui-error';
        error.setAttribute('data-board-ui-error', '');

        const inputs = new Map();
        const showForm = () => {
          dialog.innerHTML = '';
          const title = document.createElement('h3');
          title.textContent = `${op.label} — ${ticket.id}`;
          dialog.appendChild(title);
          for (const field of op.fields) {
            if (field.kind === 'fixed') continue;
            const label = document.createElement('label');
            label.textContent = `${field.label}${field.required ? ' (required)' : ''}`;
            let input;
            if (field.kind === 'choice') {
              input = document.createElement('select');
              buildOption(input, '', 'Choose…');
              (field.options || []).forEach((option) => buildOption(input, option, String(option)));
            } else if (field.name === 'reason' || field.name === 'text' || field.name === 'detail' || field.name === 'blocked_reason') {
              input = document.createElement('textarea');
            } else {
              input = document.createElement('input');
              input.type = 'text';
            }
            const preset = presets[field.name] !== undefined ? presets[field.name] : field.value;
            if (preset !== undefined) input.value = Array.isArray(preset) ? preset.join(', ') : String(preset);
            input.setAttribute('data-board-ui-field', field.name);
            inputs.set(field.name, input);
            label.appendChild(input);
            dialog.appendChild(label);
          }
          error.textContent = '';
          dialog.appendChild(error);
          const actions = document.createElement('div');
          actions.className = 'board-ui-actions';
          const cancel = document.createElement('button');
          cancel.type = 'button';
          cancel.textContent = 'Cancel';
          cancel.setAttribute('data-board-ui-cancel', '');
          cancel.addEventListener('click', () => close(null));
          const review = document.createElement('button');
          review.type = 'button';
          review.className = 'primary';
          review.textContent = 'Review';
          review.setAttribute('data-board-ui-review', '');
          review.addEventListener('click', () => {
            const payload = {};
            for (const field of op.fields) {
              const value = presets[field.name] !== undefined && field.kind === 'fixed'
                ? presets[field.name] : boardUiFieldValue(field, inputs.get(field.name));
              if (field.required && (value === '' || (Array.isArray(value) && !value.length))) {
                error.textContent = `${field.label} is required.`;
                return;
              }
              payload[field.name] = value;
            }
            showConfirmation(payload);
          });
          actions.append(cancel, review);
          dialog.appendChild(actions);
        };

        const showConfirmation = async (payload) => {
          dialog.innerHTML = '';
          const title = document.createElement('h3');
          title.textContent = `Confirm: ${op.label}`;
          dialog.appendChild(title);
          let prepared;
          try {
            // The ticket as it is now, and the version a grant will be bound to.
            prepared = op.mode === 'delegated'
              ? await boardUiRequest({ ticket_id: ticket.id, operation: op.operation, payload, prepare: true })
              : { ticket_id: ticket.id, title: ticket.title, state: ticket.state, assignee: ticket.assignee };
          } catch (failure) {
            error.textContent = failure.message;
            dialog.appendChild(error);
            const back = document.createElement('button');
            back.type = 'button';
            back.textContent = 'Close';
            back.setAttribute('data-board-ui-cancel', '');
            back.addEventListener('click', () => close(null));
            dialog.appendChild(back);
            return;
          }
          const summary = document.createElement('ul');
          summary.className = 'board-ui-summary';
          summary.setAttribute('data-board-ui-summary', '');
          const line = (text) => { const item = document.createElement('li'); item.textContent = text; summary.appendChild(item); };
          line(`Ticket: ${prepared.ticket_id} — ${prepared.title}`);
          line(`Now: ${stateLabel(prepared.state)}, assigned to ${roleLabel(prepared.assignee)}`);
          line(`Action: ${op.label}${op.to ? ` → ${stateLabel(op.to)}` : ''}`);
          for (const field of op.fields) {
            line(`${field.label}: ${boardUiValueText(payload[field.name])}`);
          }
          if (prepared.target) {
            line(`Merging ${prepared.ticket_id} (${prepared.title}) into ${prepared.target.ticket_id} (${prepared.target.title})`);
          }
          dialog.appendChild(summary);
          const authority = document.createElement('div');
          authority.className = 'board-ui-authority';
          authority.textContent = op.mode === 'delegated'
            ? `Done as you, using the ${roleLabel(prepared.authority || 'director')}'s operation permission through the board UI. It is recorded as your decision.`
            : 'Done as you.';
          dialog.appendChild(authority);
          error.textContent = '';
          dialog.appendChild(error);
          const actions = document.createElement('div');
          actions.className = 'board-ui-actions';
          const back = document.createElement('button');
          back.type = 'button';
          back.textContent = 'Cancel';
          back.setAttribute('data-board-ui-cancel', '');
          back.addEventListener('click', () => close(null));
          const confirm = document.createElement('button');
          confirm.type = 'button';
          confirm.className = 'primary';
          confirm.textContent = `Confirm ${op.label}`;
          confirm.setAttribute('data-board-ui-confirm', '');
          confirm.addEventListener('click', () => close({ payload, prepared }));
          actions.append(back, confirm);
          dialog.appendChild(actions);
        };

        document.body.appendChild(overlay);
        const needsInput = op.fields.some((field) => field.kind !== 'fixed' && presets[field.name] === undefined);
        if (needsInput) {
          showForm();
        } else {
          const payload = {};
          op.fields.forEach((field) => { payload[field.name] = presets[field.name] !== undefined ? presets[field.name] : field.value; });
          showConfirmation(payload);
        }
      });
    }

    async function performBoardUiAction(ticket, op, presets = {}) {
      const decision = await openBoardUiDialog(ticket, op, presets);
      if (!decision) {
        throw new Error(`${op.label} was not confirmed.`);
      }
      const path = `/api/tickets/${encodeURIComponent(ticket.id)}/actions/${encodeURIComponent(op.operation)}`;
      try {
        let options = { skipFreshCheck: true, skipReloadReplay: true };
        if (op.mode === 'delegated') {
          const grant = await boardUiRequest({
            ticket_id: ticket.id, operation: op.operation, payload: decision.payload,
            ticket_version: decision.prepared.ticket_version,
          });
          options = { ...options, delegation: grant.token, retriedToken: true };
        }
        const result = await postTicketAction(path, decision.payload, 'user', options);
        setCreateStatus(`${op.label}: done for ${ticket.id}.`);
        await requestBoardReload();
        return result;
      } catch (failure) {
        // Refused: say why, and replace what may be a stale view.
        setCreateStatus(`${op.label} refused: ${failure.message}`, true);
        await requestBoardReload();
        throw failure;
      }
    }

    // An operation by name, with what a natural control already knows (a toggle's new value, a blocker list).
    async function runBoardUiOperation(ticket, operation, presets = {}) {
      const name = operation.replaceAll('_', ' ');
      let described;
      try {
        described = await describeBoardUiActions(ticket.id);
      } catch (failure) {
        setCreateStatus(`${name} is unavailable: ${failure.message}`, true);
        throw failure;
      }
      const op = (described.operations || []).find((candidate) => candidate.operation === operation);
      if (!op) {
        const message = described.delegation_unavailable
          ? `${name} is a Director operation, and ${described.delegation_unavailable}.`
          : `${name} is not available for ${ticket.id} now.`;
        setCreateStatus(message, true);
        throw new Error(message);
      }
      return performBoardUiAction(ticket, op, presets);
    }

    // The panel: every operation the server offers for this ticket that has no natural control elsewhere.
    async function renderBoardUiActions(ticket, container) {
      container.setAttribute('data-board-ui-actions', ticket.id);
      container.classList.add('board-ui-actions');
      let described;
      try {
        described = await describeBoardUiActions(ticket.id);
      } catch (failure) {
        const note = document.createElement('div');
        note.className = 'soft-note';
        note.textContent = `Actions unavailable: ${failure.message}`;
        container.appendChild(note);
        return;
      }
      if (described.delegation_unavailable) {
        // Why this page is offered none of the Director's operations: said, not silently missing.
        const note = document.createElement('div');
        note.className = 'soft-note';
        note.setAttribute('data-board-ui-unavailable', '');
        note.textContent = `Director operations: ${described.delegation_unavailable}.`;
        container.appendChild(note);
      }
      for (const op of described.operations || []) {
        if (op.placement) continue;
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = op.label;
        button.setAttribute('data-board-ui-op', op.operation);
        if (op.variant) button.setAttribute('data-board-ui-variant', op.variant);
        button.title = op.mode === 'delegated'
          ? `A ${roleLabel(described.authority)} operation, done as you after you confirm it.`
          : 'Your own action.';
        button.addEventListener('click', async () => {
          try {
            await performBoardUiAction(ticket, op);
          } catch (failure) {
            // already reported
          }
        });
        container.appendChild(button);
      }
    }
"""
