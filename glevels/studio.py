"""Sensors for the Studio gates: what a client project carries, and what nothing may say or open.

    studio.py nerves FILE...   correct.studio.client-nerves: no mention of Gabriel's machinery
    studio.py egress FILE...   correct.security.egress-closed: the allow-list is never open
    studio.py contract REPO    correct.studio.project-contract: what the studio reads from a repo

Exit 0 passes, 1 is a catch (each one listed), 2 means the check could not run.
Stdlib only.
"""
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# correct.studio.client-nerves: Brain, the pulse, the spine and their tools are Gabriel's alone.
# "pulse" and "spine" alone are ordinary words in a web project, so only the system's own
# phrases and tool names count. create_ticket is not one of them: the studio's own MCP has a
# create_ticket tool, and the home instructions name it.
MACHINERY = [
    (re.compile(r"\bbrain\b", re.I), "Brain"),
    (re.compile(r"\bthe pulse\b|\bpulse page\b", re.I), "the pulse"),
    (re.compile(r"\bthe (?:alert )?spine\b|\b\w+'s spine\b", re.I), "the spine"),
    (re.compile(r"\b(?:brain|pulse|spine)_[a-z_]+\b", re.I), "a Brain tool"),
]

# correct.security.egress-closed. A wildcard is open in practice when anyone can register or
# host a name under its suffix: a top-level domain, a registry suffix such as co.uk, or one of
# these platform suffixes. Not the whole public suffix list, just the wide ones.
SHARED_SUFFIXES = {
    "amazonaws.com", "s3.amazonaws.com", "cloudfront.net", "azurewebsites.net", "blob.core.windows.net",
    "appspot.com", "cloudfunctions.net", "run.app", "web.app", "firebaseapp.com", "googleusercontent.com",
    "github.io", "githubusercontent.com", "gitlab.io", "pages.dev", "workers.dev", "r2.dev",
    "vercel.app", "netlify.app", "herokuapp.com", "onrender.com", "fly.dev", "deno.dev", "glitch.me",
    "repl.co", "replit.dev", "supabase.co", "ngrok.io", "ngrok-free.app", "ngrok.app",
    "trycloudflare.com", "loca.lt", "duckdns.org", "ddns.net",
}
# Second-level labels under a two-letter country code that anyone can register beneath.
REGISTRY_SLDS = {"co", "com", "net", "org", "gov", "ac", "edu", "ltd", "plc", "me", "or", "ne", "gen", "nom", "biz", "info"}
HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")

# correct.studio.project-contract
SOURCE = {".py", ".js", ".mjs", ".ts", ".go", ".rs", ".rb", ".php"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", "output"}
HEALTH_ROUTE = re.compile(r"""["'`]/health/?["'`]""")


def cannot(msg):
    print(f"impossible: {msg}")
    sys.exit(2)


def nerves(paths):
    hits = 0
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            cannot(f"cannot read {path}: {e}")
        for n, line in enumerate(text.splitlines(), 1):
            for pattern, name in MACHINERY:
                m = pattern.search(line)
                if m:
                    hits += 1
                    print(f"{path}:{n}: names {name} ({m.group(0)}): {line.strip()[:120]}")
    return 1 if hits else 0


def open_entry(entry):
    """Why an allow-list entry is open, or None when it names a bounded set of hosts."""
    if not isinstance(entry, str):
        return "not a host name"
    h = entry.strip().lower().rstrip(".")
    if set(h) <= {"*", "."}:
        return "allows every host"
    if "*" in h[2:] or ("*" in h and not h.startswith("*.")):
        return "a wildcard pattern, not a host or *.suffix"
    labels = h[2:].split(".") if h.startswith("*.") else []
    if len(labels) == 1 and re.fullmatch(r"[a-z][a-z0-9-]*", labels[0]):
        return f"covers the whole .{labels[0]} top-level domain"
    if not HOST.match(h):
        return "not a host name (no IP literals, ranges, ports or URLs)"
    if not labels:
        return None
    if len(labels) == 2 and labels[0] in REGISTRY_SLDS and len(labels[1]) == 2:
        return f"covers a registry suffix anyone can register under ({h[2:]})"
    if h[2:] in SHARED_SUFFIXES:
        return f"covers a platform where anyone can host a name ({h[2:]})"
    return None


def egress(paths):
    hits = 0
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            cannot(f"cannot read {path} as JSON: {e}")
        if not isinstance(data, dict):
            continue
        for key in ("allow", "egress"):
            if key not in data:
                continue
            entries = data[key]
            if not isinstance(entries, list):
                hits += 1
                print(f"{path}: {key} must be a list of host names, not {json.dumps(entries)[:60]}")
                continue
            for entry in entries:
                why = open_entry(entry)
                if why:
                    hits += 1
                    print(f"{path}: {key} entry {json.dumps(entry)[:80]} {why}")
    return 1 if hits else 0


def repo_files(repo):
    """Tracked files, or every file when git cannot list them. A studio checkout keeps its git
    metadata outside the tree (g-studio var/repos-git), so walking it is the normal case there."""
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True)
    if out.returncode == 0 and out.stdout:
        return {f for f in out.stdout.split("\0") if f}
    files = set()
    for d, dirs, names in os.walk(repo):
        dirs[:] = [x for x in dirs if x not in SKIP]
        files |= {os.path.relpath(os.path.join(d, n), repo) for n in names}
    return files


def contract(repo):
    repo = Path(repo)
    if not repo.is_dir():
        cannot(f"{repo} is not a directory")
    files = repo_files(repo)
    problems = [f"{n}: missing at the root" for n in ("CLAUDE.md", "AGENTS.md") if n not in files]
    for f in sorted(files):
        p = Path(f)
        if p.name == "CLAUDE.md" and p.parent != Path(".") and str(p.with_name("AGENTS.md")) not in files:
            problems.append(f"{p.with_name('AGENTS.md')}: missing beside {f} (mirror it or symlink it)")
    manifest = None
    if "studio.json" not in files:
        problems.append('studio.json: missing at the root (the smallest valid one is {"views": []})')
    else:
        try:
            manifest = json.loads((repo / "studio.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"studio.json: does not parse: {e}")
        else:
            if not isinstance(manifest, dict):
                problems.append("studio.json: must be a JSON object")
                manifest = None
    if manifest and manifest.get("backend") is not None:
        served = False
        for f in files:
            if Path(f).suffix in SOURCE and (repo / f).is_file():
                if HEALTH_ROUTE.search((repo / f).read_text(encoding="utf-8", errors="replace")):
                    served = True
                    break
        if not served:
            problems.append("studio.json declares a backend, but no source file serves /health")
        try:
            tool = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {})
        except (OSError, ValueError) as e:
            problems.append(f"pyproject.toml: cannot read depends: {e}")
        else:
            # The gcore checker accepts both spellings of the table, so this does too.
            if "depends" not in (tool.get("gcore") or tool.get("g-core") or {}):
                problems.append("pyproject.toml: a runtime declares [tool.gcore] depends ([] when it has none)")
    for p in problems:
        print(p)
    return 1 if problems else 0


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("nerves", "egress", "contract"):
        print(__doc__.strip())
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "contract":
        return contract(args[0])
    return nerves(args) if cmd == "nerves" else egress(args)


if __name__ == "__main__":
    sys.exit(main())
