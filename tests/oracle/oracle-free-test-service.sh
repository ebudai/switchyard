#!/bin/bash
# SYRD-642 test service: Oracle Database Free in ONE DISPOSABLE container, for the Oracle board suites.
#
#   oracle-free-test-service.sh start      start docker if needed; create (once) and start the container
#   oracle-free-test-service.sh tcps       (SYRD-645) add a TCPS listener on 127.0.0.1:2484, and give the owner
#                                          the client's wallet: the CA's public certificate, nothing secret.
#                                          Run it again after each `start`, which restores the stock listener
#   oracle-free-test-service.sh status     container state, port binding and image; never a secret
#   oracle-free-test-service.sh teardown   remove the container and its data volume; stop docker if we started it
#
# Run as root (the one privileged step the Director authorised on SYRD-642, option 1b). What it does:
# * The image is pinned by digest: gvenzl/oracle-free 23.9-slim.
# * The listener is published on 127.0.0.1:1521 only, never on another interface.
# * Passwords are read from the project account's owner-only files (mode 0600, generated unprivileged by
#   tests/oracle/oracle_test_credentials.py init), checked against a strict alphabet, and handed to the
#   container as files (SYRD-645). They sit in a directory only the database's uid can read, mounted read-only
#   at /run/secrets; the image reads ORACLE_PASSWORD_FILE and APP_USER_PASSWORD_FILE once, when it first creates
#   the database; they are deleted as soon as that first start ends, and the container is started once more,
#   because the image exports what it read into that start's processes. No password is ever in the
#   container's environment or spec, in a command line, or printed. A container created by an earlier version
#   of this script holds them in its environment: `start` refuses it, and teardown, then start, replaces it.
# * Everything it creates carries the label switchyard.disposable=SYRD-642; `teardown` removes all of it.
#   It never touches another container, image, volume or network, and installs nothing.
# * TCPS (SYRD-645). The container's image has no Java, so it cannot make a wallet. `tcps` runs openssl and
#   orapki once from Oracle's own Free image, pinned by digest, as a throwaway container with no network that
#   mounts only the data volume. There it makes a test CA (an X.509 v3 CA certificate, which strict TLS clients
#   require of a trust anchor) and a server certificate (CN=syrd645-tcps-test, serverAuth, 127.0.0.1) signed by
#   it, imports the server's key and chain into an auto-login wallet, and deletes the CA's key at once, so
#   nothing else can be signed. The listener then serves TCPS on 2484, published on 127.0.0.1 only, without
#   client authentication. The owner gets the CA certificate alone, as ewallet.pem, which is what
#   python-oracledb reads. No private key leaves the volume. Both images stay pulled; `docker rmi` removes them.
set -euo pipefail
umask 077

OWNER=switchyard-agent
CRED_DIR=/home/switchyard-agent/.config/switchyard-oracle-test
IMAGE='docker.io/gvenzl/oracle-free:23.9-slim@sha256:945400df5e3fc9589db628223385f906e1024932dc3b72e118fc4fcd0f0e9bbb'
NAME=syrd642-oracle-free
VOLUME=syrd642-oracle-free-data
LABEL=switchyard.disposable=SYRD-642
TOOL_IMAGE='container-registry.oracle.com/database/free@sha256:f988b0c04c4c386cd306a2a914c0d7a9702d83acc31b064a28ad8eb6278a8fba'
WALLET=/opt/oracle/oradata/tcps-wallet
SERVER_DN=CN=syrd645-tcps-test
NETADMIN=/opt/oracle/product/23ai/dbhomeFree/network/admin
CLIENT_WALLET=$CRED_DIR/tcps-wallet
ENVFILE=/root/.syrd642-oracle-free.env  # earlier versions' env file; teardown still removes one left behind
SECRETS=/root/.syrd642-oracle-secrets
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

