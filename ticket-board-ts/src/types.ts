// Ticket state machine, modeled as a string-literal discriminated union so
// that adding a state without updating every switch is a compile error, not
// a silent runtime fallthrough. Ground truth enforcement is still the
// `enforce_ticket_workflow_update` trigger in schema.sql (SECURITY DEFINER,
// single writer) -- this is a fail-fast client-side mirror, not a bypass.

export const TICKET_STATES = [
  "backlog",
  "analysis",
  "ready",
  "in_progress",
  "audit",
  "user_review",
  "director_review",
  "done",
  "cancelled",
] as const;

export type TicketState = (typeof TICKET_STATES)[number];

export const ASSIGNEES = [
  "unassigned",
  "main",
  "app",
  "perf",
  "ops",
  "audit",
  "agent",
  "director",
  "research",
] as const;

export type Assignee = (typeof ASSIGNEES)[number];

export const CALLER_ROLES = [
  "director",
  "user",
  "main",
  "app",
  "ops",
  "perf",
  "audit",
  "research",
] as const;

export type CallerRole = (typeof CALLER_ROLES)[number];

function assertNever(value: never): never {
  throw new Error(`unhandled ticket state: ${String(value)}`);
}

/**
 * The legal next-state set per current state. This is the TS mirror of
 * `enforce_ticket_workflow_update`'s adjacency checks (schema.sql). See
 * PORT-NOTES.md for why a `switch` here is a strictly stronger guarantee
 * than a lookup object.
 */
export function legalNextStates(state: TicketState): readonly TicketState[] {
  switch (state) {
    case "backlog":
      return ["analysis", "ready"];
    case "analysis":
      return ["ready", "backlog", "cancelled"];
    case "ready":
      return ["in_progress", "analysis", "backlog", "cancelled"];
    case "in_progress":
      return ["audit", "ready", "analysis", "backlog", "cancelled"];
    case "audit":
      return ["user_review", "director_review", "analysis", "backlog", "cancelled"];
    case "user_review":
      return ["director_review", "audit", "analysis", "backlog", "cancelled"];
    case "director_review":
      return ["done", "ready", "analysis", "backlog", "cancelled"];
    case "done":
      return ["analysis", "backlog"];
    case "cancelled":
      return ["analysis", "backlog"];
    default:
      return assertNever(state);
  }
}

export function isLegalTransition(from: TicketState, to: TicketState): boolean {
  if (from === to) return true;
  return legalNextStates(from).includes(to);
}

export function isTicketState(value: string): value is TicketState {
  return (TICKET_STATES as readonly string[]).includes(value);
}

export function isAssignee(value: string): value is Assignee {
  return (ASSIGNEES as readonly string[]).includes(value);
}

export function isCallerRole(value: string): value is CallerRole {
  return (CALLER_ROLES as readonly string[]).includes(value);
}

export interface TicketComment {
  who: string;
  text: string;
  ts: string;
}

export interface Ticket {
  id: string;
  title: string;
  body: string;
  assignee: Assignee;
  state: TicketState;
  blocked_by: string[];
  parent_id: string;
  blocked_reason: string;
  implementation: string;
  audit_prompt: string;
  audit_signoff: boolean;
  needs_user_signoff: boolean;
  user_signoff: boolean;
  manually_controlled: boolean;
  commit_hash: string;
  commit_exempt: boolean;
  created: string;
  updated: string;
  comments: TicketComment[];
  screenshots: string[];
  screenshots_info: { path: string; available: boolean }[];
  screenshot: string | null;
  screenshot_available: boolean;
}
