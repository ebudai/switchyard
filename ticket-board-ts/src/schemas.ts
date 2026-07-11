// Zod schemas at the two boundaries where untyped data enters the process:
// HTTP request bodies (JSON from the network) and Postgres rows (jsonb /
// text[] columns postgres.js hands back as `any`). Everything *between*
// those boundaries is plain typed TS -- that's the point: types are cheap
// once the untrusted edges are pinned down.

import { z } from "zod";
import { ASSIGNEES, CALLER_ROLES, TICKET_STATES } from "./types.ts";

export const TicketStateSchema = z.enum(TICKET_STATES);
export const AssigneeSchema = z.enum(ASSIGNEES);
export const CallerRoleSchema = z.enum(CALLER_ROLES);

export const TicketCommentSchema = z.object({
  who: z.string(),
  text: z.string(),
  ts: z.string(),
});

// Shape returned by _pg_select_ticket_rows in app.py / the equivalent query
// in db.ts. postgres.js already parses jsonb into JS values and text[] into
// JS arrays, so this schema is validating *content*, not JSON syntax.
export const TicketRowSchema = z.object({
  id: z.string(),
  title: z.string(),
  body: z.string(),
  state: TicketStateSchema,
  assignee: AssigneeSchema,
  parent_id: z.string().nullable(),
  blocked_reason: z.string(),
  implementation: z.string(),
  audit_prompt: z.string(),
  audit_signoff: z.boolean(),
  needs_eric_signoff: z.boolean(),
  eric_signoff: z.boolean(),
  manually_controlled: z.boolean(),
  commit_hash: z.string().nullable(),
  commit_exempt: z.boolean(),
  created_text: z.string(),
  updated_text: z.string(),
  blocked_by: z.array(z.string()).nullable(),
  comments: z.array(TicketCommentSchema).nullable(),
  screenshots: z.array(z.string()).nullable(),
});
export type TicketRow = z.infer<typeof TicketRowSchema>;

// POST /api/tickets/actions/create_ticket -- M1 supports the basic
// (non-"advanced") create path only, mirroring app.py's create_ticket().
export const CreateTicketBodySchema = z.object({
  title: z.string(),
  body: z.string().optional().default(""),
  assignee: AssigneeSchema.optional().default("unassigned"),
  needs_eric_signoff: z.boolean().optional().default(false),
  blocked_by: z.array(z.string()).optional().default([]),
  blocked_reason: z.string().optional().default(""),
});

// POST /api/tickets/<id>/actions/route -- Python accepts either "state" or
// the legacy "new_state" key (server.py: payload.get("state",
// payload.get("new_state", ""))); mirrored here for wire compatibility.
export const RouteBodySchema = z.object({
  state: z.string().optional(),
  new_state: z.string().optional(),
  assignee: z.string().optional().default(""),
});
