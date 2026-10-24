#!/bin/bash
# SYRD-642 test service: Oracle Database Free in ONE DISPOSABLE container, for the Oracle board suites.
#
#   oracle-free-test-service.sh start      start docker if needed; create (once) and start the container
#   oracle-free-test-service.sh status     container state, port binding and image; never a secret
#   oracle-free-test-service.sh teardown   remove the container and its data volume; stop docker if we started it
#
# Run as root (the one privileged step the Director authorised on SYRD-642, option 1b). What it does:
# * The image is pinned by digest: gvenzl/oracle-free 23.9-slim.
# * The listener is published on 127.0.0.1:1521 only, never on another interface.
# * Passwords are read from the project account's owner-only files (mode 0600, generated unprivileged by
#   tests/oracle/oracle_test_credentials.py init), checked against a strict alphabet, and handed to docker
#   through a root-only env file that is deleted straight after `docker create`. No password is ever in a
#   command line or printed.
# * Everything it creates carries the label switchyard.disposable=SYRD-642; `teardown` removes all of it.
#   It never touches another container, image, volume or network, and installs nothing.
set -euo pipefail
umask 077

OWNER=switchyard-agent
CRED_DIR=/home/switchyard-agent/.config/switchyard-oracle-test
IMAGE='docker.io/gvenzl/oracle-free:23.9-slim@sha256:945400df5e3fc9589db628223385f906e1024932dc3b72e118fc4fcd0f0e9bbb'
NAME=syrd642-oracle-free
VOLUME=syrd642-oracle-free-data
LABEL=switchyard.disposable=SYRD-642
ENVFILE=/root/.syrd642-oracle-free.env
MARKER=/root/.syrd642-started-docker

die() { echo "oracle-free-test-service: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

read_secret() {
    local file="$CRED_DIR/$1" value
    [ -f "$file" ] && [ ! -L "$file" ] || die "$file is missing or not a regular file; run oracle_test_credentials.py init"
    [ "$(stat -c %U "$file")" = "$OWNER" ] && [ "$(stat -c %a "$file")" = 600 ] \
        || die "$file must be owned by $OWNER with mode 0600"
    value=$(head -c 64 "$file" | tr -d '\n')
    [[ "$value" =~ ^[A-Za-z][A-Za-z0-9]{15,29}$ ]] || die "$file does not hold a password in the expected alphabet"
    printf '%s' "$value"
}

status() {
    docker container inspect "$NAME" --format \
        'container {{.Name}}: {{.State.Status}} (health {{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}); image {{.Config.Image}}; ports {{json .HostConfig.PortBindings}}; labels {{json .Config.Labels}}' \
        2>/dev/null || echo "container $NAME: absent"
}

case "${1:-}" in
start)
    if ! systemctl is-active --quiet docker.service; then
        systemctl start docker.service
        touch "$MARKER"
    fi
    if ! docker container inspect "$NAME" >/dev/null 2>&1; then
        docker pull --quiet "$IMAGE" >/dev/null
        rm -f "$ENVFILE"
        {
            printf 'ORACLE_PASSWORD=%s\n' "$(read_secret system.password)"
            printf 'APP_USER=SYRD_BOARD_OWNER\n'
            printf 'APP_USER_PASSWORD=%s\n' "$(read_secret bootstrap.password)"
        } > "$ENVFILE"
        docker volume create --label "$LABEL" "$VOLUME" >/dev/null
        docker create --name "$NAME" --label "$LABEL" --restart no --memory 4g \
            --publish 127.0.0.1:1521:1521 --env-file "$ENVFILE" \
            --mount "type=volume,src=$VOLUME,dst=/opt/oracle/oradata" "$IMAGE" >/dev/null
        rm -f "$ENVFILE"
    fi
    docker start "$NAME" >/dev/null
    for _ in $(seq 1 120); do
        if docker logs "$NAME" 2>&1 | grep -q "DATABASE IS READY TO USE"; then
            status
            echo "ready: 127.0.0.1:1521/FREEPDB1"
            exit 0
        fi
        sleep 5
    done
    status
    die "the database did not report ready within 10 minutes; see: docker logs $NAME"
    ;;
status)
    status
    ;;
teardown)
    docker rm --force "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    rm -f "$ENVFILE"
    if [ -e "$MARKER" ]; then
        systemctl stop docker.service docker.socket
        rm -f "$MARKER"
    fi
    echo "removed container $NAME and volume $VOLUME"
    ;;
*)
    die "usage: $0 start|status|teardown"
    ;;
esac
