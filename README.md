# Switchyard

Switchyard coordinates local multi-role project work through a shared ticket
board, role launchers, repository policy hooks, and install/deploy helpers.

If you want to run it yourself, start with
[the fresh-machine install list](docs/fresh-machine-install.md) before
installing.

## From Clone To Installed Command

Start with [the fresh-machine install list](docs/fresh-machine-install.md).
It names the operating-system packages, Python package, and agent CLI
authentication requirements.

Then run the installer scripts in this order:

1. `scripts/install-switchyard-prereqs`

   Installs host packages with `apt` or `pacman`, installs `psycopg`, and
   prints the agent CLI requirement. On apt systems it refreshes the package
   index before installing. On Arch-family systems it does not run `pacman -Sy`;
   run `sudo pacman -Syu` first as described in the fresh-machine install list.

2. `scripts/install-switchyard`

   Prints the privileged command block that exports the current checkout into
   `/opt/switchyard`, updates `/opt/switchyard/current`, and installs the
   public `switchyard` command, normally at `/usr/local/bin/switchyard`.
   Applying that block needs root. A non-root user is expected to run
   `scripts/install-switchyard`, inspect the printed `sudo env ... --apply`
   command, and have a sudo-capable operator run it.

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
