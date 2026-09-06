#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/install-switchyard"
TMPDIR_T="$(mktemp -d)"
trap 'rm -r "$TMPDIR_T"' EXIT

source_repo="$TMPDIR_T/source"
shared_root="$TMPDIR_T/opt/switchyard"
install_path="$TMPDIR_T/bin/switchyard"
mkdir -p "$source_repo/scripts" "$(dirname "$install_path")"

write_source() {
    local version="$1"
    cat >"$source_repo/switchyard" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
    --help|-h|help)
        printf 'switchyard help $version\n'
        ;;
    --version|version)
        printf 'switchyard $version\n'
        ;;
    --switchyard-wrapper-requires-root)
        shift
        case "\${1:-}" in
            new|status)
                printf 'requires-root\n'
                ;;
            *)
                printf 'no-root\n'
                ;;
        esac
        ;;
    *)
        printf 'switchyard $version:%s\n' "\$*"
        ;;
esac
EOF
    chmod 0755 "$source_repo/switchyard"
    cat >"$source_repo/scripts/team-launcher" <<'EOF'
#!/usr/bin/env bash
printf 'team-launcher:%s\n' "$*"
EOF
    chmod 0755 "$source_repo/scripts/team-launcher"
    cat >"$source_repo/scripts/install-switchyard" <<'EOF'
#!/usr/bin/env bash
printf 'source-checkout installer placeholder\n'
EOF
    chmod 0755 "$source_repo/scripts/install-switchyard"
}

commit_source() {
    local message="$1"
    git -C "$source_repo" add switchyard scripts/team-launcher scripts/install-switchyard
    git -C "$source_repo" commit -m "$message" >/dev/null
    git -C "$source_repo" rev-parse HEAD
}

git -C "$source_repo" init -q
git -C "$source_repo" config user.email test@example.invalid
git -C "$source_repo" config user.name "Switchyard Test"
write_source one
first_sha="$(commit_source "first release")"

SWITCHYARD_SOURCE_REPO="$source_repo" \
SWITCHYARD_SOURCE_REF="$first_sha" \
SWITCHYARD_SHARED_INSTALL_ROOT="$shared_root" \
SWITCHYARD_INSTALL_PATH="$install_path" \
    "$SCRIPT" --apply >"$TMPDIR_T/install-one.log"

first_release="$shared_root/releases/$first_sha"
[[ -x "$first_release/switchyard" ]] || {
    echo "FAIL: first release did not export an executable switchyard" >&2
    exit 1
}
[[ "$(readlink "$shared_root/current")" == "$first_release" ]] || {
    echo "FAIL: current symlink does not point at the first release" >&2
    exit 1
}
python3 - "$first_release/.switchyard-release.json" "$first_sha" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["commit"] == sys.argv[2]
PY

[[ -x "$install_path" ]] || {
    echo "FAIL: installed switchyard should be executable" >&2
    exit 1
}
[[ ! -L "$install_path" ]] || {
    echo "FAIL: installed switchyard must be a trampoline, not a symlink" >&2
    exit 1
}
grep -q "readonly SWITCHYARD_DEFAULT_TARGET=$shared_root/current/switchyard" "$install_path" || {
    echo "FAIL: wrapper target is not the shared current switchyard" >&2
    exit 1
}
grep -Fq 'exec "$SWITCHYARD_DEFAULT_TARGET" "$@"' "$install_path" || {
    echo "FAIL: already-root wrapper branch must ignore SWITCHYARD_TARGET and execute the shared default target" >&2
    exit 1
}
if grep -Fq "/home/" "$install_path"; then
    echo "FAIL: wrapper must not embed any user's home path" >&2
    exit 1
fi

[[ "$("$install_path" --help)" == "switchyard help one" ]] || {
    echo "FAIL: reachable --help did not execute the shared target" >&2
    exit 1
}
[[ "$("$install_path" --version)" == "switchyard one" ]] || {
    echo "FAIL: reachable --version did not execute the shared target" >&2
    exit 1
}

override="$TMPDIR_T/override-switchyard"
cat >"$override" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--switchyard-wrapper-requires-root" ]]; then
    printf 'no-root\n'
    exit 0
fi
printf 'override:%s\n' "$*"
EOF
chmod 0755 "$override"

[[ "$(SWITCHYARD_TARGET="$override" "$install_path" custom)" == "override:custom" ]] || {
    echo "FAIL: non-privileged SWITCHYARD_TARGET override did not work" >&2
    exit 1
}

fake_sudo="$TMPDIR_T/fake-sudo"
fake_sudo_log="$TMPDIR_T/fake-sudo.log"
cat >"$fake_sudo" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-n" && "${2:-}" == "-v" ]]; then
    exit 0
fi
if [[ "${1:-}" == "-n" ]]; then
    shift
fi
if [[ "${1:-}" == "env" && "${2:-}" == PYTHONDONTWRITEBYTECODE=* ]]; then
    shift 2
fi
printf '%s\n' "$*" >>"${FAKE_SUDO_LOG:?}"
printf 'sudo:%s\n' "$*"
EOF
chmod 0755 "$fake_sudo"

if [[ "$(id -u)" != "0" ]]; then
    elevated_output="$(
        FAKE_SUDO_LOG="$fake_sudo_log" \
        SWITCHYARD_SUDO_BIN="$fake_sudo" \
        SWITCHYARD_TARGET="$override" \
            "$install_path" new
    )"
    [[ "$elevated_output" == "sudo:$shared_root/current/switchyard new" ]] || {
        echo "FAIL: privileged invocation did not ignore SWITCHYARD_TARGET override" >&2
        echo "$elevated_output" >&2
        exit 1
    }
    grep -q "^$shared_root/current/switchyard new$" "$fake_sudo_log" || {
        echo "FAIL: privileged invocation did not use the shared default target" >&2
        cat "$fake_sudo_log" >&2
        exit 1
    }
