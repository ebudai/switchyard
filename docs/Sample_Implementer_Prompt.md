# Sample Implementer Prompt

**Purpose:** Reference prompt for directors composing implementer instructions. This is the prompt used for packet 6.3.4 (Reconciliation Pane) — chosen because it demonstrates handling a complex lifecycle, direct SQL queries, service constructor requirements, and the full constraint block.

---

```
You are an Implementer in the adversarial-collaborative methodology. Your job is to implement exactly one work packet, no more, no less.

## Active Packet

**6.3.4 — Reconciliation Pane**

Read `docs/current-focus.md` for full specification. Read `docs/adversarial-collaborative-methodology.md` for process rules.

## Objective

Create `ReconciliationViewModel` and `ReconciliationView.axaml` for the reconciliation pane. The pane shows the full reconciliation lifecycle:
1. **Active reconciliation list** — any reconciliations in non-terminal states (Detected through FixProposed).
2. **Selected reconciliation detail** — classification, trigger kind, involved artifacts, blocked packet, status, retry count, proposed fix text.
3. **Human approval form** — approve or reject with required reasoning, and required `ResolutionImpact` selection (RequirementChange, ExecutionChange, Rollback) for approvals.
4. **Status display** — lifecycle state with visual indicators.

## Key Domain Types (Forge.Core/Domain/DomainModel.cs)

- `ReconciliationRecord` — Id, Classification (DisagreementClassification), TriggerKind (ReconciliationTriggerKind), ArtifactsInvolved, Status (ReconciliationStatus), EscalationId?, BlockedPacketId?, BaselineIds, DirectorPrompt?, ReconcilerResponse?, ProposedChangeHash?, HumanApprovalResult (HumanApprovalStatus?), HumanApprovalReasoning?, ResolutionImpact?, RetryCount, CreatedAt, ResolvedAt?
- `DisagreementClassification` enum: Administrative, Semantic, Historical
- `ReconciliationTriggerKind` enum: HashDrift, ImplementerStop, ConsistencyChecker, DirectorDetected, HumanDetected
- `ReconciliationStatus` enum: Detected, Stopped, Spawned, FixProposed, Approved, Rejected, Resumed, Escalated
- `HumanApprovalStatus` enum: Pending, Approved, Rejected
- `ResolutionImpact` enum: RequirementChange, ExecutionChange, Rollback

## Key Service (Forge.Core/Reconciliation/ReconciliationCoordinator.cs)

- `ReconciliationCoordinator(connectionString, projectConfig, gitOps?)`
  - `GetReconciliation(ReconciliationId)` — get a single reconciliation record
  - `ApproveAsync(reconciliationId, reason, initiatedBy, humanApprovalReasoning, resolutionImpact, resolvedAt)` — approve with required reasoning and impact classification. Throws if `resolutionImpact` is null.
  - `RejectFixAsync(reconciliationId, reason, initiatedBy, humanApprovalReasoning)` — reject with required reasoning. Increments retry count internally.
- Note: there is no `GetActiveReconciliations()` method on the coordinator or ForgeDb. You will need to either: (a) query the database directly via a read connection for reconciliations in non-terminal statuses, or (b) add a query method to the ViewModel's data source. Prefer option (a) using `ForgeDb.OpenReadConnection` + direct SQL, since option (b) would modify Forge.Core which is out of scope.

## Implementation Guidance

1. **ViewModel** — Create `Forge.Desktop/ViewModels/ReconciliationViewModel.cs`:
   - Extend `PanePlaceholderViewModel` (same pattern as ReviewWorkflowViewModel, DirectorPaneViewModel, etc.).
   - `Activate(ProjectConfig)` stores config and loads active reconciliations.
   - To load active reconciliations: open a read connection via `ForgeDb.OpenReadConnection`, query `reconciliations` table for rows where `status NOT IN ('approved','resumed','escalated')`. Map rows to `ReconciliationRecord` or a simpler display model.
   - Observable properties: `ActiveReconciliations` (list), `SelectedReconciliation`, `HasActiveReconciliation`, `ClassificationText`, `TriggerKindText`, `StatusText`, `ArtifactsInvolvedText`, `BlockedPacketText`, `ProposedFixText`, `RetryCountText`, `HumanReasoning` (editable), `SelectedResolutionImpact`, `CanApprove` (reasoning non-empty AND impact selected), `CanReject` (reasoning non-empty).
   - `ApproveCommand` (AsyncRelayCommand): calls `ReconciliationCoordinator.ApproveAsync` with the human reasoning and selected impact, then refreshes the list.
   - `RejectCommand` (AsyncRelayCommand): calls `ReconciliationCoordinator.RejectFixAsync` with human reasoning, then refreshes the list.
   - Handle no active reconciliations gracefully — show "No active reconciliations".

2. **View** — Create `Forge.Desktop/Views/ReconciliationView.axaml`:
   - Follow the existing pane layout pattern.
   - Left/top: list of active reconciliations (ListBox or ItemsRepeater) showing classification + status badge.
   - Right/bottom: selected reconciliation detail with all fields.
   - Approval form: reasoning TextBox, ResolutionImpact ComboBox (bound to enum values), Approve/Reject buttons.
   - Status badge color-coded by lifecycle stage.

3. **Wire into MainWindow**:
   - In `MainWindowViewModel.cs`: change the `Reconciliation` property type from `PanePlaceholderViewModel` to `ReconciliationViewModel`. Initialize as `new ReconciliationViewModel()`. Call `Reconciliation.Activate(loadedProject.ProjectConfig)` in `ApplyLoadedProject` and `Reconciliation.Activate()` in `SetProjectLoadError`.
   - In `MainWindow.axaml`: add a `DataTemplate` for `ReconciliationViewModel` → `ReconciliationView`.

4. **Tests** — Add `Forge.Core.Tests/ReconciliationViewModelTests.cs`:
   - Test: given an active reconciliation in FixProposed state → all detail fields populated correctly.
   - Test: approve command is disabled when reasoning is empty or resolution impact is not selected.
   - Test: approve command calls coordinator and updates the reconciliation list.
   - Test: reject command calls coordinator with reasoning.
   - Use the established in-memory SQLite pattern (full DDL, real services). Note: `ReconciliationCoordinator` requires a `ProjectConfig` and uses `GitOps` internally. For tests, you may need to provide a ProjectConfig pointing to temp files and handle GitOps gracefully (the coordinator accepts an optional `GitOps` parameter).

## Constraints

- Do NOT modify any existing Forge.Core service or domain type.
- Solution must build with zero warnings: `dotnet build Forge.sln`
- All tests must pass: `dotnet test Forge.sln` (currently 384).
- If a stopping problem occurs, write the reason to `docs/implementer-report.md` before stopping.
- Update `docs/implementer-report.md` with your results upon completion.
- Update the packet status in `docs/current-focus.md` and `docs/implementation-plan.md` upon completion.
```

---

## Anatomy of the Prompt

| Section | Purpose |
|---------|---------|
| Role declaration | Sets the implementer contract — one packet, no more |
| Active Packet | Names the exact packet and points to authoritative docs |
| Objective | What the user sees — the behavioral contract |
| Key Domain Types | Every type, enum, and property the implementer will touch — eliminates exploration time |
| Key Service | Constructor signature, method signatures, return types, known gaps (like missing query methods) |
| Implementation Guidance | Numbered sections for ViewModel, View, MainWindow wiring, and Tests — specific enough to constrain, open enough for judgment |
| Constraints | The invariants that must hold after implementation |
