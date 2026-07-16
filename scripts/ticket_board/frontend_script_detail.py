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
      const commentWho = document.createElement('input');
      commentWho.type = 'text';
      commentWho.placeholder = 'who';
      commentWho.value = ticketIsEricReview(ticket) ? 'eric' : 'director';
      bindDetailDraftField(draftFields, commentWho, 'commentWho', commentWho.value);
      const commentText = document.createElement('textarea');
      commentText.placeholder = 'Add a comment or bounce-back note';
      bindDetailDraftField(draftFields, commentText, 'commentText', '');
      const commentUrgentLabel = document.createElement('label');
      commentUrgentLabel.className = 'urgent-comment-toggle';
      const commentUrgent = document.createElement('input');
      commentUrgent.type = 'checkbox';
      const commentUrgentText = document.createElement('span');
      commentUrgentText.textContent = 'Urgent';
      commentUrgentLabel.append(commentUrgent, commentUrgentText);
      const detailCallerRole = () => commentWho.value;

      if (ticketIsEricReview(ticket)) {
        const signoffRecorded = !!ticket.eric_signoff;
        const ericBanner = document.createElement('div');
        ericBanner.className = signoffRecorded ? 'eric-banner eric-banner-confirmed' : 'eric-banner';
        const ericBannerSubtitle = document.createElement('div');
        ericBannerSubtitle.className = 'eric-banner-subtitle';
        ericBannerSubtitle.textContent = signoffRecorded ? 'Signed Off ✓' : 'Awaiting UAT sign-off';
        const ericBannerTitle = document.createElement('div');
        ericBannerTitle.className = 'eric-banner-title';
        ericBannerTitle.textContent = ticket.title;
        const ericBannerNote = document.createElement('div');
        ericBannerNote.className = 'eric-banner-note';
        ericBannerNote.textContent = signoffRecorded
          ? 'UAT sign-off recorded. Waiting for director completion.'
          : ericReviewCheckText(ticket);
        const ericSummary = document.createElement('div');
        ericSummary.className = 'eric-summary';
        const ericSummaryHead = document.createElement('div');
        ericSummaryHead.className = 'eric-summary-head';
        ericSummaryHead.textContent = signoffRecorded ? 'Signed-off UAT Snapshot' : 'UAT Check Before Sign-off';
        const ericSummaryStatuses = document.createElement('div');
        ericSummaryStatuses.className = 'eric-summary-statuses';
        ericReviewStatusItems(ticket).forEach((item) => {
          const row = document.createElement('div');
          row.className = 'eric-summary-status';
          const strong = document.createElement('strong');
          strong.textContent = item.label;
          const value = document.createElement('span');
          value.className = item.ok ? 'eric-summary-status-ok' : 'eric-summary-status-missing';
          value.textContent = item.ok ? (item.okText || 'ready') : (item.missingText || 'missing');
          row.append(strong, value);
          ericSummaryStatuses.appendChild(row);
        });
        const ericSummarySections = document.createElement('div');
        ericSummarySections.className = 'eric-summary-sections';
        ericReviewSummarySections(ticket).forEach((section) => {
          const sectionEl = document.createElement('div');
          sectionEl.className = 'eric-summary-section';
          const label = document.createElement('div');
          label.className = 'eric-summary-label';
          label.textContent = section.label;
          sectionEl.appendChild(label);
          if (section.checklist) {
            const list = document.createElement('ul');
            list.className = 'eric-summary-list';
            section.checklist.forEach((item) => {
              const entry = document.createElement('li');
              entry.textContent = item;
              list.appendChild(entry);
            });
            sectionEl.appendChild(list);
          } else if (section.summary) {
            const text = document.createElement('div');
            text.className = 'eric-summary-text';
            text.textContent = section.summary;
            sectionEl.appendChild(text);
          }
          ericSummarySections.appendChild(sectionEl);
        });
        ericSummary.append(ericSummaryHead, ericSummaryStatuses, ericSummarySections);
        if (signoffRecorded) {
          const confirmation = document.createElement('div');
          confirmation.className = 'eric-signoff-confirmation';
          confirmation.textContent = 'Signed off ✓ Waiting for director completion.';
          ericSummary.appendChild(confirmation);
        }
        const ericBannerActions = document.createElement('div');
        ericBannerActions.className = 'inline-actions';
        if (signoffRecorded) {
          const signedOffState = document.createElement('div');
          signedOffState.className = 'signoff-state-chip';
          signedOffState.textContent = 'Signed Off ✓';
          ericBannerActions.appendChild(signedOffState);
        } else {
          const ericBannerSignoffButton = document.createElement('button');
          ericBannerSignoffButton.className = 'primary';
          ericBannerSignoffButton.type = 'button';
          ericBannerSignoffButton.textContent = 'Sign Off';
          ericBannerSignoffButton.addEventListener('click', async () => {
            try {
              await submitEricSignoff(ticket.id, commentWho.value, commentText.value);
            } catch (error) {
              setCreateStatus(error.message, true);
              await requestBoardReload();
            }
          });
          ericBannerActions.appendChild(ericBannerSignoffButton);
        }
        ericBanner.append(ericBannerSubtitle, ericBannerTitle, ericBannerNote, ericSummary, ericBannerActions);
        box.appendChild(ericBanner);
      }

      const detailAlerts = renderAlertStack(ticket, { detail: true });
      if (detailAlerts) {
        box.appendChild(detailAlerts);
      }

      const controls = document.createElement('div');
      controls.className = 'detail-grid';

      const workflowActions = document.createElement('div');
      workflowActions.className = 'inline-actions';
      const detailAdvanceState = defaultAdvanceState(ticket);
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
      if (ticket.state !== 'done' && ticket.state !== 'cancelled') {
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
        await updateTicket(ticket.id, { assignee: assigneeSelect.value }, 'director');
      });
      assigneeLabel.appendChild(assigneeSelect);

      const parentLinkField = document.createElement('div');
      parentLinkField.innerHTML = '<div class="field-label">Parent Ticket</div>';
      const parentLinkInput = document.createElement('input');
      parentLinkInput.type = 'text';
      parentLinkInput.value = ticket.parent_id || '';
      parentLinkInput.placeholder = 'PGU-123';
      bindDetailDraftField(draftFields, parentLinkInput, 'parentId', parentLinkInput.value);
      const parentLinkActions = document.createElement('div');
      parentLinkActions.className = 'inline-actions';
      const saveParentLinkButton = document.createElement('button');
      saveParentLinkButton.textContent = 'Save Parent Link';
      saveParentLinkButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { parent_id: parentLinkInput.value.trim().toUpperCase() }, detailCallerRole());
      });
      parentLinkActions.appendChild(saveParentLinkButton);
      if (ticket.parent_id) {
        const clearParentLinkButton = document.createElement('button');
        clearParentLinkButton.textContent = 'Clear Parent Link';
        clearParentLinkButton.addEventListener('click', async () => {
          parentLinkInput.value = '';
          await updateTicket(ticket.id, { parent_id: '' }, detailCallerRole());
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
      toggles.appendChild(toggleControl('Requires UAT', ticket.needs_eric_signoff, async (checked) => {
        await updateTicket(ticket.id, { needs_eric_signoff: checked }, detailCallerRole());
      }));
      toggles.appendChild(toggleControl('Needs inspection', ticket.needs_inspection, async (checked) => {
        await updateTicket(ticket.id, { needs_inspection: checked }, 'director');
      }));
      toggles.appendChild(toggleControl('Regression', ticket.regression, async (checked) => {
        await updateTicket(ticket.id, { regression: checked }, detailCallerRole());
      }));
      if (ticket.needs_inspection) {
        toggles.appendChild(toggleControl('Inspector signoff', ticket.inspector_signoff, async (checked) => {
          await updateTicket(ticket.id, { inspector_signoff: checked }, 'inspector');
        }));
      }
      toggles.appendChild(toggleControl('Audit signoff', ticket.audit_signoff, async (checked) => {
        const patch = { audit_signoff: checked };
        if (checked && commentText.value.trim()) {
          patch.comment = { who: 'audit', text: commentText.value.trim() };
        }
        await updateTicket(ticket.id, patch, 'audit');
      }));
      toggles.appendChild(toggleControl('UAT sign-off', ticket.eric_signoff, async (checked) => {
        const patch = { eric_signoff: checked };
        if (checked && commentText.value.trim()) {
          patch.comment = { who: 'eric', text: commentText.value.trim() };
        }
        await updateTicket(ticket.id, patch, 'eric');
      }, {
        disabled: !ticket.needs_eric_signoff,
        title: ticket.needs_eric_signoff ? '' : 'Ticket does not require UAT sign-off.',
      }));

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
        await updateTicket(ticket.id, { title: titleEditInput.value }, detailCallerRole());
      });
      titleActions.appendChild(saveTitleButton);
      const mergeTargetInput = document.createElement('input');
      mergeTargetInput.type = 'text';
      mergeTargetInput.placeholder = 'PGU-123';
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
      const blockedByInput = document.createElement('input');
      blockedByInput.type = 'text';
      blockedByInput.value = formatBlockedByList(ticket.blocked_by);
      blockedByInput.placeholder = 'PGU-23, PGU-25';
      bindDetailDraftField(draftFields, blockedByInput, 'blockedBy', blockedByInput.value);
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
      addBlockedByButton.textContent = 'Add Selected';
      addBlockedByButton.disabled = blockerPicker.options.length <= 1;
      addBlockedByButton.addEventListener('click', () => {
        const blockerId = blockerPicker.value.trim().toUpperCase();
        if (!blockerId) {
          return;
        }
        const nextBlockedBy = Array.from(new Set([...parseBlockedByInput(blockedByInput.value), blockerId]));
        blockedByInput.value = formatBlockedByList(nextBlockedBy);
        persistDetailDraftField(ticket.id, 'blockedBy', blockedByInput.value);
      });
      const saveBlockedByButton = document.createElement('button');
      saveBlockedByButton.textContent = 'Save Blockers';
      saveBlockedByButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, {
          blocked_by: parseBlockedByInput(blockedByInput.value),
          blocked_reason: blockedReasonInput.value,
        }, detailCallerRole());
      });
      blockedByActions.append(blockerPicker, addBlockedByButton, saveBlockedByButton);
      const blockedByNote = document.createElement('div');
      blockedByNote.className = 'soft-note';
      blockedByNote.textContent = `${blockedBySummary(ticket)} Only non-terminal tickets are selectable as blockers.`;
      const blockedByLinks = document.createElement('div');
      blockedByLinks.className = 'field-preview';
      const blockedByLinksLabel = document.createElement('div');
      blockedByLinksLabel.className = 'field-preview-label';
      blockedByLinksLabel.textContent = 'Linked Tickets';
      blockedByLinks.append(blockedByLinksLabel, linkedTicketRow(visibleBlockedBy));
      blockedBy.append(blockedByInput, blockerPickerLabel, blockedByActions, blockedByNote, blockedByLinks);

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
        await updateTicket(ticket.id, { blocked_reason: blockedReasonInput.value }, detailCallerRole());
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
        await updateTicket(ticket.id, { implementation: implementationInput.value }, detailCallerRole());
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
        await updateTicket(ticket.id, { audit_prompt: auditPromptInput.value }, detailCallerRole());
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
      const commitActions = document.createElement('div');
      commitActions.className = 'inline-actions';
      const saveCommitButton = document.createElement('button');
      saveCommitButton.textContent = 'Save Commit';
      saveCommitButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { commit_hash: commitHashInput.value }, detailCallerRole());
      });
      commitActions.appendChild(saveCommitButton);
      commitInfo.append(commitHashInput, commitActions);
      const commitOverride = toggleControl('No commit required', ticket.commit_exempt, async (checked) => {
        await updateTicket(ticket.id, { commit_exempt: checked }, detailCallerRole());
      });
      const commitNote = document.createElement('div');
      commitNote.className = 'soft-note';
      commitNote.textContent = ticket.commit_exempt
        ? 'Done-state commit check is bypassed for this ticket.'
        : 'A verified git commit is required before moving this ticket to done.';
      commitInfo.append(commitOverride, commitNote);

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
          await updateTicket(ticket.id, {
            screenshots: ticketScreenshotPaths(ticket).filter((item) => item !== path),
          }, detailCallerRole());
        }, (entry) => {
          openImageLightbox(entry);
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
        await submitComment(ticket.id, commentWho.value, commentText.value, null, commentUrgent.checked);
      });
      commentActions.appendChild(addCommentButton);
      if (ticketIsEricReview(ticket)) {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Return for Rework';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, commentWho.value, commentText.value, 'analysis');
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticket.state === 'inspection') {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Return to Implementation';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, 'inspector', commentText.value, 'in_progress');
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticket.state === 'audit') {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Return to Implementation';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticketAllowsKickback(ticket)) {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Move to Implementation';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
        });
        commentActions.appendChild(kickbackButton);
      }
      commentComposer.append(commentWho, commentText, commentUrgentLabel, commentActions);
      comments.appendChild(commentComposer);
      if (ticket.comments.length) {
        const list = document.createElement('div');
        list.className = 'comment-list';
        ticket.comments.forEach((comment) => {
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
