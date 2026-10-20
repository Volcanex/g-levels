"""Sensor: house design system and information hierarchy. Gate correct.ui.on-system.

    python3 onsystem.py [--system auto|gdesign|baseline|TOKENS_CSS] [--out DIR]
                       [--json] [--refs] TARGET [TARGET ...]

Targets are URLs, HTML files or g-site pages, compiled fresh by spacing.py.
Desktop 1440x900 and phone 390x844 screenshots are viewport tiles, never montages.
Exit 0: no clear faults. Exit 1: blocking missing system, foreign or failed fonts,
raw failure text or clear AI tells. Exit 2: impossible to run. Other findings are
leads for the reviewer and never fail alone. Artefacts land in --out (a temp dir
by default). --refs also renders the house reference screens for the judge.
"""
import argparse
import fcntl
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from . import spacing, vendor
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from glevels import spacing, vendor

IMPECCABLE_BLOCK = {
    "gradient-text", "ai-color-palette", "dark-glow", "side-tab",
    "bounce-easing", "radial-spotlight-glow",
}
IMPECCABLE_LEAD = {
    "gpt-thin-border-wide-shadow", "border-accent-on-rounded", "codex-grid-background",
    "kicker-above-heading", "hero-eyebrow-chip", "icon-tile-stack",
    "oversized-h1", "italic-serif-display", "flat-type-hierarchy",
    "extreme-negative-tracking", "all-caps-body", "gray-on-color",
    "pulsing-dot", "blinking-cursor", "marquee", "repeated-container-text",
    "justified-text", "skipped-heading", "low-contrast", "tiny-text", "undersized-ui-text",
}
IMPECCABLE_DROP = {   # house taste overrides: copy, assets and layout gates own these
    "overused-font", "em-dash-overuse", "theater-slop-phrase", "placeholder-image", "broken-image", "straggler",
} | spacing.IMPECCABLE_LAYOUT
IMPECCABLE_DROP_FOR = {"baseline": {"cream-palette"}}   # warm off-white is Baseline's own palette
HERE = Path(__file__).resolve().parent
DEFAULT_VIEWPORTS = "desktop=1440x900,phone=390x844"


def impeccable(urls, viewports):
    external = []
    for url in urls:
        for viewport in viewports.split(","):
            size = viewport.split("=", 1)[1]
            try:
                if spacing.free_mb() < spacing.MIN_FREE_MB:
                    findings, why = None, "not enough free memory for the optional browser"
                else:
                    findings, why = vendor.impeccable(url, size)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                findings, why = None, str(e)
            kept = [f for f in findings or [] if impeccable_level(f, "gdesign") or impeccable_level(f, "baseline")]
            external.append({"url": url, "size": size, "findings": kept, "why": why})
    return external


def impeccable_level(finding, system):
    rule = finding.get("antipattern", "")
    if rule in IMPECCABLE_DROP or rule in IMPECCABLE_DROP_FOR.get(system, ()):
        return None
    if rule in IMPECCABLE_BLOCK:
        return "review" if any(finding.get(k) for k in ("houseStyle", "sourceOnly", "decorativeMotion")) else "block"
    if rule in IMPECCABLE_LEAD:
        return "review"
    category = finding.get("category", "")
    if category in ("slop", "type", "color", "motion", "quality"):
        return "review"
    return None


def targets(targets, tmp, profiles):
    urls, servers = spacing.resolve(targets, tmp)
    root = Path(profiles["gdesign"]["referenceRoot"])
    for i, target in enumerate(targets):
        if target.startswith(("http://", "https://")):
            continue
        file = Path(target).resolve()
        if file.is_relative_to(root):
            srv, base = spacing.serve({}, root)
            servers.append(srv)
            urls[i] = base + "/" + str(file.relative_to(root))
    return urls, servers


