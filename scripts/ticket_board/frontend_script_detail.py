"""Ticket-detail rendering JavaScript for the board frontend."""

SCRIPT_DETAIL = """    function selectedTicket() {
      return state.tickets.find((ticket) => ticket.id === state.selectedId) || null;
    }

    // The ticket's open reminder snooze in one sentence, or '' (SYRD-537).
    function reminderSnoozeText(snooze) {
      if (!snooze) {
        return '';
      }
      const setBy = `batch ${snooze.batch}, by ${snooze.by}: ${snooze.reason}`;
      if (snooze.status === 'invalidated') {
        return `Reminders: snooze no longer applies, ${(snooze.changed || []).join(', ')} changed (${setBy})`;
      }
      if (snooze.status === 'due') {
        return `Reminders: snooze due at ${snooze.until}, queue-ready notice pending (${setBy})`;
      }
      return `Reminders: snoozed until ${snooze.until} (${setBy})`;
    }

    // The candidate's size evidence in one sentence, or '' (SYRD-541): what the
    // board measured, what is unresolved, and what was approved.
    // SYRD-113: root's record of each privileged job asked for this ticket, one line each.
    function privilegedJobLines(jobs) {
      const said = {running: 'running', succeeded: 'succeeded', failed: 'failed', timed_out: 'timed out', rolled_back: 'rolled back'};
      return (jobs || []).map((job) => `Privileged job ${job.job_id}: ${job.action} ${said[job.state] || job.state}` +
        (job.exit_status === null || job.exit_status === undefined ? '' : ` (exit ${job.exit_status})`) +
        (job.requester_role ? `, asked by ${job.requester_role}` : '') +
        (job.state === 'running' ? '' : ` - ${job.record_dir}, stdout sha256 ${((job.artifacts || {})['stdout.log'] || '?').slice(0, 12)}`));
    }

    function sizeReviewText(review) {
      if (!review) {
        return '';
      }
      const open = (review.findings || []).filter((finding) => finding.state === 'open');
      if (open.length) {
        return `Size review: unresolved for ${(review.candidate || '').slice(0, 12)} - ` + open.map((finding) =>
          finding.reason === 'scan_failed'
            ? `scan failed: ${finding.detail}`
            : `${finding.path} ${finding.before} -> ${finding.after} lines (${finding.reason})`).join('; ');
      }
      const files = (review.files || []).map((file) => `${file.path} ${file.before} -> ${file.after}`);
      const excepted = (review.findings || []).filter((finding) => finding.state === 'excepted').length;
      return `Size review: no unresolved growth${excepted ? ` (${excepted} excepted)` : ''}` +
        (files.length ? ` - at or over 1,100 lines: ${files.join(', ')}` : '');
    }

    // Where the ticket stands in its implementer's queue, or '' (SYRD-568).
    function serialQueueText(queue) {
      if (!queue) {
        return '';
      }
      if (queue.active) {
        return `Queue: ${queue.implementer}'s active ticket`;
      }
      const waiting = (queue.waiting_on || []).length
        ? `, blocked by ${queue.waiting_on.join(', ')}`
        : '';
      return `Queue: #${queue.position} in ${queue.implementer}'s queue, behind ${queue.active_ticket || 'nothing active'}${waiting}`;
    }

    function renderDetail() {
      const ticket = selectedTicket();
      if (!state.detailOpen || !ticket) {
        detailContentEl.innerHTML = '<div class="meta">No ticket selected.</div>';
        syncDetailOverlay();
        return;
      }

      detailContentEl.innerHTML = '';

      const box = document.createElement('div');
      box.className = 'detail-box';
      const draftFields = new Map();
      // SYRD-598: the board page acts as the User and nobody else. A Director
      // operation is the User's, confirmed, under a grant the server issues.
      const commentWho = { value: 'user' };
      const commentAuthor = document.createElement('span');
      commentAuthor.className = 'soft-note';
      commentAuthor.setAttribute('data-comment-author', 'user');
      commentAuthor.textContent = 'Commenting as User';
      const commentText = document.createElement('textarea');
      commentText.placeholder = 'Add a comment or bounce-back note';
      commentText.value = readPersistentCommentDraft(ticket.id);
      commentText.addEventListener('input', () => {
        writePersistentCommentDraft(ticket.id, commentText.value);
      });
      bindDetailDraftField(draftFields, commentText, 'commentText', '');
      const commentUrgentLabel = document.createElement('label');
      commentUrgentLabel.className = 'urgent-comment-toggle';
      const commentUrgent = document.createElement('input');
      commentUrgent.type = 'checkbox';
      const commentUrgentText = document.createElement('span');
      commentUrgentText.textContent = 'Urgent';
      commentUrgentLabel.append(commentUrgent, commentUrgentText);
      bindDetailDraftField(draftFields, commentUrgent, 'commentUrgent', 'false');
      const detailCallerRole = () => commentWho.value;
      const updateDetailTicket = async (patch, callerRole = null) => {
        try {
          await updateTicket(ticket.id, patch, callerRole || detailCallerRole());
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      };
      const updateDetailTicketForToggle = async (patch, callerRole = null) => {
        try {
          await updateTicket(ticket.id, patch, callerRole || detailCallerRole());
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
          throw error;
        }
      };

      // SYRD-629: what the User needs to do, before anything else on the ticket.
      const uatPanel = renderUatPanel(ticket, draftFields);
      if (uatPanel) {
        box.appendChild(uatPanel);
      }

      const detailAlerts = renderAlertStack(ticket, { detail: true });
      if (detailAlerts) {
        box.appendChild(detailAlerts);
      }

      const controls = document.createElement('div');
      controls.className = 'detail-grid';

      const workflowActions = document.createElement('div');
      workflowActions.className = 'inline-actions';
      // SYRD-598: what may be done for this ticket now, as the server describes it
      // from the live workflow: the User's own actions, and the Director
      // operations the board lends to a confirmed board-UI action.
      void renderBoardUiActions(ticket, workflowActions);

      const assigneeLabel = document.createElement('div');
      assigneeLabel.innerHTML = '<span class="field-label">Assignee</span>';
      const assigneeValue = document.createElement('span');
      assigneeValue.setAttribute('data-detail-assignee', ticket.assignee);
      assigneeValue.textContent = roleLabel(ticket.assignee);
      assigneeLabel.appendChild(assigneeValue);

      // SYRD-93: what this ticket is waiting on, when it is waiting to be
      // published. The implementer sees that its ask is recorded; the control
      // role sees exactly what it is being asked to validate, and can refuse it
      // here with a reason. Publishing itself is not a browser action: it needs
      // a credential this page has no way to reach, by design.
      const publicationField = document.createElement('div');
      publicationField.className = 'publication-request';
      publicationField.setAttribute('data-publication', '');
      const publication = (ticket && ticket.publication) || {};
      if (publication && publication.id) {
        publicationField.innerHTML = '<div class="field-label">Publication requested</div>';
        const summary = document.createElement('div');
        summary.setAttribute('data-publication-summary', '');
        summary.textContent =
          publication.requested_by +
          ' asks to publish ' +
          publication.ref +
          ' at ' +
          String(publication.commit_hash || '').slice(0, 12) +
          ' (bundle ' + publication.bundle_path + ')';
        publicationField.appendChild(summary);
        const how = document.createElement('div');
        how.className = 'muted';
        how.setAttribute('data-publication-command', '');
        how.textContent = 'Control role: switchyard-publish ' + ticket.id;
        publicationField.appendChild(how);
      }

      const parentLinkField = document.createElement('div');
      parentLinkField.innerHTML = '<div class="field-label">Parent Ticket</div>';
      const parentLinkInput = document.createElement('input');
      parentLinkInput.type = 'text';
      parentLinkInput.value = ticket.parent_id || '';
      parentLinkInput.placeholder = `${BOARD_IDENTITY.ticketPrefix}-123`;
      bindDetailDraftField(draftFields, parentLinkInput, 'parentId', parentLinkInput.value);
      const parentLinkActions = document.createElement('div');
      parentLinkActions.className = 'inline-actions';
      const saveParentLinkButton = document.createElement('button');
      saveParentLinkButton.textContent = 'Save Parent Link';
      saveParentLinkButton.addEventListener('click', async () => {
        await updateDetailTicket({ parent_id: parentLinkInput.value.trim().toUpperCase() });
      });
      parentLinkActions.appendChild(saveParentLinkButton);
      if (ticket.parent_id) {
        const clearParentLinkButton = document.createElement('button');
        clearParentLinkButton.textContent = 'Clear Parent Link';
        clearParentLinkButton.addEventListener('click', async () => {
          parentLinkInput.value = '';
          await updateDetailTicket({ parent_id: '' });
        });
        parentLinkActions.appendChild(clearParentLinkButton);
      }
      parentLinkField.append(parentLinkInput, parentLinkActions);
      if (ticket.parent_id) {
        const parentPreview = document.createElement('div');
        parentPreview.className = 'field-preview';
        const parentPreviewLabel = document.createElement('div');
        parentPreviewLabel.className = 'field-preview-label';
        parentPreviewLabel.textContent = 'Current Parent';
        parentPreview.append(parentPreviewLabel, linkedTicketRow([ticket.parent_id]));
        parentLinkField.appendChild(parentPreview);
      }

      controls.append(assigneeLabel, parentLinkField, publicationField);

      const toggles = document.createElement('div');
      toggles.className = 'tag-row';
      if (!state.workflow) {
      toggles.appendChild(toggleControl('Requires UAT', ticket.needs_user_signoff, async (checked) => {
        await updateDetailTicketForToggle({ needs_user_signoff: checked });
      }));
      }
      toggles.appendChild(toggleControl('Regression', ticket.regression, async (checked) => {
        await updateDetailTicketForToggle({ regression: checked });
      }));
      toggles.appendChild(toggleControl('Manual control', ticket.manually_controlled, async (checked) => {
        await runBoardUiOperation(ticket, 'set_manually_controlled', { manually_controlled: checked });
      }, {
        title: 'A Director operation: hold or release this ticket, done as you after you confirm it.',
      }));
      if (!state.workflow) {
      toggles.appendChild(toggleControl('UAT sign-off', ticket.user_signoff, async (checked) => {
        const patch = { user_signoff: checked };
        if (checked && commentText.value.trim()) {
          patch.comment = { who: 'user', text: commentText.value.trim() };
        }
        await updateDetailTicketForToggle(patch, 'user');
      }, {
        disabled: !ticket.needs_user_signoff,
        title: ticket.needs_user_signoff ? '' : 'Ticket does not require UAT sign-off.',
      }));

      }
      const meta = document.createElement('div');
      meta.className = 'meta';
      const titleField = document.createElement('div');
      titleField.innerHTML = '<div class="field-label">Title</div>';
      const titleEditInput = document.createElement('input');
      titleEditInput.type = 'text';
      titleEditInput.value = ticket.title;
      titleEditInput.placeholder = 'Short issue title';
      bindDetailDraftField(draftFields, titleEditInput, 'title', titleEditInput.value);
      const titleActions = document.createElement('div');
      titleActions.className = 'inline-actions';
      const saveTitleButton = document.createElement('button');
      saveTitleButton.textContent = 'Save Title';
      saveTitleButton.addEventListener('click', async () => {
        await updateDetailTicket({ title: titleEditInput.value });
      });
      titleActions.appendChild(saveTitleButton);
      const mergeTargetInput = document.createElement('input');
      mergeTargetInput.type = 'text';
      mergeTargetInput.placeholder = `${BOARD_IDENTITY.ticketPrefix}-123`;
      mergeTargetInput.setAttribute('aria-label', 'Merge target ticket');
      bindDetailDraftField(draftFields, mergeTargetInput, 'mergeTarget', '');
      const mergeButton = document.createElement('button');
      mergeButton.textContent = 'Merge Into…';
      mergeButton.addEventListener('click', async () => {
        try {
          const targetId = mergeTargetInput.value.trim().toUpperCase();
          if (!targetId) {
            throw new Error('merge requires a target ticket ID');
          }
          await runBoardUiOperation(ticket, 'merge', { target_id: targetId });
        } catch (error) {
          // reported by the confirmation flow
        }
      });
      titleActions.append(mergeTargetInput, mergeButton);
      titleField.append(titleEditInput, titleActions);
      const metaLine1 = document.createElement('div');
      metaLine1.className = 'detail-ticket-headline';
      const strong = document.createElement('strong');
      strong.textContent = ticket.id;
      const stageChip = document.createElement('span');
      stageChip.className = `detail-stage-chip detail-stage-${ticket.state}`;
      stageChip.textContent = stateLabel(ticket.state);
      const titleText = document.createElement('span');
      titleText.textContent = ticket.title;
      metaLine1.append(strong, stageChip, titleText);
      const metaLine2 = document.createElement('div');
      metaLine2.textContent = `Created: ${formatWhen(ticket.created)} | Updated: ${formatWhen(ticket.updated)}`;
      meta.append(metaLine1, metaLine2);
      // Why the owner's notice has not arrived, where hover cannot reach
      // (SYRD-266): the card itself carries only the highlight.
      const deliveryHint = activeWorkDeliveryHint(ticket);
      if (deliveryHint) {
        const deliveryLine = document.createElement('div');
        deliveryLine.className = 'detail-delivery';
        deliveryLine.textContent = `Notice: ${deliveryHint}`;
        meta.appendChild(deliveryLine);
      }
      // SYRD-537: a reminder snooze is scheduling, so it is said here and not
      // on the card (SYRD-266), and it says nothing about delivery.
      const queueText = serialQueueText(ticket.serial_queue);
      if (queueText) {
        const queueLine = document.createElement('div');
        queueLine.className = 'detail-serial-queue';
        queueLine.textContent = queueText;
        meta.appendChild(queueLine);
      }
      const sizeText = sizeReviewText(ticket.size_review);
      if (sizeText) {
        const sizeLine = document.createElement('div');
        sizeLine.className = 'detail-size-review';
        sizeLine.textContent = sizeText;
        meta.appendChild(sizeLine);
      }
      for (const jobText of privilegedJobLines(ticket.privileged_jobs)) {
        const jobLine = document.createElement('div');
        jobLine.className = 'detail-privileged-job';
        jobLine.textContent = jobText;
        meta.appendChild(jobLine);
      }
      const snoozeText = reminderSnoozeText(ticket.reminder_snooze);
      if (snoozeText) {
        const snoozeLine = document.createElement('div');
        snoozeLine.className = 'detail-reminder-snooze';
        snoozeLine.textContent = snoozeText;
        meta.appendChild(snoozeLine);
      }
      if (ticket.origin_project || ticket.external_source_ref) {
        const originLine = document.createElement('div');
        const originParts = [];
        if (ticket.origin_project) {
          originParts.push(`Origin: ${ticket.origin_project}`);
        }
        if (ticket.external_source_ref) {
          originParts.push(`External Source: ${ticket.external_source_ref}`);
        }
        originLine.textContent = originParts.join(' | ');
        meta.appendChild(originLine);
      }
      const visibleBlockedBy = unresolvedBlockedBy(ticket);
      if (visibleBlockedBy.length) {
        const metaLine3 = document.createElement('div');
        metaLine3.appendChild(document.createTextNode('Blocked By: '));
        metaLine3.appendChild(linkedTicketRow(visibleBlockedBy));
        meta.appendChild(metaLine3);
      }

      const linkedTickets = document.createElement('div');
      linkedTickets.innerHTML = '<div class="field-label">Linked Tickets</div>';
      if (ticket.parent_id) {
        const parentRow = document.createElement('div');
        parentRow.className = 'field-preview';
        const parentLabel = document.createElement('div');
        parentLabel.className = 'field-preview-label';
        parentLabel.textContent = 'Parent';
        parentRow.append(parentLabel, linkedTicketRow([ticket.parent_id]));
        linkedTickets.appendChild(parentRow);
      }
      const childTickets = childTicketsForBoard(ticket);
      if (childTickets.length) {
        linkedTickets.appendChild(buildChildTicketList(childTickets));
      } else if (!ticket.parent_id) {
        const linkedEmpty = document.createElement('div');
        linkedEmpty.className = 'soft-note';
        linkedEmpty.textContent = 'No linked children.';
        linkedTickets.appendChild(linkedEmpty);
      }

      const body = document.createElement('div');
      body.innerHTML = '<div class="field-label">Body</div>';
      body.appendChild(linkedTextBlock(ticket.body, '(no body)'));

      const blockedBy = document.createElement('div');
      blockedBy.innerHTML = '<div class="field-label">Blocked By</div>';
      const blockedByActions = document.createElement('div');
      blockedByActions.className = 'inline-actions';
      const blockerPickerLabel = document.createElement('div');
      blockerPickerLabel.className = 'soft-note';
      blockerPickerLabel.textContent = 'Add blocker';
      const blockerPicker = document.createElement('select');
      buildOption(blockerPicker, '', 'Choose a non-terminal ticket…');
      availableBlockerTickets(ticket).forEach((candidate) => {
        buildOption(blockerPicker, candidate.id, `${candidate.id} - ${candidate.title}`);
      });
      const addBlockedByButton = document.createElement('button');
      addBlockedByButton.type = 'button';
      addBlockedByButton.textContent = 'Add Blocker';
      addBlockedByButton.disabled = blockerPicker.options.length <= 1;
      addBlockedByButton.addEventListener('click', async () => {
        const blockerId = blockerPicker.value.trim().toUpperCase();
        if (!blockerId) {
          return;
        }
        const nextBlockedBy = Array.from(new Set([...(ticket.blocked_by || []), blockerId]));
        const blockedReasonValue = blockedReasonInput.value.trim() || `Waiting on ${blockerId}.`;
        blockedReasonInput.value = blockedReasonValue;
        try {
          await runBoardUiOperation(ticket, 'set_blockers', { blocked_by: nextBlockedBy, blocked_reason: blockedReasonValue });
        } catch (error) {
          // reported by the confirmation flow
        }
      });
      blockedByActions.append(blockerPicker, addBlockedByButton);
      const blockedByNote = document.createElement('div');
      blockedByNote.className = 'soft-note';
      blockedByNote.textContent = `${blockedBySummary(ticket)} Only non-terminal tickets are selectable as blockers.`;
      const blockedByLinks = document.createElement('div');
      blockedByLinks.className = 'field-preview';
      const blockedByLinksLabel = document.createElement('div');
      blockedByLinksLabel.className = 'field-preview-label';
      blockedByLinksLabel.textContent = 'Linked Tickets';
      blockedByLinks.append(blockedByLinksLabel, linkedTicketRow(visibleBlockedBy));
      if (visibleBlockedBy.length) {
        const removeBlockedByActions = document.createElement('div');
        removeBlockedByActions.className = 'inline-actions';
        visibleBlockedBy.forEach((blockerId) => {
          const removeBlockedByButton = document.createElement('button');
          removeBlockedByButton.type = 'button';
          removeBlockedByButton.textContent = `Remove Blocker ${blockerId}`;
          removeBlockedByButton.addEventListener('click', async () => {
            const nextBlockedBy = (ticket.blocked_by || []).filter((item) => item !== blockerId);
            try {
              // The reason stays while anything is still awaited, and goes with the last blocker:
              // the board refuses a reason with nothing to wait on (SYRD-285).
              const remainingReason = nextBlockedBy.length === 0 ? '' : (ticket.blocked_reason || '');
              await runBoardUiOperation(ticket, 'set_blockers', {
                blocked_by: nextBlockedBy,
                blocked_reason: remainingReason,
              });
            } catch (error) {
              // reported by the confirmation flow
            }
          });
          removeBlockedByActions.appendChild(removeBlockedByButton);
        });
        blockedByLinks.appendChild(removeBlockedByActions);
      }
      blockedBy.append(blockerPickerLabel, blockedByActions, blockedByNote, blockedByLinks);

      const blockedReason = document.createElement('div');
      blockedReason.innerHTML = '<div class="field-label">Blocked Reason</div>';
      const blockedReasonInput = document.createElement('textarea');
      blockedReasonInput.className = 'compact-textarea';
      blockedReasonInput.value = ticket.blocked_reason || '';
      blockedReasonInput.placeholder = 'Why this ticket is blocked right now.';
      bindDetailDraftField(draftFields, blockedReasonInput, 'blockedReason', blockedReasonInput.value);
      const blockedReasonActions = document.createElement('div');
      blockedReasonActions.className = 'inline-actions';
      const saveBlockedReasonButton = document.createElement('button');
      saveBlockedReasonButton.textContent = 'Save Blocked Reason';
      saveBlockedReasonButton.addEventListener('click', async () => {
        try {
          await runBoardUiOperation(ticket, 'set_blockers', {
            blocked_by: ticket.blocked_by || [], blocked_reason: blockedReasonInput.value,
          });
        } catch (error) {
          // reported by the confirmation flow
        }
      });
      blockedReasonActions.appendChild(saveBlockedReasonButton);
      const blockedReasonNote = document.createElement('div');
      blockedReasonNote.className = 'soft-note';
      blockedReasonNote.textContent = 'Required when blocked-by dependencies are set. Also use this for non-dependency stalls.';
      blockedReason.append(blockedReasonInput, blockedReasonActions, blockedReasonNote);

      const implementation = document.createElement('div');
      implementation.innerHTML = '<div class="field-label">Implementation</div>';
      const implementationInput = document.createElement('textarea');
      implementationInput.value = ticket.implementation || '';
      implementationInput.placeholder = 'Director-authored implementation package/spec for the implementer at in_progress.';
      bindDetailDraftField(draftFields, implementationInput, 'implementation', implementationInput.value);
      const implementationActions = document.createElement('div');
      implementationActions.className = 'inline-actions';
      const saveImplementationButton = document.createElement('button');
      saveImplementationButton.textContent = 'Save Implementation';
      saveImplementationButton.addEventListener('click', async () => {
        await updateDetailTicket({ implementation: implementationInput.value });
      });
      implementationActions.appendChild(saveImplementationButton);
      implementation.append(
        implementationInput,
        implementationActions,
        linkedPreview('Rendered Preview', ticket.implementation, '(no implementation yet)'),
      );

      const auditPrompt = document.createElement('div');
      auditPrompt.innerHTML = '<div class="field-label">Audit Notes</div>';
      const auditPromptInput = document.createElement('textarea');
      auditPromptInput.value = ticket.audit_prompt || '';
      auditPromptInput.placeholder = 'Optional notes for audit or review context.';
      bindDetailDraftField(draftFields, auditPromptInput, 'auditPrompt', auditPromptInput.value);
      const auditPromptActions = document.createElement('div');
      auditPromptActions.className = 'inline-actions';
      const saveAuditPromptButton = document.createElement('button');
      saveAuditPromptButton.textContent = 'Save Audit Notes';
      saveAuditPromptButton.addEventListener('click', async () => {
        await updateDetailTicket({ audit_prompt: auditPromptInput.value });
      });
      auditPromptActions.appendChild(saveAuditPromptButton);
      auditPrompt.append(
        auditPromptInput,
        auditPromptActions,
        linkedPreview('Rendered Preview', ticket.audit_prompt, '(no audit notes yet)'),
      );

      const commitInfo = document.createElement('div');
      commitInfo.innerHTML = '<div class="field-label">Commit Hash</div>';
      const commitHashInput = document.createElement('input');
      commitHashInput.type = 'text';
      commitHashInput.value = ticket.commit_hash || '';
      commitHashInput.placeholder = 'Required before done unless exempt';
      commitHashInput.readOnly = true;  // the implementer records it with its submission
      bindDetailDraftField(draftFields, commitHashInput, 'commitHash', commitHashInput.value);
      commitInfo.append(commitHashInput);
      const commitNote = document.createElement('div');
      commitNote.className = 'soft-note';
      commitNote.textContent = ticket.commit_exempt
        ? 'Done-state commit check is bypassed for this ticket.'
        : 'A verified git commit is required before moving this ticket to done.';
      commitInfo.append(commitNote);

      const attachmentUpload = document.createElement('div');
      attachmentUpload.className = 'attach-panel detail-attach-panel';
      const detailImageInput = document.createElement('input');
      detailImageInput.className = 'visually-hidden';
      detailImageInput.type = 'file';
      detailImageInput.accept = 'image/*';
      detailImageInput.multiple = true;
      const detailSetGrid = document.createElement('div');
      detailSetGrid.className = 'attach-set-grid';
      const detailSetLabel = document.createElement('label');
      detailSetLabel.textContent = 'Set';
      const detailSetSelect = document.createElement('select');
      detailSetSelect.dataset.attachSet = 'true';
      [
        ['', 'Ungrouped'],
        ['target', 'Target'],
        ['attempt', 'Attempt'],
        ['feedback', 'Feedback'],
      ].forEach(([value, label]) => buildOption(detailSetSelect, value, label));
      detailSetLabel.appendChild(detailSetSelect);
      const detailAttemptLabel = document.createElement('label');
      detailAttemptLabel.textContent = 'Set #';
      const detailAttemptInput = document.createElement('input');
      detailAttemptInput.dataset.attachAttempt = 'true';
      detailAttemptInput.type = 'number';
      detailAttemptInput.min = '1';
      detailAttemptInput.max = '999';
      detailAttemptInput.placeholder = '3';
      detailAttemptLabel.appendChild(detailAttemptInput);
      const detailLabelLabel = document.createElement('label');
      detailLabelLabel.textContent = 'Label';
      const detailLabelInput = document.createElement('input');
      detailLabelInput.dataset.attachLabel = 'true';
      detailLabelInput.type = 'text';
      detailLabelInput.placeholder = 'uat rework, zoom-in, feedback';
      detailLabelLabel.appendChild(detailLabelInput);
      detailSetGrid.append(detailSetLabel, detailAttemptLabel, detailLabelLabel);
      const detailAttachButton = document.createElement('button');
      detailAttachButton.type = 'button';
      detailAttachButton.textContent = 'Attach image';
      const attachmentHelp = document.createElement('div');
      attachmentHelp.className = 'attach-help';
      attachmentHelp.textContent = 'Choose image files, drag them here, or paste while editing this ticket.';
      detailAttachButton.addEventListener('click', () => {
        detailImageInput.click();
      });
      detailImageInput.addEventListener('change', async () => {
        try {
          await attachImageFiles(detailImageInput.files, 'detail', readAttachmentSetOptions(attachmentUpload));
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        } finally {
          detailImageInput.value = '';
        }
      });
      attachmentUpload.append(detailImageInput, detailSetGrid, detailAttachButton, attachmentHelp);
      wireImageDropZone(attachmentUpload, 'detail', () => readAttachmentSetOptions(attachmentUpload));

      box.append(
        titleField,
        meta,
        workflowActions,
        controls,
        toggles,
        linkedTickets,
        blockedReason,
        body,
        blockedBy,
        implementation,
        auditPrompt,
        commitInfo,
        attachmentUpload,
      );

      if (ticketScreenshotEntries(ticket).length) {
        const imageWrap = document.createElement('div');
        imageWrap.innerHTML = '<div class="field-label">Attachments</div>';
        const entries = ticketScreenshotEntries(ticket).map((entry) => ({
          ...entry,
          label: screenshotLabelFor(entry.path),
        }));
        const groups = document.createElement('div');
        groups.className = 'attachment-set-list';
        renderAttachmentSetGroups(groups, entries, 'Remove attachment', async (path) => {
          await updateDetailTicket({
            screenshots: ticketScreenshotPaths(ticket).filter((item) => item !== path),
          });
        }, (entry) => {
          openImageLightbox(entry, ticket);
        });
        imageWrap.appendChild(groups);
        box.appendChild(imageWrap);
      }

      const comments = document.createElement('div');
      comments.innerHTML = '<div class="field-label">Comments</div>';
      const commentComposer = document.createElement('div');
      commentComposer.className = 'comment-composer';
      const commentActions = document.createElement('div');
      commentActions.className = 'inline-actions';
      const addCommentButton = document.createElement('button');
      addCommentButton.textContent = 'Add Comment';
      addCommentButton.addEventListener('click', async () => {
        try {
          await submitComment(ticket.id, commentWho.value, commentText.value, null, commentUrgent.checked);
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      });
      commentActions.appendChild(addCommentButton);
      if (!state.workflow) {
      if (ticket.state === 'draft') {
        const releaseButton = document.createElement('button');
        releaseButton.textContent = 'Release to Triage';
        releaseButton.addEventListener('click', async () => {
          try {
            await submitComment(ticket.id, commentWho.value, commentText.value, 'analysis');
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        commentActions.appendChild(releaseButton);
      }
      }
      commentComposer.append(commentAuthor, commentText, commentUrgentLabel, commentActions);
      comments.appendChild(commentComposer);
      if (ticket.comments.length) {
        const list = document.createElement('div');
        list.className = 'comment-list';
        const sortedComments = ticket.comments
          .map((comment, index) => ({ comment, index }))
          .sort((left, right) => {
            const leftTime = Date.parse(left.comment.ts || '');
            const rightTime = Date.parse(right.comment.ts || '');
            const leftValid = Number.isFinite(leftTime);
            const rightValid = Number.isFinite(rightTime);
            if (leftValid && rightValid && leftTime !== rightTime) {
              return rightTime - leftTime;
            }
            if (leftValid !== rightValid) {
              return leftValid ? -1 : 1;
            }
            return right.index - left.index;
          });
        sortedComments.forEach(({ comment }) => {
          const item = document.createElement('div');
          item.className = 'comment';
          const header = document.createElement('div');
          const who = document.createElement('strong');
          who.textContent = comment.who;
          // SYRD-677: a reporting tenant's follow-up on its own report, not one of this board's roles.
          const reporter = /^reporter:(.+)$/.exec(String(comment.who || ''));
          if (reporter) {
            item.classList.add('comment-external-reporter');
            item.setAttribute('data-comment-origin', 'external-reporter');
            who.textContent = `Reporter (${reporter[1]}, external)`;
          }
          const ts = document.createElement('span');
          ts.className = 'meta';
          ts.textContent = ` ${formatWhen(comment.ts)}`;
          header.append(who);
          if (comment.urgent) {
            const urgent = document.createElement('span');
            urgent.className = 'comment-urgent-marker';
            urgent.textContent = 'URGENT';
            header.appendChild(urgent);
          }
          header.append(ts);
          const text = document.createElement('div');
          text.className = 'body-text linked-text';
          appendLinkedTicketText(text, comment.text);
          item.append(header, text);
          list.appendChild(item);
        });
        comments.appendChild(list);
      } else {
        const empty = document.createElement('div');
        empty.className = 'meta';
        empty.textContent = 'No comments.';
        comments.appendChild(empty);
      }
      box.appendChild(comments);

      detailContentEl.appendChild(box);
      restoreDetailDraft(ticket.id, draftFields);
      syncDetailOverlay();
    }
"""