started() {  # wait for the start at $1 (UTC) to report ready; its lines only, as earlier starts said ready too
    local said
    for _ in $(seq 1 120); do
        said=$(docker logs --since "$1" "$NAME" 2>&1)
        grep -q "DATABASE IS READY TO USE" <<<"$said" && return 0
        grep -q "DATABASE STARTUP FAILED" <<<"$said" && { status; die "the database did not start; see: docker logs $NAME"; }
        sleep 5
    done
    status
    die "the database did not report ready within 10 minutes; see: docker logs $NAME"
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
        # Files, read once by the image when it first creates the database, and deleted when this start ends.
        rm -rf -- "$SECRETS"
        mkdir -m 0700 "$SECRETS"
        read_secret system.password > "$SECRETS/oracle_password"
        read_secret bootstrap.password > "$SECRETS/app_user_password"
        chown -R 54321:54321 "$SECRETS"
        chmod 0400 "$SECRETS/oracle_password" "$SECRETS/app_user_password"
        trap 'rm -f -- "$SECRETS/oracle_password" "$SECRETS/app_user_password"' EXIT
        created=1
        docker volume create --label "$LABEL" "$VOLUME" >/dev/null
        docker create --name "$NAME" --label "$LABEL" --restart no --memory 4g \
            --publish 127.0.0.1:1521:1521 --publish 127.0.0.1:2484:2484 \
            --env APP_USER=SYRD_BOARD_OWNER --env ORACLE_PASSWORD_FILE=/run/secrets/oracle_password \
            --env APP_USER_PASSWORD_FILE=/run/secrets/app_user_password \
            --mount "type=bind,src=$SECRETS,dst=/run/secrets,readonly" \
            --mount "type=volume,src=$VOLUME,dst=/opt/oracle/oradata" "$IMAGE" >/dev/null
    elif grep -qE '^(ORACLE_PASSWORD|APP_USER_PASSWORD)=' \
            <<<"$(docker container inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}')"; then
        die "container $NAME holds its passwords in its environment (created before SYRD-645): $0 teardown, then start"
    elif [ "$(docker container inspect "$NAME" --format '{{.State.Running}}')" != true ]; then
        # The image's network configuration, as it shipped. It lives in the data volume (the image links
        # $NETADMIN there), so `tcps`'s listener outlives the container, and the image's start-up, which stops
        # and restarts the listener, fails with a TCPS address in it. Copied from the image itself, as the
        # database's own account, with no network; `tcps` adds the listener back.
        docker run --rm --network none --user 54321:54321 --label "$LABEL" \
            --mount "type=volume,src=$VOLUME,dst=/opt/oracle/oradata" --entrypoint /bin/bash "$IMAGE" -c \
            "cp --remove-destination $NETADMIN/listener.ora $NETADMIN/sqlnet.ora /opt/oracle/oradata/dbconfig/FREE/"
    fi
    at=$(date -u +%Y-%m-%dT%H:%M:%SZ); docker start "$NAME" >/dev/null; started "$at"
    if [ -n "${created:-}" ]; then
        # The image exports the passwords it read into the processes of the start that created the database;
        # started again, with the files gone, no process carries them (SYRD-645).
        rm -f -- "$SECRETS/oracle_password" "$SECRETS/app_user_password"
        docker stop --time 120 "$NAME" >/dev/null  # time for the database's own clean shutdown
        at=$(date -u +%Y-%m-%dT%H:%M:%SZ); docker start "$NAME" >/dev/null; started "$at"
    fi
    status
    echo "ready: 127.0.0.1:1521/FREEPDB1"
    ;;
tcps)
    [ "$(docker container inspect "$NAME" --format '{{.State.Running}}' 2>/dev/null)" = true ] \
        || die "container $NAME is not running; run: $0 start"
    [ "$(docker container inspect "$NAME" --format '{{json (index .HostConfig.PortBindings "2484/tcp")}}')" \
        = '[{"HostIp":"127.0.0.1","HostPort":"2484"}]' ] \
        || die "container $NAME does not publish 127.0.0.1:2484 (it predates SYRD-645): $0 teardown, then start"
    if ! docker exec "$NAME" test -f "$WALLET/cwallet.sso"; then
        docker pull --quiet "$TOOL_IMAGE" >/dev/null
        docker run --rm --network none --user 54321:54321 --label "$LABEL" \
            --mount "type=volume,src=$VOLUME,dst=/opt/oracle/oradata" --entrypoint /bin/bash "$TOOL_IMAGE" -c "set -e
                umask 077; rm -rf $WALLET; mkdir -p $WALLET; cd $WALLET
                # Its own configuration: the image's default one adds a second basicConstraints to a CA, which
                # OpenSSL 3 clients reject as an invalid certificate.
                printf '%s\n' '[req]' 'distinguished_name = dn' 'prompt = no' '[dn]' 'CN = unused' '[ca]' \
                    'basicConstraints = critical,CA:TRUE' 'keyUsage = critical,keyCertSign,cRLSign' \
                    'subjectKeyIdentifier = hash' '[server]' 'basicConstraints = CA:FALSE' \
                    'keyUsage = critical,digitalSignature,keyEncipherment' 'extendedKeyUsage = serverAuth' \
                    'subjectAltName = IP:127.0.0.1,DNS:localhost' 'subjectKeyIdentifier = hash' \
                    'authorityKeyIdentifier = keyid' > certs.cnf
                openssl req -config certs.cnf -x509 -extensions ca -newkey rsa:2048 -nodes -days 365 -sha256 \
                    -subj /CN=syrd645-tcps-test-ca -keyout ca.key -out ca-cert.pem 2>/dev/null
                openssl req -config certs.cnf -newkey rsa:2048 -nodes -sha256 -subj /$SERVER_DN -keyout server.key \
                    -out server.csr 2>/dev/null
                openssl x509 -req -in server.csr -CA ca-cert.pem -CAkey ca.key -CAcreateserial -days 365 -sha256 \
                    -extfile certs.cnf -extensions server -out server-cert.pem 2>/dev/null
                rm -f ca.key ca-cert.srl server.csr certs.cnf
                openssl pkcs12 -export -in server-cert.pem -inkey server.key -certfile ca-cert.pem -passout pass: \
                    -out server.p12
                rm -f server.key
                orapki wallet create -wallet $WALLET -auto_login_only >/dev/null
                orapki wallet import_pkcs12 -wallet $WALLET -auto_login_only -pkcs12file server.p12 -pkcs12pwd '' >/dev/null
                rm -f server.p12"
    fi
    docker exec "$NAME" sh -c "set -e
