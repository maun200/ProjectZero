#!/usr/bin/env bash
# One-time coordinator-host setup for tunnel-only device access (run with sudo).
#
#   sudo scripts/grid_host_setup.sh [coordinator-port]      # default 8765
#   sudo scripts/grid_host_setup.sh --rollback
#
# Creates a system user `gridtunnel` without shell or home, a root-owned key file and an sshd
# drop-in that allows this user nothing but a local forward to 127.0.0.1:<port>. The global sshd
# configuration and all other users are left unchanged; the drop-in is validated with `sshd -t`
# and removed again if validation fails.
set -euo pipefail

USER_NAME=gridtunnel
KEY_DIR=/etc/ssh/community-grid
DROP_IN=/etc/ssh/sshd_config.d/60-community-grid.conf

[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }

if [[ "${1:-}" == "--rollback" ]]; then
    rm -f "$DROP_IN"
    rm -rf "$KEY_DIR"
    if id "$USER_NAME" >/dev/null 2>&1; then userdel "$USER_NAME"; fi
    sshd -t
    echo "Rolled back: drop-in, key directory and user $USER_NAME removed."
    exit 0
fi

PORT="${1:-8765}"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || { echo "Invalid port: $PORT" >&2; exit 1; }

if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$USER_NAME"
fi
install -d -m 0755 -o root -g root "$KEY_DIR"
[[ -e "$KEY_DIR/authorized_keys" ]] || install -m 0644 -o root -g root /dev/null "$KEY_DIR/authorized_keys"

cat > "$DROP_IN" <<EOF
# Community AI Grid: devices may only forward to the local coordinator. Rollback: grid_host_setup.sh --rollback
Match User $USER_NAME
    AuthorizedKeysFile $KEY_DIR/authorized_keys
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:$PORT
    PermitListen none
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    ForceCommand /usr/sbin/nologin
Match all
EOF
chmod 0644 "$DROP_IN"

if ! sshd -t; then
    rm -f "$DROP_IN"
    echo "sshd rejected the drop-in; it was removed again. Nothing else changed in sshd." >&2
    exit 1
fi
# Socket-activated sshd (Ubuntu default) reads the config per connection; a classic daemon needs a reload.
if systemctl is-active --quiet ssh.service; then systemctl reload ssh.service; fi
if systemctl is-active --quiet sshd.service; then systemctl reload sshd.service; fi

echo "Effective settings for $USER_NAME:"
sshd -T -C "user=$USER_NAME,host=device,addr=127.0.0.1" \
    | grep -E '^(authorizedkeysfile|authenticationmethods|passwordauthentication|allowtcpforwarding|permitopen|permittty|forcecommand|x11forwarding) '
echo "Done. Rollback: sudo $0 --rollback"
