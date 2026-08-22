// HTTP routing for the full write-action surface: GET /api/board, POST
// /api/tickets/actions/<create_ticket|file_bug>, POST
// /api/tickets/<id>/actions/<op>. Response shapes and error-status mapping
// mirror server.py's do_GET/handle_ticket_action exactly (see
// PORT-NOTES.md for the one intentional response-shape divergence: merge).
import type { Sql } from "./db.ts";
import { boardSnapshot } from "./board.ts";
import { EventHub, eventStream } from "./events.ts";
import {
  addComment,
  auditKickBack,
  auditSignOff,
  cancelTicket,
  createTicket,
  deferTicket,
  editFields,
  ericReopen,
  ericSignOff,
  fileBug,
  getStoreSignature,
  getTicket,
  markDone,
  mergeTickets,
  routeTicket,
  setBlockers,
  setManuallyControlled,
  startWork,
  submitToAudit,
} from "./ticket_store.ts";
import {
  AddCommentBodySchema,
  CommitHashBodySchema,
  CreateTicketBodySchema,
  EditFieldsBodySchema,
  FileBugBodySchema,
  MergeBodySchema,
  ReasonBodySchema,
  RouteBodySchema,
  SetBlockersBodySchema,
  SetManuallyControlledBodySchema,
} from "./schemas.ts";
import {
  AuthError,
  callerRoleFromHeaders,
  isOperation,
  type Operation,
  requireAssigneeMatch,
  requireOperationAllowed,
  requiresAssigneeMatch,
} from "./auth.ts";
import { isAssignee, isTicketState, type CallerRole, type Ticket } from "./types.ts";
import { ZodError } from "zod";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function textResponse(message: string, status: number): Response {
  return new Response(message, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function errorToResponse(err: unknown): Response {
  if (err instanceof AuthError) return textResponse(err.message, err.status);
  if (err instanceof ZodError) return textResponse(err.issues.map((i) => i.message).join("; "), 400);
  if (err instanceof Error) return textResponse(err.message, 400);
  return textResponse(String(err), 400);
}

async function readJson(req: Request): Promise<unknown> {
  const text = await req.text();
  return text ? JSON.parse(text) : {};
}

async function handleCreateTicket(sql: Sql, hub: EventHub, body: unknown): Promise<Response> {
  const payload = CreateTicketBodySchema.parse(body);
  const created = await createTicket(sql, payload);
  hub.notifyChange(await getStoreSignature(sql));
  return jsonResponse({ ticket: created }, 201);
}

async function handleFileBug(sql: Sql, hub: EventHub, body: unknown): Promise<Response> {
  const payload = FileBugBodySchema.parse(body);
  const sourceTicketId = payload.source_ticket_id ?? payload.parent_id ?? "";
  const created = await fileBug(sql, { ...payload, source_ticket_id: sourceTicketId });
  hub.notifyChange(await getStoreSignature(sql));
  return jsonResponse({ ticket: created }, 201);
}

// One handler per ticket-scoped operation (everything except create_ticket
// and file_bug, which don't take a ticket id). Typed as
// `Record<Exclude<Operation, "create_ticket" | "file_bug">, ...>` so adding
// an Operation in auth.ts without adding a case here is a compile error --
// the same exhaustiveness device as legalNextStates() in types.ts.
type TicketOperation = Exclude<Operation, "create_ticket" | "file_bug">;
type TicketOpHandler = (sql: Sql, ticketId: string, caller: CallerRole, body: unknown) => Promise<Ticket | { source: Ticket; target: Ticket }>;

const TICKET_OPERATION_HANDLERS: Record<TicketOperation, TicketOpHandler> = {
  route: async (sql, ticketId, _caller, body) => {
    const payload = RouteBodySchema.parse(body);
    const state = payload.state ?? payload.new_state ?? "";
    const assignee = payload.assignee ?? "";
    if (!isTicketState(state)) throw new Error(`invalid state: ${state}`);
    if (!isAssignee(assignee)) throw new Error(`invalid assignee: ${assignee}`);
    return await routeTicket(sql, ticketId, state, assignee);
  },
  start_work: (sql, ticketId) => startWork(sql, ticketId),
  submit_to_audit: (sql, ticketId, _caller, body) => {
    const payload = CommitHashBodySchema.parse(body);
    return submitToAudit(sql, ticketId, payload.commit_hash);
  },
  audit_sign_off: (sql, ticketId) => auditSignOff(sql, ticketId),
  audit_kick_back: (sql, ticketId, caller, body) => {
    const payload = ReasonBodySchema.parse(body);
    return auditKickBack(sql, ticketId, caller, payload.reason ?? payload.text ?? "");
  },
  user_sign_off: (sql, ticketId) => ericSignOff(sql, ticketId),
  user_reopen: (sql, ticketId, caller, body) => {
    const payload = ReasonBodySchema.parse(body);
    return ericReopen(sql, ticketId, caller, payload.reason ?? payload.text ?? "");
  },
  mark_done: (sql, ticketId, _caller, body) => {
    const payload = CommitHashBodySchema.parse(body);
    return markDone(sql, ticketId, payload.commit_hash);
  },
  defer: (sql, ticketId) => deferTicket(sql, ticketId),
  cancel: (sql, ticketId, caller, body) => {
    const payload = ReasonBodySchema.parse(body);
    return cancelTicket(sql, ticketId, caller, payload.reason ?? payload.text ?? "");
  },
  set_manually_controlled: (sql, ticketId, _caller, body) => {
    const payload = SetManuallyControlledBodySchema.parse(body);
    return setManuallyControlled(sql, ticketId, payload.manually_controlled ?? payload.value ?? false);
  },
  set_blockers: (sql, ticketId, _caller, body) => {
    const payload = SetBlockersBodySchema.parse(body);
    return setBlockers(sql, ticketId, payload.blocked_by ?? payload.ids ?? [], payload.blocked_reason ?? payload.reason ?? "");
  },
  add_comment: (sql, ticketId, caller, body) => {
    const payload = AddCommentBodySchema.parse(body);
    return addComment(sql, ticketId, caller, payload.text);
  },
  edit_fields: (sql, ticketId, _caller, body) => {
    const payload = EditFieldsBodySchema.parse(body);
    return editFields(sql, ticketId, payload);
  },
  merge: (sql, ticketId, _caller, body) => {
    const payload = MergeBodySchema.parse(body);
    return mergeTickets(sql, ticketId, payload.target_id.trim().toUpperCase());
  },
};

async function handleTicketAction(
  sql: Sql,
  hub: EventHub,
  req: Request,
  operation: string,
  ticketId: string | null,
): Promise<Response> {
  const caller = callerRoleFromHeaders(req.headers);
  if (!isOperation(operation)) {
    throw new Error(`unknown ticket operation: ${operation}`);
  }
  requireOperationAllowed(operation, caller);
  if (requiresAssigneeMatch(operation) && ticketId !== null) {
    const ticket = await getTicket(sql, ticketId);
    requireAssigneeMatch(operation, caller, ticket.assignee);
  }

  const body = await readJson(req);

  if (operation === "create_ticket") return await handleCreateTicket(sql, hub, body);
  if (operation === "file_bug") return await handleFileBug(sql, hub, body);

  if (ticketId === null) {
    throw new Error(`${operation} requires a ticket id`);
  }
  const handler = TICKET_OPERATION_HANDLERS[operation];
  const result = await handler(sql, ticketId, caller, body);
  hub.notifyChange(await getStoreSignature(sql));
  if (operation === "merge") return jsonResponse(result);
  return jsonResponse({ ticket: result });
}

const NO_ID_ACTION_RE = /^\/api\/tickets\/actions\/([^/]+)\/?$/;
const TICKET_ACTION_RE = /^\/api\/tickets\/([^/]+)\/actions\/([^/]+)\/?$/;

export function createHandler(sql: Sql, hub: EventHub): (req: Request) => Promise<Response> {
  return async (req: Request): Promise<Response> => {
    const url = new URL(req.url);
    try {
      if (req.method === "GET" && url.pathname === "/api/board") {
        return jsonResponse(await boardSnapshot(sql));
      }
      if (req.method === "GET" && url.pathname === "/events") {
        return new Response(eventStream(hub, req.signal), {
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
          },
        });
      }
      if (req.method === "POST") {
        const noIdMatch = url.pathname.match(NO_ID_ACTION_RE);
        if (noIdMatch) {
          const operation = decodeURIComponent(noIdMatch[1]!);
          return await handleTicketAction(sql, hub, req, operation, null);
        }
        const ticketMatch = url.pathname.match(TICKET_ACTION_RE);
        if (ticketMatch) {
          // app.py normalizes ticket ids (.strip().upper()) inside every
          // method (route_ticket, get_ticket, merge_tickets, ...); done
          // once here at the single HTTP entry point for the same effect.
          const ticketId = decodeURIComponent(ticketMatch[1]!).trim().toUpperCase();
          const operation = decodeURIComponent(ticketMatch[2]!);
          return await handleTicketAction(sql, hub, req, operation, ticketId);
        }
      }
      return textResponse("Not found", 404);
    } catch (err) {
      return errorToResponse(err);
    }
  };
}
