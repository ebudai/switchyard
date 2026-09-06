# Switchyard Fresh-Machine Install Notes

From a fresh clone, install Switchyard with one command:

```bash
git clone https://github.com/ebudai/switchyard.git switchyard
cd switchyard
sudo ./install
```

The installer prints each system-changing command before it runs. It is
non-interactive: it installs host packages, creates the shared Python venv,
and installs the public `switchyard` command, and it asks nothing. `--yes` is
accepted for compatibility and has no prompts to answer. `--dry-run` prints
every command it would run and exits without changing the system.

Switchyard does not install, download, or upgrade the agent CLIs. Installing
`claude`, `codex`, `agy`, or `hermes` is yours to do, with that vendor's own
installer, for the user that will own the project. Switchyard detects which
ones are present and walks their sign-in when you create a project, so the
final output of `sudo ./install` points you at `switchyard new` rather than
naming a CLI.

Authentication cannot be automated and is not attempted at install time. It
happens once per detected CLI at project creation.

## System Packages

### apt

```bash
sudo apt-get update
sudo apt-get install python3 postgresql postgresql-client tmux git curl python3-venv python3-pil acl
```

### pacman

```bash
sudo pacman -Syu
sudo pacman -S python postgresql tmux git curl python-pip python-pillow acl
```

The installer runs `apt-get update` before apt installs and prints that refresh.
It does not run `pacman -Sy`; Arch-family systems should use the full
`pacman -Syu` upgrade first to avoid partial-upgrade breakage.

Konsole is not a base dependency. On KDE or when forcing the separate-window
layout, also install it explicitly:

```bash
sudo apt-get install konsole
# or
sudo pacman -S konsole
```

## Python Package

Switchyard's ticket board imports Psycopg 3 from the interpreter that runs the
board service and notify listener. On Ubuntu 24.04 / noble, the distro package
`python3-psycopg` exists but is `3.1.17-2`, which does not satisfy
Switchyard's `psycopg>=3.3,<4` pin. Debian/Ubuntu also mark system Python as
externally managed under PEP 668, so `pip install --user` is refused. Do not use
`--break-system-packages`.

On apt systems, `sudo ./install` creates the shared Switchyard venv. The venv
includes system site packages so the distro `python3-pil` package remains
visible while the venv's Psycopg shadows noble's older distro Psycopg package:

```bash
sudo install -d -m 0755 /opt/switchyard
sudo python3 -m venv --system-site-packages /opt/switchyard/venv
sudo /opt/switchyard/venv/bin/python -m pip install 'psycopg>=3.3,<4'
sudo /opt/switchyard/venv/bin/python -c 'import psycopg, PIL; print(psycopg.__version__, PIL.__version__)'
sudo /opt/switchyard/venv/bin/python scripts/ticket-board.py --help
```

Board service renderers prefer `/opt/switchyard/venv/bin/python` when it
exists and is executable, so the installed Psycopg is used by the process that
imports it. The explicit import check proves both Psycopg and Pillow resolve;
the entry-point help check exercises the board's module-scope imports, including
`PIL`.

On Arch-family systems, the current system-user pip path remains:

```bash
python3 -m pip install --user 'psycopg>=3.3,<4'
```

## Agent CLI

Switchyard supports four agent CLIs and installs none of them. Install at
least one yourself before creating a project, and install it **for the user
that will own the project** -- a CLI installed only for the human who runs
`switchyard` will not be found.

Switchyard never runs the commands below. They are recorded here as the
vendor-documented Linux methods observed when this file was written; check
each against the vendor's own documentation, which is authoritative and moves
without telling us. The same four commands are printed by Switchyard when a
project's role needs a CLI that is not installed, from
`AGENT_CLI_INSTALL_COMMANDS` in `scripts/team_launcher.py` -- update both
together. A test asserts this table and that one agree, so they cannot drift
apart quietly. Before running any `curl ... | bash` or `curl ... | sh`
command, you can fetch the script URL without piping it and read the script
first.

| CLI | Vendor-documented Linux install | Vendor documentation |
| --- | --- | --- |
| `claude` | `curl -fsSL https://claude.ai/install.sh \| bash`; Anthropic also documents a signed apt repository whose Debian/Ubuntu package is `claude-code`, not `claude`. No official pacman package is documented. | <https://code.claude.com/docs/en/setup> |
| `codex` | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | <https://learn.chatgpt.com/docs/codex/cli> |
| `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | <https://antigravity.google/docs/cli/install/> |
| `hermes` | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` | <https://hermes-agent.nousresearch.com/docs/getting-started/installation> |

After installing, `switchyard new` detects the CLIs that are present and walks
you through each one's sign-in before any pane starts; that is where you
complete authentication. Hermes signs in with `hermes login`; the others
authenticate on first run.

## Notes

- Python must be 3.11 or newer.
- `tmux` is required for role sessions and the non-KDE/headless viewer layout.
- `konsole` is required only for the KDE/separate-window layout path. On a
  Zorin 18.1 / Ubuntu 24.04 noble dry run, `apt-get install --dry-run konsole`
  listed 141 packages, including KDE Frameworks 5 packages such as
  `libkf5kirigami2`, `libkf5parts-plugins`, `libkf5xmlgui-bin`, and
  `libkf5quickaddons5`; Qt5 Wayland support including
  `libqt5waylandcompositor5`; `qml-module-org-kde-*` packages; `sonnet-plugins`;
  and `libvoikko1`.
- `python-is-python3` is not required. Zorin may not provide a bare `python`
  command, but Switchyard invokes `python3`; the bare `python` package name is
  only used in the pacman package set.
- `PyYAML` is optional for preserving an existing Hermes config.

## Install Scripts

```bash
sudo ./install
```

The top-level installer calls the internal prereq and release installers in the
right order. For operators who need a reviewable privileged block instead of an
automatic release install, `scripts/install-switchyard --print-commands` still
prints the release-install block, and `scripts/install-switchyard --apply`
performs that internal release step.
