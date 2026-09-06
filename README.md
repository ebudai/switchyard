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
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  present          map persistent role sessions into stable display slots at runtime
  set-vcs-close-role
                   set which existing project role can mark tickets done
  set-role-runtime change an existing role's agent runtime and reconnect its panes
  agy-credential   show, set, or clear this host's agy credential source
  role-prompt      show, set, or clear a role's onboarding prompt
  stop             stop a project's configured tmux pane sessions
  teardown         remove project board provisioning artifacts after a dry-run review
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: board-skill, new, register, upgrade, add-role, present, set-vcs-close-role, set-role-runtime, agy-credential, seed-role-credentials, role-prompt, stop, teardown, status, validate-models.
```
<!-- switchyard-help:end -->

## License

Switchyard is licensed under the GNU Affero General Public License version 3.
See [LICENSE](LICENSE) for the full terms.

The project owner currently holds the project copyright. A commercial license
can be discussed with the project owner if that ever becomes necessary.
