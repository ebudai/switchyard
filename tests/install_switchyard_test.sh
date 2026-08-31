#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/install-switchyard"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

source_path="$TMPDIR_T/live-checkout/switchyard"
install_path="$TMPDIR_T/bin/switchyard"
mkdir -p "$(dirname "$source_path")"
cat >"$source_path" <<'EOF'
#!/usr/bin/env bash
printf 'version-one:%s\n' "$*"
EOF
chmod +x "$source_path"

SWITCHYARD_SOURCE_PATH="$source_path" \
SWITCHYARD_INSTALL_PATH="$install_path" \
    "$SCRIPT" --apply >"$TMPDIR_T/install.log"

[[ -x "$install_path" ]] || {
    echo "FAIL: installed switchyard should be executable" >&2
    exit 1
}
[[ ! -L "$install_path" ]] || {
    echo "FAIL: installed switchyard must be a trampoline, not a symlink into /home/agent" >&2
    exit 1
}
if grep -q "version-one" "$install_path"; then
    echo "FAIL: installer copied launcher source instead of writing a trampoline" >&2
    exit 1
fi

fake_privilege_bin="$TMPDIR_T/fake-privilege-bin"
fake_privilege="$fake_privilege_bin/sudo"
fake_privilege_log="$TMPDIR_T/fake-privilege.log"
mkdir -p "$fake_privilege_bin"
cat >"$fake_privilege" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-n" && "${2:-}" == "-v" ]]; then
    exit 0
fi
if [[ "${1:-}" == "-n" ]]; then
    shift
fi
printf '%s\n' "$*" >>"${FAKE_PRIVILEGE_LOG:?}"
exec "$@"
EOF
chmod +x "$fake_privilege"

first_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_log" \
    PATH="$fake_privilege_bin:$PATH" \
        "$install_path" otto
)"
[[ "$first_output" == "version-one:otto" ]] || {
    echo "FAIL: installed switchyard did not execute the source entrypoint" >&2
    exit 1
}
if [[ "$(id -u)" != "0" ]]; then
    if [ -s "$fake_privilege_log" ]; then
        echo "FAIL: trampoline elevated a bare project launch" >&2
        cat "$fake_privilege_log" >&2
        exit 1
    fi
fi

help_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_log" \
    PATH="$fake_privilege_bin:$PATH" \
        "$install_path" --help
)"
grep -q "switchyard <project name or slug>" <<<"$help_output" || {
    echo "FAIL: installed switchyard did not print embedded --help" >&2
    echo "$help_output" >&2
    exit 1
}
grep -q "Bare project names start or attach the project" <<<"$help_output" || {
    echo "FAIL: installed switchyard help does not explain bare project launch" >&2
    echo "$help_output" >&2
    exit 1
}
if [[ "$(id -u)" != "0" && -s "$fake_privilege_log" ]]; then
    echo "FAIL: trampoline elevated --help" >&2
    cat "$fake_privilege_log" >&2
    exit 1
fi

cat >"$source_path" <<'EOF'
#!/usr/bin/env bash
printf 'version-two:%s\n' "$*"
EOF
chmod +x "$source_path"
second_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_log" \
    PATH="$fake_privilege_bin:$PATH" \
        "$install_path" new
)"
[[ "$second_output" == "version-two:new" ]] || {
    echo "FAIL: switchyard install went stale instead of following the live source" >&2
    exit 1
}
if [[ "$(id -u)" != "0" ]]; then
    grep -q "^$source_path new$" "$fake_privilege_log" || {
        echo "FAIL: trampoline did not acquire privilege for switchyard new" >&2
        cat "$fake_privilege_log" >&2
        exit 1
    }
fi

fake_privilege_noexec_bin="$TMPDIR_T/fake-privilege-noexec-bin"
fake_privilege_noexec="$fake_privilege_noexec_bin/sudo"
fake_privilege_noexec_log="$TMPDIR_T/fake-privilege-noexec.log"
mkdir -p "$fake_privilege_noexec_bin"
cat >"$fake_privilege_noexec" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-n" && "${2:-}" == "-v" ]]; then
    exit 0
fi
if [[ "${1:-}" == "-n" ]]; then
    shift
