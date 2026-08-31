#!/usr/bin/env bash
# Fail if front-end code assigns untrusted markup.
#
# This dashboard renders text the user does not control: email subjects and
# senders, transaction payees, and model-written rationales. Any of those can
# contain markup. The CSP forbids inline script, but defence in depth means the
# DOM never parses that text as HTML in the first place — use textContent.
#
# The check runs against app/src/dashboard/static only. It deliberately does not
# offer an ignore comment: an escape hatch here would be used, and the whole
# point is that there is no safe case in this codebase.

set -uo pipefail

TARGET='app/src/dashboard/static'
[ -d "$TARGET" ] || { echo "no $TARGET yet — nothing to check"; exit 0; }

if hits=$(grep -rn -E '\.(inner|outer)HTML|insertAdjacentHTML|document\.write' \
            --include='*.js' --include='*.html' "$TARGET" 2>/dev/null); then
  echo "BLOCKED — markup sink in front-end code:"
  echo "$hits" | sed 's/^/    /'
  cat <<'MSG'

  Use textContent, or build nodes with document.createElement.
  These render attacker-controlled strings (email subjects, payees, model
  output); assigning them as HTML is stored XSS.
MSG
  exit 1
fi

echo "clean — no markup sinks in $TARGET"
