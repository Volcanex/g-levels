"""Sensor: capability gaps between sibling surfaces and uninterpreted results.

    python3 parity.py [--out DIR] [--json] [--except FILE] [--require "cap|other label, cap"] [--source]
                      TARGET [TARGET ...]

Targets are HTML files, URLs (urllib), or walk output directories. Each walk.json
state is a surface, using its saved controls. HTML is parsed without running scripts
or rendering CSS. Capabilities are controls, h2/h3 headings and data-capability names.
Exceptions are lines of "capability: reason". Findings go to --out (default: a new
temp dir, printed at the end).

--source reads HTML as component source, where forms ship hidden until data loads.

Exit 0: no clear fault. Exit 1: a sibling lacks a capability named with --require (or
GLEVELS_CAPABILITIES), under "blocking". Other gaps and lone controls are leads: siblings
differ for good reasons, so the reviewer names what must match. Exit 2: could not run (including fewer than two surfaces), printed as "impossible:
...". Interpretation leads under "for the reviewer" never fail a run alone.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

if __package__:
    from .claims import Page, Element, compact, finish, read_target
else:
    from claims import Page, Element, compact, finish, read_target

INTERPRET = re.compile(r"\b(?:so what|means|because|which means|you should|next steps?|we recommend|why it matters)\b", re.I)
RESULT = re.compile(r"\b(?:results?|findings?|scores?|rankings?)\b", re.I)


def normalise(s):
    words = re.sub(r"[^\w\s]|[\d_]", " ", s.lower()).split()
    out = []
    for word in words:
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        out.append(word)
    return " ".join(out)


def control_name(n, nodes):
    attrs = n.attrs
    if attrs.get("aria-label"):
        return attrs["aria-label"]
    if attrs.get("aria-labelledby"):
        label = " ".join(e.text() for key in attrs["aria-labelledby"].split() for e in nodes if e.attrs.get("id") == key)
        if label:
            return label
    labels = [e for e in nodes if e.tag == "label" and attrs.get("id") and e.attrs.get("for") == attrs["id"]]
    if labels:
        return " ".join(e.text() for e in labels)
    parent = n.parent
    while parent:
        if parent.tag == "label":
            return compact(" ".join(c if isinstance(c, str) else c.text() for c in parent.children if c is not n))
        parent = parent.parent
    if n.tag in {"button", "a"} or attrs.get("role") in {"button", "switch", "checkbox"}:
        return n.text() or attrs.get("title", "")
    if n.tag == "input" and attrs.get("type") in {"submit", "button", "reset"}:
        return attrs.get("value", attrs.get("type"))
    return attrs.get("title") or attrs.get("placeholder") or attrs.get("name") or attrs.get("id") or ""


def html_surface(target, html):
    page = Page(html)
    nodes = list(page.root.elements())
    capabilities, sections, bare = set(), set(), []
    for n in nodes:
        role = n.attrs.get("role", "")
        is_control = n.tag in {"button", "select", "textarea"} or (n.tag == "input" and n.attrs.get("type") != "hidden") or role in {"button", "switch", "checkbox", "combobox"} or (
            n.tag == "a" and re.search(r"btn|button", n.attrs.get("class", ""), re.I))
        if is_control:
            capabilities.add(normalise(control_name(n, nodes)))
        if n.tag in {"h2", "h3"}:
            sections.add(normalise(n.text()))
        if n.attrs.get("data-capability"):
            capabilities.update(normalise(s) for s in re.split(r"[,;]", n.attrs["data-capability"]))
        if n.tag not in {"table", "ul", "ol"}:
            continue
        parent = n.parent
        while parent:
            heads = [c for c in parent.children if isinstance(c, Element) and c.tag in {"h1", "h2", "h3", "p"}]
            marker = parent.attrs.get("id", "") + " " + parent.attrs.get("class", "")
            marker += " " + " ".join(c for c in parent.children if isinstance(c, str))
            if RESULT.search(marker) or any(RESULT.search(h.text()) for h in heads):
                prose = " ".join(c if isinstance(c, str) else c.text() for c in parent.children
                                 if isinstance(c, str) or c.tag not in {"table", "ul", "ol", "h1", "h2", "h3", "nav"})
                if not INTERPRET.search(prose):
                    bare.append(n.place())
                break
            parent = parent.parent
    capabilities.discard("")
    sections.discard("")
    return {"target": target, "capabilities": sorted(capabilities), "sections": sorted(sections - capabilities),
            "bare_results": bare}


def walk_surfaces(target):
    root = Path(target)
    data = json.loads((root / "walk.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("states"), list):
        raise ValueError(f"{target}: walk.json needs a states list")
    surfaces = []
    for i, state in enumerate(data["states"]):
        if not isinstance(state, dict) or not isinstance(state.get("controls"), list) or state.get("error"):
            raise ValueError(f"{target}: state {i} has no readable controls or failed to render")
        caps, sections = set(), set()
        for c in state["controls"]:
            if isinstance(c, str):
                caps.add(normalise(c))
            elif isinstance(c, dict):
                if c.get("visible") is False or c.get("type") == "hidden":
                    continue
                caps.add(normalise(c.get("label") or c.get("name") or c.get("text") or c.get("aria-label") or c.get("ariaLabel") or ""))
                if c.get("capability") or c.get("data-capability"):
                    caps.add(normalise(c.get("capability") or c.get("data-capability")))
            else:
                raise ValueError(f"{target}: state {i} has an unreadable control")
        for heading in state.get("headings", []):
            if isinstance(heading, str):
                sections.add(normalise(heading))
            elif isinstance(heading, dict):
                sections.add(normalise(heading.get("text", "")))
        text = state.get("text", "")
        if not isinstance(text, str):
            raise ValueError(f"{target}: state {i} text is unreadable")
        if text.startswith("text/") or text.endswith(".txt"):
            text = (root / text).read_text(encoding="utf-8")
        caps.discard("")
        name = state.get("name") or state.get("id") or f"state {i}"
        sections.discard("")
        surfaces.append({"target": f"{target}/{name}", "capabilities": sorted(caps), "sections": sorted(sections - caps),
                         "bare_results": ["saved results"] if RESULT.search(text) and not INTERPRET.search(text) else []})
    return surfaces


def exceptions(path):
    waived = {}
    if path:
        for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            capability, sep, reason = line.partition(":")
            capability = normalise(capability)
            if not sep or not capability or not reason.strip():
                raise ValueError(f"{path}:{i}: expected capability: reason")
            waived[capability] = reason.strip()
    return waived


def compare(surfaces, waived, required=()):
    # A capability the reviewer names (--require) blocks on every sibling that lacks it. The rest is
    # leads: on real pages, sibling editors differ in fields for good reasons, and blocking on every
    # unmatched label buried the one gap that mattered under dozens that didn't.
    findings = []
    total = len(surfaces)
    for names in required:
        need = names[0]
        if need in waived:
            continue
        missing = [s for s in surfaces if not any(n in c or c in n and len(c) > 3 for n in names
                                                  for c in s["capabilities"] + s.get("sections", []))]
        if missing and len(missing) < total:
            for s in missing:
                findings.append({"check": "required", "severity": "block", "target": s["target"], "selector": need,
                                 "detail": f"has no {need!r}, which {total - len(missing)} of {total} siblings have"})
        elif missing:
            findings.append({"check": "required", "severity": "review", "target": "all siblings", "selector": need,
                             "detail": f"no sibling has {need!r}: check the name, or build it"})
    for key in ("capabilities", "sections"):
        have = defaultdict(list)
        for i, s in enumerate(surfaces):
            for c in s.get(key, []):
                have[c].append(i)
        noun = "control" if key == "capabilities" else "section"
        for capability, owners in sorted(have.items()):
            if capability in waived or any(capability in names for names in required):
                continue
            if len(owners) >= 2 and len(owners) * 2 >= total:
                for i, s in enumerate(surfaces):
                    if i not in owners:
                        findings.append({"check": "gap", "severity": "review", "target": s["target"], "selector": capability,
                                         "detail": f"has no {capability!r} {noun} that {len(owners)} of {total} siblings have"})
            if total >= 3 and len(owners) == 1:
                s = surfaces[owners[0]]
                findings.append({"check": "lone", "severity": "review", "target": s["target"], "selector": capability,
                                 "detail": f"only this surface has the {noun} {capability!r} (1 of {total})"})
    for s in surfaces:
        for place in s["bare_results"]:
            findings.append({"check": "interpretation", "severity": "review", "target": s["target"], "selector": place,
                             "detail": "results are a bare list/table with no nearby explanation of what they mean or what to do"})
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--out", help="findings directory (default: a new temp dir)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--except", dest="except_file")
    ap.add_argument("--require", help="comma-separated capabilities every sibling must have (blocking)")
    ap.add_argument("--source", action="store_true", help="read HTML as source markup: hidden forms still count")
    a = ap.parse_intermixed_args()
    try:
        out_dir = Path(a.out or tempfile.mkdtemp(prefix="glevels-parity-")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        waived = exceptions(a.except_file or os.environ.get("GLEVELS_EXCEPT"))
        # From `glevels check`, the charter's runner names the sibling pages in GLEVELS_SIBLINGS.
        a.targets = a.targets or os.environ.get("GLEVELS_SIBLINGS", "").split()
        # "add a question|add another keyword": one capability that siblings may label differently.
        required = [tuple(normalise(x) for x in r.split("|") if normalise(x))
                    for r in re.split(r"[,;]", a.require or os.environ.get("GLEVELS_CAPABILITIES", "")) if normalise(r)]
        Element.SOURCE = a.source
        surfaces = []
        for target in a.targets:
            if Path(target).is_dir():
                surfaces.extend(walk_surfaces(target))
            else:
                text, html = read_target(target)
                if not html:
                    raise ValueError(f"{target}: parity needs HTML or a walk directory")
                surfaces.append(html_surface(target, text))
        if len(surfaces) < 2:
            raise ValueError("parity needs at least two sibling surfaces")
        findings = compare(surfaces, waived, required)
        return finish("parity", "surfaces", surfaces, findings, out_dir, a.json,
                      {"waived": [{"capability": c, "reason": r} for c, r in waived.items()]})
    except (OSError, ValueError) as e:
        print(f"impossible: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