fi
printf '%s\n' "$*" >>"${FAKE_PRIVILEGE_LOG:?}"
printf 'privileged:%s\n' "$*"
EOF
chmod +x "$fake_privilege_noexec"
chmod 000 "$(dirname "$source_path")"
if ! inaccessible_help_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_noexec_log" \
    PATH="$fake_privilege_noexec_bin:$PATH" \
        "$install_path" --help 2>&1
)"; then
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: --help failed when the target was inaccessible" >&2
    echo "$inaccessible_help_output" >&2
    exit 1
fi
grep -q "switchyard <project name or slug>" <<<"$inaccessible_help_output" || {
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: inaccessible target --help did not print help" >&2
    echo "$inaccessible_help_output" >&2
    exit 1
}
if [[ -s "$fake_privilege_noexec_log" ]]; then
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: inaccessible target --help tried to use sudo" >&2
    cat "$fake_privilege_noexec_log" >&2
    exit 1
fi
if ! inaccessible_bare_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_noexec_log" \
    PATH="$fake_privilege_noexec_bin:$PATH" \
        "$install_path" otto 2>&1
)"; then
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: inaccessible target bare project did not delegate cleanly to privilege command" >&2
    echo "$inaccessible_bare_output" >&2
    exit 1
fi
[[ "$inaccessible_bare_output" == "privileged:$source_path otto" ]] || {
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: inaccessible target bare project did not pass through the privilege command" >&2
    echo "$inaccessible_bare_output" >&2
    exit 1
}
grep -q "^$source_path otto$" "$fake_privilege_noexec_log" || {
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: inaccessible target bare project was not delegated to the privilege command" >&2
    cat "$fake_privilege_noexec_log" >&2
    exit 1
}
>"$fake_privilege_noexec_log"
if ! inaccessible_output="$(
    FAKE_PRIVILEGE_LOG="$fake_privilege_noexec_log" \
    PATH="$fake_privilege_noexec_bin:$PATH" \
        "$install_path" new 2>&1
)"; then
    chmod 755 "$(dirname "$source_path")"
    echo "FAIL: trampoline touched the target before acquiring privilege" >&2
    echo "$inaccessible_output" >&2
    exit 1
fi
chmod 755 "$(dirname "$source_path")"
[[ "$inaccessible_output" == "privileged:$source_path new" ]] || {
    echo "FAIL: trampoline did not pass the target through the privilege command" >&2
    echo "$inaccessible_output" >&2
    exit 1
}
grep -q "^$source_path new$" "$fake_privilege_noexec_log" || {
    echo "FAIL: inaccessible source was not delegated to the privilege command" >&2
    cat "$fake_privilege_noexec_log" >&2
    exit 1
}

>"$fake_privilege_noexec_log"
if [[ "$(id -u)" != "0" ]]; then
    for privileged_args in \
        "new" \
        "register $TMPDIR_T/project.json" \
        "upgrade otto" \
        "status" \
        "validate-models otto" \
        "stop otto"; do
        privileged_output="$(
            FAKE_PRIVILEGE_LOG="$fake_privilege_noexec_log" \
            PATH="$fake_privilege_noexec_bin:$PATH" \
                "$install_path" $privileged_args
        )"
        [[ "$privileged_output" == "privileged:$source_path $privileged_args" ]] || {
            echo "FAIL: trampoline did not acquire privilege for switchyard $privileged_args" >&2
            echo "$privileged_output" >&2
            exit 1
        }
        grep -q "^$source_path $privileged_args$" "$fake_privilege_noexec_log" || {
            echo "FAIL: switchyard $privileged_args did not reach the privilege command" >&2
            cat "$fake_privilege_noexec_log" >&2
            exit 1
        }
    done
fi

fake_privilege_denied="$TMPDIR_T/sudo-denied"
cat >"$fake_privilege_denied" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-n" && "${2:-}" == "-v" ]]; then
    printf 'sudo: a password is required\n' >&2
    exit 1
fi
printf 'FAIL: sudo would have prompted or blocked: %s\n' "$*" >&2
sleep 10
EOF
chmod +x "$fake_privilege_denied"
if [[ "$(id -u)" != "0" ]]; then
    denied_stdout="$TMPDIR_T/denied.stdout"
    denied_stderr="$TMPDIR_T/denied.stderr"
    set +e
    timeout 2 env SWITCHYARD_SUDO_BIN="$fake_privilege_denied" "$install_path" new >"$denied_stdout" 2>"$denied_stderr"
    denied_status=$?
    set -e
    if [[ "$denied_status" == "0" ]]; then
        echo "FAIL: privileged switchyard new unexpectedly succeeded without sudo" >&2
        exit 1
    fi
    if [[ "$denied_status" == "124" ]]; then
        echo "FAIL: privileged switchyard new blocked instead of failing fast" >&2
        cat "$denied_stderr" >&2
        exit 1
    fi
    grep -q "switchyard: command requires root: switchyard new" "$denied_stderr" || {
        echo "FAIL: no-sudo failure did not name the privileged command" >&2
        cat "$denied_stderr" >&2
        exit 1
    }
    if grep -q "FAIL: sudo would have prompted" "$denied_stderr"; then
        echo "FAIL: wrapper attempted an interactive sudo after non-interactive preflight failed" >&2
        cat "$denied_stderr" >&2
        exit 1
    fi
