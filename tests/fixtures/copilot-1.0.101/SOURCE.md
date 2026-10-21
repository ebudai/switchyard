# GitHub Copilot CLI 1.0.101, captured for SYRD-634

These are recordings of the real standalone CLI, not the retired `gh copilot` extension:
- `@github/copilot` 1.0.101, a Node loader (`npm-loader.js`);
- its platform package `@github/copilot-linux-x64` 1.0.101, a self-contained ELF executable `copilot`.

Both were downloaded from registry.npmjs.org on 2026-10-20, and their sha512 integrity was checked against the registry. Nothing was installed on the host; the vendor installer was never run.

**How the files were made.** Every file was produced by the native executable in bubblewrap with `--unshare-net` (no network), a read-only root and a throwaway `HOME` and `COPILOT_HOME`:

- `help*.txt` and `version.txt`: the output of `copilot --help`, `copilot help config|environment|commands`, `copilot login --help`, `copilot skill --help` and `copilot --version`.
- `screens/*.txt`: `tmux capture-pane -p` of the interactive CLI in a private tmux server at 160x45.
  - Model turns went to a local fake OpenAI-compatible server, through Copilot's own offline bring-your-own-key mode (`COPILOT_OFFLINE=true`, `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_TYPE=openai`, a placeholder key). So no GitHub account, token or model service was used.
  - `unauthenticated.txt`, `not-logged-in-prompt.txt` and `login-account.txt` are the same CLI with those provider settings removed: what a person with no login sees.
- `hooks/*.jsonl`: the stdin payload of every hook event. A user hook file, `$COPILOT_HOME/hooks/probe.json` with `version: 1`, registered every documented event, and each line holds `{"event", "payload"}`.

The throwaway paths are rewritten to `/home/owner`, and the work folder is `/home/owner/work`.

| file | what the CLI did |
|---|---|
| screens/trust.txt | first launch in a folder: "Confirm folder trust" |
| screens/ready.txt | after "Yes, and remember this folder": the `❯` input between rules |
| screens/reply.txt | one turn answered |
| screens/working.txt | a model call in flight: "◎ Working esc" (the glyph cycles ◎ ◉ ○ ●) |
| screens/permission.txt | the model asked to run `touch …`: "Do you want to run this command?" |
| screens/cleared.txt | after `/clear`: "Started a fresh session." |
| screens/unauthenticated.txt | no login: "Please use /login to sign in to use Copilot" |
| screens/not-logged-in-prompt.txt | a prompt typed with no login: "✗ You must be logged in to send messages" |
| screens/login-account.txt | `/login`: "What account do you want to log into?" |
| screens/resume-unknown-id.txt | `copilot --resume <unknown id>`: "Error: No session, task, or name matched", and the CLI exits |
| screens/quota-retrying.txt | every model call answered 429: the CLI keeps retrying behind "Working" |
| hooks/turn.jsonl | a new session's first prompt, its reply, `/exit` |
| hooks/resume-then-clear.jsonl | `--resume=<id>`, a prompt, `/clear`, a prompt, `/exit` |
| hooks/permission-then-escape.jsonl | a tool needing approval, then Escape (no `agentStop` follows) |
| hooks/quota-429.jsonl | three `errorOccurred` (`errorContext: model_call`, recoverable), no `agentStop` |
| hooks/preassigned-session-id.jsonl | `--session-id=11111111-2222-4333-8444-555555555555` names the new session |
| hooks/session-id-resume-then-in-session-resume.jsonl | `--session-id=<existing id>` resumes it; `/resume <other id>` in session switches to that one |

## A signed-in account refused its models (SYRD-634 DAT)

Live UAT found an account whose Copilot policy denies the CLI: it signs in, then
prints `Model catalog failed to load: ... access denied by Copilot policy` above an
ordinary ready input, and every prompt fails. The same 1.0.101 binary was recorded
in that state offline, without an account:

- bwrap with no network; a fake `COPILOT_GITHUB_TOKEN`; `HTTPS_PROXY` set to a local
  CONNECT proxy that terminates TLS with a throwaway CA (`NODE_EXTRA_CA_CERTS`).
- The proxy answers GitHub's API as a signed-in business seat, and answers the
  model catalog (`GET https://api.business.githubcopilot.com/models`) and every
  model call with 403 `{"error": {"message": "access denied by Copilot policy"}}`,
  the reason the live pane showed. The `allowed` control answers the catalog.
- Every other endpoint answered 404, so the MCP lines on these screens
  ("Third-party MCP servers are disabled ...", "Failed to connect to MCP server")
  come from the stand-in API, not from the account under test. They appear on the
  allowed control too.

| file | what the CLI did |
|---|---|
| screens/model-catalog-denied-auto.txt | no model configured, startup: "! Model catalog failed to load: Error: 403 access denied by Copilot policy" above the ready input |
| screens/model-catalog-denied-auto-prompt.txt | the same, after one prompt: "✗ Failed to load models: Error: 403 ..." |
| screens/model-catalog-denied-model.txt | `--model claude-haiku-5.5`, startup: the same catalog line above the ready input |
| screens/model-catalog-denied-model-prompt.txt | the same, after one prompt: the model call's 403 as "✗ Authorization error. Your credentials may be expired or invalid. Run /login to sign in again." |
| screens/model-catalog-allowed.txt | the control: catalog answered, no catalog line, the ready input |
| screens/model-picker-allowed.txt | `/model` typed once the input is drawn, catalog answered: the picker, "Search models…", listing the catalog's model |
| screens/model-picker-denied.txt | `/model` with the catalog refused: "✗ Failed to load models: access denied by Copilot policy." and no picker |
| screens/model-picker-slow-denied.txt | the catalog refused 12 s late: no startup line at all, and only after repeated `/model` the "Model catalog failed to load" line |

A refusal that arrives late is never drawn at startup (12 s late: no line by 20 s), and `/model` typed while the
catalog is still loading opens nothing; typed again once it has loaded, it opens the picker or reports the refusal.
So readiness is the picker itself, asked for until it answers, and never a quiet screen.