def report(results, out_dir):
    block, review = [], []
    for r in results:
        tag = f"{r['url']}  {r['viewport']}  system {r.get('system', '?')}"
        bs, rs, groups = [], [], {}
        for f in r.get("findings", []):
            groups.setdefault((f["severity"], f["check"]), []).append(f)
        for (severity, check), fs in groups.items():
            n = r.get("counts", {}).get(check, len(fs))
            lines = [f"{check} ({n} found):"]
            for f in fs[:10 if check == "raw-css-value" else 5]:
                lines.append(f"  {f['selector']} {f.get('sample', '')!r}: {f['detail']}"
                             + (f" at {f['rect']} page px" if f.get("rect") else ""))
            (bs if severity == "block" else rs).append("\n    ".join(lines))
        rs.extend(r.get("notes", []))
        if bs:
            block.append(tag + "\n    " + "\n    ".join(bs))
        if rs:
            review.append(tag + "\n    " + "\n    ".join(rs))
    print(f"on-system: {len(results)} renders, {len(block)} blocking, {len(review)} for the reviewer")
    for title, lines in [("blocking (clear faults, fix these)", block),
                         ("for the reviewer (leads, judge against the screenshots)", review)]:
        if lines:
            print(f"\n{title}:")
            for line in lines:
                print("  " + line)
    print("\nscreenshots, for the review half of the gate:")
    for r in results:
        print(f"  {r['url']}  {r['viewport']}")
        for shot in r.get("screenshots", []):
            print(f"    {shot}")
        if r.get("notShown"):
            print(f"    {r['notShown']}px below not shown")
    refs = [ref for r in results for ref in r.get("references", [])]
    if refs:
        print("\nreference screens, for the judge:")
        for ref in refs:
            print(f"  {ref['url']}  {ref['viewport']}")
            for shot in ref.get("screenshots", []):
                print(f"    {shot}")
            if ref.get("error"):
                print(f"    did not render: {ref['error']}")
            if ref.get("notShown"):
                print(f"    {ref['notShown']}px below not shown")
    print(f"\nfindings: {out_dir}/findings.json")
    return 1 if block else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--system", default="auto")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refs", action="store_true")
    ap.add_argument("--viewports", default=DEFAULT_VIEWPORTS)
    a = ap.parse_args()
    try:
        for viewport in a.viewports.split(","):
            name, size = viewport.split("=")
            width, height = map(int, size.split("x"))
            if not name or width <= 0 or height <= 0:
                raise ValueError
    except ValueError:
        spacing.impossible("invalid viewports; use name=WIDTHxHEIGHT, separated by commas")
    if not shutil.which("node"):
        spacing.impossible("node is not installed")
    out_dir = Path(a.out or tempfile.mkdtemp(prefix="glevels-onsystem-")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles = json.loads((HERE / "systems.json").read_text())
    css = a.system if a.system.startswith(("http://", "https://")) else str(Path(a.system).resolve())
    a.system = next((name for name, p in profiles.items() if css in p["stylesheets"]), a.system)
    if a.system not in ("auto", *profiles):
        profiles["custom"] = {"stylesheets": [css], "sheetNames": [css.split("?")[0].rsplit("/", 1)[-1]],
                              "recogniseSheets": [], "tokens": [], "families": [], "mono": [], "references": []}
        a.system = "custom"
    servers = []
    try:
        with tempfile.TemporaryDirectory(prefix="glevels-onsystem-build-") as tmp, open(spacing.LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mb = spacing.free_mb()
            if mb < spacing.MIN_FREE_MB:
                spacing.impossible(f"only {mb} MB of memory free, a browser needs {spacing.MIN_FREE_MB}. Try again when the host is quieter")
            urls, servers = targets(a.targets, Path(tmp), profiles)
            try:
                plugin, lint_why = vendor.ensure("stylelint-declaration-strict-value")
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                plugin, lint_why = None, str(e)
            config = {"profiles": profiles, "system": a.system, "out": str(out_dir), "urls": urls,
                      "viewports": a.viewports, "plugin": str(plugin) if plugin else None, "lintWhy": lint_why}
            if a.refs:
                srv, base = spacing.serve({}, Path(profiles["gdesign"]["referenceRoot"]))
                servers.append(srv)
                config["refs"] = {name: [base + "/" + ref if not ref.startswith("https://") else ref
                                        for ref in profile["references"]] for name, profile in profiles.items()}
            config["impeccable"] = impeccable(urls, a.viewports)
            cfg = Path(tmp) / "run.json"
            cfg.write_text(json.dumps(config))
            mb = spacing.free_mb()
            if mb < spacing.MIN_FREE_MB:
                spacing.impossible(f"only {mb} MB of memory free, a browser needs {spacing.MIN_FREE_MB}. Try again when the host is quieter")
            p = subprocess.run(["node", str(HERE / "onsystem.mjs"), str(cfg)], capture_output=True, text=True, timeout=600)
            if p.returncode:
                spacing.impossible(f"the browser run failed: {(p.stderr or p.stdout).strip()[-600:]}")
            results = json.loads((out_dir / "findings.json").read_text())
            for r in results:
                for f in r.pop("impeccable", []):
                    level = impeccable_level(f, r["system"])
                    if level:
                        f["severity"] = level
                        r["findings"].append(f)
                        r["counts"][f["check"]] = r["counts"].get(f["check"], 0) + 1
                seen = {f["selector"] for f in r["findings"] if f["check"] == "impeccable:gradient-text"}
                own = [f for f in r["findings"] if f["check"] == "gradient-text" and f["selector"] in seen]
                if own:
                    r["findings"] = [f for f in r["findings"] if f not in own]
                    r["counts"]["gradient-text"] -= len(own)
                    if r["counts"]["gradient-text"] <= 0:
                        del r["counts"]["gradient-text"]
                r["url"] = a.targets[urls.index(r["url"])]
            (out_dir / "findings.json").write_text(json.dumps(results, indent=2) + "\n")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        spacing.impossible(str(e))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    errors = [r["error"] for r in results if r.get("error")]
    if errors:
        spacing.impossible("; ".join(errors) + f"; findings: {out_dir}/findings.json")
    if a.json:
        print(json.dumps({"renders": results, "out": str(out_dir)}, indent=2))
        return int(any(f["severity"] == "block" for r in results for f in r["findings"]))
    return report(results, out_dir)


if __name__ == "__main__":
    sys.exit(main())
