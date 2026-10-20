"""Mechanical half of correct.ui.shared-layout: one layout source for every client.

usage: shared_layout.py <repo>

g-command-web: the dash layout tests (placement only in config.json `layout`).
g-command-apple: the fixture manifest carries the server's layouts and panel
order, and no Swift view names a panel to place it (a line marked
`// layout-exception: <reason>` is a surface no web page has).
Exit 0 passes, 1 is a catch, 2 means the check could not run.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

WEB = Path("/home/gabriel/g-command-web")
PANELS = ("calendar", "brief", "tasks", "money", "health", "compute", "tickets", "crons")


def web(repo):
    python = repo / ".venv/bin/python3"
    if not python.exists():
        print(f"no venv at {python}")
        return 2
    return subprocess.run([str(python), "-m", "pytest", "-q", "tests/test_dash_layout.py"], cwd=repo).returncode


def server_layouts():
    code = ("import json; from core.panels.manifest import build_manifest; m = build_manifest('pages'); "
            "print(json.dumps({x['slug']: [x['panels'], x['layout']] for s in m['sections'] for e in s['entries'] "
            "for x in [e] + e.get('tabs', []) if 'layout' in x}))")
    out = subprocess.run([str(WEB / ".venv/bin/python3"), "-c", code], cwd=WEB, capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else None


def apple(repo):
    server = server_layouts()
    if server is None:
        print(f"could not build the server manifest from {WEB}")
        return 2
    fixture = json.loads((repo / "Fixtures/fixture-manifest.json").read_text())
    pages = {x["slug"]: x for s in fixture["sections"] for e in s["entries"] for x in [e] + e.get("tabs", [])}
    faults = []
    for slug, (panels, layout) in server.items():
        page = pages.get(slug)
        if page is None:
            faults.append(f"fixture manifest lacks the panel page {slug}")
        elif page.get("panels") != panels or page.get("layout") != layout:
            faults.append(f"fixture {slug}: panels or layout differ from the server (rerun Scripts/make_fixtures.py after matching it)")
    literal = re.compile(r'"(' + "|".join(PANELS) + r')"')
    for path in sorted((repo / "Kit/Sources/GCommandUI").rglob("*.swift")) + sorted((repo / "macOS").rglob("*.swift")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if literal.search(line) and "accessibilityIdentifier" not in line and "layout-exception:" not in line:
                faults.append(f"{path.relative_to(repo)}:{number}: names a panel; placement comes from the manifest layout")
    for fault in faults:
        print(fault)
    return 1 if faults else 0


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    if (repo / "core/panels/layout.py").exists():
        return web(repo)
    if (repo / "Kit/Sources/GCommandCore").exists():
        return apple(repo)
    print(f"{repo} is neither g-command-web nor g-command-apple")
    return 2


if __name__ == "__main__":
    sys.exit(main())
