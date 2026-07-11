// Entry point for the scratch TS ticket-board service. Run:
//   deno task start
// or:
//   deno run --allow-net --allow-env --allow-read --allow-write=/var/run/postgresql main.ts
import { connect, connectForEventPolling, dbConfigFromEnv } from "./src/db.ts";
import { EventHub } from "./src/events.ts";
import { createHandler } from "./src/server.ts";

const PORT_DEFAULT = 8771; // scratch port; 8770 belongs to the live Python board.

async function main() {
  const port = Number(Deno.env.get("PORT") ?? PORT_DEFAULT);
  const dbConfig = dbConfigFromEnv();
  const sql = connect(dbConfig);
  const eventPollSql = connectForEventPolling(dbConfig);
  const hub = new EventHub(eventPollSql);
  await hub.init();
  const handler = createHandler(sql, hub);

  console.log(`[porter] db: ${dbConfig.username}@${dbConfig.host}/${dbConfig.database}`);
  console.log(`[porter] listening on http://127.0.0.1:${port}/`);

  Deno.serve({ port, hostname: "127.0.0.1" }, handler);
}

main();
