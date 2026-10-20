"""Sensor: missing, broken and hard-to-find options. Gate correct.ux.option-tree.

    python3 optiontree.py [--out DIR] [--json] [--src DIR] [--routes FILE]
                         [--max-states 40] [--max-actions 200] [--deep 4]
                         [--persona FILE] [--walk EXISTING_DIR] TARGET...

Targets are URLs, HTML files or directories with index.html. With --src and no
target, only the source is scanned (URL-only switches, opt-in defaults). walk.mjs crawls them
in one Chromium; --walk instead analyses a saved walk.json without a browser.
Screenshots, findings.json, tree.md and the optional separate judge's persona.md
go in --out (default: a new temp directory, printed at the end).

Exit 0: no clear fault. Exit 1: clear faults, listed under "blocking". Exit 2:
could not run, printed as "impossible: <reason>". Leads under "for the reviewer"
need judgement and never fail the run alone.
"""
import argparse
import fcntl
import functools
import http.server
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

HERE = Path(__file__).resolve().parent
MIN_FREE_MB = int(os.environ.get("GSPACING_MIN_FREE_MB", "600"))
LOCK = Path(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"))
SOURCE_EXTS = {".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".py", ".html", ".dart", ".php", ".rb"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
QUERY = re.compile(
    r"(?:\b(?:searchParams|params|request\.(?:args|query_params))\s*\.\s*get|"
    r"new\s+URLSearchParams\s*\([^;\n]*?\)\s*\.\s*get)\s*\(\s*['\"]([^'\"]+)['\"]"
    r"|\breq\.query\.([A-Za-z_$][\w$]*)"
    r"|(?:\$_GET|Uri\.base\.queryParameters)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]"
)
# A FastAPI flag that is off unless the request asks: a default the user never sees.
OPT_IN = re.compile(r"\b([a-z_]\w*)\s*:\s*bool\s*=\s*Query\(\s*(?:default\s*=\s*)?False\b")
ROUTE = re.compile(
    r"@(?:app|router)\.(?:get|route)\s*\(\s*['\"]([^'\"]+)['\"]"
    r"|\bpath\s*:\s*['\"]([^'\"]+)['\"]"
    r"|<Route\b[^>]*\bpath\s*=\s*['\"]([^'\"]+)['\"]"
)
INVITE = re.compile(
    r"\b(?:you can|you['’]ll be able to|you may)\b|^\W*(?:choose|change|pick|select|connect|add|set up|turn on|"
    r"turn off|upgrade|export|download|filter|sort|edit|invite|join|enter|get in|switch)\b", re.I
)
EMPTY = re.compile(r"\b(?:no results|nothing here|no data yet|empty|not connected|none yet)\b", re.I)
STOP = set("a an and are as at be been by can for from here in into is it its of on or our the their then this to up with you your will able ll please now using use click button".split())
JUDGE = ("You have never seen this product. Use only what is on screen. For each task, "
         "try to do it from the entry URL, narrate each step, save a full-resolution "
         "screenshot per step, and report reached/stuck with the step and the quoted "
         "on-screen words that misled or were missing. Then read tree.md and the "
         "leads, and say which leads you confirm.")


def free_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def place(state):
    return f"{state.get('title') or state['url']} ({state['url']})"


def enabled(control):
    return control.get("visible", True) and not control.get("disabled", False)


def matches(control, action):
    for key in ("ref", "selector", "name"):
        if action.get(key) and control.get(key):
            return action[key] == control[key]
    return False


def control_key(state, control):
    url = urlsplit(state["url"])
    return (url.netloc, url.path, control.get("selector") or control.get("name") or control.get("ref"),
            control.get("role"), control.get("type"), control.get("name"), control.get("href"))


def graph(walk):
    states = {s["id"]: s for s in walk["states"]}
    outgoing = {sid: [] for sid in states}
    for edge in walk.get("edges", []):
        if edge.get("from") in outgoing:
            outgoing[edge["from"]].append(edge)
    roots = [s["id"] for s in states.values() if s.get("depth") == 0]
    if not roots:
        roots = [s["id"] for s in states.values() if s["url"] in walk.get("entry", [])]
    if not roots:
        roots = [next(iter(states))]
    distances = dict.fromkeys(roots, 0)
    queue = deque(roots)
    while queue:
        sid = queue.popleft()
        for edge in outgoing[sid]:
            target = edge.get("to")
            if target not in states or edge.get("outcome") == "skipped":
                continue
            cost = int(edge.get("action", {}).get("kind") in {"click", "select", "submit"})
            distance = distances[sid] + cost
            if distance < distances.get(target, float("inf")):
                distances[target] = distance
                (queue.append if cost else queue.appendleft)(target)
    return states, outgoing, distances, roots


def clear_error(error):
    kind = error.get("kind")
    return (kind in {"pageerror", "errortext", "blank"}
            or kind == "console" and error.get("sameOrigin") is True and error.get("level", "error") == "error"
            or kind == "http" and error.get("sameOrigin") is True and error.get("status", 0) >= 500)


def state_text(state, walk_dir):
    value = state.get("text") or ""
    if value.endswith(".txt") and "\n" not in value:
        return (Path(walk_dir) / value).read_text()
    return value


def route_key(value, base):
    url = urlsplit(urljoin(base, value))
    return url.netloc, unquote(url.path).rstrip("/") or "/"


def scan_source(src):
    switches, routes, opt_in = [], [], []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix not in SOURCE_EXTS:
                continue
            text = path.read_text(errors="replace")
            for regex, found in ((QUERY, switches), (ROUTE, routes), (OPT_IN, opt_in)):
                for match in regex.finditer(text):
                    value = next(v for v in match.groups() if v is not None)
                    if regex is ROUTE and (not value.startswith("/") or re.search(r"[:{}<>*]", value)):
                        continue
                    line = text.count("\n", 0, match.start()) + 1
                    found.append((value, f"{path.relative_to(src)}:{line}"))
    return switches, routes, opt_in


def source_leads(src, params=(), reached=None, bases=()):
    switches, declarations, opt_in = scan_source(src)
    review, seen = [], set()
    for param, location in switches:
        if param not in params and param not in seen:
            review.append(f"option only reachable by typing ?{param}= into the URL (read at {location})")
            seen.add(param)
    for param, location in dict.fromkeys(opt_in):
        review.append(f"default: {param} is off unless the request asks for it (declared at {location}): is off what most users want on day one?")
    if reached is not None:
        for route, location in dict.fromkeys(declarations):
            if not any(route_key(route, base) in reached for base in bases):
                review.append(f"route {route} declared at {location}: never reached by the crawl")
    return review


def words(text):
    return set(re.findall(r"[a-z]+", text.lower())) - STOP


def analyse(walk, walk_dir=".", src=None, routes=(), deep=4):
    states, outgoing, distances, roots = graph(walk)
    blocking, review, defaults = [], [], []
    seen_defaults, appearances = set(), {}
    reached, linked, params = set(), set(), set()
    order = {sid: i for i, sid in enumerate(states)}
    for sid, state in states.items():
        if sid not in distances:
            continue
        where = place(state)
        reached.add(route_key(state["url"], state["url"]))
        controls = state.get("controls", [])
        available = [c for c in controls if enabled(c)]
        text = state_text(state, walk_dir)
        errors = [e for e in state.get("errors", []) if clear_error(e)]
        if errors:
            blocking.append(f"{where}: error or blank state: " + "; ".join(e.get("message") or e["kind"] for e in errors))
        elif not text.strip() and not available:
            blocking.append(f"{where}: blank state reached from the entry")
        for edge in outgoing[sid]:
            action = edge.get("action", {})
            name = action.get("name") or action.get("ref") or action.get("kind", "action")
            errors = [e for e in edge.get("errors", []) if clear_error(e)]
            if edge.get("outcome") == "error" or errors:
                detail = "; ".join(e.get("message") or e["kind"] for e in errors or edge.get("errors", [])) or "action failed"
                blocking.append(f"clicking '{name}' on {where} led to an error: {detail}")
            elif edge.get("outcome") == "unclickable":
                review.append(f"'{name}' on {where}: the walker could not click it ({edge.get('note', 'timed out')}); "
                              "check by hand whether something covers it")
            elif edge.get("outcome") == "none" and any(matches(c, action) for c in available):
                blocking.append(f"'{name}' on {where}: nothing happened (dead control)")
        if not available:
            review.append(f"{where}: dead end, no enabled controls offered")
            if EMPTY.search(text):
                review.append(f"{where}: empty state with no control offered: {EMPTY.search(text)[0]}")
        else:
            tried = [e for e in outgoing[sid] if e.get("outcome") != "skipped"]
            all_tried = all(any(matches(c, e.get("action", {})) for e in tried) for c in available)
            if all_tried and tried and all(e.get("to") in order and order[e["to"]] <= order[sid] for e in tried):
                review.append(f"{where}: dead end, outbound controls only return to already-seen states")
        near = available[:]
        for edge in outgoing[sid]:
            if edge.get("to") in states and edge.get("outcome") != "skipped":
                near.extend(c for c in states[edge["to"]].get("controls", []) if enabled(c))
        names = words(" ".join(c.get("name") or "" for c in near))
        implied = 0
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            invitation = INVITE.search(sentence.strip()) if len(sentence.split()) <= 25 else None
            sentence = sentence.strip()
            if invitation and words(sentence[invitation.start():]) and not words(sentence[invitation.start():]) & names:
                review.append(f'{where}: implied option has no matching control here or one hop away: "{sentence.strip()}"')
                implied += 1
                if implied == 15:
                    break
        for control in controls:
            key = control_key(state, control)
            appearances.setdefault(key, []).append((state, control))
            href = control.get("href")
            if href:
                linked.add(route_key(href, state["url"]))
                params.update(parse_qs(urlsplit(urljoin(state["url"], href)).query, keep_blank_values=True))
            if control.get("tag", "").lower() == "select" and control.get("name"):
                params.add(control["name"])
            toggle = control.get("type") in {"checkbox", "radio"} or control.get("role") in {"checkbox", "switch", "radio"}
            select = control.get("tag", "").lower() == "select" or control.get("role") in {"combobox", "listbox"}
            if key not in seen_defaults and (toggle or select):
                seen_defaults.add(key)
                if select:
                    value = ", ".join(o["label"] for o in control.get("options", []) if o.get("selected")) or str(control.get("value") or "nothing selected")
                else:
                    value = "on" if control.get("checked") else "off"
                defaults.append(f"{control.get('name') or control.get('ref')}: {value} by default on {where}")
        for form in state.get("forms", []):
            params.update(f["name"] for f in form.get("fields", []) if f.get("name"))
    for group in appearances.values():
        state, control = group[0]
        name = control.get("name") or control.get("ref") or control.get("selector")
        if all(c.get("visible") is False for _, c in group):
            review.append(f"'{name}' on {place(state)}: exists but not findable, hidden in every observed state")
        elif min(distances[s["id"]] for s, c in group if c.get("visible", True)) > deep:
            review.append(f"'{name}' on {place(state)}: exists but not findable until more than {deep} decisions")
    bases = [states[sid]["url"] for sid in roots]
    for route in routes:
        if not any(route_key(route, base) in reached | linked for base in bases):
            blocking.append(f"unreachable route {route}: never reached and no control href points to it")
    if src:
        review.extend(source_leads(src, params, reached, bases))
    branches = [{"id": sid, "title": states[sid].get("title"), "url": states[sid]["url"], "decisions": d}
                for sid, d in distances.items()]
    median = statistics.median(distances.values())
    for item in branches:
        if item["decisions"] > deep:
            review.append(f"{place(states[item['id']])}: branching needs {item['decisions']} decisions from entry (over {deep})")
    review.extend(defaults)
    review.append(f"branching: median {median:g} decisions from entry across {len(branches)} states")
    untried = sum(1 for sid in distances for c in states[sid].get("controls", []) if enabled(c)
                  and not any(e.get("outcome") != "skipped" and matches(c, e.get("action", {})) for e in outgoing[sid]))
    budget = dict(walk.get("budget") or {}, untried=untried)
    if budget.get("hit"):
        review.append(f"crawl stopped on budget: {untried} enabled controls went untried")
    return {"blocking": blocking, "review": review, "defaults": defaults,
            "branching": {"states": branches, "median": median, "deep": deep}, "budget": budget}


def tree_markdown(walk, walk_dir, out_dir):
    states, outgoing, _, roots = graph(walk)
    lines, seen = ["# Option tree", ""], set()

    def visit(sid, depth):
        state = states[sid]
        pad = "  " * depth
        lines.append(f"{pad}- {place(state)}" + (" (seen above)" if sid in seen else ""))
        if sid in seen:
            return
        seen.add(sid)
        shot = state.get("screenshot")
        if shot:
            source = Path(walk_dir) / shot
            if source.is_file():
                dest = Path(out_dir) / "shots" / source.name
                dest.parent.mkdir(exist_ok=True)
                if source.resolve() != dest.resolve():
                    shutil.copy2(source, dest)
                shot = str(dest)
            else:
                shot = str(source)
        lines.append(f"{pad}  - screenshot: {shot or 'not captured'}")
        used, targets = set(), []
        for control in state.get("controls", []):
            name = control.get("name") or control.get("ref") or "unnamed control"
            edges = [e for e in outgoing[sid] if matches(control, e.get("action", {}))]
            if not edges:
                status = "hidden" if control.get("visible") is False else "disabled" if control.get("disabled") else "untried"
                lines.append(f"{pad}  - {name} -> {status}")
            for edge in edges:
                used.add(id(edge))
                target = states.get(edge.get("to"))
                lines.append(f"{pad}  - {name} -> {edge.get('outcome')}" + (f": {place(target)}" if target else ""))
                if target:
                    targets.append(target["id"])
        for edge in outgoing[sid]:
            if id(edge) not in used:
                target = states.get(edge.get("to"))
                lines.append(f"{pad}  - {edge.get('action', {}).get('name') or 'action'} -> {edge.get('outcome')}" + (f": {place(target)}" if target else ""))
                if target:
                    targets.append(target["id"])
        for target in dict.fromkeys(targets):
            visit(target, depth + 1)

    for sid in roots + list(states):
        if sid not in seen:
            visit(sid, 0)
    return "\n".join(lines) + "\n"


def persona_markdown(path, entry):
    path = Path(path)
    persona = tomllib.loads(path.read_text()) if path.suffix.lower() == ".toml" else json.loads(path.read_text())
    if not isinstance(persona, dict) or not isinstance(persona.get("tasks"), list):
        raise ValueError("persona needs a tasks list")
    packet = {key: persona.get(key, "") for key in ("reader", "background", "tasks")}
    return f"Entry URL: {persona.get('entry') or entry}\n\n{json.dumps(packet, indent=2, ensure_ascii=False)}\n\n{JUDGE}\n"


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def crawl(targets, out_dir, max_states, max_actions):
    if shutil.which("node") is None:
        raise ValueError("node is not installed")
    if not (HERE / "walk.mjs").is_file():
        raise ValueError("walk.mjs is not available")
    servers, urls, roots = [], [], {}
    try:
        for target in targets:
            if target.startswith(("http://", "https://")):
                urls.append(target)
                continue
            path = Path(target).resolve()
            path = path / "index.html" if path.is_dir() else path
            if not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
                raise ValueError(f"not an HTML file or directory with index.html: {target}")
            if path.parent not in roots:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(path.parent)))
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                roots[path.parent] = f"http://127.0.0.1:{server.server_port}"
            urls.append(f"{roots[path.parent]}/{quote(path.name)}")
        with open(LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mb = free_mb()
            if mb < MIN_FREE_MB:
                raise ValueError(f"only {mb} MB of memory free, a browser needs {MIN_FREE_MB}. Try again when the host is quieter")
            cmd = ["node", str(HERE / "walk.mjs"), "--mode", "tree", "--out", str(out_dir),
                   "--max-states", str(max_states), "--max-actions", str(max_actions)]
            if any(urlsplit(url).hostname not in {"localhost", "127.0.0.1"} for url in urls):
                cmd.append("--safe")
            result = subprocess.run(cmd + urls, capture_output=True, text=True, timeout=600,
                                    env=dict(os.environ, GLEVELS_WALK_LOCKED="1"))
            if result.returncode:
                raise ValueError(f"the browser crawl failed: {(result.stderr or result.stdout).strip()[-600:]}")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def report(findings, walk, out_dir, persona):
    print(f"optiontree: {len(walk['states'])} states, {len(findings['blocking'])} blocking, {len(findings['review'])} for the reviewer")
    for key, label in (("blocking", "blocking (clear faults, fix these)"), ("review", "for the reviewer (leads, judge them against the screenshots)")):
        if findings[key]:
            print(f"\n{label}:")
            for item in findings[key]:
                print("  " + item)
    print(f"\nscreenshots: {out_dir}/shots/ (paths per state in tree.md)")
    print(f"tree: {out_dir}/tree.md")
    if persona:
        print(f"persona: {out_dir}/persona.md")
    print(f"findings: {out_dir}/findings.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--out", help="output directory (default: a new temp dir)")
    ap.add_argument("--json", action="store_true", help="print findings.json instead of the report")
    ap.add_argument("--src")
    ap.add_argument("--routes")
    ap.add_argument("--max-states", type=int, default=40)
    ap.add_argument("--max-actions", type=int, default=200)
    ap.add_argument("--deep", type=int, default=4)
    ap.add_argument("--persona")
    ap.add_argument("--walk", help="analyse an existing walk directory without crawling")
    args = ap.parse_args(argv)
    try:
        if not args.walk and not args.targets:
            # From `glevels check`, the charter's runner names the entry URL in GLEVELS_URL.
            args.targets = os.environ.get("GLEVELS_URL", "").split()
        if not args.walk and not args.targets and args.src:
            # Source only: the hidden switches and opt-in defaults, without a crawl.
            if not Path(args.src).is_dir():
                raise ValueError(f"source directory does not exist: {args.src}")
            review = source_leads(args.src)
            findings = {"blocking": [], "review": review, "source_only": True}
            if args.out:
                Path(args.out).mkdir(parents=True, exist_ok=True)
                (Path(args.out) / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
            if args.json:
                print(json.dumps(findings, indent=2))
            else:
                print(f"optiontree: source only, {len(review)} for the reviewer (no crawl: give a URL for the option graph)")
                for line in review:
                    print("  " + line)
            return 0
        if not args.walk and not args.targets:
            raise ValueError("give a target or --walk DIR (or set GLEVELS_URL)")
        if args.max_states < 1 or args.max_actions < 1 or args.deep < 0:
            raise ValueError("crawl budgets must be positive and --deep non-negative")
        if args.src and not Path(args.src).is_dir():
            raise ValueError(f"source directory does not exist: {args.src}")
        out_dir = Path(args.out or tempfile.mkdtemp(prefix="glevels-optiontree-")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        walk_dir = Path(args.walk).resolve() if args.walk else out_dir
        if not args.walk:
            crawl(args.targets, out_dir, args.max_states, args.max_actions)
        walk = json.loads((walk_dir / "walk.json").read_text())
        if not isinstance(walk, dict) or not walk.get("states") or not isinstance(walk["states"], list):
            raise ValueError("walk.json has no captured states")
        routes = [s.strip() for s in Path(args.routes).read_text().splitlines() if s.strip() and not s.lstrip().startswith("#")] if args.routes else []
        findings = analyse(walk, walk_dir, args.src, routes, args.deep)
        tree = tree_markdown(walk, walk_dir, out_dir)
        if args.persona:
            (out_dir / "persona.md").write_text(persona_markdown(args.persona, walk.get("entry", [walk["states"][0]["url"]])[0]))
        (out_dir / "tree.md").write_text(tree)
        (out_dir / "findings.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False) + "\n")
        if args.json:
            print(json.dumps(findings, indent=2, ensure_ascii=False))
        else:
            report(findings, walk, out_dir, args.persona)
        return 1 if findings["blocking"] else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"impossible: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
