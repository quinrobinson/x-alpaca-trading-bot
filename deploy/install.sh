#!/usr/bin/env bash
# install.sh — install or update x-alpaca-trading-bot on an Ubuntu host.
#
# Idempotent: safe to re-run. Does not touch any existing system services.
# Designed to coexist with other apps on a shared droplet.
#
# Usage (as root or via sudo):
#
#   sudo bash install.sh                       # full install
#   sudo bash install.sh --update              # pull latest + restart
#
# Tunable via env vars (defaults shown):
#   INSTALL_DIR=/opt/x-alpaca-trading-bot
#   SERVICE_USER=xalpaca
#   API_PORT=8000
#   REPO_URL=  (required on first install if INSTALL_DIR is empty)
#   REPO_REF=main
#   PYTHON=python3.12
#
# What it does:
#   1. Install OS deps (python3.12, venv, git) without removing anything.
#   2. Create the xalpaca system user + INSTALL_DIR.
#   3. Clone the repo (or pull if it already exists).
#   4. Build a Python venv and pip install -e .
#   5. Install the systemd unit and enable it.
#   6. Print the next-step checklist.
#
# What it does NOT do:
#   - Touch Postgres (Supabase is external).
#   - Touch nginx/Caddy. Bring your own reverse proxy if you have one.
#   - Write or overwrite .env. You provide it at INSTALL_DIR/.env after
#     this script finishes.

set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/x-alpaca-trading-bot}
SERVICE_USER=${SERVICE_USER:-xalpaca}
API_PORT=${API_PORT:-8000}
REPO_URL=${REPO_URL:-}
REPO_REF=${REPO_REF:-main}
PYTHON=${PYTHON:-python3.12}
SERVICE_NAME=x-alpaca-bot

UPDATE_ONLY=0
if [[ "${1:-}" == "--update" ]]; then
    UPDATE_ONLY=1
fi

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "error: must run as root (use sudo)" >&2
        exit 1
    fi
}

ensure_python() {
    if command -v "$PYTHON" >/dev/null 2>&1; then
        return
    fi
    echo "==> installing $PYTHON"
    . /etc/os-release
    case "$VERSION_ID" in
        24.04|24.10)
            apt-get update
            apt-get install -y "$PYTHON" "${PYTHON}-venv"
            ;;
        22.04)
            apt-get update
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa
            apt-get update
            apt-get install -y "$PYTHON" "${PYTHON}-venv"
            ;;
        *)
            echo "error: unsupported Ubuntu $VERSION_ID. Install $PYTHON manually." >&2
            exit 1
            ;;
    esac
}

ensure_user() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        echo "==> creating service user $SERVICE_USER"
        useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
}

clone_or_update() {
    if [[ ! -d "$INSTALL_DIR/.git" ]]; then
        if [[ -z "$REPO_URL" ]]; then
            echo "error: $INSTALL_DIR is empty and REPO_URL is unset." >&2
            echo "       set REPO_URL=https://github.com/youruser/x-alpaca-trading-bot.git and re-run." >&2
            exit 1
        fi
        echo "==> cloning $REPO_URL -> $INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    # Modern git (>= 2.35.2) refuses to operate in a dir owned by another
    # user. Mark our install dir as trusted before running fetch/checkout.
    # System-level config; idempotent.
    git config --system --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    cd "$INSTALL_DIR"
    echo "==> fetching ref $REPO_REF"
    git fetch origin "$REPO_REF"
    # `reset --hard` is the right semantic for a deploy: we want the droplet
    # to match origin exactly, not merge against whatever's local. This also
    # works correctly across force-pushes (which `pull --ff-only` doesn't).
    git checkout "$REPO_REF" 2>/dev/null || git checkout -B "$REPO_REF" "origin/$REPO_REF"
    git reset --hard "origin/$REPO_REF"
}

build_venv() {
    cd "$INSTALL_DIR"
    if [[ ! -d .venv ]]; then
        echo "==> creating venv"
        "$PYTHON" -m venv .venv
    fi
    echo "==> installing python deps"
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e .
}

install_systemd_unit() {
    local unit_src="$INSTALL_DIR/deploy/x-alpaca-bot.service"
    local unit_dst="/etc/systemd/system/${SERVICE_NAME}.service"

    echo "==> writing $unit_dst"
    # Substitute @SERVICE_USER@, @INSTALL_DIR@, @API_PORT@ into the unit file.
    sed -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        -e "s|@API_PORT@|$API_PORT|g" \
        "$unit_src" >"$unit_dst"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
}

fix_perms() {
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
}

restart_service() {
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "==> restarting $SERVICE_NAME"
        systemctl restart "$SERVICE_NAME"
    else
        echo "==> starting $SERVICE_NAME"
        systemctl start "$SERVICE_NAME" || true   # may need .env; non-fatal
    fi
}

next_steps() {
    cat <<EOF

------------------------------------------------------------
$SERVICE_NAME installed at $INSTALL_DIR

Next steps you must do manually:

  1. Create the env file:
       sudo -u $SERVICE_USER cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env
       sudo -u $SERVICE_USER nano $INSTALL_DIR/.env
     (fill in Alpaca paper keys, ANTHROPIC_API_KEY, POLYGON_API_KEY,
      X_BEARER_TOKEN, X_TARGET_ACCOUNT_ID, DATABASE_URL from Supabase,
      TELEGRAM creds if you want alerts)

  2. Start the service:
       sudo systemctl start $SERVICE_NAME

  3. Watch the logs to confirm it boots cleanly:
       sudo journalctl -u $SERVICE_NAME -f

  4. Sanity check the API:
       curl http://localhost:$API_PORT/healthz

  5. Update later:
       sudo bash $INSTALL_DIR/deploy/install.sh --update

If port $API_PORT conflicts with something else on this droplet, edit
the unit at /etc/systemd/system/${SERVICE_NAME}.service (change the
--port flag), daemon-reload, and restart.

------------------------------------------------------------
EOF
}

main() {
    require_root
    if [[ $UPDATE_ONLY -eq 1 ]]; then
        clone_or_update
        build_venv
        # Always re-stamp the systemd unit on --update so changes to the
        # template (TimeoutStopSec, ExecStart args, sandbox flags) actually
        # land. install_systemd_unit is idempotent; daemon-reload is cheap.
        install_systemd_unit
        fix_perms
        restart_service
        echo "update complete"
        return
    fi

    ensure_python
    ensure_user
    clone_or_update
    build_venv
    install_systemd_unit
    fix_perms
    next_steps
}

main "$@"
