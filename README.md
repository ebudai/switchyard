# Switchyard

Switchyard coordinates local multi-role project work through a shared ticket
board, role launchers, repository policy hooks, and install/deploy helpers.

## From Clone To Installed Command

Start with a fresh clone, then run the one installer command:

```bash
git clone <repo> switchyard
cd switchyard
sudo ./install
```

Run `sudo ./install --dry-run` first to see every command it would run without
changing the system.

The installer refreshes apt package metadata on Debian/Ubuntu systems, installs
host packages, creates the shared `/opt/switchyard/venv` with
`--system-site-packages`, runs the ticket-board dependency and entry-point
checks, asks whether to run the selected agent CLI installer, and installs the
public `switchyard` command, normally at `/usr/local/bin/switchyard`. The
default CLI is `claude`; pass `--cli codex`, `--cli agy`, or `--cli hermes` to
choose a different one. Its final output is the authentication command that
still needs a human login.

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
  new              create and provision a new project
  register         register an existing project config
  upgrade          update generated project artifacts and report release drift
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  set-vcs-close-role
                   set which existing project role can mark tickets done
  stop             stop a project's configured tmux pane sessions
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: new, register, upgrade, add-role, set-vcs-close-role, stop, status, validate-models.
```
<!-- switchyard-help:end -->

## License

Switchyard is licensed under the GNU Affero General Public License version 3.
See [LICENSE](LICENSE) for the full terms.

The project owner currently holds the project copyright. A commercial license
can be discussed with the project owner if that ever becomes necessary.
