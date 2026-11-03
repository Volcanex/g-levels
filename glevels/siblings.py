"""Mechanical half of correct.kit.builds-alone: the sibling checkouts an app's build reaches into.

    siblings.py REPO [--as NAME]

Lists every path the build files point outside the repo: `.package(path:)` in Package.swift,
every `path:` in project.yml (local packages and source or resource folders alike) and
`file:`/`link:` specs in package.json. Each is resolved against its own file and named by the
sibling checkout it lands in (a worktree takes its main repo's name). Then it applies the rule:

  g-studio                   g-design only
  g-studio-apple             g-design, and in g-studio only the shared app-contract fixtures
                             (tests/fixtures/app-contract, Debug resources: Studio reading Studio)
  g-command-apple            g-design, g-studio-apple's Kit/ (the GStudioKit package) and the
                             same app-contract fixtures, for its Studio section's fixture mode
  g-command-web              g-design, and in g-studio only a mount entry (a file or folder
                             whose name says mount); the reviewer confirms Studio publishes it

Other repos have no rule: their siblings are listed and pass. Whether the builds actually run
and pass is the review half.

Exit 0 when every sibling is allowed, 1 listing each that is not, 2 when it cannot run.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import registry
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from glevels import registry

ANY = ""


def kit_only(subpath):
    return subpath == "Kit" or subpath.startswith("Kit/")


def contract_fixtures(subpath):
    return subpath == "tests/fixtures/app-contract" or subpath.startswith("tests/fixtures/app-contract/")


def mount_entry(subpath):
    return bool(subpath) and "mount" in Path(subpath).name.lower()


# Per app: the siblings it may reach, each with a test on the path inside that sibling.
RULES = {
    "g-studio": {"g-design": None},
    "g-studio-apple": {"g-design": None, "g-studio": (contract_fixtures, "only the app-contract fixtures")},
    "g-command-apple": {"g-design": None, "g-studio-apple": (kit_only, "only Kit/, the GStudioKit package"),
                        "g-studio": (contract_fixtures, "only the app-contract fixtures")},
    "g-command-web": {"g-design": None, "g-studio": (mount_entry, "only Studio's published mount entry")},
}
SKIP_DIRS = {".git", "node_modules", ".build", "DerivedData", ".venv", "__pycache__"}
SWIFT_PACKAGE = re.compile(r"\.package\s*\((?:[^()]|\([^()]*\))*\)", re.S)
SWIFT_PATH = re.compile(r'\bpath\s*:\s*"([^"]+)"')
YML_PATH = re.compile(r"^\s*(?:-\s*)?path\s*:\s*(.+?)\s*$")
NPM_LOCAL = ("file:", "link:")
DEP_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def unquote(value):
    value = re.sub(r"\s+#.*$", "", value).strip()
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value


def declared(rel, text):
    """(line, raw path) for each path this build file points at."""
    name = Path(rel).name
    if name == "Package.swift":
        for call in SWIFT_PACKAGE.finditer(text):
            for m in SWIFT_PATH.finditer(call.group(0)):
                yield text.count("\n", 0, call.start() + m.start()) + 1, m.group(1)
    elif name == "project.yml":
        for n, line in enumerate(text.splitlines(), 1):
            m = YML_PATH.match(line)
            if m:
                yield n, unquote(m.group(1))
    elif name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return
        lines = text.splitlines()
        for section in DEP_SECTIONS:
            for dep, spec in (data.get(section) or {}).items():
                if isinstance(spec, str) and spec.startswith(NPM_LOCAL):
                    n = next((i for i, line in enumerate(lines, 1) if f'"{dep}"' in line), 1)
                    yield n, spec.split(":", 1)[1]


def build_files(repo):
    p = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True)
    if p.returncode == 0:
        files = [f for f in p.stdout.split("\0") if f]
    else:
        files = [str(f.relative_to(repo)) for f in repo.rglob("*")
                 if f.is_file() and not SKIP_DIRS & set(f.relative_to(repo).parts)]
    return sorted(f for f in files if Path(f).name in ("Package.swift", "project.yml", "package.json"))


def sibling_of(repo, target):
    """(sibling name, path inside it), or (None, target) when target is not beside the repo."""
    try:
        parts = target.relative_to(repo.parent).parts
    except ValueError:
        return None, str(target)
    if not parts:
        return None, str(target)
    top = repo.parent / parts[0]
    name = registry.main_name(top) if top.is_dir() else parts[0]
    return name, "/".join(parts[1:])


def outside(repo):
    """(file, line, raw, sibling, subpath) for every declared path that leaves the repo."""
    out = []
    for rel in build_files(repo):
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        base = (repo / rel).parent
        for n, raw in declared(rel, text):
            target = Path(os.path.normpath(base / os.path.expanduser(raw)))
            if target == repo or repo in target.parents:
                continue
            sibling, subpath = sibling_of(repo, target)
            out.append((rel, n, raw, sibling, subpath))
    return out


def verdict(name, sibling, subpath):
    """None when allowed, else why not."""
    rule = RULES.get(name)
    if rule is None:
        return None
    allowed = ", ".join(rule)
    if sibling not in rule:
        return f"{name} may depend only on {allowed}"
    test = rule[sibling]
    if test is not None and not test[0](subpath):
        return f"{name} may use {test[1]} from {sibling}"
    return None


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
        print("usage: siblings.py REPO [--as NAME]")
        return 2
    repo = Path(argv[0]).resolve()
    if not repo.is_dir():
        print(f"{repo} is not a directory")
        return 2
    name = name or repo_name(repo)
    found = outside(repo)
    bad = 0
    for rel, n, raw, sibling, subpath in found:
        why = verdict(name, sibling, subpath)
        where = f"{sibling}/{subpath}".rstrip("/") if sibling else f"{subpath}, not beside the repo"
        print(f"{rel}:{n}: {raw} -> {where}" + (f": not allowed, {why}" if why else ""))
        bad += why is not None
    rule = RULES.get(name)
    allowed = f"allowed: {', '.join(rule)}" if rule else "no sibling rule for this repo, listed only"
    print(f"{name}: {len(found)} path(s) outside the repo, {bad} not allowed ({allowed})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
