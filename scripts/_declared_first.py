"""Find a const/let used above its own declaration, inside one block.

Deliberately conservative. Strings and comments are blanked first so a word
inside a message is not mistaken for a reference, property keys are skipped,
and only the block that actually encloses the declaration is searched -- a
name used in a sibling block is a different binding and not this bug.
"""
import re
import sys
from pathlib import Path

DECL = re.compile(r"^\s*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=")


def blank_noise(text):
    """Replace string and comment contents with spaces, keeping offsets."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            quote, j = c, i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out.append(" " * (min(j, n - 1) - i + 1))
            i = min(j, n - 1) + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def depths(lines):
    """Block depth at the start of each line.

    Braces only. Parentheses and brackets continue an expression, they do not
    open a scope -- counting them made every continuation line of a multi-line
    call look like a nested block, and two sibling `if` blocks each declaring
    the same name looked like one using the other's.
    """
    out, d = [], 0
    for ln in lines:
        out.append(d)
        d += ln.count("{") - ln.count("}")
    return out


def check(path):
    raw = path.read_text()
    clean = blank_noise(raw).splitlines()
    lines = raw.splitlines()
    depth = depths(clean)
    problems = []

    for i, line in enumerate(clean):
        m = DECL.match(line)
        if not m:
            continue
        name, here = m.group(1), depth[i]
        # Walk back to where this block opened.
        start = 0
        for j in range(i - 1, -1, -1):
            if depth[j] < here:
                start = j + 1
                break
        word = re.compile(r"(?<![\w$.])%s(?![\w$])" % re.escape(name))
        for j in range(start, i):
            # Exactly this block. Shallower is outside it; deeper is a nested
            # block, which is a different scope and a different binding.
            if depth[j] != here:
                continue
            if word.search(clean[j]) and not re.search(
                    r"%s\s*:" % re.escape(name), clean[j]):
                problems.append((j + 1, i + 1, name, lines[j].strip()))
                break
    return problems


def main(target):
    bad = 0
    for path in sorted(Path(target).glob("*.js")):
        for used, declared, name, line in check(path):
            bad += 1
            print("BLOCKED -- %s:%d uses %r, declared on line %d"
                  % (path, used, name, declared))
            print("    %s" % line)
    if bad:
        print("\nconst and let are not hoisted: reading one above its own")
        print("declaration throws at runtime, and the file still parses.")
        return 1
    print("clean - nothing is used above its own declaration")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
