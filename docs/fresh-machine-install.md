# Switchyard Fresh-Machine Install List

## System Packages

### apt

```bash
sudo apt-get update
sudo apt-get install python3 postgresql postgresql-client tmux konsole git curl python3-pip acl
```

### pacman

```bash
sudo pacman -S python postgresql tmux konsole git curl python-pip acl
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
- `konsole` is required; the pane layout format is Konsole-specific.
- `PyYAML` is optional for preserving an existing Hermes config.

## Install Script

```bash
scripts/install-switchyard-prereqs
```
