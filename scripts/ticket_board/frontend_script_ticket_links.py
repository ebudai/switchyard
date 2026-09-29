"""Ticket-reference and linked-ticket rendering JavaScript.

Looks a ticket ID up in the current board snapshot and renders it as a
reference button that opens its detail, or a disabled "not found" reference
for an ID the board does not hold, including another board's qualified
reference. Also renders child-ticket lists, free text with ticket IDs turned
into references line by line, the linked-text previews and a row of linked
tickets. SCRIPT_CORE splices this in at its original position; it uses
SCRIPT_CORE's shared state, TICKET_REF_PATTERN, openDetail and stateLabel,
and SCRIPT_CORE and SCRIPT_DETAIL call its functions.
"""

from __future__ import annotations

SCRIPT_TICKET_LINKS = """    function ticketById(ticketId) {
      const normalizedId = String(ticketId || '').toUpperCase();
      return state.tickets.find((ticket) => ticket.id === normalizedId) || null;
    }

    function buildTicketReference(ticketId, label = ticketId) {
      const normalizedId = String(ticketId || '').toUpperCase();
      const reference = document.createElement('button');
      reference.type = 'button';
      reference.className = 'ticket-ref';
      reference.textContent = String(label || normalizedId);
      if (ticketById(normalizedId)) {
        reference.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          openDetail(normalizedId);
        });
      } else {
        reference.disabled = true;
        reference.classList.add('ticket-ref-missing');
        reference.title = 'Ticket not found in the current board snapshot.';
      }
      return reference;
    }

    function buildChildTicketList(children, { compact = false } = {}) {
      const wrap = document.createElement('div');
      wrap.className = compact ? 'child-ticket-list child-ticket-list-compact' : 'child-ticket-list';

      const head = document.createElement('div');
      head.className = 'child-ticket-head';
      head.textContent = children.length === 1 ? '1 linked child' : `${children.length} linked children`;
      wrap.appendChild(head);

      const list = document.createElement('div');
      list.className = 'child-ticket-items';
      children.forEach((child) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'child-ticket-item';
        if (state.selectedId === child.id) {
          row.classList.add('selected');
        }
        row.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          openDetail(child.id);
        });

        const text = document.createElement('div');
        text.className = 'child-ticket-text';
        const id = document.createElement('div');
        id.className = 'child-ticket-id';
        id.textContent = child.id;
        const title = document.createElement('div');
        title.className = 'child-ticket-title';
        title.textContent = child.title;
        text.append(id, title);

        const stateChip = document.createElement('span');
        stateChip.className = 'tag child-ticket-state';
        stateChip.textContent = stateLabel(child.state);
        row.append(text, stateChip);
        list.appendChild(row);
      });
      wrap.appendChild(list);
      return wrap;
    }

    function appendLinkedTicketText(container, text) {
      const source = text || '';
      const lines = source.split(/\\r?\\n/);
      lines.forEach((line, lineIndex) => {
        let cursor = 0;
        TICKET_REF_PATTERN.lastIndex = 0;
        let match = TICKET_REF_PATTERN.exec(line);
        while (match) {
          if (match.index > cursor) {
            container.appendChild(document.createTextNode(line.slice(cursor, match.index)));
          }
          container.appendChild(buildTicketReference(match[1], match[0]));
          cursor = match.index + match[0].length;
          match = TICKET_REF_PATTERN.exec(line);
        }
        if (cursor < line.length) {
          container.appendChild(document.createTextNode(line.slice(cursor)));
        }
        if (lineIndex < lines.length - 1) {
          container.appendChild(document.createElement('br'));
        }
      });
    }

    function linkedTextBlock(text, emptyText = '(none)') {
      const block = document.createElement('div');
      block.className = 'body-text linked-text';
      appendLinkedTicketText(block, text && text.length ? text : emptyText);
      return block;
    }

    function linkedPreview(label, text, emptyText = '(none)') {
      const preview = document.createElement('div');
      preview.className = 'field-preview';
      const previewLabel = document.createElement('div');
      previewLabel.className = 'field-preview-label';
      previewLabel.textContent = label;
      preview.append(previewLabel, linkedTextBlock(text, emptyText));
      return preview;
    }

    function linkedTicketRow(ticketIds) {
      const row = document.createElement('div');
      row.className = 'ticket-ref-row';
      if (!ticketIds.length) {
        const empty = document.createElement('div');
        empty.className = 'soft-note';
        empty.textContent = 'No linked tickets.';
        row.appendChild(empty);
        return row;
      }
      ticketIds.forEach((ticketId) => {
        row.appendChild(buildTicketReference(ticketId));
      });
      return row;
    }

"""