fi

printout="$(
    SWITCHYARD_SOURCE_PATH=/home/agent/Projects/pgu/switchyard \
    SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard \
        "$SCRIPT" --print-commands
)"

grep -q "SWITCHYARD_SOURCE_PATH=/home/agent/Projects/pgu/switchyard" <<<"$printout" || {
    echo "FAIL: printed command block does not point at the shared checkout switchyard" >&2
    exit 1
}
grep -q "SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard" <<<"$printout" || {
    echo "FAIL: printed command block does not install under /usr/local/bin" >&2
    exit 1
}
grep -q -- "--apply" <<<"$printout" || {
    echo "FAIL: printed command block does not apply the installer" >&2
    exit 1
}
if grep -q -- "--user eric\\|setfacl\\|SWITCHYARD_ALLOWED_USER" <<<"$printout"; then
    echo "FAIL: printed command block must not grant Eric traverse access to /home/agent" >&2
    echo "$printout" >&2
    exit 1
fi
grep -q "sudo env sh -c 'command -v switchyard'" <<<"$printout" || {
    echo "FAIL: printed command block does not verify sudo secure_path resolution" >&2
    exit 1
}
if grep -q "install -m 0755 .*switchyard" "$SCRIPT"; then
    echo "FAIL: installer must not copy switchyard into place" >&2
    exit 1
fi

fake_sudo="$TMPDIR_T/sudo-no-switchyard-path"
cat >"$fake_sudo" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-V" ]]; then
    printf "Value to override user's \$PATH with: /usr/bin:/bin\n"
    exit 0
fi
exit 99
EOF
chmod +x "$fake_sudo"
missing_secure_path_install="$TMPDIR_T/secure-path-missing/bin/switchyard"
if SWITCHYARD_SOURCE_PATH="$source_path" \
    SWITCHYARD_INSTALL_PATH="$missing_secure_path_install" \
    SWITCHYARD_SUDO_BIN="$fake_sudo" \
    SWITCHYARD_FORCE_SECURE_PATH_CHECK=1 \
        "$SCRIPT" --apply >"$TMPDIR_T/secure-path-missing.log" 2>&1; then
    echo "FAIL: installer accepted an install dir missing from sudo secure_path" >&2
    exit 1
fi
grep -q "is not in sudo secure_path" "$TMPDIR_T/secure-path-missing.log" || {
    echo "FAIL: secure_path failure did not explain the missing install dir" >&2
    cat "$TMPDIR_T/secure-path-missing.log" >&2
    exit 1
}
[[ ! -e "$missing_secure_path_install" ]] || {
    echo "FAIL: installer wrote the trampoline after secure_path rejection" >&2
    exit 1
}

fake_sudo_ok="$TMPDIR_T/sudo-with-switchyard-path"
secure_path_install="$TMPDIR_T/secure-path-ok/bin/switchyard"
cat >"$fake_sudo_ok" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "-V" ]]; then
    printf "Value to override user's \\\$PATH with: /usr/bin:$(dirname "$secure_path_install"):/bin\\n"
    exit 0
fi
exit 99
EOF
chmod +x "$fake_sudo_ok"
SWITCHYARD_SOURCE_PATH="$source_path" \
SWITCHYARD_INSTALL_PATH="$secure_path_install" \
SWITCHYARD_SUDO_BIN="$fake_sudo_ok" \
SWITCHYARD_FORCE_SECURE_PATH_CHECK=1 \
    "$SCRIPT" --apply >"$TMPDIR_T/secure-path-ok.log"
[[ -x "$secure_path_install" && ! -L "$secure_path_install" ]] || {
    echo "FAIL: installer rejected a directory that sudo secure_path contains" >&2
    exit 1
}

echo "install_switchyard_test: ok"
