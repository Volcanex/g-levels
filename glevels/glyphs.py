"""Sensor: house-banned glyphs in reader-facing text. Exit 1 and list them if any.
`--text` prints the reader-facing text instead, for other sensors such as ganti.

For HTML and SVG, style, script and tags are stripped and entities decoded first, so CSS
semicolons and &nbsp; never count. For JavaScript only the text of its strings and template
literals is read: statements, comments and the code inside ${} are not copy. A template's tags
are markup, and a literal that is itself code is skipped whole: shader source, or one whose every
semicolon ends a declaration (CSS, a cookie's attributes). Line numbers stay the file's own.
"""
import html
import re
import sys

BANNED = {"—": "em dash", "·": "middle dot", ";": "semicolon"}
JS = (".js", ".mjs", ".cjs")
# After one of these (or a keyword below) a slash starts a regular expression, not a division.
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "void", "yield", "await", "delete", "new", "throw"}
# A tag, or what is left of <${Part} name=...> once its ${} is blanked.
TAG = re.compile(r"<(?:[A-Za-z/!]|\s+[A-Za-z-]+=)[^<>]*>")
SHADER = re.compile(r"\bgl_[A-Za-z]+\b|\bvoid\s+main\s*\(|#(?:version|ifdef|ifndef|endif|define|include|pragma)\b"
                    r"|\bprecision\s+(?:high|medium|low)p\b|\buniform\s+\w|\b(?:vec[234]|mat[234]|sampler2D)\b")
# A semicolon that ends a declaration: what follows is the end, a brace, the next name and its : or =, or a cookie flag.
DECLARED = re.compile(r";(?!\s*(?:$|\}|!?[-\w]+\s*[:=]|(?:Secure|HttpOnly|Partitioned)\b))")


# A statement: a name, an assignment and a semicolon ("material.diffuseColor = diffuseColor.rgb;").
STATEMENT = re.compile(r"[\w.\]]+\s*[-+*/]?=\s*[^;=<>]+;")


def code_literal(text):
    return bool(SHADER.search(text) or STATEMENT.search(text)) or (";" in text and not DECLARED.search(text))


def _blank(text):
    return re.sub(r"[^\n]", " ", text)


def js_text(src):
    """The source with everything but string and template text blanked, newlines kept."""
    out = [c if c == "\n" else " " for c in src]
    spans = []      # (start, end, literal) for each run of literal text, a template split at its ${}
    i, n = 0, len(src)
    depth = []      # [brace depth, literal] for each open ${...}
    count = [0]
    prev = ""       # last significant code token, for telling a regex from a division

    def new():
        count[0] += 1
        return count[0]

    def string(i, q):
        start = i
        while i < n and src[i] != q and src[i] != "\n":
            i += 2 if src[i] == "\\" else 1
        spans.append((start, min(i, n), new()))
        return i + 1

    def template(i, lit):
        start = i
        while i < n:
            c = src[i]
            if c == "\\":
                i += 2
            elif c == "`":
                spans.append((start, i, lit))
                return i + 1
            elif c == "$" and src.startswith("${", i):
                spans.append((start, i, lit))
                depth.append([0, lit])
                return i + 2
            else:
                i += 1
        spans.append((start, n, lit))
        return n

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
        elif src.startswith("/*", i):
            i = src.find("*/", i + 2)
            i = n if i < 0 else i + 2
        elif c in "'\"":
            i = string(i + 1, c)
            prev = "str"
        elif c == "`":
            i = template(i + 1, new())
            prev = "str"
        elif c == "}" and depth and depth[-1][0] == 0:
            i = template(i + 1, depth.pop()[1])
            prev = "str"
        elif c == "/" and (prev == "" or prev in REGEX_AFTER or prev in REGEX_KEYWORDS):
            i += 1
            klass = False
            while i < n and src[i] != "\n":
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "[":
                    klass = True
                elif src[i] == "]":
                    klass = False
                elif src[i] == "/" and not klass:
                    break
                i += 1
            i += 1
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            prev = "regex"
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev = src[i:j]
            i = j
        else:
            if depth and c == "{":
                depth[-1][0] += 1
            elif depth and c == "}":
                depth[-1][0] -= 1
            prev = c
            i += 1
    whole = {}
    for a, b, lit in spans:
        whole[lit] = whole.get(lit, "") + ("0" if lit in whole else "") + src[a:b]
    for a, b, lit in spans:
        if not code_literal(whole[lit]):
            out[a:b] = src[a:b]
    text = "".join(out)
    text = re.sub(r"\\u([0-9a-fA-F]{4})|\\u\{([0-9a-fA-F]+)\}", lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
    text = TAG.sub(lambda m: _blank(m.group()), text)
    return "\n".join(html.unescape(line) for line in text.split("\n"))


def text_of(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    if path.endswith((".html", ".htm", ".svg")):
        s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
        s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    elif path.endswith(JS):
        s = js_text(s)
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
