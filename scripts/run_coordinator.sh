#!/usr/bin/env bash
# Run the coordinator on loopback with persistent local secrets and database (both outside the repo).
#
#   scripts/run_coordinator.sh        # foreground; Ctrl+C stops
#
# The first run creates ~/.config/community-grid/coordinator.env (mode 0600) with random keys and
# CAG_ALLOWED_MODELS=qwen2.5-coder:1.5b; edit that file to approve other models. grid_device.py reads
# the admin key from the same file. The database lives in ~/.local/share/community-grid/.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
CONFIG=${CAG_CONFIG_FILE:-$HOME/.config/community-grid/coordinator.env}
DATA=${XDG_DATA_HOME:-$HOME/.local/share}/community-grid

if [[ ! -e "$CONFIG" ]]; then
    mkdir -p -m 700 "$(dirname "$CONFIG")"
    key() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))'; }
    (umask 077; cat > "$CONFIG" <<EOF
# Community AI Grid coordinator secrets: local only, never commit or share.
CAG_ADMIN_KEY=$(key)
CAG_USER_KEY=$(key)
CAG_DB_PATH=$DATA/grid.sqlite3
CAG_ALLOWED_MODELS=qwen2.5-coder:1.5b
CAG_PORT=8765
EOF
    )
    echo "Created $CONFIG (mode 0600)" >&2
fi
[[ $(stat -c %a "$CONFIG") == 600 ]] || { echo "$CONFIG must have mode 600" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$CONFIG"
set +a
mkdir -p -m 700 "$(dirname "$CAG_DB_PATH")"
cd "$ROOT"
echo "Coordinator on http://127.0.0.1:${CAG_PORT:-8765} (loopback only), database $CAG_DB_PATH" >&2
exec "$PY" -m uvicorn coordinator.app.main:app --host 127.0.0.1 --port "${CAG_PORT:-8765}" --no-access-log
