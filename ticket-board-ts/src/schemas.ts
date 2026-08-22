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
  needs_user_signoff: z.boolean(),
  user_signoff: z.boolean(),
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
  needs_user_signoff: z.boolean().optional().default(false),
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

// POST /api/tickets/actions/file_bug -- same create_ticket_record() shape as
// create_ticket, but state is always "analysis" and parent_id comes from
// source_ticket_id (falling back to parent_id, per server.py).
export const FileBugBodySchema = z.object({
  title: z.string(),
  body: z.string().optional().default(""),
  source_ticket_id: z.string().optional(),
  parent_id: z.string().optional(),
  assignee: AssigneeSchema.optional().default("unassigned"),
  needs_user_signoff: z.boolean().optional().default(false),
  blocked_by: z.array(z.string()).optional().default([]),
  blocked_reason: z.string().optional().default(""),
});

// submit_to_audit / mark_done both take a commit_hash; DB functions are the
// sole validators of hex-commit format (see schema.sql).
export const CommitHashBodySchema = z.object({
  commit_hash: z.string().optional().default(""),
});

// audit_kick_back / user_reopen / cancel: server.py accepts either "reason"
// or the legacy "text" key for the mandatory explanatory comment.
export const ReasonBodySchema = z.object({
  reason: z.string().optional(),
  text: z.string().optional(),
});

export const SetManuallyControlledBodySchema = z.object({
  manually_controlled: z.boolean().optional(),
  value: z.boolean().optional().default(false),
});

// set_blockers: server.py accepts "blocked_by" or legacy "ids", and
// "blocked_reason" or legacy "reason". Ticket-ID format/self-reference is
// validated by the DB's set_blockers() CHECK-backed logic, not here.
export const SetBlockersBodySchema = z.object({
  blocked_by: z.array(z.string()).optional(),
  ids: z.array(z.string()).optional().default([]),
  blocked_reason: z.string().optional(),
  reason: z.string().optional().default(""),
});

export const AddCommentBodySchema = z.object({
  text: z.string().optional().default(""),
});

// edit_fields: field-name allowlisting happens outside this schema (to
// reproduce server.py's "list every invalid field" error, not just the
// first), everything else about content validation is the DB's job.
export const EditFieldsBodySchema = z.record(z.string(), z.unknown());

export const MergeBodySchema = z.object({
  target_id: z.string(),
});
