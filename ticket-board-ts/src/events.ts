// Real-time push for GET /events, mirroring server.py's TicketBoardEventHub.
//
// The Python mechanism is NOT Postgres LISTEN/NOTIFY -- it's in-process
// pub/sub (every write handler calls `events.notify_change(store_signature())`
// immediately after its own write) backstopped by a 1s poll loop that
// re-fetches `store_signature()` to catch changes from *outside* this
// process (a second writer, or manual DB edits). Each SSE connection holds
// a maxsize-1 "latest version" slot; concurrent version bumps coalesce
// (only the latest survives) since a client's response to *any* event is
// always a full GET /api/board re-fetch, never a diff.
//
// A pure Postgres LISTEN on the existing `ticket_board_state_transition`
// channel (fired by schema.sql's triggers) was considered instead, but it
// only fires on ticket *state* changes -- add_comment/edit_fields/
// set_blockers writes don't change `state` and would go unnoticed, which
// Python's board does NOT miss (its signature includes comment/blocker/
// attachment counts). Reproducing that with LISTEN would mean altering the
// triggers to NOTIFY more broadly, which is out of scope (schema/functions
// are read-only for this port). So: same polling design as Python, kept
// off the request-handling connection pool per the "critical" constraint
// below.
import type { Sql } from "./db.ts";
import { getStoreSignature } from "./ticket_store.ts";

const SCAN_INTERVAL_MS = 1000;
const KEEPALIVE_MS = 15000;

interface Listener {
  notify(version: number): void;
}

export class EventHub {
  private version = 0;
  private signature: string | null = null;
  private readonly listeners = new Set<Listener>();
  private timer: ReturnType<typeof setInterval> | undefined;

  // `pollSql` MUST be a connection dedicated to this hub, separate from the
  // pool serving HTTP requests -- a 1-per-second poll holding the request
  // pool's only slot(s) would starve concurrent requests. main.ts wires
  // this up as its own single-connection `postgres()` client.
  constructor(private readonly pollSql: Sql, scanIntervalMs = SCAN_INTERVAL_MS) {
    this.timer = setInterval(() => {
      this.pollOnce().catch((err) => {
        console.error("[events] poll failed:", err instanceof Error ? err.message : err);
      });
    }, scanIntervalMs);
  }

  async init(): Promise<void> {
    this.signature = await getStoreSignature(this.pollSql);
  }

  private async pollOnce(): Promise<void> {
    const next = await getStoreSignature(this.pollSql);
    if (next !== this.signature) {
      this.signature = next;
      this.bump();
    }
  }

  // Called by write handlers right after a successful mutation, passing
  // the signature they can cheaply compute on their own (request-pool)
  // connection -- this both notifies immediately (no 1s poll latency for
  // changes made through this server) and updates the hub's baseline so
  // the next backstop poll doesn't redundantly re-fire on the same change.
  notifyChange(signature?: string): number {
    if (signature !== undefined) this.signature = signature;
    return this.bump();
  }

  private bump(): number {
    this.version += 1;
    for (const listener of this.listeners) listener.notify(this.version);
    return this.version;
  }

  register(): { version: number; wait: () => Promise<number>; unregister: () => void } {
    let resolve!: (v: number) => void;
    let pending = new Promise<number>((r) => (resolve = r));
    const listener: Listener = {
      notify(v) {
        resolve(v);
        pending = new Promise((r) => (resolve = r));
      },
    };
    this.listeners.add(listener);
    return {
      version: this.version,
      wait: () => pending,
      unregister: () => this.listeners.delete(listener),
    };
  }

  close(): void {
    clearInterval(this.timer);
  }
}

function sseEvent(version: number): Uint8Array {
  return new TextEncoder().encode(`event: board\ndata: ${JSON.stringify({ version })}\n\n`);
}

const KEEPALIVE_BYTES = new TextEncoder().encode(": keepalive\n\n");

// One ReadableStream per client connection, matching server.py's
// serve_events(): initial version immediately, then either a fresh version
// on change or a keepalive comment every 15s, until the client disconnects.
export function eventStream(hub: EventHub, signal: AbortSignal): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      const { version: initialVersion, wait, unregister } = hub.register();
      let closed = false;
      const onAbort = () => {
        closed = true;
        unregister();
        try {
          controller.close();
        } catch {
          // already closed
        }
      };
      signal.addEventListener("abort", onAbort);
      controller.enqueue(sseEvent(initialVersion));

      try {
        while (!closed) {
          const timeout = new Promise<"timeout">((resolve) => setTimeout(() => resolve("timeout"), KEEPALIVE_MS));
          const result = await Promise.race([wait().then((v) => ({ v })), timeout]);
          if (closed) break;
          if (result === "timeout") {
            controller.enqueue(KEEPALIVE_BYTES);
          } else {
            controller.enqueue(sseEvent(result.v));
          }
        }
      } catch {
        // client gone mid-write; cancel() below handles cleanup
      } finally {
        signal.removeEventListener("abort", onAbort);
        unregister();
        if (!closed) {
          try {
            controller.close();
          } catch {
            // already closed
          }
        }
      }
    },
  });
}
