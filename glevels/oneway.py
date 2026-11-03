"""Sensor: dependencies point one way. Studio never references G-Command code, and g-design
references neither Studio nor G-Command.

    oneway.py REPO [--as NAME]

The repo's rule comes from its name (a worktree or clone takes its main repo's name), or --as:
g-studio and g-studio-apple may not name g-command*/GCommand*/gcommand, and g-design may not
name those or g-studio*/GStudio*/gstudio. Other repos have no rule and pass.

It reads only the places code is pulled in, in tracked files: Package.swift package and
product references, project.yml packages and their path, package, url, product and framework
values, Swift import lines, package.json dependency names and specs, JS and TS import, export
from, import() and require() specifiers, and Python import lines. A name in prose, a comment,
demo data or a domain in config is not an import.

Exit 0 when nothing is referenced, 1 listing file:line for each reference, 2 when it cannot run.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import registry
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from glevels import registry

COMMAND = re.compile(r"(?<![a-z0-9])g-?command", re.I)
STUDIO_OR_COMMAND = re.compile(r"(?<![a-z0-9])g-?(studio|command)", re.I)
RULES = {
    "g-studio": ("G-Command", COMMAND),
    "g-studio-apple": ("G-Command", COMMAND),
    "g-design": ("Studio or G-Command", STUDIO_OR_COMMAND),
}
SKIP_DIRS = {".git", "node_modules", ".build", "DerivedData", ".venv", "__pycache__"}
JS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"}
DEP_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
SWIFT_PACKAGE_LINE = re.compile(r"\.package\s*\(|\.product\s*\(|\b(package|path|url)\s*:")
SWIFT_IMPORT = re.compile(r"^\s*(?:@[\w]+(?:\([^)]*\))?\s+)*import\s+"
                          r"(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?([A-Za-z_][\w.]*)", re.M)
YML_VALUE = re.compile(r"^\s*(?:-\s*)?(path|package|url|product|framework)\s*:\s*(.+?)\s*$")
JS_SPECIFIERS = (
    re.compile(r"\bimport\s+(?:[^'\"`;()]*?\bfrom\s*)?(['\"])([^'\"\n]+)\1"),
    re.compile(r"\bexport\s+(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s*from\s*(['\"])([^'\"\n]+)\1"),
    re.compile(r"\bimport\s*\(\s*(['\"`])([^'\"`\n]+)\1\s*\)"),
    re.compile(r"\brequire\s*\(\s*(['\"`])([^'\"`\n]+)\1\s*\)"),
)
PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)", re.M)
PY_FROM = re.compile(r"^\s*from\s+([\w.]+)\s+import\b", re.M)


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def unquote(value):
    value = re.sub(r"\s+#.*$", "", value).strip()
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value


def package_swift(text):
    for n, line in enumerate(text.splitlines(), 1):
        if SWIFT_PACKAGE_LINE.search(line):
            for m in STRING.finditer(line):
                yield n, "package reference", m.group(1)


def swift_imports(text):
    for m in SWIFT_IMPORT.finditer(text):
        yield line_of(text, m.start(1)), "Swift import", m.group(1)


def project_yml(text):
    in_packages = False
    for n, line in enumerate(text.splitlines(), 1):
        if line and not line[0].isspace() and not line.startswith("#"):
            in_packages = line.split(":", 1)[0].strip() == "packages"
            continue
        m = re.match(r"^  ([^\s#][^:]*):", line)
        if in_packages and m:
            yield n, "package", unquote(m.group(1))
        m = YML_VALUE.match(line)
        if m:
            yield n, m.group(1), unquote(m.group(2))


def package_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return
    lines = text.splitlines()
    for section in DEP_SECTIONS:
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            n = next((i for i, line in enumerate(lines, 1) if f'"{name}"' in line), 1)
            yield n, f"{section} entry", f"{name}: {spec}"


def js_imports(text):
    for pattern in JS_SPECIFIERS:
        for m in pattern.finditer(text):
            yield line_of(text, m.start(2)), "import", m.group(2)


def py_imports(text):
    for m in PY_IMPORT.finditer(text):
        for part in m.group(1).split(","):
            yield line_of(text, m.start(1)), "import", part.split()[0]
    for m in PY_FROM.finditer(text):
        yield line_of(text, m.start(1)), "import", m.group(1)


def references(rel, text):
    """(line, kind, value) for every place this file pulls code in."""
    name, suffix = Path(rel).name, Path(rel).suffix
    if name == "Package.swift":
        yield from package_swift(text)
    if suffix == ".swift":
        yield from swift_imports(text)
    elif name == "project.yml":
        yield from project_yml(text)
    elif name == "package.json":
        yield from package_json(text)
    elif suffix in JS:
        yield from js_imports(text)
    elif suffix == ".py":
        yield from py_imports(text)


def tracked(repo):
    """Tracked files, or every file outside build and dependency folders when repo is not in git."""
    p = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True)
    if p.returncode == 0:
        return [f for f in p.stdout.split("\0") if f]
    return sorted(str(f.relative_to(repo)) for f in repo.rglob("*")
                  if f.is_file() and not SKIP_DIRS & set(f.relative_to(repo).parts))


def scan(repo, pattern):
    hits, scanned = [], 0
    for rel in tracked(repo):
        name, suffix = Path(rel).name, Path(rel).suffix
        if not (suffix in JS or suffix in (".swift", ".py") or name in ("project.yml", "package.json")):
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for n, kind, value in references(rel, text):
            if pattern.search(value):
                hits.append((rel, n, kind, value))
    return hits, scanned


def repo_name(repo):
    name = registry.main_name(repo)
    if name not in RULES:
        name = registry.origin_name(repo) or name
    return name


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    name = None
    if "--as" in argv:
        i = argv.index("--as")
        name = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 1:
        print("usage: oneway.py REPO [--as NAME]")
        return 2
    repo = Path(argv[0]).resolve()
    if not repo.is_dir():
        print(f"{repo} is not a directory")
        return 2
    name = name or repo_name(repo)
    if name not in RULES:
        print(f"{name}: no one-way rule (rules cover {', '.join(RULES)}), nothing to check")
        return 0
    label, pattern = RULES[name]
    hits, scanned = scan(repo, pattern)
    for rel, n, kind, value in hits:
        print(f"{rel}:{n}: {kind} names {label}: {value}")
    if hits:
        print(f"{name}: {len(hits)} reference(s) to {label} in {scanned} files read")
        return 1
    print(f"{name}: no reference to {label} in {scanned} files read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
