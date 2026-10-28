"""Sensor: an integer id cast from an interpolated vertex attribute in shader node code.

A whole number written on every vertex of a triangle reaches the fragment stage
interpolated, and can land a hair under itself (2.9999). `int()` truncates it to
the id below, so scattered pixels take the wrong layer, body or material. Round
before the cast. A cast that only feeds the vertex stage (positionNode) sees the
attribute exact: mark it `idcast: vertex` on its line or the line above.

Exit 1 and list each unrounded cast. Files that never call attribute() pass.
"""
import re
import sys

CAST = re.compile(r"\b(u?int)\(\s*")
ASSIGN = re.compile(r"\b(\w+)\s*=\s*attribute\(")
MARK = "idcast: vertex"


def call_end(s, i):
    """Index just past the parenthesised call whose '(' is at s[i]."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(s)


def rounded(chain):
    return re.search(r"\.\s*(round|floor|ceil)\s*\(", chain) is not None


def unrounded_names(s):
    """Variables assigned straight from attribute(...) with no rounding on the chain."""
    names = set()
    for m in ASSIGN.finditer(s):
        end = call_end(s, m.end() - 1)
        chain = re.match(r"(\s*\.\s*\w+\s*(\([^()]*\))?)*", s[end:]).group(0)
        if not rounded(chain):
            names.add(m.group(1))
    return names


def inspect(text):
    if "attribute(" not in text:
        return []
    lines, names, hits = text.splitlines(), unrounded_names(text), []
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    for m in CAST.finditer(text):
        arg = text[m.end():call_end(text, m.end() - 1) - 1]
        head = re.match(r"\w+", arg)
        direct = arg.lstrip().startswith("attribute(") and not rounded(arg)
        named = head is not None and head.group(0) in names and not rounded(arg[head.end():])
        if not (direct or named):
            continue
        n = next(i for i, start in enumerate(starts) if start > m.start())
        here, above = lines[n - 1], lines[n - 2] if n > 1 else ""
        if here.lstrip().startswith(("//", "*")):
            continue
        if MARK in here or MARK in above:
            continue
        hits.append((n, here.strip()[:140]))
    return hits


def main():
    found = 0
    for path in sys.argv[1:]:
        text = open(path, encoding="utf-8", errors="replace").read()
        for n, line in inspect(text):
            found += 1
            print(f"{path}:{n}: integer cast of an unrounded attribute: {line}")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
