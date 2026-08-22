// Mirrors the postgres-backend methods of scripts/ticket_board/app.py's
// TicketBoardApp (_pg_list_tickets, _pg_get_ticket, _pg_create_ticket_record,
// route_ticket). All writes go through the SECURITY DEFINER functions in
// ticket_board.* -- direct table DML is REVOKEd from ticket_board_service,
// so there is no "just UPDATE the row" escape hatch even if we wanted one.

import type { Sql, SqlLike } from "./db.ts";
import { ASSIGNEES, TICKET_STATES, type Assignee, type CallerRole, type Ticket, type TicketState } from "./types.ts";
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
    t.needs_user_signoff,
    t.user_signoff,
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
    needs_user_signoff: raw.needs_user_signoff,
    user_signoff: raw.user_signoff,
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

// Mirrors app.py's _pg_store_signature(): a cheap per-ticket fingerprint
// (not the full row) used purely to detect "something changed" for
// /events polling -- comment/blocker/attachment *counts* catch additions
// there without having to diff their full content.
const STORE_SIGNATURE_QUERY = `
SELECT
    t.id,
    t.row_updated_at::text AS row_updated_at,
    t.updated_text,
    (SELECT count(*)::int FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id) AS blocker_count,
    (SELECT count(*)::int FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id) AS comment_count,
    (SELECT count(*)::int FROM ticket_board.ticket_attachments a WHERE a.ticket_id = t.id) AS attachment_count
FROM ticket_board.tickets t
ORDER BY t.ticket_number;
`;

export async function getStoreSignature(sql: SqlLike): Promise<string> {
  const rows = await sql.unsafe(STORE_SIGNATURE_QUERY);
  return JSON.stringify(
    rows.map((row) => [
      String(row.id),
      String(row.row_updated_at),
      String(row.updated_text),
      Number(row.blocker_count),
      Number(row.comment_count),
      Number(row.attachment_count),
    ]),
  );
}

export async function listTickets(sql: SqlLike): Promise<Ticket[]> {
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

export async function getTicket(sql: SqlLike, ticketId: string): Promise<Ticket> {
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
  needs_user_signoff: boolean;
  blocked_by: string[];
  blocked_reason: string;
}

function requireNonEmpty(value: string, field: string): string {
  const trimmed = value.trim();
  if (!trimmed) throw new Error(`${field} must be a non-empty string`);
  return trimmed;
}

// Shared by create_ticket and file_bug: both insert via a single-purpose DB
// function, then optionally route() and set_blockers() in the same
// transaction -- the only difference between the two ops is which "insert"
// function creates the row and what its extra arguments are.
async function createTicketVia(
  sql: Sql,
  insert: (tx: SqlLike) => Promise<string>,
  input: Pick<CreateTicketInput, "assignee" | "needs_user_signoff" | "blocked_by" | "blocked_reason">,
): Promise<Ticket> {
  if (input.blocked_by.length > 0 && !input.blocked_reason.trim()) {
    throw new Error("blocked_reason must be non-empty when blocked_by is set");
  }
  // Ported behavior, not a new restriction: app.py's _pg_create_ticket_record
  // explicitly raises on needs_user_signoff=true because the underlying DB
  // insert functions (create_ticket/file_bug) have nowhere to put the flag
  // on initial insert. Matching that here (rather than silently dropping
  // the field) is exactly the kind of gap that's easy to lose in a port;
  // see PORT-NOTES.md.
  if (input.needs_user_signoff) {
    throw new Error("postgres function API does not support initial signoff fields yet");
  }
  return await sql.begin(async (tx) => {
    const ticketId = await insert(tx);
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
    return await getTicket(tx, ticketId);
  });
}

async function insertReturningId(tx: SqlLike, sql: string, params: string[]): Promise<string> {
  const [row] = await tx.unsafe(sql, params);
  if (!row) throw new Error("insert returned no row");
  return String(row.id);
}

export async function createTicket(sql: Sql, input: CreateTicketInput): Promise<Ticket> {
  requireNonEmpty(input.title, "title");
  return await createTicketVia(
    sql,
    (tx) => insertReturningId(tx, "SELECT ticket_board.create_ticket($1, $2) AS id;", [input.title, input.body]),
    input,
  );
}

export interface FileBugInput extends CreateTicketInput {
  source_ticket_id: string;
}

export async function fileBug(sql: Sql, input: FileBugInput): Promise<Ticket> {
  requireNonEmpty(input.title, "title");
  const sourceId = requireNonEmpty(input.source_ticket_id, "source_ticket_id").toUpperCase();
  return await createTicketVia(
    sql,
    (tx) =>
      insertReturningId(tx, "SELECT ticket_board.file_bug($1, $2, $3) AS id;", [input.title, input.body, sourceId]),
    input,
  );
}

export async function routeTicket(
  sql: Sql,
  ticketId: string,
  state: TicketState,
  assignee: Assignee,
): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.route($1, $2, $3);", [ticketId, state, assignee]);
    return await getTicket(tx, ticketId);
  });
}

