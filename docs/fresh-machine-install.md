# Switchyard Fresh-Machine Install Notes

From a fresh clone, install Switchyard with one command:

```bash
git clone <repo> switchyard
cd switchyard
sudo ./install
```

The installer prints each system-changing command before it runs. On a terminal
it asks about each missing agent CLI with a `[Y/n]` prompt and shows the exact
vendor installer command before running it. Empty input means yes. Pass
`--cli claude`, `--cli codex`, `--cli agy`, or `--cli hermes` to limit CLI
installation to one runtime. `--yes` answers yes to every CLI that would be
prompted, so bare `--yes` runs all four missing CLI installers and
`--yes --cli claude` runs only Claude without prompting. `--no-cli` skips CLI
installation. If stdin/stdout is not a TTY and `--yes` was not passed, CLI
installation is skipped instead of blocking.

Authentication cannot be automated. The final output of `sudo ./install` tells
you which installed CLI to run and sign in to, prints a checklist when more than
one CLI was installed, or tells you to install one CLI manually if all CLI
installers were skipped. Hermes signs in with `hermes login`; the other CLIs
authenticate on first run.

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

`sudo ./install` can run the vendor-documented agent CLI installers below. These
commands are not distro packages on the target platforms, so the installer asks
before running each missing CLI unless `--yes`, `--cli`, or `--no-cli` was
supplied. Pass `--cli <name>` to install or prompt for only one CLI. A failed,
declined, or skipped CLI install does not roll back the Switchyard host install.

Install and authenticate at least one CLI before launching panes:

Before running any `curl ... | bash` or `curl ... | sh` command, you can fetch
the script URL without piping it and read the script first.

| CLI | Debian/Ubuntu | Arch-family | How this was established |
| --- | --- | --- | --- |
| `claude` | `curl -fsSL https://claude.ai/install.sh \| bash`; Anthropic also documents a signed apt repository whose Debian/Ubuntu package is `claude-code`, not `claude`. | `curl -fsSL https://claude.ai/install.sh \| bash`; no official pacman package is documented by Anthropic. | Anthropic Claude Code setup docs: <https://code.claude.com/docs/en/setup>. |
| `codex` | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | Official OpenAI Codex CLI quickstart: <https://learn.chatgpt.com/docs/codex/cli>. |
| `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | Google Antigravity CLI Installation & auth docs: <https://antigravity.google/docs/cli/install/>. |
| `hermes` | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` | Nous Research Hermes Agent installation docs: <https://hermes-agent.nousresearch.com/docs/getting-started/installation>. |

After installation, open the chosen CLI and complete authentication. `sudo
./install` leaves this as its final output because it cannot be automated. For
Codex, the official quickstart says to run `codex` from a project directory and
sign in on first launch. Claude Code similarly requires running `claude` and
following the login prompts. Antigravity opens a browser sign-in flow for `agy`
when no saved session exists, and Hermes requires choosing or authenticating a
provider, for example with `hermes model` or `hermes setup --portal`.

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
