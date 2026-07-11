// Timestamp formatting matched to app.py's two distinct serializers --
// these are NOT interchangeable, and JS's default `Date.toISOString()`
// matches neither: it's UTC-with-milliseconds-and-"Z" where Python uses
// UTC-with-no-fraction-and-"+00:00" for one field, and *local* time with a
// completely different, non-ISO layout for the other.

function pad(n: number, width = 2): string {
  return String(n).padStart(width, "0");
}

// app.py: iso_now() = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
// -> "2026-07-11T16:46:31+00:00" (UTC, second precision, "+00:00" not "Z").
export function isoNow(date: Date = new Date()): string {
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}+00:00`
  );
}

// app.py: format_timestamp() = datetime.fromtimestamp(mtime, timezone.utc)
//   .astimezone().strftime("%Y-%m-%d %H:%M:%S")
// -- converts to the *server's local* timezone (astimezone() with no arg),
// space-separated, no "T", no offset suffix, no fraction. Uses the JS
// runtime's local-timezone Date getters, which follow the same TZ/
// /etc/localtime the Python process resolves against.
export function formatLocalTimestamp(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