fi

mv "$shared_root/current" "$shared_root/current.missing"
fallback_sudo_log="$TMPDIR_T/fallback-sudo.log"
[[ "$("$install_path" --help)" == "switchyard help one" ]] || {
    echo "FAIL: missing target --help did not use embedded fallback" >&2
    exit 1
}
[[ "$("$install_path" --version)" == "switchyard one" ]] || {
    echo "FAIL: missing target --version did not use embedded fallback" >&2
    exit 1
}
set +e
missing_output="$(
    FAKE_SUDO_LOG="$fallback_sudo_log" \
    SWITCHYARD_SUDO_BIN="$fake_sudo" \
        "$install_path" new 2>&1
)"
missing_status=$?
set -e
[[ "$missing_status" == "126" ]] || {
    echo "FAIL: missing shared target should fail with 126" >&2
    echo "$missing_output" >&2
    exit 1
}
grep -q "shared target is not executable: $shared_root/current/switchyard" <<<"$missing_output" || {
    echo "FAIL: missing target error did not name the shared target" >&2
    echo "$missing_output" >&2
    exit 1
}
grep -Fq 'recover from a fresh clone of the canonical GitHub repository with:' <<<"$missing_output" || {
    echo "FAIL: missing target error did not describe canonical GitHub recovery" >&2
    echo "$missing_output" >&2
    exit 1
}
grep -Fq 'git clone https://github.com/ebudai/switchyard.git "$tmpdir"' <<<"$missing_output" || {
    echo "FAIL: missing target recovery command did not clone the canonical GitHub repository" >&2
    echo "$missing_output" >&2
    exit 1
}
grep -Fq 'SWITCHYARD_SOURCE_REPO="$tmpdir"' <<<"$missing_output" || {
    echo "FAIL: missing target recovery command did not install from the fresh clone" >&2
    echo "$missing_output" >&2
    exit 1
}
grep -Fq '"$tmpdir/scripts/install-switchyard" --apply' <<<"$missing_output" || {
    echo "FAIL: missing target recovery command did not run the fresh clone installer" >&2
    echo "$missing_output" >&2
    exit 1
}
if grep -Fq "/data/git/" <<<"$missing_output"; then
    echo "FAIL: missing target recovery command must not reference the local cache" >&2
    echo "$missing_output" >&2
    exit 1
fi
if grep -Fq "/home/" <<<"$missing_output"; then
    echo "FAIL: missing target recovery command must not reference any user's home" >&2
    echo "$missing_output" >&2
    exit 1
fi
if grep -Fq "$first_release/scripts/install-switchyard --apply" <<<"$missing_output"; then
    echo "FAIL: missing target recovery command must not point into the broken release" >&2
    echo "$missing_output" >&2
    exit 1
fi
[[ ! -s "$fallback_sudo_log" ]] || {
    echo "FAIL: missing target attempted sudo instead of failing with recovery instructions" >&2
    cat "$fallback_sudo_log" >&2
    exit 1
}
mv "$shared_root/current.missing" "$shared_root/current"

write_source two
second_sha="$(commit_source "second release")"
SWITCHYARD_SOURCE_REPO="$source_repo" \
SWITCHYARD_SOURCE_REF="$second_sha" \
SWITCHYARD_SHARED_INSTALL_ROOT="$shared_root" \
SWITCHYARD_INSTALL_PATH="$install_path" \
    "$SCRIPT" --apply >"$TMPDIR_T/install-two.log"
second_release="$shared_root/releases/$second_sha"
[[ "$("$install_path" --version)" == "switchyard two" ]] || {
    echo "FAIL: second install did not activate the new release" >&2
    exit 1
}
ln -sfn "$first_release" "$shared_root/current"
[[ "$("$install_path" --version)" == "switchyard one" ]] || {
    echo "FAIL: rollback symlink did not restore the first release" >&2
    exit 1
}
ln -sfn "$second_release" "$shared_root/current"
[[ "$("$install_path" --version)" == "switchyard two" ]] || {
    echo "FAIL: rollback-forward symlink did not restore the second release" >&2
    exit 1
}

printout="$(
    SWITCHYARD_SOURCE_REPO="$source_repo" \
    SWITCHYARD_SOURCE_REF="$second_sha" \
    SWITCHYARD_SHARED_INSTALL_ROOT=/opt/switchyard \
    SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard \
        "$SCRIPT" --print-commands
)"

grep -q "SWITCHYARD_SOURCE_REPO=$source_repo" <<<"$printout" || {
    echo "FAIL: printed command block does not name the source checkout" >&2
    exit 1
}
grep -q "SWITCHYARD_SHARED_INSTALL_ROOT=/opt/switchyard" <<<"$printout" || {
    echo "FAIL: printed command block does not install under /opt/switchyard" >&2
    exit 1
}
grep -q "SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard" <<<"$printout" || {
    echo "FAIL: printed command block does not install under /usr/local/bin" >&2
    exit 1
}
grep -q "ln -sfn /opt/switchyard/releases/<previous-sha> /opt/switchyard/current" <<<"$printout" || {
    echo "FAIL: printed command block does not document plain-symlink rollback" >&2
    exit 1
}
grep -q "sudo env sh -c 'command -v switchyard'" <<<"$printout" || {
    echo "FAIL: printed command block does not verify sudo secure_path resolution" >&2
    exit 1
}

echo "install_switchyard_test: ok"
