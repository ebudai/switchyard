// Postgres client. Client choice: `postgres` (postgres.js) over `pg` --
// postgres.js is TS-native (no @types/pg gap), has a tagged-template query
// API that composes cleanly with the SECURITY DEFINER function calls below,
// and its connection pool defaults are simpler to reason about for a
// scratch service this size. `pg` would be the safer pick for a service
// that needs COPY streaming or LISTEN/NOTIFY fan-out; M1 needs neither.
import postgres from "postgres";

export interface DbConfig {
  host: string;
  database: string;
  username: string;
}

export function dbConfigFromEnv(): DbConfig {
  return {
    host: Deno.env.get("PGHOST") ?? "/var/run/postgresql",
    database: Deno.env.get("PGDATABASE") ?? "pgu",
    username: Deno.env.get("PGUSER") ?? "ticket_board_service",
  };
}

export function connect(config: DbConfig = dbConfigFromEnv(), max = 5) {
  return postgres({
    host: config.host,
    database: config.database,
    username: config.username,
    max,
    // The service role connects over a trusted peer-auth Unix socket
    // (scripts/ticket_board/schema.sql's `require_actor` gates writes to
    // exactly this role) -- no TLS/password negotiation applies here.
  });
}

// A single dedicated connection for src/events.ts's backstop poll loop --
// deliberately NOT drawn from the request-handling pool (see events.ts).
// It's still a plain one-shot query connection, not a Postgres LISTEN
// session; see events.ts for why polling was chosen over LISTEN/NOTIFY.
export function connectForEventPolling(config: DbConfig = dbConfigFromEnv()) {
  return connect(config, 1);
}

export type Sql = ReturnType<typeof connect>;

// postgres.js's transaction callback hands back a `TransactionSql`, a
// sibling type to `Sql` (both extend `ISql`) rather than a subtype of it --
// so helpers that run inside `sql.begin(async (tx) => ...)` and also get
// called with the top-level `sql` need this wider type, not `Sql` itself.
export type SqlLike = postgres.ISql<{}>;
