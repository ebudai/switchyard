# agy 1.5.17 transcripts (SYRD-891)

agy's own `transcript_full.jsonl`, as the installed agy 1.5.17 wrote it on 2026-11-27. Each was recorded offline by
SYRD-890's probe (`tests/fixtures/syrd890_probe`): a private user and network namespace, a throwaway HOME, the real
hook installer and shell guard, and a stand-in model asking for `run_command echo PROBE-TOOL-RAN`. The files are
unedited.

- `denied_no_reason.jsonl`: three calls, each denied with "tool call denied by pre-tool hook: " and an empty reason.
  The guard answered `{}`, as f05a1b37's did, which is ocstab's incident.
- `ran.jsonl`: the same three calls with the fixed guard. Each ran (`status: DONE`).
- `refused_with_reason.jsonl`: one call the guard refused on purpose (board prose with a backtick command). It was
  denied with the guard's reason.
