// Application-layer authorization, mirroring server.py's
// OPERATION_ALLOWED_ROLES / require_operation_allowed. This is real
// enforcement, not decoration: schema.sql's `require_actor(p_allowed_roles,
// action)` accepts a role array per call site but never reads it (see
// PORT-NOTES.md) -- it only checks that the DB *session* role is
// ticket_board_service, which is true for every HTTP request alike. So the
// per-operation "who" check lives entirely here, same as it does in Python.
import { CALLER_ROLE_HEADER } from "./constants.ts";
import { CALLER_ROLES, type CallerRole, isCallerRole } from "./types.ts";

export type Operation = "create_ticket" | "route";

const OPERATION_ALLOWED_ROLES: Record<Operation, readonly CallerRole[]> = {
  create_ticket: ["director", "eric"],
  route: ["director"],
};

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

export const ALL_CALLER_ROLES = CALLER_ROLES;
