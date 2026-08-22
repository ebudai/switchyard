// Application-layer authorization, mirroring server.py's
// OPERATION_ALLOWED_ROLES / require_operation_allowed. This is real
// enforcement, not decoration: schema.sql's `require_actor(p_allowed_roles,
// action)` accepts a role array per call site but never reads it (see
// PORT-NOTES.md) -- it only checks that the DB *session* role is
// ticket_board_service, which is true for every HTTP request alike. So the
// per-operation "who" check lives entirely here, same as it does in Python.
import { CALLER_ROLE_HEADER } from "./constants.ts";
import { CALLER_ROLES, type Assignee, type CallerRole, isCallerRole } from "./types.ts";

// The full write-action surface from server.py's OPERATION_ALLOWED_ROLES.
// `Record<Operation, ...>` below is the exhaustiveness device: TS refuses to
// compile if an Operation is added here without a matching entry in the
// roles map (auth.ts) or the dispatch table (server.ts) -- the same
// guarantee legalNextStates() gives the state machine in types.ts.
export type Operation =
  | "create_ticket"
  | "file_bug"
  | "route"
  | "start_work"
  | "submit_to_audit"
  | "audit_sign_off"
  | "audit_kick_back"
  | "user_sign_off"
  | "user_reopen"
  | "mark_done"
  | "defer"
  | "cancel"
  | "set_manually_controlled"
  | "set_blockers"
  | "add_comment"
  | "edit_fields"
  | "merge";

const IMPLEMENTER_ROLES = ["main", "app", "ops", "perf", "research"] as const satisfies readonly CallerRole[];

const OPERATION_ALLOWED_ROLES: Record<Operation, readonly CallerRole[]> = {
  create_ticket: ["director", "user"],
  file_bug: IMPLEMENTER_ROLES,
  route: ["director"],
  start_work: IMPLEMENTER_ROLES,
  submit_to_audit: IMPLEMENTER_ROLES,
  audit_sign_off: ["audit"],
  audit_kick_back: ["audit"],
  user_sign_off: ["user"],
  user_reopen: ["user"],
  mark_done: ["director"],
  defer: ["director"],
  cancel: ["director"],
  set_manually_controlled: ["director"],
  set_blockers: ["director"],
  add_comment: CALLER_ROLES,
  edit_fields: CALLER_ROLES,
  merge: ["director"],
};

// server.py's require_operation_allowed also gates start_work/submit_to_audit
// on the CALLER being the ticket's current assignee, on top of the role
// check above -- an implementer role alone isn't enough; it must be *your*
// ticket.
const ASSIGNEE_GATED_OPERATIONS = new Set<Operation>(["start_work", "submit_to_audit"]);

export class AuthError extends Error {
  constructor(message: string, readonly status: 400 | 403) {
    super(message);
  }
}

export function callerRoleFromHeaders(headers: Headers): CallerRole {
  const raw = headers.get(CALLER_ROLE_HEADER) ?? "";
  const role = raw.trim().toLowerCase();
  if (!role) {
    throw new AuthError(`missing ${CALLER_ROLE_HEADER}`, 400);
  }
  if (!isCallerRole(role)) {
    throw new AuthError(`invalid caller role: ${raw}`, 400);
  }
  return role;
}

export function requireOperationAllowed(operation: Operation, caller: CallerRole): void {
  const allowed = OPERATION_ALLOWED_ROLES[operation];
  if (!allowed.includes(caller)) {
    throw new AuthError(`${caller} cannot call ${operation}`, 403);
  }
}

const ALL_OPERATIONS = Object.keys(OPERATION_ALLOWED_ROLES) as Operation[];

export function isOperation(value: string): value is Operation {
  return (ALL_OPERATIONS as string[]).includes(value);
}

export function requiresAssigneeMatch(operation: Operation): boolean {
  return ASSIGNEE_GATED_OPERATIONS.has(operation);
}

export function requireAssigneeMatch(operation: Operation, caller: CallerRole, ticketAssignee: Assignee): void {
  if (ticketAssignee.trim().toLowerCase() !== caller) {
    throw new AuthError(`${caller} cannot call ${operation} for ticket assigned to ${ticketAssignee}`, 403);
  }
}

export const ALL_CALLER_ROLES = CALLER_ROLES;
