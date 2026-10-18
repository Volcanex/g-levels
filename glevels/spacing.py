"""Sensor: spacing and layout faults a person spots at a glance. Gate correct.ui.spacing.

    python3 spacing.py [--out DIR] [--json] TARGET [TARGET ...]

A TARGET is a URL, an HTML file, or a g-site page (`pages/<dir>/content.html`). A g-site
page is compiled fresh into a temp dir and served through a small proxy in front of the
running g-site server (GSPACING_BASE, default http://127.0.0.1:8096), so the check sees
the edit, not the last deploy, and the page's own /api calls and assets still resolve.
A plain HTML file is served from its own directory. Several targets are read as sibling
screens: their shared landmarks (header, nav, h1, main, footer) are compared.

Every target renders at desktop 1280x800 and phone 390x844 in one Chromium (spacing.mjs).
Screenshots and findings.json land in --out (default: a temp dir, printed at the end).

Exit 0: no clear faults. Exit 1: clear faults, listed under "blocking". Exit 2: could not
run (no memory, no server, no browser), printed as "impossible: ...". Leads that need
judgement are listed under "for the reviewer" and never fail the run on their own.
"""
import argparse
import fcntl
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
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("GSPACING_BASE", "http://127.0.0.1:8096").rstrip("/")
MIN_FREE_MB = int(os.environ.get("GSPACING_MIN_FREE_MB", "600"))
LOCK = Path(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"))
HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "upgrade"}


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
        srv, base = serve({}, f.parent)
        servers.append(srv)
        urls.append(f"{base}/{f.name}")
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
    """Sibling screens: the same landmark should sit in the same place on each."""
    out = []
    by_vp = {}
    for r in results:
        if r.get("landmarks"):
            by_vp.setdefault(r["vp"], []).append(r)
    for vp, rs in by_vp.items():
        if len(rs) < 2:
            continue
        for key in ("header", "nav", "[role=navigation]", "[role=banner]", "h1", "main", "footer"):
            have = [(r["url"], r["landmarks"][key]) for r in rs if key in r["landmarks"]]
            if len(have) < 2:
                continue
            x0, y0, w0, _ = have[0][1]
            worst = max(max(abs(x - x0), abs(w - w0), abs(y - y0) if key != "footer" else 0) for _, (x, y, w, _) in have)
            if worst <= 2:
                continue
            chrome = key in ("header", "nav", "[role=navigation]", "[role=banner]")
            sev = "block" if (chrome and worst > 4) or (key == "h1" and worst >= 48) else "review"
            where = ", ".join(f"{u.rsplit('/', 1)[-1] or u} at x={x} y={y} w={w}" for u, (x, y, w, _) in have)
            out.append({"check": "landmark-moves-between-screens", "severity": sev, "viewport": vp,
                        "selector": key, "detail": f"{key} moves up to {worst}px across sibling screens: {where}"})
    return out


def report(results, extra, out_dir):
    block, review = [], []
    for r in results:
        tag = f"{r['url']}  {r['viewport']}"
        if r.get("error"):
            block.append(f"{tag}\n    did not render: {r['error']}")
            continue
        if r.get("status") and r["status"] >= 400:
            block.append(f"{tag}\n    HTTP {r['status']}")
        groups = {}
        for f in r.get("findings", []):
            groups.setdefault((f["severity"], f["check"]), []).append(f)
        for (sev, check), fs in groups.items():
            n = r.get("counts", {}).get(check, len(fs)) if len(groups) else len(fs)
            lines = [f"{tag}\n    {check}" + (f" ({n} found, first 3)" if len(fs) > 3 else "") + ":"]
            for f in fs[:3]:
                lines.append(f"      {f['selector']} {f['detail']}" + (f"  at {f['rect']}" if f.get("rect") else ""))
            (block if sev == "block" else review).append("\n".join(lines))
        rh = r.get("rhythm")
        if rh:
            rows = "; ".join(f"{x['selector']} ({x['sample']})" for x in rh["rows"][:4])
            review.append(f"{tag}\n    grid: {rh['local']} of {rh['edgesChecked']} edges off the 4px grid in their container, "
                          f"{rh['half']} on half pixels, {rh['gaps']} of {rh['gapPairs']} stacked gaps off the spacing scale"
                          + (f". Worst: {rows}" if rows else ""))
        if r.get("cls", 0) > 0.25:   # web.dev's "poor", and g-design's reserved-slot rule
            block.append(f"{tag}\n    layout-shift: {r['cls']} while loading, so things already on screen moved. "
                         "Reserve each slot's size before its data arrives")
        elif r.get("cls", 0) > 0.05:
            review.append(f"{tag}\n    layout-shift: {r['cls']} while loading (things already on screen moved)")
        if r.get("consoleErrors"):
            review.append(f"{tag}\n    console errors: " + " | ".join(r["consoleErrors"][:3]))
    for f in extra:
        (block if f["severity"] == "block" else review).append(f"sibling screens  {f['viewport']}\n    {f['check']}: {f['detail']}")

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
        more = f", {r['notShown']}px below not shown" if r.get("notShown") else ""
        print(f"  {r['url']}  {r['viewport']}: {len(shots)} tiles{more}")
        for s in shots:
            print(f"    {s}")
    print(f"\nfindings: {out_dir}/findings.json")
    return 1 if block else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--out", help="where screenshots and findings.json go (default: a new temp dir)")
    ap.add_argument("--json", action="store_true", help="print findings.json instead of the report")
    ap.add_argument("--viewports", default="desktop=1280x800,phone=390x844")
    a = ap.parse_args()

    if shutil.which("node") is None:
        impossible("node is not installed")
    out_dir = Path(a.out or tempfile.mkdtemp(prefix="glevels-spacing-")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="glevels-spacing-build-") as tmp, open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # one browser at a time on this host
        mb = free_mb()
        if mb < MIN_FREE_MB:
            impossible(f"only {mb} MB of memory free, a browser needs {MIN_FREE_MB}. Try again when the host is quieter")
        urls, servers = resolve(a.targets, Path(tmp))
        t0 = time.time()
        p = subprocess.run(["node", str(HERE / "spacing.mjs"), "--out", str(out_dir), "--viewports", a.viewports, *urls],
                           capture_output=True, text=True, timeout=600)
        for s in servers:
            s.shutdown()
        if p.returncode:
            impossible(f"the browser run failed: {(p.stderr or p.stdout).strip()[-600:]}")
    results = json.loads((out_dir / "findings.json").read_text())
    # name the targets as the caller gave them, not by the proxy port
    for r in results:
        i = urls.index(r["url"])
        r["url"] = a.targets[i]
    extra = compare_landmarks(results)
    if a.json:
        print(json.dumps({"renders": results, "siblings": extra, "seconds": round(time.time() - t0, 1)}, indent=2))
        sys.exit(1 if any(f["severity"] == "block" for r in results for f in r.get("findings", [])) or
                 any(f["severity"] == "block" for f in extra) else 0)
    sys.exit(report(results, extra, out_dir))


if __name__ == "__main__":
    main()
