# Adversarial-Collaborative LLM Development Methodology

> **PARTIALLY SUPERSEDED (2026-08-30).** Written 2026-07-05, before the ticket board
> existed. Read it for the **principles**, not the mechanics.
>
> **Still current — and the reason to keep reading this file.** The seven principles below
> have held up, and two of them described mechanisms we only built later: *"completion over
> parallelism, with one structural exception"* became the board's serial-focus rule, and
> *"behavioral constraints evolve from observed failures"* is still literally how the
> process works — most board rules exist because something went wrong once.
>
> **Superseded.** The **Roles** section names an *Editor*, *Reviewers (Gemini, GPT)* and a
> *UI Designer Instance*; the current roles are director, implementers, audit, inspector,
> research and the user. The four **Phases** are now an eleven-stage pipeline with entry
> gates. This document mentions tickets, kickbacks and sign-offs not at all.
>
> **For current practice:** `switchyard-board-guide.md` (mechanics — stages, permissions,
> the write API, directorctl) and `switchyard-director-guide.md` (judgment — review
> discipline, merge vs kick back, escalation).

## What This Is

A process for building software at a quality higher than any single participant — human or LLM — could achieve alone. The method uses multiple LLM instances in distinct roles to co-design, critique, plan, and implement a system through structured adversarial review. The human drives design and exercises oversight through the software's behavior, not through reviewing every line of code.

The core insight: LLMs are better collaborators than solo performers. A single instance will confidently produce plausible-looking work that silently drifts from intent. Multiple instances with different models, roles, and adversarial incentives catch each other's mistakes. The human stays in the loop through design authority, UI-based functional testing, and adjudication of impasses — not as a bottleneck through which every interaction must flow.

---

## Principles

**1. Separation of authorship and criticism.**
The instance that wrote something must not be the sole judge of its quality. Different models have different failure modes; exploiting that diversity is the point.

**2. The human is the driver, not a passenger.**
The human makes every substantive *design* decision and retains final authority at all times. During design and review (Phases 1-3), the human is deeply involved in every decision. During implementation (Phase 4), the human's oversight mechanism shifts from code review to functional testing through the UI — the adversarial process between LLMs generates technical detail faster than any human can absorb, so the human exercises judgment through the software's behavior, not through its source code. The human adjudicates impasses, makes abort/reset decisions, and catches problems that manifest as incorrect behavior. The invariant is not that the human reads every line, but that the human retains meaningful oversight through a feedback mechanism that scales with the pace of development.

**3. Dependency order over capability order.**
When the order in which things should be built conflicts with the order in which they become useful, build order wins. This prevents the seductive trap of demoing a feature whose foundations are sand.

**4. Immutable history, living plans.**
Design documents and implementation plans are living artifacts that evolve, but every change is deliberate and logged. The decision log records *why* things were decided, not just *what* — because "what" is visible in the artifacts, but "why" is what prevents future instances from relitigating settled questions.

**5. Completion over parallelism, with one structural exception.**
Avoid parallel *implementation* of unresolved dependencies — partial progress across many fronts is the natural failure mode of LLM-assisted work. However, *critique and validation* can be parallelized where safe. The one structural exception is the **core/UI parallel track**: during Phase 4, the core governance engine and the UI are developed simultaneously. The core runs through the autonomous LLM adversarial loop while the human and a UI designer instance iterate on the interface. Features are hooked up incrementally so the human can test each capability through the UI as soon as its backing code exists. This parallel track is not a violation of the principle — it is the mechanism by which the human maintains meaningful oversight at the pace LLMs produce.

**6. The human selects for global fit, not local elegance.**
When a subproblem has multiple valid solutions, the human should choose the one that best fits the rest of the system, removes the most special cases, and collapses the most unsolved problem space — even if it's not the most elegant solution to the subproblem in isolation. LLMs tend to optimize locally; humans are better at navigating the dependency graph of a design and spotting that a slightly uglier choice here eliminates three complications downstream. This is a distinct and irreplaceable contribution that justifies the human's involvement beyond mere supervision.

**7. Behavioral constraints evolve from observed failures.**
When an implementer instance exhibits a problematic behavior — writing tautological tests, redesigning adjacent systems, silently resolving document conflicts — the response is not just to fix the immediate problem but to encode a rule preventing recurrence. These rules accumulate in a behavioral contract (e.g., AGENTS.md) that hardens over time. The process gets stricter where it has been burned, not uniformly.

---

## Roles

### The Human (Operator)

The only participant who persists across the entire project. The human's contributions are concentrated where human judgment actually scales:

- **Design and review (Phases 1-3):** Deeply involved in every decision. Co-designing with the editor, adjudicating review points, selecting for global fit, confirming models and plans.
- **UI design (Phase 4, parallel track):** Working with a UI designer instance to create the interface. This is where architectural taste meets usability, and where the human's feel for "how should this work?" is irreplaceable.
- **Functional testing through the UI (Phase 4, ongoing):** The human's primary oversight mechanism during implementation. As features are hooked up, the human tests them through the UI and discovers problems that the LLM adversarial process missed — things that look correct in code but behave incorrectly in practice.
- **Adjudicating impasses:** When LLM instances reach genuine disagreement they cannot resolve, the human makes the call. This should be rare if the design phase was thorough.
- **Abort/reset decisions:** When the human sees through the UI that something is fundamentally wrong, they call for a course correction.
- **Periodically sampling raw diffs and test results directly,** not just the director's summaries.

The human does NOT attempt to keep up with the technical detail of every implementation block and adversarial review debate. The adversarial process between LLMs generates detail faster than any human can absorb. Attempting to review everything leads to either project stalls (the human becomes the bottleneck) or rubber-stamping (the human approves without reading, which is worse than not reviewing). The human exercises oversight through the UI — if the LLMs agreed on something wrong, the human discovers it when the feature doesn't work correctly.

### The Editor (e.g., Claude Opus)

The primary co-designer and authoring instance. The editor is not a document wrangler — it is an active design partner that contributes ideas, pushes back on the human's proposals, suggests alternatives, identifies tensions in the requirements, and proposes solutions. Responsible for:

- Co-designing the system with the human during the design phase, contributing substantive ideas and pushing back where it disagrees
- Producing and maintaining the design document as the shared record of joint conclusions
- Generating the domain model and data model from the reviewed design document
- Assessing reviewer criticisms and incorporating those deemed worthwhile
- Maintaining the right to dismiss criticisms, with the understanding that dismissed items may be escalated

The editor should be a strong general-purpose model with good judgment about tradeoffs. It is the closest thing to a "lead architect" in this process — a peer to the human during design, not a subordinate.

### Reviewers (e.g., Gemini, GPT)

