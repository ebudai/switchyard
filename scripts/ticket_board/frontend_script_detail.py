"""Ticket-detail rendering JavaScript for the board frontend."""

SCRIPT_DETAIL = """    function selectedTicket() {
      return state.tickets.find((ticket) => ticket.id === state.selectedId) || null;
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
      const commentWho = document.createElement('select');
      commentWho.setAttribute('aria-label', 'Comment author');
      const commentAuthorRoles = state.callerRoles.length
        ? state.callerRoles
        : ['director', 'main', 'app', 'ops', 'perf', 'audit', 'inspector', 'research', 'user'];
      commentAuthorRoles.forEach((role) => {
        buildOption(commentWho, role, roleLabel(role));
      });
      commentWho.value = state.workflow && ticket.assignee !== 'unassigned' ? ticket.assignee : (ticketIsUserReview(ticket) ? 'user' : 'director');
      bindDetailDraftField(draftFields, commentWho, 'commentWho', commentWho.value);
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

      if (ticketIsUserReview(ticket)) {
        const signoffRecorded = !!ticket.user_signoff;
        const userBanner = document.createElement('div');
        userBanner.className = signoffRecorded ? 'user-banner user-banner-confirmed' : 'user-banner';
        const userBannerSubtitle = document.createElement('div');
        userBannerSubtitle.className = 'user-banner-subtitle';
        userBannerSubtitle.textContent = signoffRecorded ? 'Signed Off ✓' : 'Awaiting UAT sign-off';
        const userBannerTitle = document.createElement('div');
        userBannerTitle.className = 'user-banner-title';
        userBannerTitle.textContent = ticket.title;
        const userBannerNote = document.createElement('div');
        userBannerNote.className = 'user-banner-note';
        userBannerNote.textContent = signoffRecorded
          ? 'UAT sign-off recorded. Waiting for director completion.'
          : userReviewCheckText(ticket);
        const userSummary = document.createElement('div');
        userSummary.className = 'user-summary';
        const userSummaryHead = document.createElement('div');
        userSummaryHead.className = 'user-summary-head';
        userSummaryHead.textContent = signoffRecorded ? 'Signed-off UAT Snapshot' : 'UAT Check Before Sign-off';
        const userSummaryStatuses = document.createElement('div');
        userSummaryStatuses.className = 'user-summary-statuses';
        userReviewStatusItems(ticket).forEach((item) => {
          const row = document.createElement('div');
          row.className = 'user-summary-status';
          const strong = document.createElement('strong');
          strong.textContent = item.label;
          const value = document.createElement('span');
          value.className = item.ok ? 'user-summary-status-ok' : 'user-summary-status-missing';
          value.textContent = item.ok ? (item.okText || 'ready') : (item.missingText || 'missing');
          row.append(strong, value);
          userSummaryStatuses.appendChild(row);
        });
        const userSummarySections = document.createElement('div');
        userSummarySections.className = 'user-summary-sections';
        userReviewSummarySections(ticket).forEach((section) => {
          const sectionEl = document.createElement('div');
          sectionEl.className = 'user-summary-section';
          const label = document.createElement('div');
          label.className = 'user-summary-label';
          label.textContent = section.label;
          sectionEl.appendChild(label);
          if (section.checklist) {
            const list = document.createElement('ul');
            list.className = 'user-summary-list';
            section.checklist.forEach((item) => {
              const entry = document.createElement('li');
              entry.textContent = item;
              list.appendChild(entry);
            });
            sectionEl.appendChild(list);
          } else if (section.summary) {
            const text = document.createElement('div');
            text.className = 'user-summary-text';
            text.textContent = section.summary;
            sectionEl.appendChild(text);
          }
          userSummarySections.appendChild(sectionEl);
        });
        userSummary.append(userSummaryHead, userSummaryStatuses, userSummarySections);
        if (signoffRecorded) {
          const confirmation = document.createElement('div');
          confirmation.className = 'user-signoff-confirmation';
          confirmation.textContent = 'Signed off ✓ Waiting for director completion.';
          userSummary.appendChild(confirmation);
        }
        const userBannerActions = document.createElement('div');
        userBannerActions.className = 'inline-actions';
        if (signoffRecorded) {
          const signedOffState = document.createElement('div');
          signedOffState.className = 'signoff-state-chip';
          signedOffState.textContent = 'Signed Off ✓';
          userBannerActions.appendChild(signedOffState);
        } else {
          const userBannerSignoffButton = document.createElement('button');
          userBannerSignoffButton.className = 'primary';
          userBannerSignoffButton.type = 'button';
          userBannerSignoffButton.textContent = 'Sign Off';
          userBannerSignoffButton.addEventListener('click', async () => {
            try {
              await submitUserSignoff(ticket.id, commentWho.value, commentText.value);
            } catch (error) {
              setCreateStatus(error.message, true);
              await requestBoardReload();
            }
          });
          userBannerActions.appendChild(userBannerSignoffButton);
        }
        userBanner.append(userBannerSubtitle, userBannerTitle, userBannerNote, userSummary, userBannerActions);
        box.appendChild(userBanner);
      }

      const detailAlerts = renderAlertStack(ticket, { detail: true });
      if (detailAlerts) {
        box.appendChild(detailAlerts);
      }

      const controls = document.createElement('div');
      controls.className = 'detail-grid';

      const workflowActions = document.createElement('div');
      workflowActions.className = 'inline-actions';
      if (state.workflow) {
        for (const [name, spec] of Object.entries(state.workflow.flags || {})) {
          if (spec.kind !== 'gate') continue;
          const label = document.createElement('label');
          const input = document.createElement('input'); input.type = 'checkbox';
          input.checked = ticket[name] ?? ticket.workflow_flags?.[name] ?? spec.default;
          const updateGateEnabled = () => { input.disabled = commentWho.value !== 'director'; };
          updateGateEnabled(); commentWho.addEventListener('change', updateGateEnabled);
          label.append(input, document.createTextNode(name.replaceAll('_', ' ')));
          input.addEventListener('change', async () => {
            try {
              await postTicketAction(`/api/tickets/${encodeURIComponent(ticket.id)}/actions/set_workflow_flags`, {[name]: input.checked}, commentWho.value);
              await requestBoardReload();
            } catch (error) { input.checked = !input.checked; setCreateStatus(error.message, true); }
          });
          workflowActions.appendChild(label);
        }
        for (const action of ticket.workflow_actions || []) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = action.label;
          const updateEnabled = () => {
            button.disabled = !action.actors.includes(commentWho.value) || (action.owner_scoped && ticket.assignee !== commentWho.value);
            button.title = `Allowed: ${action.actors.join(', ')}`;
          };
          updateEnabled();
          commentWho.addEventListener('change', updateEnabled);
          button.addEventListener('click', async () => {
            try {
              const payload = { target: action.to };
              if (commentText.value.trim()) payload.text = commentText.value.trim();
              if (action.require_reason && !payload.text) throw new Error('Enter the reason in the comment box.');
              if (commitHashInput.value.trim()) payload.commit_hash = commitHashInput.value.trim();
              if (action.require_commit && !ticket.commit_exempt && !payload.commit_hash) throw new Error('Enter the implementation commit.');
              await postTicketAction(`/api/tickets/${encodeURIComponent(ticket.id)}/actions/${encodeURIComponent(action.action)}`, payload, commentWho.value);
              await requestBoardReload();
            } catch (error) { setCreateStatus(error.message, true); }
          });
          workflowActions.appendChild(button);
        }
      }
      const detailAdvanceState = state.workflow ? null : defaultAdvanceState(ticket);
      if (detailAdvanceState) {
        const advanceButton = document.createElement('button');
        advanceButton.className = 'primary';
        advanceButton.type = 'button';
        advanceButton.textContent = `Advance -> ${stateLabel(detailAdvanceState)}`;
        const blockedReason = advanceBlockedReason(ticket);
        if (blockedReason) {
          advanceButton.disabled = true;
          advanceButton.title = blockedReason;
        }
        advanceButton.addEventListener('click', async () => {
          try {
            await advanceTicket(ticket.id, commentText.value);
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        workflowActions.appendChild(advanceButton);
      }
      if (!state.workflow && ticket.state !== 'done' && ticket.state !== 'cancelled') {
        const cancelButton = document.createElement('button');
        cancelButton.type = 'button';
        cancelButton.textContent = 'Cancel Ticket';
        cancelButton.title = 'Requires a cancellation reason in the comment box below.';
        cancelButton.addEventListener('click', async () => {
          try {
            await cancelTicket(ticket.id, cancelReason(commentText.value));
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        workflowActions.appendChild(cancelButton);
      }

      const assigneeLabel = document.createElement('label');
      assigneeLabel.innerHTML = '<span class="field-label">Assignee</span>';
      const assigneeSelect = document.createElement('select');
      state.assignees.forEach((assignee) => buildOption(assigneeSelect, assignee, roleLabel(assignee)));
      assigneeSelect.value = ticket.assignee;
      assigneeSelect.addEventListener('change', async () => {
        await updateDetailTicket({ assignee: assigneeSelect.value }, 'director');
      });
      assigneeLabel.appendChild(assigneeSelect);

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

      controls.append(assigneeLabel, parentLinkField);

      const toggles = document.createElement('div');
      toggles.className = 'tag-row';
      if (!state.workflow) {
      toggles.appendChild(toggleControl('Requires UAT', ticket.needs_user_signoff, async (checked) => {
        await updateDetailTicketForToggle({ needs_user_signoff: checked });
      }));
      toggles.appendChild(toggleControl('Needs inspection', ticket.needs_inspection, async (checked) => {
        await updateDetailTicketForToggle({ needs_inspection: checked }, 'director');
      }));
      toggles.appendChild(toggleControl('Needs audit', ticket.needs_audit !== false, async (checked) => {
        await updateDetailTicketForToggle({ needs_audit: checked }, 'director');
      }));
      }
      toggles.appendChild(toggleControl('Regression', ticket.regression, async (checked) => {
        await updateDetailTicketForToggle({ regression: checked });
      }));
      toggles.appendChild(toggleControl('Manual control', ticket.manually_controlled, async (checked) => {
        await updateDetailTicketForToggle({ manually_controlled: checked }, 'director');
      }, {
        title: 'Director-only: hold or release this ticket outside automatic workflow movement.',
      }));
      if (!state.workflow) {
      if (ticket.needs_inspection) {
        toggles.appendChild(toggleControl('Inspector signoff', ticket.inspector_signoff, async (checked) => {
          await updateDetailTicketForToggle({ inspector_signoff: checked }, 'inspector');
        }));
      }
      toggles.appendChild(toggleControl('Audit signoff', ticket.audit_signoff, async (checked) => {
        const patch = { audit_signoff: checked };
        if (checked && commentText.value.trim()) {
          patch.comment = { who: 'audit', text: commentText.value.trim() };
        }
        await updateDetailTicketForToggle(patch, 'audit');
      }, {
        disabled: ticket.needs_audit === false,
        title: ticket.needs_audit === false ? 'Ticket does not require audit sign-off.' : '',
      }));
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
          const confirmed = window.confirm(
            `Merge ${ticket.id} into ${targetId}? ${ticket.id}'s comments/attachments move to ${targetId} and ${ticket.id} is closed as merged.`,
          );
          if (!confirmed) {
            return;
          }
          await mergeTicket(ticket.id, targetId);
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
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
        await updateDetailTicket({
          blocked_by: nextBlockedBy,
          blocked_reason: blockedReasonValue,
        });
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
            await updateDetailTicket({
              blocked_by: nextBlockedBy,
            });
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
        await updateDetailTicket({ blocked_reason: blockedReasonInput.value });
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
      bindDetailDraftField(draftFields, commitHashInput, 'commitHash', commitHashInput.value);
      commitInfo.append(commitHashInput);
      const commitOverride = toggleControl('No commit required', ticket.commit_exempt, async (checked) => {
        await updateDetailTicketForToggle({ commit_exempt: checked });
      });
      const commitNote = document.createElement('div');
      commitNote.className = 'soft-note';
      commitNote.textContent = ticket.commit_exempt
        ? 'Done-state commit check is bypassed for this ticket.'
        : 'A verified git commit is required before moving this ticket to done.';
      commitInfo.append(commitOverride, commitNote);

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
      } else if (ticketIsUserReview(ticket)) {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Return for Rework';
        kickbackButton.addEventListener('click', async () => {
          try {
            await submitComment(ticket.id, commentWho.value, commentText.value, 'analysis');
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticket.state === 'inspection') {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Return to Implementation';
        kickbackButton.addEventListener('click', async () => {
          try {
            await submitComment(ticket.id, 'inspector', commentText.value, 'in_progress');
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticket.state === 'audit') {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Return to Implementation';
        kickbackButton.addEventListener('click', async () => {
          try {
            await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticketAllowsKickback(ticket)) {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Move to Implementation';
        kickbackButton.addEventListener('click', async () => {
          try {
            await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        commentActions.appendChild(kickbackButton);
      }
      }
      commentComposer.append(commentWho, commentText, commentUrgentLabel, commentActions);
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
