"""Sensor: the page in every browser engine. Gate correct.ui.every-engine.

    python3 engines.py [--engines chromium,webkit] [--viewports 320x720,390x844,1440x900]
                       [--schemes light,dark] [--out DIR] [--json] [URL ...]

With no URL, GLEVELS_URL is read (URLs separated by spaces). Each page opens in every engine, at
every viewport, in each colour scheme, and is measured once its fonts and requests have settled.
WebKit is the engine inside Safari on every iPhone, so a page that only ever ran in Chromium has
not been tried on most phones.

Blocking: an uncaught exception, a console error, a same-origin request that fails or answers
4xx or 5xx, a page the reader can scroll sideways (with the elements that stick out), a page with
no text and no canvas.
For the reviewer: a page whose background is the same in light and dark, a page more than a fifth
taller in one engine than another, cross-origin failures.

Exit 0: nothing blocking. 1: blocking faults. 2: could not run (no URL, Playwright or an engine
missing, too little memory), printed as "impossible: ...". Screenshots land in --out (default: a
new temp dir, printed at the end). Uses Python Playwright, whose browsers match its own version.
"""
import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

LOCK = Path(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"))
MIN_FREE_MB = 700

MEASURE = """() => {
  const de = document.documentElement, W = de.clientWidth
  window.scrollTo(100000, window.scrollY)
  const sideways = window.scrollX
  window.scrollTo(0, window.scrollY)
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    [...el.classList].slice(0, 3).map(c => '.' + c).join('')
  const out = []
  if (sideways > 0) {
    const clips = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const s = getComputedStyle(p); if (s.overflowX !== 'visible' && p.getBoundingClientRect().right <= W + 1) return true } return false }
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect()
      if (!r.width || r.right <= W + 1 || clips(el)) continue
      const p = el.parentElement
      if (p && p !== document.body && p.getBoundingClientRect().right > W + 1 && !clips(p)) continue
      out.push(name(el) + ' ends at ' + Math.round(r.right) + 'px')
      if (out.length >= 5) break
    }
  }
  return {width: W, scrollWidth: de.scrollWidth, sideways, over: out, height: de.scrollHeight,
    root: getComputedStyle(de).backgroundColor, body: getComputedStyle(document.body).backgroundColor,
    text: document.body.innerText.trim().length, canvas: document.querySelectorAll('canvas').length}
}"""


def impossible(msg):
    print(f"impossible: {msg}")
    sys.exit(2)


def free_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def slug(url):
    p = urlsplit(url)
    return (re.sub(r"[^a-z0-9]+", "-", (p.path or "/").lower()).strip("-") or "home")[:40]


def visit(browser, engine, url, scheme, size, out):
    w, h = size
    origin = urlsplit(url).netloc
    ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
    page = ctx.new_page()
    found = {"block": [], "lead": []}
    where = f"{engine} {w}px {scheme}"

    def own(u):
        return urlsplit(u).netloc == origin

    page.on("pageerror", lambda e: found["block"].append(f"{where}: uncaught {str(e).splitlines()[0][:200]}"))

    def console(m):
        if m.type != "error":
            return
        loc = m.location or {}
        src = loc.get("url") or ""
        text = m.text.splitlines()[0][:200] if m.text else ""
        # A failed load is reported once, by the response or requestfailed handler.
        if text.startswith("Failed to load resource"):
            return
        (found["block"] if not src or own(src) else found["lead"]).append(f"{where}: console error {text}" + (f" ({src})" if src else ""))

    page.on("console", console)

    def response(r):
        if r.status >= 400:
            (found["block"] if own(r.url) else found["lead"]).append(f"{where}: {r.status} {r.url}")

    page.on("response", response)

    def failed(r):
        err = r.failure or "failed"
        if "ERR_ABORTED" in err or "cancelled" in err.lower():
            return
        (found["block"] if own(r.url) else found["lead"]).append(f"{where}: request failed {r.url} ({err})")

    page.on("requestfailed", failed)
    m = None
    try:
        page.goto(url, wait_until="load", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            found["lead"].append(f"{where}: requests still running after 8 s")
        page.evaluate("document.fonts && document.fonts.ready.then(() => 1)")
        page.wait_for_timeout(600)
        m = page.evaluate(MEASURE)
        page.screenshot(path=str(out / f"{engine}-{slug(url)}-{scheme}-{w}.png"))
    except Exception as e:
        found["block"].append(f"{where}: the page did not load ({str(e).splitlines()[0][:200]})")
    finally:
        ctx.close()
    if m:
        if m["sideways"] > 0:
            found["block"].append(f"{where}: scrolls sideways by {m['sideways']}px (page {m['scrollWidth']}px on a {m['width']}px screen)"
                                  + (": " + "; ".join(m["over"]) if m["over"] else ""))
        if m["text"] < 20 and not m["canvas"]:
            found["block"].append(f"{where}: blank page ({m['text']} characters of text, no canvas)")
    return m, found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--engines", default="chromium,webkit")
    ap.add_argument("--viewports", default="320x720,390x844,1440x900")
    ap.add_argument("--schemes", default="light,dark")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    urls = a.urls or os.environ.get("GLEVELS_URL", "").split()
    if not urls:
        impossible("no URL: pass one, or run from glevels check with a .glevels/pages.json")
    for u in urls:
        if urlsplit(u).scheme not in ("http", "https"):
            impossible(f"{u} is not an http URL")
    try:
        sizes = [tuple(int(n) for n in v.split("x")) for v in a.viewports.split(",")]
    except ValueError:
        impossible(f"bad --viewports {a.viewports!r}, want WxH,WxH")
    engines = [e.strip() for e in a.engines.split(",") if e.strip()]
    schemes = [s.strip() for s in a.schemes.split(",") if s.strip()]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        impossible("Python Playwright is not installed here (it is on h, with Chromium and WebKit)")
    free = free_mb()
    if free is not None and free < MIN_FREE_MB:
        impossible(f"only {free} MB of memory free, {MIN_FREE_MB} needed")
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="glevels-engines-"))
    out.mkdir(parents=True, exist_ok=True)

    block, lead, runs = [], [], []
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sync_playwright() as p:
            for engine in engines:
                kind = getattr(p, engine, None)
                if kind is None:
                    impossible(f"Playwright has no engine called {engine}")
                try:
                    browser = kind.launch()
                except Exception as e:
                    impossible(f"{engine} will not start here: {str(e).splitlines()[0][:200]}")
                try:
                    for url in urls:
                        for scheme in schemes:
                            for size in sizes:
                                m, found = visit(browser, engine, url, scheme, size, out)
                                runs.append({"engine": engine, "url": url, "scheme": scheme, "width": size[0], "m": m})
                                block += [f"{url} {x}" for x in found["block"]]
                                lead += [f"{url} {x}" for x in found["lead"]]
                finally:
                    browser.close()

    done = [r for r in runs if r["m"]]
    if "light" in schemes and "dark" in schemes:
        for e in engines:
            for url in urls:
                for w, _ in sizes:
                    pair = {r["scheme"]: r["m"] for r in done if r["engine"] == e and r["url"] == url and r["width"] == w}
                    if len(pair) == 2 and (pair["light"]["root"], pair["light"]["body"]) == (pair["dark"]["root"], pair["dark"]["body"]):
                        lead.append(f"{url} {e} {w}px: the same background in light and dark ({pair['light']['body']}). Fine only if the page has one look on purpose.")
    for url in urls:
        for scheme in schemes:
            for w, _ in sizes:
                hs = {r["engine"]: r["m"]["height"] for r in done if r["url"] == url and r["scheme"] == scheme and r["width"] == w}
                if len(hs) > 1 and min(hs.values()) > 0 and max(hs.values()) / min(hs.values()) > 1.2:
                    lead.append(f"{url} {w}px {scheme}: page height differs by engine " + ", ".join(f"{k} {v}px" for k, v in hs.items()) + ". Compare the screenshots.")

    block = list(dict.fromkeys(block))
    lead = list(dict.fromkeys(lead))
    if a.json:
        print(json.dumps({"engines": engines, "urls": urls, "loads": len(runs), "blocking": block, "leads": lead, "out": str(out)}))
    else:
        print(f"{len(runs)} loads: {len(urls)} page(s) x {', '.join(engines)} x {len(sizes)} widths x {', '.join(schemes)}")
        if block:
            print("blocking:")
            for b in block:
                print(f"  {b}")
        if lead:
            print("for the reviewer:")
            for l in lead:
                print(f"  {l}")
        if not block and not lead:
            print("clean in every engine")
        print(f"screenshots: {out}")
    sys.exit(1 if block else 0)


if __name__ == "__main__":
    main()