// set_config(..., true) scopes the setting to the current transaction
// (is_local=true), matching app.py's _pg_set_caller_role. Only needed
// before calling DB functions whose body reads current_app_actor() to
// attribute an appended comment (audit_kick_back, user_reopen, cancel,
// add_comment) -- everything else ignores it.
async function setCallerRole(tx: SqlLike, callerRole: CallerRole): Promise<void> {
  await tx.unsafe("SELECT set_config('ticket_board.caller_role', $1, true);", [callerRole]);
}

function requireComment(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) throw new Error("comment requires non-empty who and text");
  return trimmed;
}

export async function startWork(sql: Sql, ticketId: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.start_work($1);", [ticketId]);
    return await getTicket(tx, ticketId);
  });
}

export async function submitToAudit(sql: Sql, ticketId: string, commitHash: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.submit_to_audit($1, $2);", [ticketId, commitHash]);
    return await getTicket(tx, ticketId);
  });
}

export async function auditSignOff(sql: Sql, ticketId: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.audit_sign_off($1);", [ticketId]);
    return await getTicket(tx, ticketId);
  });
}

export async function auditKickBack(
  sql: Sql,
  ticketId: string,
  callerRole: CallerRole,
  reason: string,
): Promise<Ticket> {
  const comment = requireComment(reason);
  return await sql.begin(async (tx) => {
    await setCallerRole(tx, callerRole);
    await tx.unsafe("SELECT ticket_board.audit_kick_back($1, $2);", [ticketId, comment]);
    return await getTicket(tx, ticketId);
  });
}

export async function ericSignOff(sql: Sql, ticketId: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.user_sign_off($1);", [ticketId]);
    return await getTicket(tx, ticketId);
  });
}

export async function ericReopen(sql: Sql, ticketId: string, callerRole: CallerRole, reason: string): Promise<Ticket> {
  const comment = requireComment(reason);
  return await sql.begin(async (tx) => {
    await setCallerRole(tx, callerRole);
    await tx.unsafe("SELECT ticket_board.user_reopen($1, $2);", [ticketId, comment]);
    return await getTicket(tx, ticketId);
  });
}

export async function markDone(sql: Sql, ticketId: string, commitHash: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.mark_done($1, $2);", [ticketId, commitHash]);
    return await getTicket(tx, ticketId);
  });
}

export async function deferTicket(sql: Sql, ticketId: string): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.defer($1);", [ticketId]);
    return await getTicket(tx, ticketId);
  });
}

export async function cancelTicket(sql: Sql, ticketId: string, callerRole: CallerRole, reason: string): Promise<Ticket> {
  const comment = requireComment(reason);
  return await sql.begin(async (tx) => {
    await setCallerRole(tx, callerRole);
    await tx.unsafe("SELECT ticket_board.cancel($1, $2);", [ticketId, comment]);
    return await getTicket(tx, ticketId);
  });
}

