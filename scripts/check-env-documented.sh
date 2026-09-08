#!/usr/bin/env bash
# Every environment variable named in .env.example must exist in the code.
#
# .env.example drifted badly once: it promised eleven variables — SESSION_KEY_PATH,
# DATA_ROOT, CERTS_DIR and others — that nothing had read for a long time, left
# behind when the deployment stopped being a set of containers. Someone
# following it would configure a service that ignored them silently, which is
# the worst way for configuration to be wrong.
#
# This does not check the reverse. An undocumented variable is untidy; a
# documented one that does nothing is a trap.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
for v in $(grep -oE "^#? ?[A-Z][A-Z0-9_]+=" .env.example | tr -d '#= ' | sort -u); do
  if ! grep -rq "\"$v\"" app/src/dashboard/*.py scripts/*.sh install.sh 2>/dev/null; then
    echo "  DEAD  $v — named in .env.example, read by nothing"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "BLOCKED — .env.example documents variables the code does not read."
  exit 1
fi
echo "clean — every variable in .env.example is read somewhere"
