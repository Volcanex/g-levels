"""Sensor: spacing and layout faults a person spots at a glance. Gate correct.ui.spacing.

    python3 spacing.py [--out DIR] [--json] [--landscape] TARGET [TARGET ...]
                       [--viewports phone=390x844,desktop=1440x900]
                       [--states loaded,loading,error,long-text]
                       [--steps N] [--step-key ArrowRight] [--wait-for SELECTOR]
                       [--storage-state FILE] [--header 'Name: value']

A TARGET is a URL, an HTML file, or a g-site page (`pages/<dir>/content.html`). A g-site
page is compiled fresh into a temp dir and served through a small proxy in front of the
running g-site server (GSPACING_BASE, default http://127.0.0.1:8096), so the check sees
the edit, not the last deploy, and the page's own /api calls and assets still resolve.
A plain HTML file is served with its relative assets. Several targets are read as sibling
screens: their shared landmarks (header, nav, h1, main, footer) are compared.

Defaults: phone-s 320x568, phone 390x844, tablet 768x1024, desktop 1440x900.
--landscape adds phone-land 844x390. Loading (held fetch/XHR), error (HTTP 500) and
long-text (WCAG text spacing and longer labels) run at 390px only. --states loaded
selects just the ordinary page. --steps measures each key press as a sibling screen.
One Chromium renders them in turn. Fixed/sticky occlusion at the page ends blocks;
stress-state faults are leads except sideways scrolling and text overlapping text.
Screenshots and findings.json land in --out (default: a temp dir, printed at the end).

Exit 0: no clear faults. Exit 1: clear faults, listed under "blocking". Exit 2: could not
run (no memory, no server, no browser), printed as "impossible: ...". Leads that need
judgement are listed under "for the reviewer" and never fail the run on their own.
"""
import argparse
import fcntl
import html.parser
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import vendor
except ImportError:
    import vendor

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("GSPACING_BASE", "http://127.0.0.1:8096").rstrip("/")
MIN_FREE_MB = int(os.environ.get("GSPACING_MIN_FREE_MB", "600"))
LOCK = Path(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"))
HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "upgrade"}
IMPECCABLE_LAYOUT = {
    "text-overflow", "text-occlusion", "clipped-overflow-container", "edge-flush-cards",
    "heading-rhythm", "first-viewport-column-overflow", "monotonous-spacing", "nested-cards",
    "line-length", "cramped-padding", "body-text-viewport-edge", "numbered-section-labels",
}


def impossible(msg):
    print(f"impossible: {msg}")
    sys.exit(2)


def free_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def gsite_page(path):
    """(repo, page_dir) when path is a g-site style pages/<dir>/content.html, else None."""
    p = Path(path).resolve()
    if p.name not in ("content.html", "content.md") or p.parent.parent.name != "pages":
        return None
    repo = p.parent.parent.parent
    return (repo, p.parent) if (repo / "compile.py").is_file() else None


