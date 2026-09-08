#!/usr/bin/env bash
# Run the front-end behaviour tests.
#
# check-js-syntax proves a module parses. It cannot prove the module is
# right, and the front end holds real logic -- what a timestamp means, how
# money is parsed, which bar is which. A timezone bug shipped from exactly
# that gap.
#
# TZ is pinned to a zone that is not UTC, deliberately. Reading a naive
# timestamp as local instead of UTC gives the identical answer in UTC, so a
# suite that runs there proves nothing about the bug it is meant to catch.
#
# Uses node when the host has one, otherwise a node container. Skips loudly
# rather than failing when neither exists: a check nobody can run must not
# block everybody.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

[ -d tests/js ] || { echo "no tests/js yet"; exit 0; }

TZ_FOR_TESTS="America/Chicago"

if command -v node >/dev/null 2>&1; then
  TZ="$TZ_FOR_TESTS" node tests/js/run.mjs
elif command -v docker >/dev/null 2>&1 \
     && docker run --rm node:22-alpine node --version >/dev/null 2>&1; then
  docker run --rm -e TZ="$TZ_FOR_TESTS" -v "$PWD:/w:ro" -w /w \
    node:22-alpine node tests/js/run.mjs
else
  echo "SKIPPED — no node and no usable container"
  exit 0
fi
