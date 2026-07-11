// HTTP routing for the M1 slice: GET /api/board, POST
// /api/tickets/actions/create_ticket, POST /api/tickets/<id>/actions/route.
// Response shapes and error-status mapping mirror server.py's do_GET/do_POST
// exactly (see PORT-NOTES.md for the one intentional divergence).
import type { Sql } from "./db.ts";
import { boardSnapshot } from "./board.ts";
import { createTicket, routeTicket } from "./ticket_store.ts";
import { CreateTicketBodySchema, RouteBodySchema } from "./schemas.ts";
import { AuthError, callerRoleFromHeaders, requireOperationAllowed } from "./auth.ts";
import { isAssignee, isTicketState } from "./types.ts";
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

async function handleCreateTicket(sql: Sql, req: Request): Promise<Response> {
  const caller = callerRoleFromHeaders(req.headers);
  requireOperationAllowed("create_ticket", caller);
  const payload = CreateTicketBodySchema.parse(await req.json());
  const created = await createTicket(sql, payload);
  return jsonResponse({ ticket: created }, 201);
}

async function handleRoute(sql: Sql, req: Request, ticketId: string): Promise<Response> {
  const caller = callerRoleFromHeaders(req.headers);
  requireOperationAllowed("route", caller);
  const payload = RouteBodySchema.parse(await req.json());
  const state = payload.state ?? payload.new_state ?? "";
  const assignee = payload.assignee ?? "";
  if (!isTicketState(state)) throw new Error(`invalid state: ${state}`);
  if (!isAssignee(assignee)) throw new Error(`invalid assignee: ${assignee}`);
  const updated = await routeTicket(sql, ticketId, state, assignee);
  return jsonResponse({ ticket: updated });
}

const TICKET_ACTION_RE = /^\/api\/tickets\/([^/]+)\/actions\/([^/]+)\/?$/;

export function createHandler(sql: Sql): (req: Request) => Promise<Response> {
  return async (req: Request): Promise<Response> => {
    const url = new URL(req.url);
    try {
      if (req.method === "GET" && url.pathname === "/api/board") {
        return jsonResponse(await boardSnapshot(sql));
      }
      if (req.method === "POST" && url.pathname === "/api/tickets/actions/create_ticket") {
        return await handleCreateTicket(sql, req);
      }
      const actionMatch = url.pathname.match(TICKET_ACTION_RE);
      if (req.method === "POST" && actionMatch) {
        const [, rawTicketId, operation] = actionMatch;
        const ticketId = decodeURIComponent(rawTicketId!);
        if (operation === "route") {
          return await handleRoute(sql, req, ticketId);
        }
        return textResponse(`unknown ticket operation: ${operation}`, 400);
      }
      return textResponse("Not found", 404);
    } catch (err) {
      return errorToResponse(err);
    }
  };
}