def compile_gsite(repo, page_dir, tmp):
    """Compile one page with the repo's own compile_page into tmp, never into output/."""
    py = repo / ".venv/bin/python"
    py = str(py) if py.exists() else sys.executable
    code = (
        "import json, pathlib, sys\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "import compile as c\n"
        f"c.OUTPUT_DIR = pathlib.Path({str(tmp)!r})\n"
        f"out, cfg, err = c.compile_page(pathlib.Path({str(page_dir)!r}), c.load_shell())\n"
        "print('GSPACING' + json.dumps({'out': str(out) if out else None, 'slug': (cfg or {}).get('slug'), 'err': err}))\n"
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([py, "-c", code], cwd=repo, env=env, capture_output=True, text=True)
    line = next((x for x in p.stdout.splitlines() if x.startswith("GSPACING")), None)
    if p.returncode or not line:
        impossible(f"could not compile {page_dir.name}: {(p.stderr or p.stdout).strip()[-400:]}")
    info = json.loads(line[len("GSPACING"):])
    if not info["out"]:
        impossible(f"{page_dir.name} did not compile (draft, or no config.json)")
    return info["slug"] or page_dir.name, Path(info["out"])


class Proxy(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve(pages, upstream):
    """Serve {url path: html file} and pass every other request to upstream (or a static root)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _pass(self):
            path = self.path.split("?")[0]
            if path in pages:
                body = pages[path].read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if isinstance(upstream, Path):   # static root for a plain HTML file
                f = (upstream / path.lstrip("/")).resolve()
                if upstream in f.parents and f.is_file():
                    body = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    ctype = {".css": "text/css", ".js": "text/javascript", ".mjs": "text/javascript",
                             ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2",
                             ".json": "application/json", ".html": "text/html"}.get(f.suffix, "application/octet-stream")
                    self.send_header("Content-Type", ctype)
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                else:
                    self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host", "accept-encoding"}}
            req = urllib.request.Request(upstream + self.path, data=data, method=self.command, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as e:
                resp = e
            except Exception:
                self.send_error(502)
                return
            body = resp.read()
            self.send_response(resp.status if hasattr(resp, "status") else resp.code)
            for k, v in resp.headers.items():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _pass

    srv = Proxy(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def resolve(targets, tmp):
    """Turn targets into URLs, starting whatever servers they need."""
    urls, servers, gsite = [], [], {}
    for t in targets:
        if t.startswith(("http://", "https://")):
            urls.append(t)
            continue
        if not Path(t).exists():
            impossible(f"no such file or URL: {t}")
        g = gsite_page(t)
        if g:
            slug, out = compile_gsite(*g, tmp)
            gsite[f"/{slug}"] = out
            urls.append(f"/{slug}")
            continue
        f = Path(t).resolve()
        if f.suffix not in (".html", ".htm"):
            impossible(f"{t} is not a page: give a URL, an HTML file or a g-site pages/<dir>/content.html")
        root = f.parent

        class Assets(html.parser.HTMLParser):
            def handle_starttag(self, tag, attrs):
                nonlocal root
                attrs = dict(attrs)
                key = "href" if tag == "link" else "src" if tag in ("script", "img", "source", "video", "audio", "iframe") else None
                ref = urllib.parse.urlsplit(attrs.get(key, ""))
                if ref.scheme or ref.netloc or not ref.path or ref.path.startswith("/"):
                    return
                asset = (f.parent / urllib.parse.unquote(ref.path)).resolve()
                if asset.is_file():
                    root = Path(os.path.commonpath((root, asset.parent)))

        Assets().feed(f.read_text(errors="replace"))
        srv, base = serve({}, root)
        servers.append(srv)
        urls.append(f"{base}/{f.relative_to(root).as_posix()}")
    if gsite:
        try:
            urllib.request.urlopen(BASE + "/", timeout=5)
        except Exception as e:
            impossible(f"the g-site server at {BASE} is not answering ({e}); start it or set GSPACING_BASE")
        srv, base = serve(gsite, BASE)
        servers.append(srv)
        urls = [base + u if u.startswith("/") else u for u in urls]
    return urls, servers


def compare_landmarks(results):
    """Compare sibling screens, keeping stress states out of ordinary comparisons."""
    out, by_vp = [], {}
    for r in results:
        if r.get("landmarks") and r.get("state", "loaded") == "loaded":
            by_vp.setdefault(r["vp"], []).append(r)
    for vp, rs in by_vp.items():
        for key in ("header", "nav", "[role=navigation]", "[role=banner]", "h1", "main", "footer", "screen-title"):
            groups = {}
            for r in rs:
                if key in r["landmarks"]:
                    groups.setdefault(r["url"] if key == "screen-title" else None, []).append((r, r["landmarks"][key]))
            for have in groups.values():
                if key == "screen-title" and not any(r.get("step") for r, _ in have):
                    continue
                if len(have) < 2:
                    continue
                first, (x0, y0, w0, _) = have[0]
                for r, rect in have[1:]:
                    x, y, w, _ = rect
                    worst = max(abs(x - x0), abs(w - w0) if key not in ("h1", "screen-title") else 0,
                                abs(y - y0) if key != "footer" else 0)
                    if worst <= 2:
                        continue
                    chrome = key in ("header", "nav", "[role=navigation]", "[role=banner]")
                    # Slides and wizard steps change layout on purpose (a cover, a section, a split), so
                    # a title moving between steps is a lead; between sibling pages it is a fault.
                    stepped = r.get("step") or first.get("step")
                    sev = "block" if (chrome and worst > 4) or (key in ("h1", "screen-title") and worst >= 48
                                                                and not stepped) else "review"
                    title = key == "screen-title"
                    out.append({"check": "titles-move-between-siblings" if title else "landmark-moves-between-screens",
                                "severity": sev, "viewport": vp, "url": r["url"], "render": r.get("render", "loaded"),
                                "selector": r.get("landmarkSamples", {}).get(key, key), "rect": rect,
                                "detail": f"{key} moves up to {worst}px across sibling screens: "
                                          f"{first['url']} {first.get('render', 'loaded')} at x={x0} y={y0} w={w0}; "
                                          f"{r.get('render', 'loaded')} at x={x} y={y} w={w}"})
    return out


def compare_states(results):
    for r in results:
        if r.get("state") not in ("loading", "error"):
            continue
        loaded = next((x for x in results if x["url"] == r["url"] and x["vp"] == r["vp"]
                       and x.get("render") == "loaded"), None)
        if not loaded:
            continue
        before = {a["selector"]: a for a in loaded.get("anchors", [])}
        for a in r.get("anchors", []):
            b = before.get(a["selector"])
            if not b or a["sample"] != b["sample"] or abs(a["rect"][1] - b["rect"][1]) <= 4:
                continue
            r["findings"].append({"check": "state-layout-changes", "severity": "review",
                                  "selector": f"{a['selector']} \"{a['sample']}\"", "rect": a["rect"],
                                  "detail": f"y={a['rect'][1]} in {r['render']}, y={b['rect'][1]} when loaded; "
                                            "check that the data slot reserves its space"})
            if sum(f["check"] == "state-layout-changes" for f in r["findings"]) >= 3:
                break


def impeccable(results, authenticated=False):
    """A second opinion, after our browser closes and while the servers and lock live."""
    noted = False
    for r in results:
        if r.get("render") != "loaded" or r.get("error") or (r.get("vp") not in ("phone", "desktop")
                and r["viewport"].split()[-1].split("x")[0] not in ("390", "1440")):
            continue
        try:
            if authenticated:
                findings, why = None, "the optional detector cannot inherit --header or --storage-state"
            elif free_mb() < MIN_FREE_MB:
                findings, why = None, "not enough free memory for the optional browser"
            else:
                findings, why = vendor.impeccable(r["url"], r["viewport"].split()[-1])
        except Exception as e:
            findings, why = None, f"{type(e).__name__} while running the optional detector"
        if why:
            if not noted:
                r.setdefault("notes", []).append(f"impeccable: did not run: {why}")
                noted = True
            continue
        for f in findings or []:
            rule = f.get("antipattern")
            if rule not in IMPECCABLE_LAYOUT:
                continue
            location = next((x for x in r.get("layout", []) if x["selector"] == f.get("selector", "body")), {})
            r["findings"].append({"check": f"impeccable: {rule}", "severity": "review",
                                  "selector": location.get("sample") or f.get("selector") or "body",
                                  "detail": f.get("snippet") or f.get("message") or f.get("name") or rule,
                                  "rect": location.get("rect") or f.get("rect")})


def report(results, extra, out_dir):
    block, review, failed = [], [], []
    for r in results:
        tag = f"{r['url']}  {r['viewport']}  {r.get('render', 'loaded')}"
        if r.get("error"):
            failed.append(f"{tag}: {r['error']}")
            continue
        bs, rs = [], []
        if r.get("status", 0) and r["status"] >= 400:
            bs.append(f"HTTP {r['status']}")
        groups = {}
        fs = r.get("findings", []) + [f for f in extra if f.get("url") == r["url"]
              and f["viewport"] == r["vp"] and f.get("render") == r.get("render", "loaded")]
        for f in fs:
            groups.setdefault((f["severity"], f["check"]), []).append(f)
        for (sev, check), fs in groups.items():
            n = r.get("counts", {}).get(check, len(fs))
            lines = [check + (f" ({n} found, first 3)" if len(fs) > 3 else "") + ":"]
            for f in fs[:3]:
                lines.append(f"  {f['selector']} {f['detail']}" + (f"  at {f['rect']}" if f.get("rect") else ""))
            (bs if sev == "block" else rs).append("\n    ".join(lines))
        rh = r.get("rhythm")
        if rh:
            rows = "; ".join(f"{x['selector']} ({x['sample']})" for x in rh["rows"][:4])
            rs.append(f"grid: {rh['local']} of {rh['edgesChecked']} edges off the 4px grid in their container, "
                      f"{rh['half']} on half pixels, {rh['gaps']} of {rh['gapPairs']} stacked gaps off the spacing scale"
                      + (f". Worst: {rows}" if rows else ""))
        if r.get("cls", 0) > 0.05:
            line = f"layout-shift: {r['cls']} while loading (things already on screen moved)"
            (bs if r["cls"] > 0.25 and r.get("state") == "loaded" else rs).append(line)
        if r.get("consoleErrors"):
            rs.append("console errors: " + " | ".join(r["consoleErrors"][:3]))
        rs.extend(r.get("notes", []))
        if bs:
            block.append(tag + "\n    " + "\n    ".join(bs))
        if rs:
            review.append(tag + "\n    " + "\n    ".join(rs))

    for error in failed:
        print(f"impossible: {error}")
    print(f"spacing: {len(results)} renders, {len(block)} blocking, {len(review)} for the reviewer")
    if block:
        print("\nblocking (clear faults, fix these):")
        for b in block:
            print("  " + b)
    if review:
        print("\nfor the reviewer (leads, judge them against the screenshots):")
        for b in review:
            print("  " + b)
    print("\nscreenshots, for the review half of the gate:")
    for r in results:
        shots = r.get("screenshots", [])
        more = f", {r['notShown']}px not shown" if r.get("notShown") else ""
        print(f"  {r['url']}  {r['viewport']}  {r.get('render', 'loaded')}: {len(shots)} tiles{more}")
        for s in shots:
            print(f"    {s}")
    print(f"\nfindings: {out_dir}/findings.json")
    return 2 if failed else 1 if block else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--out", help="where screenshots and findings.json go (default: a new temp dir)")
    ap.add_argument("--json", action="store_true", help="print findings.json instead of the report")
    ap.add_argument("--viewports", default="phone-s=320x568,phone=390x844,tablet=768x1024,desktop=1440x900")
    ap.add_argument("--landscape", action="store_true", help="also render phone-land 844x390")
    ap.add_argument("--states", default="loaded,loading,error,long-text", help="comma-separated states; stress at 390px only")
    ap.add_argument("--steps", type=int, default=0, help="measure after each of N key presses")
    ap.add_argument("--step-key", default="ArrowRight")
    ap.add_argument("--storage-state", help="Playwright storage state JSON")
    ap.add_argument("--header", action="append", default=[], help="Name: value (repeatable; values are never printed)")
    ap.add_argument("--wait-for", help="wait for a visible selector before measuring")
    a = ap.parse_args()
    if a.steps < 0:
        impossible("--steps must be zero or greater")
    if not a.states or any(s not in ("loaded", "loading", "error", "long-text") for s in a.states.split(",")):
        impossible("--states must choose loaded,loading,error,long-text")
    try:
        for v in a.viewports.split(","):
            name, size = v.split("=")
            w, h = map(int, size.split("x"))
            if not name or min(w, h) < 1:
                raise ValueError()
    except ValueError:
        impossible("--viewports must use name=WIDTHxHEIGHT, separated by commas")
    if a.landscape and "phone-land=" not in a.viewports:
        a.viewports += ",phone-land=844x390"
    headers, secrets = {}, []
    for header in a.header:
        name, sep, value = header.partition(":")
        if not sep or not name.strip() or any(c in header for c in "\r\n"):
            impossible("--header must use Name: value")
        headers[name.strip()] = value.strip()
        if value.strip():
            secrets.append(value.strip())
    options = {"headers": headers, "waitFor": a.wait_for}
    if a.storage_state:
        try:
            options["storageState"] = json.loads(Path(a.storage_state).read_text())
            secrets += [x["value"] for x in options["storageState"].get("cookies", []) if x.get("value")]
            secrets += [x["value"] for origin in options["storageState"].get("origins", [])
                        for x in origin.get("localStorage", []) if x.get("value")]
        except (OSError, ValueError, TypeError, AttributeError):
            impossible("could not read --storage-state as Playwright JSON")

    def redact(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        return value

    if shutil.which("node") is None:
        impossible("node is not installed")
    out_dir = Path(a.out or tempfile.mkdtemp(prefix="glevels-spacing-")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="glevels-spacing-build-") as tmp, open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mb = free_mb()
        if mb < MIN_FREE_MB:
            impossible(f"only {mb} MB of memory free, a browser needs {MIN_FREE_MB}. Try again when the host is quieter")
        urls, servers = resolve(a.targets, Path(tmp))
        t0 = time.time()
        try:
            cmd = ["node", str(HERE / "spacing.mjs"), "--out", str(out_dir), "--viewports", a.viewports,
                   "--states", a.states, "--steps", str(a.steps), "--step-key", a.step_key, "--options-stdin", *urls]
            try:
                p = subprocess.run(cmd, input=json.dumps(options), capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                impossible("the browser run timed out after 600s")
            if p.returncode:
                impossible(redact(f"the browser run failed: {(p.stderr or p.stdout).strip()[-600:]}"))
            results = json.loads((out_dir / "findings.json").read_text())
            impeccable(results, authenticated=bool(headers or a.storage_state))
        finally:
            for s in servers:
                s.shutdown()
    compare_states(results)
    for r in results:
        r.pop("layout", None)
        r["url"] = a.targets[urls.index(r["url"])]
    results = redact(results)
    extra = compare_landmarks(results)
    for r in results:
        r.setdefault("findings", []).extend(f for f in extra if f.get("url") == r["url"]
            and f["viewport"] == r["vp"] and f.get("render") == r.get("render", "loaded"))
    (out_dir / "findings.json").write_text(json.dumps(results, indent=2) + "\n")
    if a.json:
        print(json.dumps({"renders": results, "siblings": extra, "seconds": round(time.time() - t0, 1)}, indent=2))
        failed = any(r.get("error") for r in results)
        blocked = any(f["severity"] == "block" for r in results for f in r.get("findings", [])) or any(
            r.get("status", 0) and r["status"] >= 400 or r.get("state") == "loaded" and r.get("cls", 0) > 0.25 for r in results
        ) or any(f["severity"] == "block" for f in extra)
        sys.exit(2 if failed else 1 if blocked else 0)
    sys.exit(report(results, [], out_dir))


if __name__ == "__main__":
    main()
