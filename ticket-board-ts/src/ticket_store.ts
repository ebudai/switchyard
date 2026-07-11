// Mirrors the postgres-backend methods of scripts/ticket_board/app.py's
// TicketBoardApp (_pg_list_tickets, _pg_get_ticket, _pg_create_ticket_record,
// route_ticket). All writes go through the SECURITY DEFINER functions in
// ticket_board.* -- direct table DML is REVOKEd from ticket_board_service,
// so there is no "just UPDATE the row" escape hatch even if we wanted one.

import type { Sql } from "./db.ts";
import { ASSIGNEES, TICKET_STATES, type Assignee, type Ticket, type TicketState } from "./types.ts";
import { TicketRowSchema, type TicketRow } from "./schemas.ts";

const TICKET_ROW_QUERY = `
SELECT
    t.id,
    t.title,
    t.body,
    t.state,
    t.assignee,
    t.parent_id,
    t.blocked_reason,
    t.implementation,
    t.audit_prompt,
    t.audit_signoff,
    t.needs_eric_signoff,
    t.eric_signoff,
    t.manually_controlled,
    t.commit_hash,
    t.commit_exempt,
    t.created_text,
    t.updated_text,
    COALESCE(
        (SELECT array_agg(b.blocker_ticket_id ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id),
        ARRAY[]::text[]
    ) AS blocked_by,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('who', c.who, 'text', c.text, 'ts', c.ts_text) ORDER BY c.position)
         FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = t.id),
        '[]'::jsonb
    ) AS comments,
    COALESCE(
        (SELECT array_agg(a.path ORDER BY a.position)
         FROM ticket_board.ticket_attachments a
         WHERE a.ticket_id = t.id),
        ARRAY[]::text[]
    ) AS screenshots
FROM ticket_board.tickets t
`;

async function fileExists(path: string): Promise<boolean> {
  try {
    const info = await Deno.stat(path);
    return info.isFile;
  } catch {
    return false;
  }
}

async function rowToTicket(raw: TicketRow): Promise<Ticket> {
  const screenshots = raw.screenshots ?? [];
  const screenshotEntries = await Promise.all(
    screenshots.map(async (path) => ({ path, available: await fileExists(path) })),
  );
  return {
    id: raw.id,
    title: raw.title,
    body: raw.body,
    assignee: raw.assignee,
    state: raw.state,
    blocked_by: raw.blocked_by ?? [],
    parent_id: raw.parent_id ?? "",
    blocked_reason: raw.blocked_reason,
    implementation: raw.implementation,
    audit_prompt: raw.audit_prompt,
    audit_signoff: raw.audit_signoff,
    needs_eric_signoff: raw.needs_eric_signoff,
    eric_signoff: raw.eric_signoff,
    manually_controlled: raw.manually_controlled,
    commit_hash: raw.commit_hash ?? "",
    commit_exempt: raw.commit_exempt,
    created: raw.created_text,
    updated: raw.updated_text,
    comments: raw.comments ?? [],
    screenshots,
    screenshots_info: screenshotEntries,
    screenshot: screenshotEntries[0]?.path ?? null,
    screenshot_available: screenshotEntries[0]?.available ?? false,
  };
}

export async function listTickets(sql: Sql): Promise<Ticket[]> {
  const rows = await sql.unsafe(`${TICKET_ROW_QUERY}\nORDER BY t.ticket_number;`);
  const tickets = await Promise.all(rows.map((row) => rowToTicket(TicketRowSchema.parse(row))));
  tickets.sort((a, b) => {
    const aTerminal = a.state === "done" || a.state === "cancelled";
    const bTerminal = b.state === "done" || b.state === "cancelled";
    if (aTerminal !== bTerminal) return aTerminal ? 1 : -1;
    if (a.updated !== b.updated) return a.updated > b.updated ? -1 : 1;
    return a.id > b.id ? -1 : 1;
  });
  return tickets;
}

export async function getTicket(sql: Sql, ticketId: string): Promise<Ticket> {
  const rows = await sql.unsafe(`${TICKET_ROW_QUERY}\nWHERE t.id = $1;`, [ticketId]);
  if (rows.length === 0) {
    throw new NotFoundError(`ticket not found: ${ticketId}`);
  }
  return await rowToTicket(TicketRowSchema.parse(rows[0]));
}

export class NotFoundError extends Error {}

export interface CreateTicketInput {
  title: string;
  body: string;
  assignee: Assignee;
  needs_eric_signoff: boolean;
  blocked_by: string[];
  blocked_reason: string;
}

function requireNonEmpty(value: string, field: string): string {
  const trimmed = value.trim();
  if (!trimmed) throw new Error(`${field} must be a non-empty string`);
  return trimmed;
}

export async function createTicket(sql: Sql, input: CreateTicketInput): Promise<Ticket> {
  requireNonEmpty(input.title, "title");
  if (input.blocked_by.length > 0 && !input.blocked_reason.trim()) {
    throw new Error("blocked_reason must be non-empty when blocked_by is set");
  }
  // Ported behavior, not a new restriction: app.py's _pg_create_ticket_record
  // explicitly raises on needs_eric_signoff=true because the underlying
  // ticket_board.create_ticket() DB function only takes (title, body) --
  // there's nowhere to put the flag on initial create. Matching that here
  // (rather than silently dropping the field) is exactly the kind of gap
  // that's easy to lose in a port; see PORT-NOTES.md.
  if (input.needs_eric_signoff) {
    throw new Error("postgres function API does not support initial signoff fields yet");
  }
  return await sql.begin(async (tx) => {
    const [row] = await tx.unsafe(
      "SELECT ticket_board.create_ticket($1, $2) AS id;",
      [input.title, input.body],
    );
    if (!row) throw new Error("create_ticket returned no row");
    const ticketId = String(row.id);
    if (input.assignee !== "unassigned") {
      await tx.unsafe(
        "SELECT ticket_board.route($1, $2, $3);",
        [ticketId, "analysis", input.assignee],
      );
    }
    if (input.blocked_by.length > 0) {
      await tx.unsafe(
        "SELECT ticket_board.set_blockers($1, $2, $3);",
        [ticketId, input.blocked_by, input.blocked_reason],
      );
    }
    return await getTicket(tx as unknown as Sql, ticketId);
  });
}

export async function routeTicket(
  sql: Sql,
  ticketId: string,
  state: TicketState,
  assignee: Assignee,
): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.route($1, $2, $3);", [ticketId, state, assignee]);
    return await getTicket(tx as unknown as Sql, ticketId);
  });
}

export const STATES = TICKET_STATES;
export const ALL_ASSIGNEES = ASSIGNEES;
