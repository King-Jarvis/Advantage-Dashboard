#!/usr/bin/env bash
# Nothing may be reachable only by hovering.
#
# A control hidden with opacity:0 and revealed on :hover simply does not exist
# on a touch screen -- there is no hover to reveal it with. The bug is
# invisible on the machine it is written on and total on the device it is used
# on, which is the worst combination, and it is not the sort of thing a test
# suite notices.
#
# The fix is always the same shape: make it visible by default and hide it
# only inside @media (hover: hover), so the capability decides rather than the
# default.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
TARGET="app/src/dashboard/static"

python3 - "$TARGET" <<'PY'
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
bad = []

for path in sorted(root.glob("*.css")):
    text = path.read_text()

    # Which character ranges sit inside an @media block that mentions hover?
    safe = []
    for m in re.finditer(r"@media[^{]*hover\s*:\s*hover[^{]*\{", text):
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        safe.append((m.start(), i))

    def gated(pos):
        return any(a <= pos <= b for a, b in safe)

    # Selectors that are hidden outright.
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        sel, body = m.group(1).strip(), m.group(2)
        if not re.search(r"opacity\s*:\s*0\s*[;}]|visibility\s*:\s*hidden", body):
            continue
        if gated(m.start()):
            continue
        # Is the only thing that brings it back a :hover rule?
        base = sel.split()[-1].split(":")[0].strip()
        if not base or not base.startswith("."):
            continue
        revealed = re.search(
            re.escape(base) + r"[^{}]*\{[^{}]*opacity\s*:\s*1", text)
        hovered = re.search(
            r"[^{}]*:hover[^{}]*" + re.escape(base) + r"[^{}]*\{[^{}]*opacity\s*:\s*1|"
            + re.escape(base) + r":hover[^{}]*\{[^{}]*opacity\s*:\s*1", text)
        if revealed and hovered:
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{path}:{line}: {sel} is hidden and revealed only on hover")

if bad:
    print("BLOCKED — unreachable on a touch screen:")
    for b in bad:
        print("    " + b)
    print()
    print("  Make it visible by default and hide it inside")
    print("  @media (hover: hover) instead, so the capability decides.")
    raise SystemExit(1)

print("clean — nothing is hover-only")
PY
