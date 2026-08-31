#!/usr/bin/env bash
# Generate a local CA and a server certificate for LAN access over HTTPS.
#
# Why this is needed rather than optional: the dashboard uses Web Crypto,
# which browsers expose only in a "secure context" -- HTTPS, or localhost.
# Served over plain HTTP at a LAN address, crypto.subtle is simply undefined
# and the app fails with an opaque error. HTTPS is a functional requirement
# here, not only a security one.
#
# Two files matter:
#   ca.crt      install once per device -> no browser warnings anywhere
#   server.crt  presented by the service; signed by the CA above
#
# Without installing the CA you still get a working secure context after
# clicking through the warning once per device. The app works either way.
#
# Usage:
#   ./scripts/make-local-cert.sh --dir /path/to/certs --host 192.0.2.10 \
#       [--host myhost.lan] [--host another.name]

set -euo pipefail

DIR=""; HOSTS=(); DAYS_CA=3650; DAYS_LEAF=825

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)  DIR="$2"; shift 2 ;;
    --host) HOSTS+=("$2"); shift 2 ;;
    -h|--help) sed -n '1,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -n "$DIR" ] || { echo "--dir is required" >&2; exit 2; }
[ "${#HOSTS[@]}" -gt 0 ] || { echo "at least one --host is required" >&2; exit 2; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

mkdir -p "$DIR"; chmod 700 "$DIR"

# Build subjectAltName. An IP address must appear as IP:, not DNS: --
# browsers will not match an IP against a DNS entry, and the failure looks
# like an unrelated certificate error.
SAN=""
for h in "${HOSTS[@]}"; do
  if printf '%s' "$h" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    SAN="${SAN}IP:${h},"
  else
    SAN="${SAN}DNS:${h},"
  fi
done
SAN="${SAN}DNS:localhost,IP:127.0.0.1"

if [ ! -s "$DIR/ca.key" ]; then
  ( umask 077; openssl genrsa -out "$DIR/ca.key" 4096 2>/dev/null )
  openssl req -x509 -new -nodes -key "$DIR/ca.key" -sha256 -days "$DAYS_CA" \
    -out "$DIR/ca.crt" -subj "/CN=Personal Dashboard Local CA/O=Personal Dashboard" 2>/dev/null
  echo "  created  ca.key, ca.crt   (valid ${DAYS_CA} days)"
else
  echo "  keep     ca.key, ca.crt   (already present)"
fi

( umask 077; openssl genrsa -out "$DIR/server.key" 2048 2>/dev/null )
openssl req -new -key "$DIR/server.key" -out "$DIR/server.csr" \
  -subj "/CN=${HOSTS[0]}/O=Personal Dashboard" 2>/dev/null

cat > "$DIR/server.ext" <<EXT
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SAN}
EXT

# 825 days is the ceiling Safari accepts for a leaf certificate. Longer and
# iOS and macOS reject it outright while other browsers accept it, which is a
# confusing failure to debug.
openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
  -CAcreateserial -out "$DIR/server.crt" -days "$DAYS_LEAF" -sha256 \
  -extfile "$DIR/server.ext" 2>/dev/null
rm -f "$DIR/server.csr" "$DIR/server.ext"

chmod 600 "$DIR"/*.key
chmod 644 "$DIR"/*.crt

echo "  created  server.key, server.crt   (valid ${DAYS_LEAF} days)"
echo
echo "  SAN: ${SAN}"
echo
openssl x509 -in "$DIR/server.crt" -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/  /'
