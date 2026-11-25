#!/usr/bin/env python3
"""The sensor for correct.fasra.conversion-parity: a converted screen against the list of what the old one could do.

    python3 conversion.py TARGET [TARGET ...] [--root DIR] [--src PATH ...] [--summary]

TARGET is a parity file, a folder of them, or a repo (read as its convert/*/parity/*.json). One file per old page:
  {"old": "/address", "source": "file in the old repo", "title": "", "purpose": "", "capabilities": [...],
   "view": ["the new view's source, files or folders, relative to the repo"],
   "added": [{"endpoint": "GET /api/...", "why": ""}],
   "decision": "convert" | "stays" | "retire", "decision_by": "", "decision_reason": ""}
A capability is {"id", "kind", "what", "where", "endpoint", "writes", "spends", "status", "evidence"}: kind one of
view, chart, filter, drill, action, export, state, motion, and status missing, met or waived. The files in one
folder are one flow: they share the old screen's endpoints and the new view's source.

Refused, exit 2: a file that does not parse or lacks a field, a duplicate id, met with no evidence, met with no view
to check it against, waived with no waived_by, reason and review date, a decision other than convert with no reason.
Catches, exit 1: a capability still missing, a capability marked met whose endpoint the view's source never names,
and a path under /api/ in the view's source that no capability of the flow names and no added line explains.
Leads under "for the reviewer", never blocking: capabilities that write or spend, added endpoints, waived lines, and
paths the source builds from a prefix, which need reading by hand.
Exit 0: nothing missing and the calls agree.
"""
import argparse
import json
import re
import sys
from pathlib import Path

KINDS = ("view", "chart", "filter", "drill", "action", "export", "state", "motion")
STATUSES = ("missing", "met", "waived")
PAGE_FIELDS = ("old", "source", "title", "purpose", "capabilities")
CAP_FIELDS = ("id", "kind", "what", "where", "endpoint", "writes", "status")
DECISIONS = ("convert", "stays", "retire")
ENDPOINT = re.compile(r"/api/[^\s;,?\"'`)]*")
TEMPLATE = re.compile(r"\$\{[^}]*\}")
SOURCE_PATH = re.compile(r"/api/[A-Za-z0-9_\-./{}]*")
SOURCE_TYPES = (".js", ".mjs", ".jsx", ".ts", ".tsx", ".html")


def segments(path):
    path = path.split("?")[0].rstrip(".")
    return [s for s in path.strip("/").split("/") if s]


def wild(segment):
    return segment.startswith("{") or segment.startswith(":")


def same(a, b):
    return wild(a) or wild(b) or a == b


def exact(new, old):
    return len(new) == len(old) and all(same(a, b) for a, b in zip(new, old))


def prefix(new, old):
    return len(new) < len(old) and all(same(a, b) for a, b in zip(new, old))


def endpoints(text):
    return [segments(p) for p in ENDPOINT.findall(text or "")]


def find_root(path):
    for parent in path.resolve().parents:
        if parent.name == "convert":
            return parent.parent
    return None


def parity_files(target):
    if target.is_file():
        return [target]
    if (target / "convert").is_dir():
        return sorted((target / "convert").glob("*/parity/*.json"))
    return sorted(target.glob("*.json"))


def read_page(path, refusals):
    name = path.name
    try:
        page = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        refusals.append(f"{name}: does not read as JSON ({e})")
        return None
    if not isinstance(page, dict):
        refusals.append(f"{name}: is not an object")
        return None
    lacking = [f for f in PAGE_FIELDS if not page.get(f)]
    if lacking:
        refusals.append(f"{name}: lacks {', '.join(lacking)}")
        return None
    decision = page.get("decision", "convert")
    if decision not in DECISIONS:
        refusals.append(f"{name}: decision {decision!r} is not one of {', '.join(DECISIONS)}")
        return None
    if decision != "convert" and not page.get("decision_reason"):
        refusals.append(f"{name}: decision {decision} with no decision_reason")
    for added in page.get("added", []):
        if not isinstance(added, dict) or not endpoints(added.get("endpoint", "")) or not added.get("why"):
            refusals.append(f"{name}: an added line needs an endpoint under /api/ and a why")
    seen = set()
    for cap in page["capabilities"]:
        cid = cap.get("id", "?") if isinstance(cap, dict) else "?"
        if not isinstance(cap, dict) or [f for f in CAP_FIELDS if f not in cap]:
            refusals.append(f"{name} {cid}: lacks one of {', '.join(CAP_FIELDS)}")
            continue
        if cid in seen:
            refusals.append(f"{name} {cid}: id used twice")
        seen.add(cid)
        if cap["kind"] not in KINDS:
            refusals.append(f"{name} {cid}: kind {cap['kind']!r} is not one of {', '.join(KINDS)}")
        if cap["status"] not in STATUSES:
            refusals.append(f"{name} {cid}: status {cap['status']!r} is not one of {', '.join(STATUSES)}")
        if not isinstance(cap["writes"], bool):
            refusals.append(f"{name} {cid}: writes must be true or false")
        if cap["status"] == "met" and not str(cap.get("evidence") or "").strip():
            refusals.append(f"{name} {cid}: met with no evidence")
        if cap["status"] == "waived" and not all(str(cap.get(f) or "").strip() for f in ("waived_by", "reason", "review")):
            refusals.append(f"{name} {cid}: waived with no waived_by, reason and review date (only the owner waives)")
    return page


