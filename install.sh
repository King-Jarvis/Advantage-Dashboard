#!/usr/bin/env bash
# Install the dashboard and start it.
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash
#
# Or, having already cloned:  ./install.sh
#
# Idempotent. Running it again updates the code and restarts; it never
# overwrites your database, your keys, or your certificate.
#
# What it does, in order: check Python, fetch the code, build a virtualenv,
# generate the encryption key and a self-signed certificate, install a user
# service, start it, and print the setup code you type into the browser.
#
# It asks for no passwords and needs no root. The service is a *user* service,
# so it runs as you and touches nothing system-wide.

set -euo pipefail

REPO="${DASHBOARD_REPO:-https://github.com/OWNER/REPO.git}"
BRANCH="${DASHBOARD_BRANCH:-main}"
APP_DIR="${DASHBOARD_HOME:-$HOME/.local/share/personal-dashboard}"
DATA_DIR="${DASHBOARD_DATA:-$HOME/.dashboard}"
PORT="${DASHBOARD_PORT:-8766}"
# Bind to every interface by default: the point of this thing is to open it on
# your phone. It is served over TLS, so that is a defensible default rather
# than a careless one.
BIND="${DASHBOARD_BIND:-0.0.0.0}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n\n' "$*" >&2; exit 1; }

# ── prerequisites ────────────────────────────────────────────────────────
say "Checking prerequisites"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11 or newer is required.
  Debian/Ubuntu/Raspberry Pi OS:  sudo apt install python3 python3-venv
  macOS:                          brew install python@3.13"
info "python: $($PY --version) at $(command -v "$PY")"

"$PY" -c 'import venv' 2>/dev/null || die "the venv module is missing.
  Debian/Ubuntu/Raspberry Pi OS:  sudo apt install python3-venv"

command -v openssl >/dev/null 2>&1 || die "openssl is required to make a certificate."

# ── the code ─────────────────────────────────────────────────────────────
say "Fetching the code"
if [ -d "$APP_DIR/.git" ]; then
  info "updating $APP_DIR"
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  # Reset rather than pull: a merge conflict in an install script is a dead
  # end for anyone who did not expect to be doing git today.
  git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
elif [ -f "$(dirname "$0")/app/src/dashboard/__main__.py" ]; then
  APP_DIR="$(cd "$(dirname "$0")" && pwd)"
  info "using this checkout: $APP_DIR"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the code."
  info "cloning into $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --quiet --branch "$BRANCH" --depth 1 "$REPO" "$APP_DIR"
fi

# ── virtualenv ───────────────────────────────────────────────────────────
say "Building the environment"
VENV="$APP_DIR/.venv"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
# One dependency, pinned. Everything else is the standard library.
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
info "one dependency installed: $(grep -o '^[a-z-]*' "$APP_DIR/requirements.txt" | head -1)"

# ── data, keys, certificate ──────────────────────────────────────────────
say "Preparing $DATA_DIR"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

KEY_FILE="$DATA_DIR/token.key"
if [ ! -f "$KEY_FILE" ]; then
  ( umask 077; "$VENV/bin/python" -c \
      "import secrets;print(secrets.token_urlsafe(48))" > "$KEY_FILE" )
  info "encryption key created"
else
  info "encryption key already present, left alone"
fi

CERT="$DATA_DIR/cert.pem"; CKEY="$DATA_DIR/key.pem"
if [ ! -f "$CERT" ]; then
  # Browsers disable Web Crypto outside a secure context, so this does not
  # merely protect the traffic -- without it the page will not run at all.
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$CKEY" -out "$CERT" -subj "/CN=$(hostname)" \
    -addext "subjectAltName=DNS:$(hostname),DNS:localhost,IP:127.0.0.1" \
    >/dev/null 2>&1
  chmod 600 "$CKEY"
  info "self-signed certificate created (your browser will warn once)"
else
  info "certificate already present, left alone"
fi

# ── service ──────────────────────────────────────────────────────────────
START="$VENV/bin/python -m dashboard --host $BIND --port $PORT \
--data $DATA_DIR --cert $CERT --key $CKEY"

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  say "Installing the service"
  UNIT="$HOME/.config/systemd/user"
  mkdir -p "$UNIT"
  cat > "$UNIT/personal-dashboard.service" <<UNITEOF
[Unit]
Description=Personal dashboard
After=network-online.target

[Service]
Environment=PYTHONPATH=$APP_DIR/app/src
Environment=TOKEN_KEY_PATH=$KEY_FILE
Environment=DASHBOARD_DATA=$DATA_DIR
ExecStart=$START
Restart=on-failure
RestartSec=3
# It only ever needs its own data directory.
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=default.target
UNITEOF
  systemctl --user daemon-reload
  systemctl --user enable --quiet personal-dashboard.service
  systemctl --user restart personal-dashboard.service
  # Survive logout, so a reboot brings the dashboard back without you.
  loginctl enable-linger "$USER" >/dev/null 2>&1 || \
    info "note: could not enable lingering; it will start when you log in"
  sleep 2
  systemctl --user is-active --quiet personal-dashboard.service \
    || die "the service did not start. See: journalctl --user -u personal-dashboard -n 40"
  info "running, and will start again on boot"
  SERVICE=yes
else
  info "no systemd here -- start it yourself with the command printed below"
  SERVICE=no
fi

# ── the setup code ───────────────────────────────────────────────────────
# Ask the application, rather than watching for the token file. A file that
# has not appeared yet looks exactly like one that never will.
TOKEN="$(PYTHONPATH="$APP_DIR/app/src" TOKEN_KEY_PATH="$KEY_FILE" \
  "$VENV/bin/python" -m dashboard --data "$DATA_DIR" --setup-code 2>/dev/null || true)"

HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$HOSTIP" ] || HOSTIP="localhost"

say "Done"
if [ -n "$TOKEN" ]; then
  printf '  Open   \033[1mhttps://%s:%s/\033[0m\n' "$HOSTIP" "$PORT"
  printf '  Code   \033[1m%s\033[0m\n\n' "$TOKEN"
  info "The code creates your account, then stops working."
  info "Your browser will warn about the certificate: it is self-signed."
  info "Accept it once."
else
  printf '  Open   \033[1mhttps://%s:%s/\033[0m\n\n' "$HOSTIP" "$PORT"
  info "This install already has an account -- sign in."
fi
echo
info "Everything else -- Google, n8n, API keys -- is in Settings."
if [ "$SERVICE" = no ]; then
  echo; info "Start it with:"; echo "    PYTHONPATH=$APP_DIR/app/src TOKEN_KEY_PATH=$KEY_FILE $START"
fi
echo