Adversarial critics. Each reviewer receives the current design document and produces a structured report covering:

- Under-specified areas (behavior that would require guessing during implementation)
- Over-specified areas (unnecessary constraints that limit future flexibility)
- Missing concerns (error handling, edge cases, failure modes, security)
- Unnecessary complexity (features or abstractions not justified by requirements)
- Pessimizations (design choices that make the system slower, harder, or more fragile than necessary)
- Internal contradictions

Reviewers should be *different models* from the editor. The value comes from cognitive diversity, not from having more of the same perspective.

**Diagnosis over prescription:** Reviewers should be prompted to identify problems and explain *why* they matter, but not to propose fixes. Reviewer fixes are often lower-value than the diagnosis itself because the reviewer lacks the full context that the editor has accumulated. Requesting diagnosis-only improves signal-to-noise, shortens reports, and lets the editor focus on the real issue rather than refuting a proposed fix. When option generation is explicitly wanted (e.g., "suggest three approaches to this problem"), fixes can be requested — but the default is diagnosis only. The editor (who holds the full design context) determines the appropriate response to each valid criticism.

**Escalation rule:** When the editor dismisses a criticism, the reviewer's next report should include any dismissed items that the reviewer considers too important to drop. This forces the human to adjudicate rather than allowing the editor to silently bury valid concerns.

### The Director

The instance responsible for translating the finalized design into an executable implementation plan, composing prompts for implementer instances, and supervising implementation. The director *reads, assesses, and flags* — it does not edit project artifacts directly. All changes flow through implementer instances or human decisions.

The director should be an agentic instance with direct filesystem and git access — it needs to inspect the working tree, run `git status` and `git diff`, read any project file, and check test results without going through the human as a relay. Responsible for:

- Writing the implementation plan (the one artifact the director authors directly, before implementation begins)
- Composing prompts for each implementation instance
- Inspecting the repository state directly when assessing implementer output or diagnosing problems
- Spawning a reconciliation implementer instance when document disagreements are flagged
- Reviewing implementer edits to the implementation plan and other artifacts, flagging the human if it disagrees
- Autonomously running the implementation loop within a deliverable, escalating to the human on exceptions and at deliverable boundaries
- Flagging the human for any disagreement or problem beyond document misalignment — the director does not resolve issues locally

The director does not make edits to project artifacts during implementation. Implementers update the implementation plan, current-focus, decision log, and other documents as part of their work packets. If the director disagrees with an implementer's edit to any artifact, it flags the human for intervention rather than correcting it directly. This separation ensures that the director's supervisory judgment and the implementer's execution remain distinct roles — the director cannot silently "fix" things without the human knowing.

**Continuity decision:** The editor can continue as the director, or the director can be a fresh instance. There are tradeoffs:

*Same instance (editor continues as director):* Retains implicit understanding of design intent that didn't make it into the documents — the "I considered X and rejected it for subtle reason Y" knowledge. This is the preferred approach as long as the context window holds the full design conversation. The risk is that the design documents are never forced to stand fully alone.

*Fresh instance:* Forces the design documents to be completely self-contained, since the director has no conversational memory of the design process. This is a stronger test of document quality but loses implicit judgment. This becomes necessary when the editor's context window fills up.

The practical rule: same instance continues as director when possible; if the context window fills, the documents must be good enough to stand alone, and the quality of the handoff is the test of whether the design phase produced sufficient documentation.

### Implementers (e.g., a cost-effective coding model)

Stateless coding instances, each handling one work packet. They receive:

- A prompt written by the director (see prompt structure below)
- The current-focus document
- The TDD contract / contracts document
- The testing strategy
- The behavioral contract (AGENTS.md)
- The domain model and data model references
- Any relevant portion of the design document

Each implementer is expected to:

- Stay within the scope of its assigned work packet
- Write tests alongside implementation (TDD-first)
- Update the current-focus, decision log, and testing strategy as needed
- Stop and record conflicts rather than silently resolving them
- If current-focus and the implementation plan disagree about the active packet, stop immediately and report the mismatch

Implementers should be chosen for cost-effectiveness. They do not need to be the strongest model — the director's prompt quality and the process constraints compensate for individual instance capability.

**Implementer prompt structure:** The director's prompt for each implementer should follow a consistent shape:

- *Read list:* Which documents to read before starting (AGENTS.md, current-focus, contracts, testing strategy, implementation plan, decision log, domain model, data model).
- *Scope statement:* "We are working only on the exact packet currently designated as the active packet."
- *Goal:* One sentence stating what to implement, no more and no less.
- *Context:* What prior work is complete, what boundaries to respect, what not to redesign.
- *Instructions:* Numbered steps — read the guidance, identify the packet, restate it, do TDD, don't skip ahead, don't redesign completed boundaries. The "restate the packet" step forces the instance to demonstrate comprehension before acting.
- *Constraints:* Explicit behavioral limits — stay within this packet, do TDD first, don't redesign neighbors, keep the implementation boring and exact.
- *Done when:* Observable completion criteria including a structured summary of what changed.
- *Conflict rule:* If the documents disagree, stop and report rather than choosing silently.

### UI Designer Instance

A design-focused instance that works with the human on the UI track during Phase 4. The UI designer does not need to be the same model as the editor or director — it should be strong at visual/interaction design, responsive layout, and translating workflow descriptions into usable interfaces.

The UI designer receives the design sketch's workflow descriptions and the human's preferences, and iterates on the interface with the human. As core features are completed on the core track, the UI designer helps hook them up to the interface. The human tests each feature through the UI and reports problems back to the core track.

---

## Phases

### Phase 1: Collaborative Design

**Participants:** Human + Editor

The human and editor collaboratively co-design the system. The editor is not a document wrangler — it is an active design partner that contributes ideas, pushes back on the human's proposals, suggests alternatives, identifies tensions in the requirements, and proposes solutions. The design document is the shared artifact that captures their joint conclusions, but the editor's role is to *think* about the design, not just *record* it.

**Process:**
1. The human describes the system intent, constraints, and any existing domain knowledge.
2. The editor engages as a co-designer: contributing ideas, pushing back on proposals that have hidden costs, suggesting alternatives, and identifying concerns the human hasn't raised.
3. The editor produces or updates the design document with conclusions from the discussion.
4. The human reads the document end-to-end and annotates corrections, clarifications, missing concerns, and new ideas.
5. The editor incorporates feedback, pushes back where it disagrees, and updates the document.
6. Repeat until the human can read the entire document without producing notes.

**Exit condition:** The human performs a complete read-through of the design document and has nothing to add, correct, or question.

**Artifacts produced:** Design document / informal spec. The domain model and data model are *not* produced yet — the design document must go through adversarial review first so the models are generated from a reviewed design, not a draft.

