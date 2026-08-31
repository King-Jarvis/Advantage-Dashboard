#!/usr/bin/env bash
# Fail if any tracked file contains host-specific detail.
#
# This repo is public. LAN addresses, internal hostnames, real account
# addresses and the passwords of neighbouring services must never be committed
# — they belong in .env and deploy/compose.override.yml, both untracked.
#
# Patterns below are written with escapes and character classes so that this
# script does not match itself. The script is also excluded from the scan, as
# belt and braces. Both matter: a guard that flags its own pattern list gets
# disabled by the first person it annoys, and then it protects nothing.

set -uo pipefail

PATTERNS=(
  '10\.191\.[0-9]{1,3}\.[0-9]{1,3}'      # the LAN this was developed on
  '192\.168\.[0-9]{1,3}\.[0-9]{1,3}'     # any private LAN address
  '[a-z0-9-]+\.home\.lan'                # internal hostnames
  'evahol[e]'                            # a neighbouring service's password
  '[A-Za-z0-9._%+-]+@gmail\.com'         # real account addresses
  'ya2[9]\.[A-Za-z0-9_-]+'               # Google OAuth access token
  '1//[A-Za-z0-9_-]{20,}'                # Google OAuth refresh token
  'sk-an[t]-[A-Za-z0-9-]{10,}'           # Anthropic key
)

SELF='scripts/check-no-infra-strings.sh'
status=0

# Only tracked files: an untracked local .env is expected and fine.
mapfile -t FILES < <(git ls-files | grep -v -x -F "$SELF" || true)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "no tracked files to scan"; exit 0
fi

for pat in "${PATTERNS[@]}"; do
  if hits=$(grep -n -E -H "$pat" -- "${FILES[@]}" 2>/dev/null); then
    echo "BLOCKED — host-specific string matching /$pat/:"
    echo "$hits" | sed 's/^/    /'
    status=1
  fi
done

if [ "$status" -ne 0 ]; then
  cat <<'MSG'

  These belong in .env (untracked) with a placeholder in .env.example.
  If a value is already committed, rotate it — a public repo's history is
  permanent, and deleting the line does not unpublish the secret.
MSG
else
  echo "clean — no host-specific strings in ${#FILES[@]} tracked files"
fi
exit "$status"