export async function setManuallyControlled(sql: Sql, ticketId: string, manuallyControlled: boolean): Promise<Ticket> {
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.set_manually_controlled($1, $2);", [ticketId, manuallyControlled]);
    return await getTicket(tx, ticketId);
  });
}

export async function setBlockers(
  sql: Sql,
  ticketId: string,
  blockedBy: string[],
  blockedReason: string,
): Promise<Ticket> {
  if (blockedBy.length > 0 && !blockedReason.trim()) {
    throw new Error("blocked_reason must be non-empty when blockers are set");
  }
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.set_blockers($1, $2, $3);", [ticketId, blockedBy, blockedReason]);
    return await getTicket(tx, ticketId);
  });
}

export async function addComment(sql: Sql, ticketId: string, callerRole: CallerRole, text: string): Promise<Ticket> {
  const comment = requireComment(text);
  return await sql.begin(async (tx) => {
    await setCallerRole(tx, callerRole);
    await tx.unsafe("SELECT ticket_board.add_comment($1, $2);", [ticketId, comment]);
    return await getTicket(tx, ticketId);
  });
}

// Field-name allowlist mirrors server.py's EDIT_FIELD_NAMES; edit_fields()
// in schema.sql enforces the same set again (single-field error message),
// but server.py reports *every* invalid field at once, so that's
// reproduced here rather than left to the DB.
const EDIT_FIELD_NAMES = new Set([
  "title",
  "parent_id",
  "screenshots",
  "screenshot",
  "implementation",
  "audit_prompt",
  "needs_user_signoff",
  "commit_exempt",
  "commit_hash",
  "audit_signoff",
  "user_signoff",
]);

export async function editFields(sql: Sql, ticketId: string, patch: Record<string, unknown>): Promise<Ticket> {
  const invalidFields = Object.keys(patch).filter((key) => !EDIT_FIELD_NAMES.has(key)).sort();
  if (invalidFields.length > 0) {
    throw new Error(`edit_fields cannot update: ${invalidFields.join(", ")}`);
  }
  if (patch.audit_signoff === true) {
    throw new Error("audit_signoff=true requires audit_sign_off");
  }
  if (patch.user_signoff === true) {
    throw new Error("user_signoff=true requires user_sign_off");
  }
  return await sql.begin(async (tx) => {
    // Pass `patch` as a plain object, NOT JSON.stringify(patch): postgres.js
    // auto-serializes JS objects to jsonb correctly, but in a multi-param
    // .unsafe() call, a pre-stringified JSON string gets double-encoded
    // (the resulting jsonb value is a *string* scalar, not an object) --
    // see PORT-NOTES.md.
    // postgres.js's .d.ts types `.unsafe()` params against a generic that
    // resolves to `never` for jsonb objects here; the cast is a type-level
    // workaround for that gap, not a runtime-validation gap -- `patch` was
    // already allowlist-validated above.
    await tx.unsafe("SELECT ticket_board.edit_fields($1, $2::jsonb);", [ticketId, patch] as unknown as string[]);
    return await getTicket(tx, ticketId);
  });
}

export interface MergeResult {
  source: Ticket;
  target: Ticket;
}

export async function mergeTickets(sql: Sql, sourceId: string, targetId: string): Promise<MergeResult> {
  if (!sourceId || !targetId) {
    throw new Error("merge requires both source and target ticket IDs");
  }
  if (sourceId === targetId) {
    throw new Error("cannot merge a ticket into itself");
  }
  return await sql.begin(async (tx) => {
    await tx.unsafe("SELECT ticket_board.merge($1, $2);", [sourceId, targetId]);
    const source = await getTicket(tx, sourceId);
    const target = await getTicket(tx, targetId);
    return { source, target };
  });
}

export const STATES = TICKET_STATES;
export const ALL_ASSIGNEES = ASSIGNEES;
