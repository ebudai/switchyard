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
