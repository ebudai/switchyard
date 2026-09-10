# Switchyard

Switchyard coordinates local multi-role project work through a shared ticket
board, role launchers, repository policy hooks, and install/deploy helpers.

[GitHub](https://github.com/ebudai/switchyard) is the canonical repository.
See [CONTRIBUTING.md](CONTRIBUTING.md) for pull requests and contribution terms.

## From Clone To Installed Command

Start with a fresh clone, then run the one installer command:

```bash
git clone https://github.com/ebudai/switchyard.git switchyard
cd switchyard
sudo ./install
```

Run `sudo ./install --dry-run` first to see every command it would run without
changing the system.

The installer supports Debian/Ubuntu (apt) and Arch-family (pacman) hosts, and
stops if it finds neither. It installs host packages, creates the shared
`/opt/switchyard/venv` with `--system-site-packages`, runs the ticket-board
dependency and entry-point checks, and installs the public `switchyard`
command, normally at `/usr/local/bin/switchyard`. It is non-interactive and
asks nothing; `--yes` is accepted for compatibility and has no prompts to
answer. It refreshes package metadata itself on apt hosts but not on
Arch-family ones, where `pacman -Sy` without `-u` would leave a partial
upgrade: run `sudo pacman -Syu` yourself before `sudo ./install`.

Switchyard does not install the agent CLIs. Installing `claude`, `codex`,
`agy`, or `hermes` is yours to do, with that vendor's own installer, for the
user that will own the project. Switchyard detects which ones are present and
walks their sign-in when you create a project, so the installer's final output
points you at `switchyard new` rather than naming a CLI.

### Which version you get

`./install` installs what is checked out, and never pulls, fetches, or reaches
the network. That is deliberate: you get to run the version you chose, whether
that is the tip or a release you have pinned.

Because it never fetches, it can also install code older than you expect
without either of you noticing, so it now says which it is. If your checkout has
diverged from the upstream it tracks, the installer reports the count -- read
from what your last `git fetch` left on disk -- and installs anyway. It does not
tell you to pull. A checkout level with its upstream, which is what a fresh
clone is, prints nothing.

The same applies after installation, in the other direction. `switchyard` runs
from the installed release, so pulling a checkout does not change what runs; the
symptom is a bug you have been told is fixed, still happening. When the running
release is older than the checkout it was installed from, the command says so
and names `sudo ./install`. It never reinstalls on its own. If that checkout has
been moved or deleted, it says nothing rather than guess.

Either notice can be silenced for good, per checkout:

```bash
git -C /path/to/checkout config switchyard.versionNotice false
```

`SWITCHYARD_VERSION_NOTICE=0` silences one invocation, for scripts.

For package details, Arch-family notes, and vendor CLI provenance links, see
[the fresh-machine install list](docs/fresh-machine-install.md).

Operator print mode still exists for the internal release step:
`scripts/install-switchyard --print-commands`.

## Commands

The list below is checked against the same help text printed by
`switchyard --help`.

<!-- switchyard-help:start -->
```text
Usage:
  switchyard
  switchyard <project name or slug>
  switchyard <command> [options]

Commands:
  board-skill      install or verify the portable board skill for every agent CLI
  new              create and provision a new project
  register         register an existing project config
  upgrade          update generated project artifacts and report release drift
  finish-upgrade   run the director-owned phase of an upgrade from the director's session
  cutover-roles    legacy compatibility command (new runtimes use the project account)
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  present          map persistent role sessions into stable display slots at runtime
  attach           attach this terminal to a role's live worker by project and role name
  replace-window   replace a root-owned presentation window without stopping any worker
  set-vcs-close-role
                   set which existing project role can mark tickets done
  set-role-runtime change an existing role's agent runtime and reconnect its panes
  agy-credential   show, set, or clear this host's agy credential source
  role-prompt      show, set, or clear a role's onboarding prompt
  onboarding-readiness
                   report whether every registered tenant has migrated director onboarding
  stop             stop a project's configured tmux pane sessions
  teardown         remove project board provisioning artifacts after a dry-run review
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: board-skill, new, register, upgrade, finish-upgrade, cutover-roles, add-role, set-owner-identity, present, attach, replace-window, set-vcs-close-role, set-role-runtime, agy-credential, seed-role-credentials, role-prompt, onboarding-readiness, stop, teardown, status, validate-models.
```
<!-- switchyard-help:end -->

## License

Switchyard is licensed under the GNU Affero General Public License version 3.
See [LICENSE](LICENSE) for the full terms.

The project owner currently holds the project copyright. A commercial license
can be discussed with the project owner if that ever becomes necessary.
