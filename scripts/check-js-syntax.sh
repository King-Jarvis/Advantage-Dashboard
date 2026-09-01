#!/usr/bin/env bash
# Parse every front-end module.
#
# The front end has no build step, which is deliberate -- but it means a
# syntax error is not found until a browser silently renders nothing. A
# duplicate `const` at the top of a module takes the whole page down and
# leaves no trace anywhere the server can see.
#
# Uses node when the host has one, otherwise a node container, so the check
# still runs on a machine that only has Docker. Skips loudly rather than
# failing when neither exists: a check nobody can run must not block
# everybody.
#
# Files are parsed as .mjs. `node --check` treats a .js file as a script,
# where `import` and `export` are syntax errors, so checking them as-is would
# report a failure for every module in the directory.

set -uo pipefail

TARGET="app/src/dashboard/static"
[ -d "$TARGET" ] || { echo "no $TARGET yet"; exit 0; }
count=$(find "$TARGET" -name '*.js' | wc -l)
[ "$count" -gt 0 ] || { echo "no modules to check"; exit 0; }

SCRIPT='
set -e
st=0
work=$(mktemp -d)
for f in "$1"/*.js; do
  base=$(basename "$f" .js)
  cp "$f" "$work/$base.mjs"
  if ! out=$(node --check "$work/$base.mjs" 2>&1); then
    echo "BLOCKED -- $f"
    echo "$out" | head -6 | sed "s/^/    /"
    st=1
  fi
done
exit $st
'

if command -v node >/dev/null 2>&1; then
  sh -c "$SCRIPT" _ "$TARGET" || exit 1
elif command -v docker >/dev/null 2>&1 \
     && docker run --rm node:22-alpine node --version >/dev/null 2>&1; then
  docker run --rm -v "$PWD/$TARGET:/js:ro" node:22-alpine \
    sh -c "$SCRIPT" _ /js || exit 1
else
  echo "SKIPPED — no node and no usable container"
  exit 0
fi

echo "clean — $count modules parse"