cat > $NETADMIN/listener.ora <<EOF
LISTENER =
  (DESCRIPTION_LIST =
    (DESCRIPTION =
      (ADDRESS = (PROTOCOL = IPC)(KEY = EXTPROC_FOR_FREE))
      (ADDRESS = (PROTOCOL = TCP)(HOST=)(PORT = 1521))
      (ADDRESS = (PROTOCOL = TCPS)(HOST=)(PORT = 2484))
    )
  )

DEFAULT_SERVICE_LISTENER = FREE
WALLET_LOCATION = (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = $WALLET)))
SSL_CLIENT_AUTHENTICATION = FALSE
EOF
grep -q '^WALLET_LOCATION' $NETADMIN/sqlnet.ora || printf '%s\n' \
    'WALLET_LOCATION = (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = $WALLET)))' \
    'SSL_CLIENT_AUTHENTICATION = FALSE' >> $NETADMIN/sqlnet.ora
lsnrctl stop >/dev/null 2>&1 || true
lsnrctl start >/dev/null"
    # The database registers its services with the restarted listener on its own schedule; ask it now.
    docker exec "$NAME" sh -c "echo 'ALTER SYSTEM REGISTER;' | sqlplus -s / as sysdba" >/dev/null
    registered() {  # captured first: under pipefail, grep -q's early exit can fail the pipeline it matched in
        local said
        said=$(docker exec "$NAME" lsnrctl status 2>/dev/null) || return 1
        grep -qi 'service "freepdb1"' <<<"$said"
    }
    for _ in $(seq 1 90); do
        registered && break
        sleep 2
    done
    registered || die "the listener restarted without the FREEPDB1 service; see: docker exec $NAME lsnrctl status"
    install -d -m 0700 -o "$OWNER" -g "$OWNER" "$CLIENT_WALLET"
    docker exec "$NAME" cat "$WALLET/ca-cert.pem" > "$CLIENT_WALLET/.ewallet.pem.new"
    grep -q 'BEGIN CERTIFICATE' "$CLIENT_WALLET/.ewallet.pem.new" && ! grep -q 'PRIVATE KEY' "$CLIENT_WALLET/.ewallet.pem.new" \
        || die "the exported certificate is not a certificate alone"
    chown "$OWNER:$OWNER" "$CLIENT_WALLET/.ewallet.pem.new"
    chmod 0600 "$CLIENT_WALLET/.ewallet.pem.new"
    mv -f "$CLIENT_WALLET/.ewallet.pem.new" "$CLIENT_WALLET/ewallet.pem"
    echo "tcps ready: 127.0.0.1:2484/FREEPDB1, server certificate $SERVER_DN; client wallet $CLIENT_WALLET"
    ;;
status)
    status
    ;;
teardown)
    docker rm --force "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    rm -f "$ENVFILE"
    rm -rf -- "$SECRETS" "$CLIENT_WALLET"
    if [ -e "$MARKER" ]; then
        systemctl stop docker.service docker.socket
        rm -f "$MARKER"
    fi
    echo "removed container $NAME and volume $VOLUME"
    ;;
*)
    die "usage: $0 start|tcps|status|teardown"
    ;;
esac
