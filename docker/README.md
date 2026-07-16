# Team Container

Runs the director + implementer/auditor/ops/watchdog team in an isolated
Docker container instead of `--dangerously-skip-permissions` directly on the
bare host. See `Dockerfile`/`entrypoint.sh` for the mechanics.

## Build

The Claude Code binary itself isn't committed (240MB, changes on every
update) — copy your local install into the build context first. **Don't just
`cp (which claude) ...`** — on a typical install, `which claude` resolves to
a small wrapper script (`/usr/bin/claude`, `exec`s the real binary from
`/opt/claude-code/bin/claude`), not the real ~250MB executable. Copying the
wrapper into the image places it at the exact path it execs to, so it execs
itself forever (100% CPU, zero output, no useful error — this bit us once,
2026-07-07). Copy the real binary instead:

```fish
mkdir -p docker/claude-bin
cp /opt/claude-code/bin/claude docker/claude-bin/claude
file docker/claude-bin/claude  # sanity check: must say "ELF 64-bit ... executable", not "shell script"
docker build -t pgu-team:latest --build-arg USER_UID=(id -u) --build-arg USER_GID=(id -g) docker/
```

## Run

Only a few things are bind-mounted in: the bare git repos (so `origin`
resolves identically inside the container), `~/.claude`/`~/.claude.json`/
`~/.gitconfig` (so memory, session transcripts, auth, and git identity carry
over), and OpenAI Codex CLI's config/auth (see below). Everything else — the
working copy, build artifacts — is cloned fresh inside the container's own
filesystem at startup and never touches the bare host. That's the actual
isolation boundary: bypass-permissions mode can do whatever it wants inside
the container, but has no path to anything on the host except the git object
store, Claude's own config, and Codex's own config.

```fish
docker run -d --name pgu-team \
  -v /data/git:/data/git \
  -v ~/.claude:/home/eric/.claude \
  -v ~/.claude.json:/home/eric/.claude.json \
  -v ~/.gitconfig:/home/eric/.gitconfig:ro \
  -v /ai_models/codex-home/.codex:/home/eric/.codex \
  pgu-team:latest
```

### Codex CLI auth persistence

The image also ships OpenAI's `codex` CLI (see Dockerfile) as a manual
alternative implementer Eric runs by hand in whichever pane he wants — it's
not wired into `roles.json`/`entrypoint.sh` and isn't launched automatically.
Codex keeps its config and auth under `~/.codex` (`CODEX_HOME`, confirmed via
`codex doctor`), so the run command above bind-mounts it by default, the same
way `.claude` is handled, and `codex login` carries over across container
rebuilds/recreates. The mount source is `/ai_models/codex-home/.codex`
rather than `~/.codex` because on the host `~/.codex` is itself a symlink to
that path (it lives on Eric's Optane drive) — using the real target avoids
any ambiguity in how Docker resolves a symlinked bind-mount source.

Brings up six standing tmux sessions inside the container, each running a
`claude` instance: `pgu-director`, `pgu-main`, `pgu-ui`, `pgu-audit`,
`pgu-ops`, `pgu-research`. Ticket notifications are delivered by the
ticket-board notify-listener service; there is no `pgu-watchdog` tmux session.

Each freshly-launched `claude` instance stops at a one-time bypass-permissions
consent screen on first use in the container — confirm it once per session:

```fish
docker exec pgu-team tmux send-keys -t <session>:0.0 "2"
docker exec pgu-team tmux send-keys -t <session>:0.0 Enter
```

Use the canonical host-side `directorctl`:

```fish
/home/agent/bin/directorctl status
```

## Per-role model config (`roles.json`)

`entrypoint.sh` no longer hardcodes one model for every role. It reads
`docker/roles.json` (part of the repo — the container's fresh clone already
has it, no extra bind mount needed) and launches each role's `claude` with
that role's own `--model`:

```json
{
  "roles": {
    "director":  { "model": "claude-opus-4-8",   "resume_session_id": "<uuid>" },
    "main":      { "model": "claude-sonnet-5" },
    "ui":        { "model": "claude-sonnet-5" },
    "audit":     { "model": "claude-sonnet-5" },
    "ops":       { "model": "claude-sonnet-5" }
  }
}
```

Notification delivery deliberately has no role entry -- it runs as a service,
not as a Claude pane.

Rationale for the defaults: director gets the strongest model (Opus 4.8) —
it's the one doing planning/orchestration. Main/UI/audit/ops get Sonnet 5 —
plenty capable for implementation and review at lower cost. Edit
`docker/roles.json` and commit — the next container start (or a fresh
`docker exec ... entrypoint`) picks up the change, no script edits required.

`resume_session_id` is optional per-role (only `director` uses it today). To
override the director's resume target for one run without editing the file,
`-e DIRECTOR_SESSION_ID=<uuid>` still works (set it to an empty string to
force a fresh director instead of resuming).

## Viewing all sessions (host side)

The nested-tmux "dashboard" approach (one outer tmux window tiling six
attached inner sessions) doesn't work reliably — mouse focus and the
`Ctrl-b` prefix collide between the outer session and every inner one.
Instead, `scripts/open-team-windows.sh` opens six separate host terminal
windows (Konsole), each running one plain
`docker exec -it pgu-team tmux attach -t pgu-<role>` — no nesting, so mouse
clicks and prefix keys behave normally in whichever window has focus. Tile
them yourself (KWin quick-tile / drag-to-edge), same as the old VM setup:

```fish
scripts/open-team-windows.sh
```
