"""Sensor: house-banned glyphs in reader-facing text. Exit 1 and list them if any.
`--text` prints the reader-facing text instead, for other sensors such as ganti.

For HTML, style, script and tags are stripped and entities decoded first, so CSS
semicolons and &nbsp; never count.
"""
import html
import re
import sys

BANNED = {"—": "em dash", "·": "middle dot", ";": "semicolon"}


def text_of(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    if path.endswith((".html", ".htm")):
        s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
        s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return s


def main():
    if sys.argv[1:2] == ["--text"]:
        sys.stdout.write("\n".join(text_of(p) for p in sys.argv[2:]))
        return
    hits = 0
    for path in sys.argv[1:]:
        for n, line in enumerate(text_of(path).splitlines(), 1):
            for ch, name in BANNED.items():
                if ch in line:
                    hits += 1
                    print(f"{path}:{n}: {name}: {line.strip()[:120]}")
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
