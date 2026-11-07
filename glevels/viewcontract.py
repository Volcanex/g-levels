#!/usr/bin/env python3
"""The view contract sensor for correct.ui.view-contract (g-command-web).

    python3 viewcontract.py <repo> [--page SLUG ...] [--table]

Reads every pages/<slug>/ folder (pages/_* skipped) statically and reports, per page:
  header      no shared page head (PageHead / .page-head), or page CSS that respaces the
              title (.page-head / .page-title / h1 margin or padding), the page gutter
              (.content-page / .site-main padding or margin) or offsets with raw px top
  tokens      raw hex colours, or raw px (anything but 0 and 1px hairlines) in page CSS
  transition  a page-local entry animation: an @keyframes whose start fades or moves in
  states      fetches data but lacks a loading, an error or an empty state
CSS is each *.css in the folder, <style> blocks and style="" attributes in its *.html, and
style strings in its *.js. Scripts are its *.js, inline <script>s and /js/*.js it loads.
Pages whose body_class is full-dash-page or login-page draw no page head by design.
Exit 0 every page clean, 1 any finding, 2 cannot run.
"""
import argparse
import json
import re
import sys
from pathlib import Path

STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
STYLE_ATTR = re.compile(r"""style\s*=\s*["'`]([^"'`]*)["'`]""", re.I)
JS_STYLE = re.compile(r"""style(?:\s*=\s*\$?\{?|\.cssText\s*=\s*)\s*["'`]([^"'`]*)["'`]""")
SCRIPT_BLOCK = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
SCRIPT_SRC = re.compile(r"""<script[^>]*\bsrc=["']/js/([\w./-]+?\.js)""", re.I)
COMMENT = re.compile(r"/\*.*?\*/", re.S)
HEX = re.compile(r"(?<![\w&#-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
PX = re.compile(r"(?<![\w.-])(\d*\.?\d+)px\b")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
KEYFRAMES = re.compile(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", re.S)
FETCH = re.compile(r"\bfetch\s*\(|\bgetJson\s*\(|\buseJson\s*\(|\bnew EventSource\s*\(|\bXMLHttpRequest\b")
LOADING = re.compile(r"\bLoaded\b|SkeletonRows|skeleton|aria-busy|\bLoading\b|\bloading\b|spinner", re.I)
ERROR = re.compile(r"\bLoaded\b|ErrorState|state-error|\.catch\s*\(|\bcatch\s*[({]|onerror|\berror\b", re.I)
EMPTY = re.compile(r"\bEmpty\b|class=[\"'][^\"']*\bempty\b|\.empty\b|\bNo \w+|Nothing\b|\bnone yet\b|\.length\s*===?\s*0|!\w+\.length", re.I)
HEAD = re.compile(r"PageHead|page-head")
NO_HEAD_CLASSES = {"full-dash-page", "login-page"}


def sources(page: Path, static_js: Path):
    css, js = [], []
    for f in sorted(page.rglob("*")):
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        rel = f.relative_to(page).as_posix()
        if f.suffix == ".css":
            css.append((rel, text))
        elif f.suffix == ".html":
            css += [(rel, b) for b in STYLE_BLOCK.findall(text)]
            css += [(rel, a) for a in STYLE_ATTR.findall(STYLE_BLOCK.sub("", text))]
            js += [(rel, b) for b in SCRIPT_BLOCK.findall(text)]
            for src in SCRIPT_SRC.findall(text):
                p = static_js / src
                if p.is_file():
                    js.append((f"/js/{src}", p.read_text(errors="replace")))
        elif f.suffix in (".js", ".mjs"):
            js.append((rel, text))
            css += [(rel, s) for s in JS_STYLE.findall(text)]
    return css, js


def check_page(page: Path, static_js: Path) -> dict:
    cfg = json.loads((page / "config.json").read_text())
    css, js = sources(page, static_js)
    alltext = "\n".join(t for _, t in js) + "\n".join(
        (f.read_text(errors="replace") for f in page.rglob("*.html")))
    found = {"header": [], "tokens": [], "transition": [], "states": []}
    if not HEAD.search(alltext) and not (set(str(cfg.get("body_class", "")).split()) & NO_HEAD_CLASSES):
        found["header"].append("no shared page head (PageHead or .page-head)")
    hexes = px = 0
    for rel, text in css:
        text = COMMENT.sub("", text)
        for m in HEX.finditer(text):
            hexes += 1
        decl_blocks = RULE.findall(text) or [("", text)]
        for sel, body in decl_blocks:
            for decl in body.split(";"):
                if ":" not in decl:
                    continue
                prop, val = (s.strip().lower() for s in decl.split(":", 1))
                raw = [v for v in PX.findall(re.sub(r"var\([^)]*\)", "", val)) if float(v) not in (0, 1)]
                px += len(raw)
                s = sel.lower()
                if re.search(r"page-head|page-title|\bh1\b", s) and re.match(r"(margin|padding)", prop):
                    found["header"].append(f"{rel}: respaces the title ({sel.strip()[:50]} {{{prop}}})")
                if re.search(r"content-page|site-main|(^|[\s,])\.content\b", s) and re.match(r"(margin|padding)", prop):
                    found["header"].append(f"{rel}: overrides the page gutter ({sel.strip()[:50]} {{{prop}}})")
                if prop in ("top", "margin-top", "padding-top", "inset") and raw and re.search(r"\bbody\b|page|content|site-main|\bh1\b|title|data-page-app", s):
                    found["header"].append(f"{rel}: raw px {prop} ({sel.strip()[:50]}: {val[:30]})")
        for name, frames in KEYFRAMES.findall(text):
            start = re.search(r"(?:from|0%)\s*\{([^}]*)\}", frames)
            if start and re.search(r"opacity|translate|transform|scale", start.group(1)) and name != "g-vt-in":
                found["transition"].append(f"{rel}: @keyframes {name} enters the page")
    if hexes:
        found["tokens"].append(f"{hexes} raw hex colour(s)")
    if px:
        found["tokens"].append(f"{px} raw px value(s)")
    script = "\n".join(t for _, t in js)
    if FETCH.search(script):
        missing = [n for n, rx in (("loading", LOADING), ("error", ERROR), ("empty", EMPTY)) if not rx.search(script + alltext)]
        if missing:
            found["states"].append("fetches data with no " + "/".join(missing) + " state")
    # dedupe header lines, keep order
    found["header"] = list(dict.fromkeys(found["header"]))
    return {"slug": page.name, "found": found, "score": hexes + px + 5 * (len(found["header"]) + len(found["transition"]) + len(found["states"]))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--page", action="append")
    ap.add_argument("--table", action="store_true", help="also print a counts table and the worst offenders")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    pages = repo / "pages"
    if not pages.is_dir():
        print(f"{pages} is not a directory: this sensor reads a G-Command web checkout")
        return 2
    results = []
    for page in sorted(p for p in pages.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        if not (page / "config.json").is_file() or (a.page and page.name not in a.page):
            continue
        try:
            results.append(check_page(page, repo / "core/static/js"))
        except (OSError, ValueError) as e:
            print(f"{page.name}: cannot read ({e})")
            return 2
    warned = [r for r in results if any(r["found"].values())]
    for r in results:
        lines = [f"{k}: {v}" for k, vs in r["found"].items() for v in vs]
        print(("WARN " if lines else "PASS ") + r["slug"] + "".join("\n  " + l for l in lines))
    print(f"\n{len(results) - len(warned)} pass, {len(warned)} warn, of {len(results)} pages")
    if a.table:
        print("\nper check: " + ", ".join(f"{k} {sum(1 for r in results if r['found'][k])}" for k in ("header", "tokens", "transition", "states")))
        print("worst: " + ", ".join(f"{r['slug']} ({r['score']})" for r in sorted(warned, key=lambda r: -r["score"])[:8]))
    return 1 if warned else 0


if __name__ == "__main__":
    sys.exit(main())
