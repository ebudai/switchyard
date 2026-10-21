"""The User's UAT panel at the top of a ticket in User Review (SYRD-629).

What the User needs to do, what they should see, and what to choose then -- the
Director's structured instructions -- before anything else on the ticket, with
a box for a question or an observation and the three things the User can do
with it: Ask Director (the ticket stays), Accept, Send to Director. Each says in
its confirmation where the ticket goes and who acts next. A ticket without
current instructions shows the Director's latest explicit UAT request, if one
can be identified, or says plainly that the Director must prepare the steps.
"""

SCRIPT_UAT = r"""
    // Text with its commands kept literal and copyable: ```fenced``` blocks and `inline` code (SYRD-629).
    function uatRichText(text) {
      const fragment = document.createDocumentFragment();
      String(text || '').split('```').forEach((segment, index) => {
        if (index % 2 === 1) {
          const block = document.createElement('div');
          block.className = 'uat-code-block';
          const pre = document.createElement('pre');
          const code = document.createElement('code');
          code.textContent = segment.replace(/^[a-z]*\n/, '').replace(/\n$/, '');
          pre.appendChild(code);
          const copy = document.createElement('button');
          copy.type = 'button';
          copy.className = 'uat-copy';
          copy.textContent = 'Copy';
          copy.addEventListener('click', async () => {
            try {
              await navigator.clipboard.writeText(code.textContent);
              copy.textContent = 'Copied';
            } catch (failure) {
              copy.textContent = 'Select and copy';
            }
          });
          block.append(pre, copy);
          fragment.appendChild(block);
          return;
        }
        segment.split('`').forEach((piece, inner) => {
          if (inner % 2 === 1) {
            const code = document.createElement('code');
            code.textContent = piece;
            fragment.appendChild(code);
          } else if (piece) {
            fragment.appendChild(document.createTextNode(piece));
          }
        });
      });
      return fragment;
    }

    // The Director's latest comment that is explicitly a UAT request, for a ticket with no structured steps.
    function uatLegacyRequest(ticket) {
      return (ticket.comments || []).filter((comment) => comment.who === 'director'
        && /^\s*(\*\*)?\s*(user\s+)?UAT\b/i.test(comment.text || '')).pop() || null;
    }

    // The User's latest question and the Director's comments after it: after the question's own comment, since
    // timestamps are whole seconds and an earlier note can share the question's.
    function uatLatestQuestion(ticket) {
      const questions = (ticket.uat && ticket.uat.questions) || [];
      const question = questions[questions.length - 1];
      if (!question) {
        return null;
      }
      const comments = ticket.comments || [];
      const asked = comments.map((comment) => comment.who === 'user'
        && comment.text === `Question for the Director: ${question.question}`).lastIndexOf(true);
      const replies = (asked >= 0 ? comments.slice(asked + 1)
        : comments.filter((comment) => String(comment.ts || '') > String(question.asked_at || '')))
        .filter((comment) => comment.who === 'director');
      return { question, replies };
    }

    function uatSection(className, heading, body) {
      const section = document.createElement('div');
      section.className = className;
      if (heading) {
        const title = document.createElement('div');
        title.className = 'uat-part-label';
        title.textContent = heading;
        section.appendChild(title);
      }
      if (body !== undefined) {
        const text = document.createElement('div');
        text.className = 'uat-part-text';
        text.appendChild(typeof body === 'string' ? uatRichText(body) : body);
        section.appendChild(text);
      }
      return section;
    }

    // The User's own decisions, as the board offers them: Accept and Send to Director.
    async function uatDecisions(ticket) {
      if (!state.workflow) {
        // A legacy board: the User's sign-off, and the reopen that hands the ticket to the Director.
        return {
          accept: { operation: 'user_sign_off', label: 'Accept', mode: 'direct', fields: [],
                    question: 'Accept this ticket?',
                    next: 'It goes to the Director, who completes it.' },
          send: { operation: 'user_reopen', label: 'Send to Director', mode: 'direct',
                  fields: [{ name: 'text', kind: 'text', label: 'Your message', required: true }],
                  question: 'Send this ticket and your message to the Director?',
                  next: 'It goes back to the Director, who reviews your message and decides what happens next.' },
        };
      }
      const described = await describeBoardUiActions(ticket.id);
      const mine = (described.operations || []).filter((op) => op.placement === 'uat_panel');
      return {
        accept: mine.find((op) => op.uat_decision === 'accept') || null,
        send: mine.find((op) => op.uat_decision === 'send') || null,
      };
    }

    const UAT_ASK = { operation: 'ask_director', label: 'Ask Director', mode: 'direct',
                      fields: [{ name: 'question', kind: 'text', label: 'Your question', required: true }],
                      question: 'Send your question to the Director?',
                      next: 'The ticket stays in User Review. The Director answers here, beside your question.' };

    function uatButton(label, className, onClick) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = className;
      button.textContent = label;
      button.addEventListener('click', async () => {
        try {
          await onClick();
        } catch (failure) {
          // performBoardUiAction has said why, or the User cancelled.
        }
      });
      return button;
    }

    // The panel, or null for a ticket that is not the User's to review or decide now. The message box is one of
    // the detail's draft fields, so a redraw while the User is typing keeps what they wrote.
    function renderUatPanel(ticket, draftFields = null) {
      const reviewing = ticketIsUserReview(ticket) && !ticket.user_signoff;
      const withDirector = ticket.state === 'user_feedback';
      if (ticketIsUserReview(ticket) && ticket.user_signoff) {
        // Accepted, and not yet moved on: nothing more to do.
        const done = document.createElement('section');
        done.className = 'uat-panel uat-panel-accepted';
        done.setAttribute('data-uat-panel', ticket.id);
        done.setAttribute('data-uat-state', 'accepted');
        done.appendChild(uatSection('uat-note', 'UAT: Accepted ✓', 'The Director completes it next.'));
        return done;
      }
      if (!reviewing && !withDirector) {
        return null;
      }
      const panel = document.createElement('section');
      panel.className = 'uat-panel';
      panel.setAttribute('data-uat-panel', ticket.id);
      const heading = document.createElement('h2');
      heading.className = 'uat-title';
      heading.textContent = withDirector ? 'UAT: The Director is reviewing your message' : 'UAT: What you need to do';
      panel.appendChild(heading);
      if (withDirector) {
        const yours = (ticket.comments || []).filter((comment) => comment.who === 'user').pop();
        if (yours) {
          panel.appendChild(uatSection('uat-message', `Your message (${yours.ts || ''})`, yours.text || ''));
        }
        panel.appendChild(uatSection('uat-note', '', 'Nothing goes back to the implementer unless the Director sends '
          + 'it. The Director will return it to you, send it on, or tell you here what happens next.'));
        return panel;
      }
      const instructions = ticket.uat && ticket.uat.instructions;
      if (instructions && instructions.current) {
        panel.setAttribute('data-uat-state', 'instructions');
        [['do_this', 'Do this'], ['you_should_see', 'You should see'], ['then_choose', 'Then choose']]
          .forEach(([key, label]) => {
            const part = uatSection('uat-part', label, instructions[key]);
            part.setAttribute('data-uat-part', key);
            panel.appendChild(part);
          });
        const meta = document.createElement('div');
        meta.className = 'uat-meta';
        meta.setAttribute('data-uat-meta', '');
        meta.textContent = `Prepared by ${roleLabel(instructions.prepared_by)} · last updated ${instructions.prepared_at}`;
        panel.appendChild(meta);
      } else if (uatLegacyRequest(ticket)) {
        const request = uatLegacyRequest(ticket);
        panel.setAttribute('data-uat-state', 'legacy');
        panel.appendChild(uatSection('uat-part', `The Director's UAT request (${request.ts || ''})`, request.text || ''));
        const meta = document.createElement('div');
        meta.className = 'uat-meta';
        meta.textContent = 'From the ticket\'s comments: these steps were not prepared in the three-part form.';
        panel.appendChild(meta);
      } else {
        panel.setAttribute('data-uat-state', 'missing');
        panel.appendChild(uatSection('uat-missing', 'Director must prepare UAT steps',
          'There is nothing to try yet. Ask the Director below, or wait for the steps to appear here.'));
      }
      const thread = uatLatestQuestion(ticket);
      if (thread) {
        const exchange = uatSection('uat-thread', `Your question (${thread.question.asked_at})`, thread.question.question);
        exchange.setAttribute('data-uat-thread', '');
        if (thread.replies.length) {
          thread.replies.forEach((reply) => exchange.appendChild(
            uatSection('uat-reply', `Director (${reply.ts || ''})`, reply.text || '')));
        } else {
          exchange.appendChild(uatSection('uat-reply-pending', '', 'Waiting for the Director\'s answer.'));
        }
        panel.appendChild(exchange);
      }
      const label = document.createElement('label');
      label.className = 'uat-message-label';
      label.textContent = 'Questions or observations';
      const message = document.createElement('textarea');
      message.className = 'uat-message-input';
      message.setAttribute('data-uat-message', '');
      message.placeholder = 'Ask a question, or say what you saw.';
      if (draftFields) {
        bindDetailDraftField(draftFields, message, 'uatMessage', '');
      }
      label.appendChild(message);
      panel.appendChild(label);
      const actions = document.createElement('div');
      actions.className = 'uat-actions';
      actions.setAttribute('data-uat-actions', '');
      const text = () => message.value.trim();
      const presets = (name) => (text() ? { [name]: text() } : {});
      // Done: the message was sent, so the box is empty again.
      const sent = async (op, values) => {
        await performBoardUiAction(ticket, op, values);
        markDetailDraftFieldsSaved(ticket.id, { uatMessage: '' });
      };
      actions.appendChild(uatButton('Ask Director', 'uat-ask', () => sent(UAT_ASK, presets('question'))));
      panel.appendChild(actions);
      uatDecisions(ticket).then(({ accept, send }) => {
        if (accept) {
          const button = uatButton('Accept', 'primary uat-accept', () => performBoardUiAction(ticket, accept, {}));
          button.setAttribute('data-uat-decision', 'accept');
          actions.appendChild(button);
        }
        if (send) {
          const field = (send.fields || []).find((candidate) => candidate.kind !== 'fixed');
          const button = uatButton('Send to Director', 'uat-send', () => sent(send, field ? presets(field.name) : {}));
          button.setAttribute('data-uat-decision', 'send');
          actions.appendChild(button);
        }
      }).catch((failure) => {
        const note = document.createElement('div');
        note.className = 'soft-note';
        note.textContent = `Accept and Send to Director are unavailable: ${failure.message}`;
        actions.appendChild(note);
      });
      return panel;
    }
"""

