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

export function connect(config: DbConfig = dbConfigFromEnv()) {
  return postgres({
    host: config.host,
    database: config.database,
    username: config.username,
    max: 5,
    // The service role connects over a trusted peer-auth Unix socket
    // (scripts/ticket_board/schema.sql's `require_actor` gates writes to
    // exactly this role) -- no TLS/password negotiation applies here.
  });
}

export type Sql = ReturnType<typeof connect>;
