"""Sensor: browser runtime faults. Gate correct.delivery.runtime-clean.

    python3 runtime.py [--out DIR] [--json] [--journey FILE]
                       [--compare OLD_DIR_OR_findings.json] TARGET [TARGET ...]

A TARGET is a URL, an HTML file, or a directory containing index.html. Files are
served from their directory so relative assets and fetches work. walk.mjs runs
one Chromium under the spacing sensor's shared flock and memory guard.
Screenshots, visible text, walk.json and findings.json land in --out (default:
a new temp dir, printed at the end).

Exit 0: no clear faults. Exit 1: clear faults listed under "blocking". Exit 2:
could not run, printed as "impossible: ...". Leads under "for the reviewer"
never fail the run alone. --compare labels blocking findings NEW or OLD and
lists fixed findings; any remaining blocking finding still exits 1.
"""
import argparse
import fcntl
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
LOCK = Path(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"))


def impossible(msg):
    print(f"impossible: {msg}")
    sys.exit(2)


def serve(root):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/favicon.ico" and not (root / "favicon.ico").exists():
                self.send_response(204)
                self.end_headers()
            else:
                super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def resolve(targets, stack):
    urls, roots = [], {}
    for target in targets:
        if target.startswith(("http://", "https://")):
            urls.append(target)
            continue
        file = Path(target).resolve()
        root = file if file.is_dir() else file.parent
        if file.is_dir():
            file = file / "index.html"
        if not file.is_file() or file.suffix.lower() not in (".html", ".htm"):
            raise ValueError(f"not an HTML file or directory containing index.html: {target}")
        if root not in roots:
            server, base = serve(root)
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            roots[root] = base
        urls.append(f"{roots[root]}/{quote(file.name)}")
    return urls


def findings(walk, targets, urls):
    blocking, review, seen = [], [], set()
    places = {state["id"]: state for state in walk["states"]}
    local_roots = {urlsplit(url).netloc: str(Path(target).resolve() if Path(target).is_dir() else Path(target).resolve().parent)
                   for target, url in zip(targets, urls) if not target.startswith(("http://", "https://"))}

    def stable(url):
        parts = urlsplit(url)
        if parts.netloc in local_roots:
            return "local:" + local_roots[parts.netloc] + parts.path + ("?" + parts.query if parts.query else "")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    def add(error, state, action=None):
        kind, message = error["kind"], error["message"]
        same = error.get("sameOrigin", True)
        level = error.get("level", "error")
        status, resource = error.get("status", 0), error.get("resourceType", "")
        block = kind in ("errortext", "blank", "freeze", "journey", "dead") or same and (
            kind in ("pageerror", "requestfailed") or kind == "console" and level == "error" or
            kind == "http" and (status >= 500 or status == 404 and resource in ("script", "stylesheet", "fetch", "xhr", "document")))
        if kind == "console" and "Failed to load resource" in message:
            related = [e for e in state.get("errors", []) if e["kind"] == "http" and e.get("url") == error.get("url")]
            if related and all(400 <= e.get("status", 0) < 500 and e.get("resourceType") in ("image", "font", "other") for e in related):
                block = False
        url = error.get("url") or state["url"]
        target = next((t for t, u in zip(targets, urls) if u == state["url"]), stable(state["url"]))
        detail = re.sub(r"\s+", " ", message).strip()
        for netloc, root in local_roots.items():
            detail = detail.replace(f"http://{netloc}", "local:" + root)
        key = json.dumps([stable(state["url"]), kind, stable(url), detail, action or ""])
        if key in seen:
            return
        seen.add(key)
        row = {"kind": kind, "message": detail, "target": target, "url": stable(url),
               "state": state["id"], "screenshot": state.get("screenshot"), "sameOrigin": same, "key": key}
        if action:
            row["action"] = action
        (blocking if block else review).append(row)

    for state in walk["states"]:
        for error in state["errors"]:
            add(error, state)
        if state.get("canvas") and not state.get("animatingCanvas") and state.get("fps"):
            add({"kind": "still-canvas", "message": "canvas drew nothing during a 1 s sample: if it should move, it is frozen"}, state)
        if state.get("fps") is not None and state["fps"] < 30 and state.get("animatingCanvas"):
            add({"kind": "fps", "message": f"animating canvas: {state['fps']} frames/s (under 30)"}, state)
        for duration in state.get("longtasks", []):
            if duration > 1000:
                add({"kind": "longtask", "message": f"main thread longtask: {round(duration)} ms"}, state)
    for edge in walk["edges"]:
        state = places.get(edge["to"]) or places.get(edge["from"])
        if state:
            for error in edge["errors"]:
                if error["kind"] in ("journey", "dead", "action"):
                    add(error, state)
    return {"targets": targets, "blocking": blocking, "review": review,
            "regressions": [], "fixed": [], "walk": "walk.json"}


def compare(result, old):
    prior = {f["key"]: f for f in old["blocking"]}
    current = {f["key"] for f in result["blocking"]}
    for finding in result["blocking"]:
        finding["status"] = "OLD" if finding["key"] in prior else "NEW"
    result["regressions"] = [f for f in result["blocking"] if f["status"] == "NEW"]
    result["fixed"] = [f for key, f in prior.items() if key not in current]


def report(result, walk, out, compared):
    print(f"runtime: {len(walk['states'])} states, {len(result['blocking'])} blocking, {len(result['review'])} for the reviewer")
    if compared:
        print(f"compare: {len(result['regressions'])} new, {len(result['blocking']) - len(result['regressions'])} old, {len(result['fixed'])} fixed")
    for key, title in (("blocking", "blocking (clear faults, fix these)"),
                       ("review", "for the reviewer (leads, judge them against the screenshots)"), ("fixed", "fixed")):
        if result[key]:
            print(f"\n{title}:")
            for finding in result[key]:
                label = f"{finding['status']} " if key == "blocking" and compared else ""
                print(f"  {label}{finding['target']}  {finding['kind']}: {finding['message']}")
    if walk["budget"]["hit"]:
        print("\nfor the reviewer: walk budget reached; the remaining steps were not checked")
    print("\nscreenshots, for the review half of the gate:")
    for state in walk["states"]:
        print(f"  {state['id']} {state['url']}: {out / state['screenshot'] if state['screenshot'] else 'unavailable (page did not answer)'}")
    print(f"\nfindings: {out}/findings.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--out", help="evidence directory (default: a new temp dir)")
    ap.add_argument("--json", action="store_true", help="print findings.json instead of the report")
    ap.add_argument("--journey", help="JSON list of browser steps")
    ap.add_argument("--compare", help="old evidence directory or findings.json")
    args = ap.parse_args()
    # From `glevels check`, the charter's runner names the URL and journey in the environment.
    args.targets = args.targets or os.environ.get("GLEVELS_URL", "").split()
    args.journey = args.journey or os.environ.get("GLEVELS_JOURNEY") or None
    if not args.targets:
        impossible("give a URL, HTML file or directory (or set GLEVELS_URL)")
    if shutil.which("node") is None:
        impossible("node is not installed")
    try:
        out = Path(args.out or tempfile.mkdtemp(prefix="glevels-runtime-")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        old = None
        if args.compare:
            source = Path(args.compare)
            old = json.loads((source / "findings.json" if source.is_dir() else source).read_text())
        with ExitStack() as stack:
            urls = resolve(args.targets, stack)
            cmd = ["node", str(HERE / "walk.mjs"), "--mode", "smoke", "--out", str(out)]
            if args.journey:
                cmd.extend(["--journey", args.journey])
            with open(LOCK, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process = subprocess.run([*cmd, *urls], capture_output=True, text=True, timeout=600,
                                         env=dict(os.environ, GLEVELS_WALK_LOCKED="1"))
            if process.returncode:
                raise RuntimeError((process.stderr or process.stdout).strip()[-600:].removeprefix("impossible: "))
        walk = json.loads((out / "walk.json").read_text())
        result = findings(walk, args.targets, urls)
        if old is not None:
            compare(result, old)
        (out / "findings.json").write_text(json.dumps(result, indent=2) + "\n")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        impossible(str(error))
    if args.json:
        print(json.dumps(result, indent=2))
        print(f"findings: {out}/findings.json", file=sys.stderr)
    else:
        report(result, walk, out, old is not None)
    sys.exit(1 if result["blocking"] else 0)


if __name__ == "__main__":
    main()