### Phase 2: Adversarial Design Review

**Participants:** Human + Editor + Reviewer(s)

Phase 2 has two stages: first the design document is reviewed and hardened, then the domain model and data model are generated from the reviewed design and themselves reviewed.

#### Stage 1: Design document review

The design document is subjected to structured adversarial review by one or more reviewers using different models.

**Process for each reviewer:**
1. The reviewer receives the design document and produces a critique report. The reviewer should identify weaknesses and explain why they matter, but should not propose fixes (see Roles > Reviewers > Diagnosis over prescription).
2. The editor receives the critique report and assesses each point, producing a response that either incorporates the criticism (determining its own fix) or explains why it was dismissed.
3. The amended document and the editor's response go back to the reviewer.
4. The reviewer re-examines the document, including flagging any dismissed items it considers too important to ignore (escalation).
5. The human reads escalated items and makes final calls.
6. Repeat until the editor determines that further critique cycles will not produce worthwhile improvements.

**Sequencing:** Reviewers are engaged sequentially, not in parallel, though parallel first rounds are a legitimate alternative when throughput matters more than sequential depth (see Tooling > Parallel vs. sequential review). The choice is situational, not doctrinal.

**Stage 1 exit condition:** The review cycle terminates when: two consecutive cycles produce only opinion/style findings, no blocker or should-fix-now findings remain open, and all escalations have been resolved or explicitly deferred. The editor recommends termination; the human concurs.

#### Stage 2: Model generation and review

Once the design document has been reviewed and hardened, the editor generates:

- A *domain model* expressed in the implementation language — strongly typed IDs, value types, enums, aggregates, records, and factory methods. This is the reviewed design translated into compilable form. It defines the vocabulary that every implementer must use, preventing the drift that occurs when each instance invents its own types for the same concept.
- A *data model* (persistence schema) — e.g., a DDL file for SQLite, a migration script for PostgreSQL. This defines the physical storage contracts: column types, dictionary-backed references, foreign key relationships, index choices. It answers questions like "is this UUID stored as TEXT or BLOB?" exactly once, in one place, so implementers build against a known schema rather than guessing.

These are not optional byproducts. They are first-class design artifacts that encode decisions which are expensive to reconcile later. Generating them *after* design review ensures they're built on a solid foundation rather than encoding a draft that might change.

The models then go through the same adversarial review process as the design document, with review criteria specific to each artifact type:

- *Domain model:* type safety, category mistake prevention, enum completeness and stability, value type invariant enforcement, factory method correctness, unnecessary coupling between aggregates.
- *Data model:* schema normalization, storage contract consistency, dictionary-backing decisions, foreign key coverage, index choices for hot access patterns, transactional boundary clarity.

Reviewers receive the full artifact set (design document + both models), not the models in isolation, because inconsistencies between artifacts are some of the highest-value findings.

**Human involvement in model review:** Unlike design document review, the human does not review model-level changes point by point. The domain model and data model are typically too large for meaningful human review, and the detailed type-level decisions (record struct vs. record class, dictionary-backing choices, index selection) have already been litigated by the adversarial process. The editor runs the adversarial cycle autonomously. The human's checkpoints are: confirming the models compile, confirming the adversarial cycle terminated properly (termination criteria met), and spot-checking for anything that feels wrong at a glance. The human's real contribution was in Phase 1, where design intent, global fit, and "what if we did B instead of A" shaped the design that the models are derived from. Escalation still applies if the editor and reviewer deadlock.

**Phase 2 exit condition:** All review cycles for all artifacts have terminated (two consecutive cycles with only opinion/style findings, no blockers or should-fix-now items remaining, all escalations resolved or deferred). The design document, domain model, and data model are mutually consistent. The domain model compiles (or would compile) in the target language. The data model's physical contracts are explicit and unambiguous.

**Artifacts produced:** Amended design document, domain model, data model, critique reports (retained for reference). Decision log entries for any non-trivial decisions made during review.

**Artifact inventory check:** Before proceeding to Phase 3, verify that every artifact produced during Phases 1-2 is formally incorporated into the canonical document set. Ask: "Is there anything we produced or discussed that an implementer would need but that isn't in a tracked document?"

### Phase 3: Implementation Planning

**Participants:** Human + Director + Reviewer(s)

The director reads the finalized design document and all supporting artifacts (domain model, data model, contracts) and produces a structured implementation plan.

**Plan structure — three levels:**

| Level | Name | Purpose |
|-------|------|---------|
| 1 | Phase | Large program area establishing the rails |
| 2 | Deliverable | Concrete capability or subsystem slice that can be validated |
| 3 | Work Packet | Small, LLM-safe task with explicit scope, invariants, tests, and done condition |

**Every work packet must specify:**
- Objective — one crisp behavior or capability
- Files/modules expected to change — so scope creep is visible
- Invariants — what must remain true
- Tests to add — unit/integration/property/perf as appropriate
- Non-goals — what this packet must not redesign
- Done condition — the observable completion signal

**Detail gradient:** Work packets in the near term (next 2-3 deliverables) should be fully specified. Work packets further out should be described at intent level only ("one-paragraph intent statement per deliverable") and expanded when preceding work completes. Detailed specification of distant work is wasted effort because the design will evolve.

**The plan includes an explicit execution order** that makes the dependency chain unambiguous and prevents implementers from parallelizing things that must be serial.

**Adversarial review:** The implementation plan undergoes the same Phase 2 adversarial review process as the design document.

**Artifacts produced:** Implementation plan, execution order.

### Phase 4: Implementation

**Prerequisites:** Design document, domain model, and data model have all completed their adversarial review cycles (Phase 2 fully complete). Implementation plan has been reviewed (Phase 3 complete). Only then do the two parallel tracks begin.

**Participants:** Human + Director + Implementer(s) + Reviewer(s) + UI Designer Instance

Phase 4 runs two parallel tracks: the **core track** (autonomous LLM-to-LLM adversarial implementation) and the **UI track** (human + UI designer instance). The key principle is that human testing and approval happen as early in the process as possible, but in a way that doesn't drown the human in technical detail.

#### UI Track (Human-Driven, Starts Immediately)

The UI track begins at the same time as core implementation, not after it. The human and a UI designer instance design and iterate on the interface from day one.

**Mock data first:** The UI is initially built against mock data, enabling the human to test usability, workflow flow, and interaction design before any core backing code exists. This means the human is testing and providing feedback from the very start of Phase 4, not waiting for features to be built.

**Incremental wiring:** As core features are completed on the core track, they are wired to the UI, replacing mock data with real functionality. The human tests each feature with real data as soon as the wiring is complete.

