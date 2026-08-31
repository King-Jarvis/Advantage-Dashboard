#!/usr/bin/env bash
# Generate the secret files the stack needs.
#
# Secrets are files, not environment variables. An env var is visible to anyone
# who can run `docker inspect` or read /proc/<pid>/environ; a file mounted at
# /run/secrets is not. The app reads the *_PATH form of each.
#
# Idempotent: an existing secret is never overwritten, so re-running after
# adding a new service generates only what is missing. Rotating means deleting
# the file and running again -- deliberately explicit, because rotation has
# consequences elsewhere (a rotated ingest key must be updated in n8n).
#
# Usage:
#   ./scripts/bootstrap.sh                      # into ./deploy/secrets
#   ./scripts/bootstrap.sh --dir /path/to/dir   # into somewhere else
#   ./scripts/bootstrap.sh --owner 10001        # chown to the container uid

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/secrets"
OWNER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)   DIR="$2"; shift 2 ;;
    --owner) OWNER="$2"; shift 2 ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

mkdir -p "$DIR"
chmod 700 "$DIR"

created=0
kept=0

# gen <name> <description> [generator]
gen() {
  local name="$1" desc="$2" gen_cmd="${3:-openssl rand -base64 36}"
  local path="$DIR/$name"
  if [ -s "$path" ]; then
    printf '  keep    %-22s %s\n' "$name" "(already present)"
    kept=$((kept + 1))
    return
  fi
  # Write with restrictive permissions from the start rather than chmod after,
  # so the value is never briefly world-readable.
  ( umask 077; $gen_cmd | tr -d '\n' > "$path" )
  chmod 600 "$path"
  printf '  create  %-22s %s\n' "$name" "$desc"
  created=$((created + 1))
}

# placeholder <name> <description>  -- you supply the value
placeholder() {
  local name="$1" desc="$2" path="$DIR/$1"
  if [ -s "$path" ]; then
    printf '  keep    %-22s %s\n' "$name" "(already present)"
    kept=$((kept + 1))
    return
  fi
  ( umask 077; : > "$path" )
  chmod 600 "$path"
  printf '  EMPTY   %-22s %s\n' "$name" "$desc"
}

echo "secrets -> $DIR"
echo

gen ingest_key      "n8n -> app authentication"
gen session_key     "signs session cookies"
gen token_key       "encrypts OAuth tokens at rest" "openssl rand -base64 32"

placeholder google_client_secret "paste from Google Cloud -> Credentials"
placeholder anthropic_api_key    "paste from console.anthropic.com"

if [ -n "$OWNER" ]; then
  chown -R "$OWNER" "$DIR" 2>/dev/null \
    && echo && echo "  owner set to $OWNER" \
    || echo && echo "  ! could not chown to $OWNER (try sudo)"
fi

echo
echo "$created created, $kept kept"
echo
cat <<NEXT

Next:

  1. Fill the two EMPTY files above. Until google_client_secret has a value,
     Google sign-in and account connection will not work.

  2. Never commit this directory. It is gitignored, and CI rejects any file
     under deploy/secrets other than .gitkeep.
NEXT
