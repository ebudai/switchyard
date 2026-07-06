# Team Container

Runs the director + implementer/auditor/ops/watchdog team in an isolated
Docker container instead of `--dangerously-skip-permissions` directly on the
bare host. See `Dockerfile`/`entrypoint.sh` for the mechanics.

## Build

The Claude Code binary itself isn't committed (240MB, changes on every
update) — copy your local install into the build context first:

```fish
mkdir -p docker/claude-bin
cp (which claude) docker/claude-bin/claude
docker build -t pgu-team:latest --build-arg USER_UID=(id -u) --build-arg USER_GID=(id -g) docker/
```

## Run

Only two things are bind-mounted in: the bare git repos (so `origin` resolves
identically inside the container) and `~/.claude`/`~/.claude.json`/
`~/.gitconfig` (so memory, session transcripts, auth, and git identity carry
over). Everything else — the working copy, build artifacts — is cloned fresh
inside the container's own filesystem at startup and never touches the bare
host. That's the actual isolation boundary: bypass-permissions mode can do
whatever it wants inside the container, but has no path to anything on the
host except the git object store and Claude's own config.

```fish
docker run -d --name pgu-team \
  -v /data/git:/data/git \
  -v ~/.claude:/home/eric/.claude \
  -v ~/.claude.json:/home/eric/.claude.json \
  -v ~/.gitconfig:/home/eric/.gitconfig:ro \
  pgu-team:latest
```

Brings up six standing tmux sessions inside the container:
`pgu-director` (resumes `DIRECTOR_SESSION_ID`, defaulting to the session ID
baked into `entrypoint.sh` — override with `-e DIRECTOR_SESSION_ID=<id>` for
a fresh director), plus fresh `pgu-main`/`pgu-ui`/`pgu-audit`/`pgu-ops`/
`pgu-watchdog`.

Each freshly-launched `claude` instance stops at a one-time bypass-permissions
consent screen on first use in the container — confirm it once per session:

```fish
docker exec pgu-team tmux send-keys -t <session>:0.0 "2"
docker exec pgu-team tmux send-keys -t <session>:0.0 Enter
```

`scripts/directorctl` works unchanged from inside the container:

```fish
docker exec pgu-team bash -c "cd ~/Projects/pgu && scripts/directorctl status"
```
