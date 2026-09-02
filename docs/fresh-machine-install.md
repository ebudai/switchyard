# Switchyard Fresh-Machine Install List

## System Packages

### apt

```bash
sudo apt-get update
sudo apt-get install python3 postgresql postgresql-client tmux git curl python3-pip acl
```

### pacman

```bash
sudo pacman -Syu
sudo pacman -S python postgresql tmux git curl python-pip acl
```

`scripts/install-switchyard-prereqs` runs `apt-get update` before apt installs
and prints that refresh. It does not run `pacman -Sy`; Arch-family systems
should use the full `pacman -Syu` upgrade first to avoid partial-upgrade
breakage.

Konsole is not a base dependency. On KDE or when forcing the separate-window
layout, also install it explicitly:

```bash
sudo apt-get install konsole
# or
sudo pacman -S konsole
```

## Python Package

```bash
python3 -m pip install --user 'psycopg>=3.3,<4'
```

## Agent CLI

Install and authenticate at least one of:

- `claude`
- `codex`
- `agy`
- `hermes`

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
scripts/install-switchyard-prereqs
scripts/install-switchyard
```

Run `scripts/install-switchyard-prereqs` first to install the packages and
Python dependency listed above. Then run `scripts/install-switchyard` to print
the privileged install command block. Applying that block needs root; a non-root
user should inspect the printed `sudo env ... --apply` command and have a
sudo-capable operator run it.