**Wiring notifications:** A lightweight notification mechanism (potentially a low-parameter daemon model monitoring the core track's packet completion status, or simply Forge itself tracking which completed packets have corresponding UI features) informs the human when a feature has been wired and is ready for testing. The human should not need to poll or guess — they are told "feature X is now live, test it."

**Process:**
1. Human and UI designer instance collaborate on the interface design from the design sketch's workflow descriptions.
2. UI is built with mock data. Human tests usability and interaction flow.
3. As core packets complete, they are wired to the UI.
4. Notification fires: "packet N wired to UI, ready for testing."
5. Human tests the feature with real data.
6. UI-level bugs and behavioral problems are reported back to the core track as new work packets or feedback to the director.
7. Functional problems discovered through UI testing trigger the same escalation/reconciliation mechanisms as any other issue.

**Why mock-first works:** The human's architectural taste and sense of "this doesn't feel right" operate through using software. A human clicking through a mock workflow will discover wrong assumptions about user intent, missing edge cases in the interaction model, and confusing state transitions — before the backing code even exists. This front-loads the human's most valuable contribution.

#### Core Track (Autonomous)

The director composes prompts for implementer instances. After each block of implementation, the code goes through adversarial review between the implementer and one or more reviewers. The LLMs communicate directly — the human is not in the loop for individual implementation blocks.

**Per-block adversarial review:** Every block of implementation is adversarially reviewed before the next block begins. The implementer produces code, a reviewer critiques it, they debate disagreements until consensus. Then a second reviewer (different model) repeats the process against the amended code. This is the design-review pattern applied to implementation. It is expensive in tokens but prevents the accumulation of bugs that are far more expensive to find and fix at the end.

**LLM-to-LLM debate protocol:** When the implementer and reviewer disagree on a point, they exchange arguments until one of three outcomes: (a) one side convinces the other (consensus), (b) both recognize they've reached an impasse and escalate to the human, or (c) the director intervenes because the debate is cycling without progress. Outcome (b) should be rare if the design phase was thorough — most implementation-level disagreements have a clear answer in the design documents, contracts, or decision log.

**Asynchronous (report-based) variant:** Some projects calibrate per-block review to a lighter-weight form: a single auditor produces a findings report (findings, evidence, open questions — no live back-and-forth, no severity ratings) instead of debating the implementer in real time. This is a legitimate calibration (see Calibrating Process Weight), not a different process. The loop must still close the same way: the director triages each finding (incorporate / defer / reject / clarify), the implementer addresses whatever was incorporated, and **the fix goes back to the same auditor for a follow-up pass** — either a clean sign-off or a further round of findings — before the block is considered resolved. The director may spot-check the fix directly (inspecting the diff, rebuilding, re-reading the report) as an additional sanity check, but that inspection does not substitute for the auditor's own confirmation. The reason is the same as Principle 1: the instance that wrote the fix, or the director who requested it, is not the party positioned to judge whether the criticism was actually satisfied — that judgment belongs to the critic.

**Why the human is not in this loop:** The adversarial process between LLMs generates technical detail faster than any human can absorb. Attempting to review every implementation debate leads to either the project stalling (the human becomes the bottleneck) or rubber-stamping (the human approves without reading, which is worse than not reviewing). The human's oversight mechanism is the UI, not code review.

**Risk of LLM agreement on incorrect code:** If two LLMs agree on something wrong, no amount of code-level review will catch it — they share blind spots. The mitigation is the UI track: the human discovers incorrect behavior when the feature doesn't work right in the app. This is why UI-in-parallel is essential, not optional.

**Foundational work packets:** The first work packets commit the reviewed domain model and data model as executable code:

- *Domain model as compilable code:* The reviewed domain model file becomes a real project file. Strongly typed IDs, value types, enums, records, and factory methods are committed and buildable.
- *Schema bootstrap:* The reviewed data model (DDL) is embedded in the project and applied as the schema creation step.

These establish the compile-time and persistence boundaries that all subsequent implementers must build against.

**Director responsibilities during the core track:**
- Compose the prompt for each implementer instance
- Orchestrate the per-block adversarial review (route implementer output to reviewers, manage debate rounds)
- Inspect the repository directly (git status, git diff, file contents, test results)
- Review implementer edits to artifacts — flag the human if it disagrees
- Spawn reconciliation instances when document disagreements are flagged
- Escalate to the human on impasses, retry gate trips, and any problem beyond document misalignment
- Maintain a running situation summary for the human

**Retry gate:** Two consecutive implementer failures on the same work packet → hard stop → human escalation. LLMs fixating on a failed approach burn tokens without progress.

#### Implementation Block Protocol

For each work packet:

1. Director composes the implementer prompt with the appropriate context policy.
2. Implementer produces code + tests.
3. **First reviewer** (different model) critiques the implementation. Implementer and reviewer debate until consensus or impasse — or, under the asynchronous variant, the reviewer/auditor produces a findings report and the director triages disposition.
4. **Second reviewer** (different model again) critiques the post-first-review code. Same protocol.
5. When all reviewers are satisfied (or impasses are escalated), the block is committed. Under the asynchronous variant, "satisfied" means the reviewer/auditor has confirmed each incorporated fix in a follow-up pass, not merely that the director judged the fix adequate.
6. Director inspects the result, runs tests, and advances to the next packet.
7. If the completed packet has a corresponding UI feature, the wiring notification fires and the human tests it.

**Implementer protocol:**
1. Read current-focus.md first.
2. Stay inside the named work packet.
3. Preserve listed invariants.
4. Add or update tests in the same change.
5. Do not redesign adjacent systems unless the task explicitly says to.
6. If a design conflict appears, stop and record it in the decision log.
7. Prefer completing one work packet fully over partially touching several.
8. When a packet finishes, update current-focus.md and status in the implementation plan.

**Document reconciliation:** When an implementer detects a disagreement between documents, it stops and reports. The director spawns a reconciliation implementer instance. The reconciliation instance fixes the documents and reports changes. The human reviews and approves before the original implementer resumes.

**Test integrity passes:** After completing each Level 1 phase, run a dedicated pass that verifies the tests are actually testing what they claim. The per-block adversarial review catches most test-theater issues in real time, but the integrity pass catches patterns that only become visible across multiple blocks.

**Director audit checkpoints:** At the completion of every Level 2 deliverable, the human must directly review: the final diff summary, test summary, current-focus update, and decision-log changes. This supplements the UI-based oversight with periodic direct inspection of the artifacts.

**Artifacts updated by implementers:** Decision log, current-focus.md, testing strategy, implementation plan status. The director reviews these updates and flags the human if it disagrees.

---

## Artifacts

### Design Document (Informal Spec)

The system's behavioral specification. Not a formal spec in the academic sense, but precise enough that an implementer should not need to guess about intended behavior. Maintained by the editor, amended through adversarial review.

### Domain Model

The system's type structure expressed as compilable code in the implementation language — strongly typed IDs, value types, enums, aggregates, records, and factory methods. Split into separate files per concern and placed in the project as real classes (e.g., `Loom.Core/Domain/StreamId.cs`, `Loom.Core/Domain/RawEventKind.cs`). This is not documentation *about* the code — it *is* the code.

Produced during Phase 2 Stage 2 from the reviewed design document, reviewed adversarially, and committed as the first implementation work packets.

The domain model is the authoritative shape reference for implementers: when an implementer needs to know what a `ChangeInsertionRequest` contains or what states a `PipelineRunStatus` can take, the domain model is the source of truth, not the implementer's imagination. It prevents category mistakes at compile time and ensures every instance uses the same vocabulary for the same concepts.

The domain model evolves during implementation through deliberate amendment with a decision log entry, not through implementer drift. If an implementer discovers that a type needs to change, the design document is updated first to confirm the change aligns with system intent, then the domain model is updated to match, then the dependent code.

### Data Model

The persistence schema — e.g., a SQLite DDL file embedded in the project as the actual schema bootstrap script, a PostgreSQL migration file. This is not a reference document under `docs/` — it is the code that creates the database. The DDL is the single authoritative source for the physical storage contracts.

Produced during Phase 2 Stage 2 from the reviewed design document, reviewed adversarially, and embedded as the schema bootstrap in the first implementation work packets.

The data model defines the physical storage contracts: which columns are BLOBs vs TEXT vs INTEGER, which tables are dictionary-backed, which relationships are enforced by foreign keys, which indexes exist for hot access patterns. Implementers building storage code must implement against this schema, not invent their own. This prevents the expensive reconciliation that occurs when multiple implementers make different assumptions about physical formats.

Like the domain model, the data model evolves through deliberate amendment with a decision log entry. If implementation reveals that the schema needs to change, the design document is updated first to confirm the change aligns with system intent, then the DDL is updated, then the code is adjusted to match.

### Implementation Plan

Three-level structure (phase / deliverable / work packet) with explicit execution order. Living document updated as work progresses. See Phase 3 for full specification.

### Decision Log (`docs/decision-log.md`)

Records *why* decisions were made, not just *what* was decided. Critical for preventing future instances from relitigating settled questions. Every entry should include:

- A short identifier (e.g., D-001)
- The decision
- The reasoning ("Why")
- The consequence for implementation
- Who made the call or what superseded it ("Changed by," if applicable)
- Date

Entries should be short. This is not a diary. It is a reference that implementer instances can check when they encounter a design question that feels like it should already be settled.

### Current Focus (`docs/current-focus.md`)

The authoritative pointer to the active work packet, immediate next steps, and blockers. Acts as an attention mechanism — each new instance reads this first to know what matters right now. Must either name exactly one active work packet or explicitly declare that the repository is in planning-only / designation mode (no implementation packet is active). This accounts for the real operational states where the project is between packets — during reconciliation, between deliverables, or during planning phases.

A well-structured current-focus includes: the current phase and deliverable, the active work packet and its status, the objective, expected files to change, invariants to preserve, tests required, non-goals, done condition, known blockers, next three tasks, and notes for the next coding session (e.g., which files are the relevant seams).

### Contracts Document (`docs/contracts.md`)

Defines the stable boundaries that should be locked down with tests before broad implementation proceeds. Covers wire shapes, persistent payload rules, error/result semantics, CLI behavior, hook/pipeline contracts, and compatibility rules. Distinct from the testing strategy (which says what *kinds* of tests to write) — the contracts document says what the *boundaries are* and what specific invariants must hold at each boundary.

### Testing Strategy (`docs/testing-strategy.md`)

The approach to testing for the project. Covers test layers (unit, integration, end-to-end, performance), required test philosophy by subsystem, false-green guardrails, and the initial high-value test sequence. Updated as the project evolves and new anti-patterns are discovered.

### Behavioral Contract (`AGENTS.md`)

The evolving behavioral rules for implementer instances, maintained by the director and updated based on observed failure modes. This is where "lessons learned" become enforceable constraints. When an implementer writes tautological tests, or silently resolves document conflicts, or redesigns adjacent systems, the fix goes into AGENTS.md so future instances are explicitly told not to repeat the behavior.

This document evolves continuously during implementation and tends to get stricter over time. It serves a different purpose from the testing strategy or contracts document — those say what to build and test; AGENTS.md says *how to behave* as an implementer, including what not to do.

### Test Integrity Pass Template

A reusable prompt template for the test integrity pass run after each Level 2 deliverable. Specifies: what to read, what the scope is (tests and docs only, no new implementation), what findings to fix, what constraints apply, and what validation looks like. The template is customized for each pass with the specific findings from that deliverable's review.

### Critique Reports (retained)

Reports from adversarial reviewers, kept for reference. Useful for understanding what concerns were raised and how they were resolved. Not actively maintained after the review phase completes.

---

## Phase Boundary Checklists

### Before leaving Phase 1 (Design) for Phase 2 (Review)

- The human can read the entire design document without producing notes.
- The editor and human agree that the design is ready for adversarial review.
- The domain model and data model do not exist yet — they are generated after the design document is reviewed, not before.

### Before leaving Phase 2 Stage 1 (Design Review) for Stage 2 (Model Generation)

- All reviewer critique cycles for the design document have terminated.
- All escalated items have been adjudicated by the human.
- The design document is stable enough to generate the domain model and data model from it.

### Before leaving Phase 2 Stage 2 (Model Review) for Phase 3 (Planning)

- The domain model and data model have been generated from the reviewed design document.
- Both models have undergone adversarial review.
- The design document, domain model, and data model are mutually consistent.
- The domain model compiles (or would compile) in the target language. Type-level errors caught now are free; type-level errors caught during implementation are expensive.
- The data model's physical contracts are explicit and unambiguous — no column where a reasonable implementer could disagree about the storage format.
- The decision log captures every non-trivial design decision made during review.
- No artifact produced during Phases 1-2 is floating outside the canonical set.

### Before leaving Phase 3 (Planning) for Phase 4 (Implementation)

- The implementation plan has undergone adversarial review.
- The first work packets in the execution order commit the domain model as compilable code and embed the data model as the schema bootstrap. These are not optional — they are the mechanism by which the reviewed design becomes authoritative for implementation.
- Near-term work packets (next 2-3 deliverables) are fully specified with objectives, expected files, invariants, tests, non-goals, and done conditions.
- Later work packets are described at intent level only.
- An explicit execution order exists.
- The contracts document is populated for all boundaries that near-term packets will touch.
- The testing strategy is written.
- AGENTS.md exists with initial behavioral rules.
- current-focus.md points to the first work packet.

---

## Artifact Authority Order

When artifacts disagree, the resolution depends on which domain the conflict falls in. Each artifact is authoritative for its own concern:

| Artifact | Authoritative for |
|----------|-------------------|
| Design document | Intent, high-level behavior, system shape |
| Domain model | Types, vocabulary, compile-time contracts |
| Data model | Physical storage format, schema contracts |
| Contracts document | Stable boundary definitions, wire shapes |
| Decision log | Why decisions were made, durable rulings |
| Current-focus | Which work packet is active right now |
| Implementation plan | Sequencing, work packet definitions, execution order |
| AGENTS.md | Behavioral rules for implementer instances |
| Testing strategy | What kinds of tests to write and how |

**Authority order for implementation:** When an implementer needs to know how to build something, the domain model and data model are authoritative for *shape*, the contracts document is authoritative for *boundary behavior*, and the implementation plan is authoritative for *scope*. The design document provides context and intent but does not override the narrower artifacts on their own domains.

**Authority order for domain-shape conflicts:** The domain model and data model have *read authority* — when an implementer needs to know the current type or storage format, the compiled model is the source of truth, not the design document. However, *change authority* flows in the opposite direction: if the domain model or data model needs to change during implementation, the design document is updated first to confirm that the change aligns with the system's intent and global fit, and then the model is deliberately updated to match the confirmed intent. The compiled artifact is authoritative for the compiler; human intent remains upstream. This prevents a rushed model change from reversing the flow of design intent and forcing the design document to retroactively justify a decision that was never validated for global fit.

**Authority order for packet designation:** current-focus.md is the authoritative pointer to the active work packet. If current-focus and the implementation plan disagree, that is a document disagreement that triggers the reconciliation flow — the implementer stops rather than guessing.

**Tie-breaking rule:** When two artifacts overlap on the same concern, the narrower, more specialized artifact wins, unless superseded by an explicit durable ruling in the decision log.

---

## Critique Severity Classification

Review points should be classified by severity to reduce fatigue and improve prioritization:

- **Blocker** — the design or implementation cannot proceed safely without addressing this.
- **Should fix now** — a real issue that will get more expensive if deferred.
- **Note for later** — valid concern, but not urgent and can be addressed in a future phase.
- **Opinion / style** — a preference rather than a correctness issue. The editor may incorporate or dismiss without debate.

Reviewers should classify their findings. The human can use severity to triage review points rather than treating every point with equal weight. Blockers must be resolved before the review cycle terminates; opinions can be dismissed without escalation.

---

## Abort and Reset Criteria

The process is well-defined for critique, amendment, reconciliation, and forward progress. It is less explicit about when the right answer is to *discard* work and start over. This section defines when that is appropriate.

**Discard a work packet's output** when:

- The code is green but clearly built against the wrong contract (e.g., the implementer used inline path text when the data model specifies dictionary-backed path_id references).
- A reconciliation pass changed an authoritative boundary underneath in-flight code, invalidating the assumptions the implementer was working from.
- An implementer silently crossed packet boundaries, and the contaminated work is too broad to fix piecemeal.

**Restart with a fresh instance** when:

- The director's context window has filled and its recommendations are noticeably degrading.
- An implementer is stuck in a loop of failed attempts and each retry is inheriting bad assumptions from the prior attempt.

**Revert a deliverable-level design choice** when:

- A test integrity pass reveals that the tests for the deliverable are fundamentally unsound (not just weak — unsound), indicating the implementation may be built on false confidence.
- Integration with the next deliverable reveals that the completed work rests on an incorrect assumption from the design phase that wasn't caught in review.

In all cases, the human makes the abort/reset decision, not the director. The director can recommend it, but discarding work is a judgment call with cost implications that the human must own.

---

## Durable Exceptions and Waivers

Every real project will eventually need to intentionally violate one of this methodology's defaults — skipping a review cycle under time pressure, accepting a work packet without full test coverage because the boundary is about to change, or allowing an implementer to touch files outside its declared scope because the alternative is worse.

When this happens:

- The exception is recorded in the decision log with the reasoning.
- The human approves it (exceptions are never self-granted by an instance).
- The entry specifies when the exception must be revisited — either at a specific milestone, after a specific deliverable, or "permanent."
- If the exception is permanent, it becomes a process amendment rather than a waiver.

This mechanism makes the methodology less brittle without making it less rigorous. An exception that is logged, approved, and time-bounded is different from a process that is silently violated.

---

## Performance Contracts and Benchmark Checkpoints

The methodology handles correctness and architecture well but is underdeveloped on performance. For systems work (databases, version control, networking), performance is a correctness concern — a system that produces the right answer too slowly is broken in practice.

**When performance contracts should enter the process:**

Early work packets are allowed to be naive about performance — the priority is correct behavior on a stable foundation. But once the project leaves the purely architectural stage (roughly: once the first end-to-end workflow is complete), performance contracts should be introduced:

- *Benchmark suites* for the hot paths identified in the design document.
- *Scaling thresholds* that define "fast enough" for the intended workload (e.g., "append 100k events in under N seconds").
- *Performance regression gates* at major milestone boundaries — a deliverable is not complete if it regresses a previously-passing benchmark without justification.

Performance contracts belong in the contracts document alongside behavioral contracts. They are subject to the same amendment-with-decision-log discipline: if a benchmark threshold needs to change, the contracts document is updated with reasoning before the code is adjusted.

**What to avoid:** Speculative performance optimization before benchmarks exist. The methodology's existing guidance ("do not perform speculative performance rewrites based on intuition alone") applies. Measure first, optimize with evidence, record the result.

---

## Anti-Patterns and Failure Modes

### Test theater

LLMs will write tests that pass without testing anything meaningful. Common patterns observed in practice:

- *Tautological assertions:* `Assert.True(true)` or equivalent constant assertions dressed up as smoke tests.
- *Banner-string testing:* Asserting that a wrapper script printed "SUCCESS" to stdout instead of verifying it actually delegated the work or propagated failures.
- *Mock-satisfiable boundaries:* Writing a test for a native interop boundary that could pass unchanged if the native library were replaced with a managed stub — proving nothing about the actual boundary crossing.
- *Success-path-only coverage:* Testing that the happy path works without ever testing what happens when the dependency is missing, the input is malformed, or the transaction fails partway through.

The test integrity pass exists to catch this, but the human should develop a nose for it. The specific false-green guardrails discovered during implementation should be encoded in the testing strategy and AGENTS.md so future instances are explicitly warned.

### Coverage chasing

Test coverage as a metric creates a perverse incentive for LLMs to write tests that exercise lines of code without testing meaningful behavior. Do not give coverage targets to instances. If coverage information is useful to the human, keep it as a human-only diagnostic.

Consider mutation testing at major milestone boundaries as an alternative signal: "can you break the code in a way no existing test catches?" This is adversarial in the same spirit as design review and harder to game.

### Scope creep via "improvement"

An implementer asked to build X will often "improve" adjacent systems Y and Z while it's in there. The work packet's "files/modules expected to change" and "non-goals" fields exist to make this visible and preventable.

### Silent conflict resolution

When two documents disagree — e.g., current-focus says the active packet is 6.2.1 but the implementation plan says 6.1.3 — an LLM will silently pick one and proceed. This is dangerous because the human may not notice the inconsistency, and the instance's choice may be wrong. The implementer prompt must explicitly instruct: "If documents disagree about the active packet, stop immediately and report the mismatch instead of choosing one silently." This turns document inconsistency into a hard stop rather than a silent guess.

### Design-document drift

After Phase 2 Stage 2, truth is distributed across the artifact set, not concentrated in the design document. Each artifact is authoritative for its own domain:

- *Design document* = intent and high-level behavior
- *Domain model* = type and vocabulary authority
- *Data model* = physical storage authority
- *Contracts document* = stable boundary authority
- *Decision log* = why and durable rulings
- *Current-focus* = active packet authority
- *Implementation plan* = sequencing authority

If implementation reveals that something needs to change, the *authoritative artifact for that concern* is updated first, then the code. However, for changes to the domain model and data model specifically, intent must be confirmed upstream in the design document before the model is changed — the compiled artifacts have *read authority* (implementers build against them), but *change authority* flows from intent through design to model (see Artifact Authority Order > domain-shape conflicts). Each artifact must stay consistent with the others, but "source of truth" is per-domain, not global.

### Director capture

The director has significant practical authority: it writes prompts, inspects the repo, reviews output, and runs the implementation loop autonomously within deliverables. This creates a risk of *director capture* — the human over-trusts the director's summaries, and a subtle director mistake replicates across many packets before a deliverable boundary catches it.

Safeguard: at defined intervals (not just deliverable boundaries), the human should sample raw diffs and test results directly rather than relying solely on the director's compressed briefing. This keeps the director from becoming an unreviewed abstraction layer between the codebase and the human.

### Echo-chamber review

Using the same model for both authoring and reviewing defeats the purpose. The value of adversarial review comes from cognitive diversity — different models have different blind spots. If you must use the same model family, at minimum use a completely fresh instance with no shared context.

### Automation-induced disengagement

The adversarial process between LLMs generates technical detail faster than any human can absorb. Attempting to review every implementation debate leads to either project stalls (human bottleneck) or rubber-stamping (human approves without reading, which is worse than not reviewing). The solution is not more human code review — it is giving the human a feedback mechanism that scales: **the UI**. The human stays engaged by using the software, not by reading the source code. Functional testing through the UI catches problems that code review misses (wrong assumptions about user intent, confusing state transitions, missing edge cases) and does so at a pace the human can sustain. If the human is not testing through the UI, they are disengaged regardless of how many reports they read.

---

## Calibrating Process Weight

This methodology is designed for complex, architecturally significant projects where the cost of getting the design wrong exceeds the cost of the process. Not every project needs every phase.

**Full process:** Novel systems with deep dependency graphs, unfamiliar domains, or high correctness requirements. Example: a version control system, a database engine, a financial system.

**Skip Phase 2 second reviewer:** When the first reviewer's critique cycle produces only minor findings and the domain is well-understood.

**Collapse Phases 1-2:** When the design space is small or well-precedented. Go straight from a brief design sketch to implementation planning.

**Skip Phase 3 adversarial review:** When the implementation plan is straightforward and the design has already been heavily reviewed. The plan is mostly a sequencing exercise in this case.

**Minimal process:** Well-understood CRUD applications, standard integrations, or small utilities. Use the work-packet structure and testing discipline without the multi-instance adversarial overhead.

The key question: "If this design has a subtle flaw, how expensive is it to fix later?" If the answer is "very," use the full process. If the answer is "I can refactor it in an afternoon," use the minimal version.

---

## Tooling Considerations

### Current state

The human does everything through copy-paste, manually managing context windows and routing outputs between instances. This works and has the advantage of forcing engagement, but at scale it causes info fatigue. Reading a 40-point adversarial report, copying it to another tab, waiting for the editor's response, reading that, agreeing with 95% of it, and repeating — the mechanical overhead eventually degrades the quality of human attention, which is the opposite of the intent.

The goal of tooling is to shift human effort from *mechanics* (selecting text, switching tabs, composing prompts, attaching documents) to *judgment* (reading each review point, understanding the criticism, evaluating the editor's response, making a call). The human should spend their attention budget on content, not on clipboard management.

### Guiding principle

Automate the plumbing. Never automate the decisions. The human must still read every review point, approve every action, and retain the ability to stop, redirect, or override at any moment. The UI should make thoughtful engagement the path of least resistance, not make rapid approval the path of least resistance.

### Target UX architecture

#### Adversarial review workflow

Instead of manually composing adversarial prompts, attaching documents, and copy-pasting reports between tabs:

1. The human clicks a button to initiate a review round. The system sends the current document set to the configured reviewers (potentially in parallel for the first round).
2. As review reports come back, the editor instance automatically receives each report and produces its assessment of each point.
3. Each review point is presented to the human individually: the reviewer's criticism on one side, the editor's response on the other.
4. Each review point is presented to the human individually: the reviewer's criticism on one side, the editor's response (incorporate or dismiss, with reasoning) on the other. The human then makes the final call:
   - **Incorporate** — accept this criticism and amend the document.
   - **Ignore** — this criticism is not worth acting on.
   - **Elaborate** — the point is interesting but underspecified; send it back for more detail.
   - **Examples** — request illustrative examples to clarify the concern.
   - **Defer** — valid concern but not for this review cycle; park it for later.
   - A free-text field for the human's reasoning, especially on disagreements and deferrals.

   When the human's choice disagrees with the editor's recommendation — in either direction (human ignores something the editor wanted to incorporate, or human incorporates something the editor dismissed) — the UI should surface a dialogue rather than accepting the override immediately. The editor presents its case for why it recommended what it did, and the human can engage in a brief back-and-forth before confirming. This serves two purposes: it catches human mistakes (the editor may have context the human forgot), and it creates a record of *why* the override happened, which is valuable for the decision log. The human's decision after the dialogue is final.
5. After all points are resolved, the amended document is produced and the human reviews the diff before the next cycle (or termination).

**Key design detail:** Reviewers should be prompted to identify problems and explain why they matter, but *not* to propose fixes. In practice, the reviewer's diagnosis is almost always valuable but its suggested fix is almost always wrong — it lacks the full context the editor has. Requesting diagnosis-only shortens reports, improves signal-to-noise, and prevents the editor from spending half its response refuting a bad fix rather than thinking about the real issue.

**Engagement safeguard:** Consider not showing the editor's recommendation until the human has indicated they've read the reviewer's point (e.g., a brief delay, or requiring a click to reveal the editor's assessment). This prevents the "just agree with the editor" reflex that would otherwise develop when the human agrees 95% of the time. The disagreement dialogue provides a second safeguard: even when the human does click reflexively, if they accidentally disagree with the editor, the dialogue that follows will surface the mistake before it becomes final.

#### Context assembly

Composing the right prompt for an implementer currently means manually gathering the work packet spec, current-focus, relevant design sections, contracts, domain model, data model, testing strategy, AGENTS.md, and decision log. The tool should:

- Automatically assemble the context package based on the active work packet and its declared dependencies.
- Show the human the assembled prompt before sending, with the ability to edit, add context, or remove irrelevant sections.
- Track which documents were included in each implementer's prompt for auditability.

#### Artifact management

Keeping the decision log, current-focus, testing strategy, and implementation plan status synchronized is error-prone. The tool should:

- Make these artifacts easy to update from within the workflow (e.g., after resolving a review point, the decision log entry is drafted automatically and the human approves or edits it).
- Show diffs between document versions across review rounds so the human can see exactly what changed.
- Flag inconsistencies between documents (e.g., current-focus names a different packet than the implementation plan) rather than letting them accumulate silently.

#### Implementation tracking

- A dashboard showing the current phase, active work packet, overall progress through the implementation plan, and recent decision log entries.
- Test integrity pass results visible alongside the deliverable they cover.
- The ability to see the history of each review point: what was raised, what was decided, and why.

### What not to automate

Even in a fully tooled environment, the following remain human actions:

- **Initiating review rounds** — the human decides when the document is ready for review, not the tool.
- **Terminating review cycles** — the human (with the editor's recommendation) decides when further critique is not worthwhile.
- **Approving each review point** — no batch "accept all" operation. Each point gets individual attention.
- **Disagreeing with the editor** — any override in either direction (human ignoring something the editor wanted, or human incorporating something the editor dismissed) triggers a dialogue where the editor makes its case. The human's decision after that dialogue is final and logged.
- **Advancing to the next work packet** — the human confirms completion, not the tool.

### Parallel vs. sequential review in a tooled environment

With tooling, sending to multiple reviewers in parallel for the first round becomes practical and saves significant wall-clock time. The tradeoff is that the second reviewer doesn't benefit from seeing the document as amended by the first reviewer's cycle. A reasonable hybrid: parallel first rounds, then sequential follow-up rounds only when needed. If the first round from each reviewer produces mostly distinct findings (which different models tend to do), parallelism costs little in finding quality while halving the wait time.

---

## Settled Questions (Previously Open)

These were open questions that have been resolved through operational experience.

**Context window continuity:** With models that support compaction (e.g., Opus), compaction handles continuity naturally — the director's context is compressed rather than lost. When compaction is unavailable and a fresh instance is needed, the outgoing director writes a *fill-in-the-gaps reasoning log* — compressed, abbreviated English capturing the judgment and reasoning that didn't make it into any formal artifact. The project artifacts provide shared context; the diary provides what the successor can't infer from the artifacts alone. Compression comes from omission (don't repeat what's in the artifacts), not from symbolic encoding. Experimentation confirmed that symbolic/non-language formats fail without shared context, while telegraphic natural language with artifact-backed context works reliably.

**Reviewer ordering:** Configurable per project. The strongest reviewer should go first (currently Gemini 2.5 Flash for its review quality), but this changes as models evolve. The methodology does not prescribe a fixed ordering — the human configures reviewer sequence based on current model strengths.

**Director exception thresholds:** An implementer forgetting to update current-focus.md is classified as document misalignment (misaligned with project state rather than with another document) and falls under the existing reconciliation flow. This means the director's exception categories remain clean: document misalignment is handled by spawning a reconciliation instance, everything else escalates to the human.

**Plan detail horizon:** The "next 2-3 deliverables" heuristic is the default, but this should be configurable per project. The director may recommend a configuration to the human at the start of Phase 4 based on its assessment of the project's complexity and dependency density.

**Cross-project learning:** The director handoff diary serves double duty — it captures operational knowledge for future directors on the same project *and* for directors on other projects. Project retrospectives (exported from Forge's prompt history and review session records) provide additional calibration material.

**Test integrity pass timing:** Initially, integrity passes find egregious issues (tautological tests, mock-satisfiable boundaries). After the findings are encoded in AGENTS.md and the testing strategy, subsequent passes find only smaller issues (neglected edge cases). The pass should run at every Level 1 phase boundary rather than every Level 2 deliverable — early passes tighten the rules, and later passes confirm the rules are holding.

**Design reconsideration vs. reconciliation:** This is the human's call. When a problem surfaces and the human concludes it stems from a design flaw (not just a document misalignment or implementation error), the editor is recalled to help resolve it. The human and editor produce a plan to either alter the existing implementation or restart from the divergence point in code history — usually a combination of both. Repeated reconciliation that doesn't resolve the underlying issue is the signal that design reconsideration is needed.

**Version-control hygiene:** Operational practice that works: commit every round of implementer work, push every Level 3 goal completion, test integrity passes are their own push. Branches are used to save backtracked work, but their practical value is limited — the director's context eventually loses awareness of old branches, and the human is unlikely to recall that a branch from weeks ago has reusable content. Backtracked code is effectively dead; it will not be reincorporated. The director tends to preserve branches rather than delete them, which is harmless but rarely beneficial.

**Director-recommended configuration:** At the start of Phase 4, the director should formally propose a project-specific configuration to the human for approval: reviewer ordering, plan detail horizon, test integrity frequency, retry gate thresholds, and any other configurable process parameters. This makes process calibration explicit rather than implicit and gives the human a single decision point for process tuning.

---

## Remaining Open Questions

None at this time. The handoff diary format has been resolved through experimentation:

**Handoff diary format (resolved):** The diary is a *fill-in-the-gaps reasoning log* — compressed, abbreviated English that captures the judgment, priorities, and reasoning that didn't make it into any formal artifact. The project artifacts (design doc, domain model, implementation plan, decision log, current-focus, AGENTS.md) provide the shared context; the diary provides what the successor can't infer from the artifacts alone. Compression comes from *omission* (the successor has the artifacts, so don't repeat them) rather than from symbolic encoding. Telegraphic style, no articles, domain abbreviations — think military radio shorthand, not hieroglyphics. The artifacts plus the diary together give the successor enough context to operate at full capacity.
