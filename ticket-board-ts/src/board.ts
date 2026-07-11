// GET /api/board response assembly, mirroring TicketBoardApp.snapshot() in
// app.py. Field set matches the Python shape field-for-field so the two
// boards are drop-in comparable; screenshot/asset dir scanning is included
// for parity even though M1's write paths don't populate attachments.
import type { Sql } from "./db.ts";
import { listTickets } from "./ticket_store.ts";
import { ALL_ASSIGNEES, STATES } from "./ticket_store.ts";
import { formatLocalTimestamp, isoNow } from "./time.ts";

const FRAME_DIR_DEFAULT = "/tmp/pgu-frames";
const STORE_DIR_DEFAULT = `${Deno.env.get("HOME") ?? ""}/.claude/pgu-tickets`;
const ASSET_DIR_DEFAULT = `${Deno.env.get("HOME") ?? ""}/.claude/pgu-tickets-assets`;

interface ScreenshotEntry {
  path: string;
  name: string;
  modified: string;
}

async function listScreenshots(frameDir: string): Promise<ScreenshotEntry[]> {
  const entries: { path: string; name: string; mtime: number }[] = [];
  try {
    for await (const entry of Deno.readDir(frameDir)) {
      if (!entry.isFile || !entry.name.endsWith(".png")) continue;
      const path = `${frameDir}/${entry.name}`;
      const stat = await Deno.stat(path);
      entries.push({ path, name: entry.name, mtime: stat.mtime?.getTime() ?? 0 });
    }
  } catch {
    return [];
  }
  entries.sort((a, b) => b.mtime - a.mtime);
  return entries.map(({ path, name, mtime }) => ({
    path,
    name,
    modified: formatLocalTimestamp(new Date(mtime)),
  }));
}

export async function boardSnapshot(sql: Sql) {
  let tickets: Awaited<ReturnType<typeof listTickets>> = [];
  const errors: { file: string; error: string }[] = [];
  try {
    tickets = await listTickets(sql);
  } catch (err) {
    errors.push({ file: "postgres", error: err instanceof Error ? err.message : String(err) });
  }
  return {
    tickets,
    errors,
    states: [...STATES],
    assignees: [...ALL_ASSIGNEES],
    screenshots: await listScreenshots(FRAME_DIR_DEFAULT),
    store_backend: "postgres",
    store_path: STORE_DIR_DEFAULT,
    frame_dir: FRAME_DIR_DEFAULT,
    asset_dir: ASSET_DIR_DEFAULT,
    refreshed_at: isoNow(),
  };
}