STYLE_UAT = """
    .uat-panel { border: 2px solid rgba(125, 211, 252, 0.6); border-radius: 8px; padding: 12px 14px; margin: 0 0 12px;
                 background: linear-gradient(180deg, rgba(125, 211, 252, 0.18), rgba(125, 211, 252, 0.08));
                 color: var(--text); }
    .uat-panel-accepted { border-color: rgba(134, 239, 172, 0.5);
                          background: linear-gradient(180deg, rgba(134, 239, 172, 0.18), rgba(134, 239, 172, 0.08)); }
    .uat-title { font-size: 1.1rem; margin: 0 0 8px; }
    .uat-part { margin: 0 0 8px; }
    .uat-part-label { color: var(--accent); font-weight: 700; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.03em; }
    .uat-part-text { white-space: pre-wrap; overflow-wrap: anywhere; }
    .uat-part-text code { font-family: ui-monospace, monospace; background: rgba(255, 255, 255, 0.1); padding: 0 3px;
                          border-radius: 3px; user-select: all; }
    .uat-code-block { position: relative; margin: 4px 0; }
    .uat-code-block pre { margin: 0; padding: 6px 8px; background: rgba(255, 255, 255, 0.08); border-radius: 6px;
                          overflow-x: auto; }
    .uat-copy { position: absolute; top: 4px; right: 4px; font-size: 0.75rem; }
    .uat-meta, .uat-reply-pending { font-size: 0.8rem; opacity: 0.8; margin: 0 0 8px; }
    .uat-missing { font-weight: 600; }
    .uat-thread { border-left: 3px solid var(--accent); padding-left: 8px; margin: 0 0 8px; }
    .uat-reply { margin: 6px 0 0; }
    .uat-message-label { display: block; font-weight: 600; margin: 4px 0; }
    .uat-message-input { display: block; width: 100%; min-height: 3.2em; box-sizing: border-box; }
    .uat-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
    .board-ui-next { margin: 6px 0; font-weight: 600; }
"""
