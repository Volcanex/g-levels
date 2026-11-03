"""Sensor: a repo declares what it connects to and what flows, in pyproject.toml.

    [tool.gcore]
    depends = ["ntfy"]          # external catalog ids, unchanged

    [[tool.gcore.connects]]
    to = "g-studio"             # a manifest id: project, service, host, system repo or dependency
    via = "http"                # http, sse, websocket, import, path, vendor, file, ssh, db, mcp
    flow = "reads owner home and projects through /api/app/*"
    dir = "out"                 # optional: out (default, this repo calls or reads `to`), in, both

`connects = []` says the repo connects to nothing, and passes. A missing key fails.
It checks the form of the declaration, not that it lists every connection the code makes.

    connections.py REPO

Exit 0 when the declaration is well formed, 1 naming each problem, 2 when the repo or the
manifest cannot be read. The manifest is GCORE_MANIFEST, default /home/gabriel/gcore/manifest.json.
"""
import json
import os
import sys
import tomllib
from pathlib import Path

MANIFEST = "/home/gabriel/gcore/manifest.json"
VIA = ("http", "sse", "websocket", "import", "path", "vendor", "file", "ssh", "db", "mcp")
DIRS = ("out", "in", "both")
# The manifest lists that hold ids a connection can point at. gcore itself lives in `system`.
SECTIONS = ("projects", "services", "hosts", "system", "dependencies")
KEYS = ("to", "via", "flow", "dir")


def manifest_ids(manifest):
    return {e["id"] for s in SECTIONS for e in manifest.get(s, []) if isinstance(e, dict) and e.get("id")}


def problems(text, ids):
    """Every problem with the [tool.gcore] connects declaration in this pyproject text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"pyproject.toml does not parse: {e}"]
    gcore = data.get("tool", {}).get("gcore")
    if not isinstance(gcore, dict):
        return ["no [tool.gcore] table: add one with its connects"]
    if "connects" not in gcore:
        return ["[tool.gcore] has no connects: add a [[tool.gcore.connects]] table per connection, "
                "or connects = [] when the repo connects to nothing"]
    connects = gcore["connects"]
    if not isinstance(connects, list):
        return ["[tool.gcore].connects must be a list of tables"]
    out = []
    for n, c in enumerate(connects, 1):
        if not isinstance(c, dict):
            out.append(f"connects entry {n} is not a table")
            continue
        where = f"connects entry {n}" + (f" (to {c['to']!r})" if isinstance(c.get("to"), str) and c["to"] else "")
        unknown = sorted(set(c) - set(KEYS))
        if unknown:
            out.append(f"{where}: unknown key {', '.join(unknown)} (keys are {', '.join(KEYS)})")
        to = c.get("to")
        if not isinstance(to, str) or not to.strip():
            out.append(f"{where}: no to")
        elif to not in ids:
            out.append(f"{where}: {to!r} is not an id in the manifest's {', '.join(SECTIONS)}")
        via = c.get("via")
        if via not in VIA:
            out.append(f"{where}: via {via!r} is not one of {', '.join(VIA)}" if via is not None
                       else f"{where}: no via (one of {', '.join(VIA)})")
        flow = c.get("flow")
        if not isinstance(flow, str) or not flow.strip():
            out.append(f"{where}: no flow, say in plain words what moves")
        if "dir" in c and c["dir"] not in DIRS:
            out.append(f"{where}: dir {c['dir']!r} is not one of {', '.join(DIRS)}")
    return out


def summary(text):
    connects = tomllib.loads(text)["tool"]["gcore"]["connects"]
    if not connects:
        return "connects = []: declares no connections"
    return "connections declared: " + "; ".join(
        f"{c.get('dir', 'out')} {c['to']} via {c['via']}" for c in connects)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: connections.py REPO")
        return 2
    repo = Path(argv[0])
    if not repo.is_dir():
        print(f"{repo} is not a directory")
        return 2
    path = Path(os.environ.get("GCORE_MANIFEST") or MANIFEST)
    try:
        ids = manifest_ids(json.loads(path.read_text()))
    except (OSError, ValueError) as e:
        print(f"cannot read the manifest {path}: {e}")
        return 2
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        print(f"{repo.name}: no pyproject.toml, so nowhere to declare [tool.gcore] connects")
        return 1
    text = pyproject.read_text()
    found = problems(text, ids)
    for p in found:
        print(f"{repo.name}/pyproject.toml: {p}")
    if found:
        return 1
    print(f"{repo.name}: {summary(text)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