def source_paths(files):
    found = {}
    for f in files:
        text = TEMPLATE.sub("{}", f.read_text(errors="replace"))
        for m in SOURCE_PATH.finditer(text):
            raw = m.group(0)
            found.setdefault((tuple(segments(raw)), raw.endswith("/")), f.name)
    return found


def source_files(root, names):
    files, absent = [], []
    for name in names:
        p = Path(name) if Path(name).is_absolute() or root is None else root / name
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in SOURCE_TYPES)
        else:
            absent.append(name)
    return files, absent


def check_flow(folder, paths, root, extra_src, refusals):
    pages = [(p, read_page(p, refusals)) for p in paths]
    pages = [(p, page) for p, page in pages if page]
    counted = [(p, page) for p, page in pages
               if page.get("decision", "convert") == "convert" or page.get("decision_by") != "owner"]
    old, added, views = [], [], list(extra_src)
    for _, page in pages:
        for cap in page["capabilities"]:
            if isinstance(cap, dict):
                old += endpoints(cap.get("endpoint") or "")
        for line in page.get("added", []):
            if isinstance(line, dict):
                added += endpoints(line.get("endpoint", ""))
        views += [v for v in page.get("view", []) if v not in views]
    files, absent = source_files(root, views)
    for name in absent:
        refusals.append(f"{folder}: view {name} is not a file or folder under {root}")
    has_met = any(c.get("status") == "met" for _, page in counted for c in page["capabilities"] if isinstance(c, dict))
    if has_met and not files:
        refusals.append(f"{folder}: capabilities are marked met but no view source is named to check them against")
    called = source_paths(files)
    catches, leads, rows = [], [], []
    for path, page in counted:
        caps = [c for c in page["capabilities"] if isinstance(c, dict)]
        tally = {s: sum(1 for c in caps if c.get("status") == s) for s in STATUSES}
        rows.append((path.stem, tally, len(caps)))
        for cap in caps:
            label = f"{cap.get('id')} {cap.get('kind')}: {str(cap.get('what'))[:110]}"
            if cap.get("status") == "missing":
                catches.append(f"missing {label}")
            elif cap.get("status") == "waived":
                leads.append(f"waived by {cap.get('waived_by')} until {cap.get('review')}: {label}")
            elif cap.get("status") == "met" and files:
                for want in endpoints(cap.get("endpoint") or ""):
                    if not any(exact(list(new), want) or prefix(list(new), want) for (new, _) in called):
                        catches.append(f"marked met, but the view's source never names /{'/'.join(want)}: {label}")
            if cap.get("status") != "waived" and (cap.get("writes") or cap.get("spends")):
                cost = f", spends: {cap['spends']}" if cap.get("spends") else ""
                leads.append(f"writes or spends, try it on staging only{cost}: {label}")
    for (new, trailing), where in sorted(called.items()):
        new = list(new)
        known = old + added
        if not trailing and any(exact(new, e) for e in known):
            continue
        if any(prefix(new, e) or (trailing and exact(new, e)) for e in known):
            leads.append(f"{where} builds a path from /{'/'.join(new)}, read the full path by hand")
        else:
            catches.append(f"{where} calls /{'/'.join(new)}, which the old screen never called and no added line explains")
    for ep in added:
        leads.append(f"added endpoint /{'/'.join(ep)}, new to this flow")
    return rows, catches, leads


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--root", help="the repo the view paths are relative to (default: the folder above convert/)")
    ap.add_argument("--src", action="append", default=[], help="more view source to read, a file or a folder")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    a = ap.parse_args(argv)
    flows = {}
    for t in a.targets:
        target = Path(t)
        if not target.exists():
            print(f"impossible: {t} does not exist")
            return 2
        for f in parity_files(target):
            flows.setdefault(f.parent, []).append(f)
    if not flows:
        print("impossible: no parity files found (a repo keeps them at convert/<flow>/parity/*.json)")
        return 2
    refusals, catches, leads, met, total, waived = [], [], [], 0, 0, 0
    for folder, paths in sorted(flows.items()):
        root = Path(a.root) if a.root else find_root(paths[0]) or (Path(a.targets[0]) if Path(a.targets[0]).is_dir() else None)
        name = folder.parent.name if folder.name == "parity" else folder.name
        rows, c, l = check_flow(name, paths, root, a.src, refusals)
        print(name)
        for stem, tally, n in rows:
            print(f"  {stem:28} {tally['met']} of {n} met, {tally['missing']} missing" + (f", {tally['waived']} waived" if tally["waived"] else ""))
            met, total, waived = met + tally["met"], total + n, waived + tally["waived"]
        catches += [f"{name}: {x}" for x in c]
        leads += [f"{name}: {x}" for x in l]
    print(f"\ntotal: {met} of {total} met, {total - met - waived} missing, {waived} waived, across {sum(len(p) for p in flows.values())} old pages")
    if refusals:
        print("\nimpossible: the files cannot be trusted as they stand")
        for r in refusals:
            print(f"  {r}")
        return 2
    if catches:
        print(f"\nblocking: {len(catches)}")
        if not a.summary:
            for c in catches:
                print(f"  {c}")
    if leads and not a.summary:
        print(f"\nfor the reviewer: {len(leads)}")
        for l in leads:
            print(f"  {l}")
    return 1 if catches else 0


if __name__ == "__main__":
    sys.exit(main())
