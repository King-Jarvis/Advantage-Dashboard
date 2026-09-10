#!/usr/bin/env bash
# No const or let may be used above the line that declares it.
#
# `const x` is not hoisted the way `var` is: touching it earlier throws
# "Cannot access 'x' before initialization" at runtime. The file parses, so
# check-js-syntax passes, and the failure only appears when that code path
# runs -- which for a panel built inside a .then() means a caught rejection
# and a screen that just says it could not load.
#
# This has shipped twice here. Once as a toggle read before it was built,
# once as a dropdown referenced in the block above its own declaration. Both
# times the symptom was a feature that silently did nothing.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 scripts/_declared_first.py app/src/dashboard/static
