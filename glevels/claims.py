"""Sensor: broken product data, provenance coverage and claims needing review.

    python3 claims.py [--out DIR] [--json] [--now YYYY-MM-DD]
                      [--data FILE ...] [--require source,as_of] TARGET [TARGET ...]

Targets are HTML/text files, URLs (fetched with urllib), or walk output directories
(text/*.txt). HTML keeps rows, list items and cards together, ignoring scripts,
styles and explicitly hidden elements. It does not execute JavaScript or render CSS.
Findings go to --out (default: a new temp dir, printed at the end).

Exit 0: no clear fault. Exit 1: clear faults, listed under "blocking". Exit 2:
could not run, printed as "impossible: ...". Leads under "for the reviewer" never
fail a run alone.
"""
import argparse
import csv
import json
import re
import sys
import tempfile
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

VOID = set("area base br col embed hr img input link meta param source track wbr".split())
MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
WINDOW = re.compile(r"\b(?:last\s+\d+\s+(?:days?|weeks?|months?|hours?)|this\s+(?:week|month|year)|today|since\s+[^.;]+|between\s+[^.;]+|in\s+" + MONTH + r"(?:\s+\d{4})?|\d+\s+(?:days?|hours?)|per\s+\w+)\b", re.I)
METRIC = re.compile(r"\b(?:visitors?|visits|users|views|sessions|traffic|mentions|share of voice|score)\b", re.I)
COUNT = r"(?:visitors?|visits|users|views|people|clicks|sessions|mentions)"
NUMBER = r"[-+−]?\s*[$£€]?\s*\d[\d,]*(?:\.\d+)?\s*%?"
BROKEN = re.compile(r"(?<!\w)(?:\$?NaN|undefined|null|None|Infinity|\[object Object\]|Invalid Date)(?!\w)|\{\{?\s*[\w.]+\s*\}?\}")
SUPER = re.compile(r"#\s*1\b|\b(?:top|best|cheapest|fastest|only|free|guaranteed|never|always)\b", re.I)
TREND = re.compile(r"(?<![\w])[-+−]\s*\d+(?:\.\d+)?\s*%?|\b(?:up\s+\d+|down|trending|growth)\b", re.I)
COMPARE = re.compile(r"\b(?:vs\.?|compared (?:with|to)|than last|previous)\b", re.I)
DATE = re.compile(r"\b(?:as of|updated|checked|last run)\s*(?:on\s*)?[:=]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}\s+" + MONTH + r"\s+\d{4}|" + MONTH + r"\s+\d{1,2},?\s+\d{4})", re.I)
VALUE_FIELD = re.compile(r"\b(?:price|cost|fare|count|score|rank|rating)\b", re.I)
SOURCES = {"source", "url", "link", "ref", "evidence"}
DATES = {"as_of", "checked", "fetched", "date", "updated"}
COUNTRIES = {
    "UK": r"\b(?:UK|United Kingdom|Britain|British|England|Scotland|Wales|London|Manchester|Birmingham|Edinburgh|GBP)\b|£",
    "NZ": r"\b(?:NZ|New Zealand|Auckland|Wellington|Christchurch|NZD)\b|NZ\$",
    "US": r"\b(?:USA|United States|American|New York|San Francisco|USD)\b|US\$",
    "AU": r"\b(?:Australia|Australian|Sydney|Melbourne|AUD)\b|A\$|AU\$",
    "CA": r"\b(?:Canada|Canadian|Toronto|Vancouver|CAD)\b|CA\$|C\$",
    "IE": r"\b(?:Ireland|Irish|Dublin)\b",
    "DE": r"\b(?:Germany|German|Berlin|Munich)\b",
    "FR": r"\b(?:France|French|Paris)\b",
}


def compact(s):
    return " ".join(s.split())


class Element:
    # SOURCE: read markup as source, not as a rendered page. Apps ship forms `hidden` and show them
    # once data loads, so in source a hidden attribute does not mean the user never sees it.
    SOURCE = False

    def __init__(self, tag, attrs=None, parent=None, line=1):
        self.tag, self.parent = tag, parent
        self.attrs = {k: v or "" for k, v in attrs or []}
        self.children, self.line = [], line
        style = self.attrs.get("style", "")
        self.hidden = bool(parent and parent.hidden) or tag in {"head", "script", "style", "template", "noscript"} or (
            not self.SOURCE and ("hidden" in self.attrs or self.attrs.get("aria-hidden") == "true" or
            bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I))))

    def text(self):
        if self.hidden:
            return ""
        return compact(" ".join(c if isinstance(c, str) else c.text() for c in self.children))

    def elements(self):
        for c in self.children:
            if isinstance(c, Element) and not c.hidden:
                yield c
                yield from c.elements()

    def place(self):
        return self.tag + ("#" + self.attrs["id"] if self.attrs.get("id") else "") + f":{self.line}"


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in {"li", "tr", "td", "th", "p"} and self.stack[-1].tag == tag:
            self.stack.pop()
        n = Element(tag, attrs, self.stack[-1], self.getpos()[0])
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def read_target(target):
    if target.startswith(("http://", "https://")):
        with urllib.request.urlopen(target, timeout=20) as response:
            return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace"), True
    p = Path(target)
    if p.suffix.lower() not in {".html", ".htm", ".txt"}:
        raise ValueError(f"{target}: expected HTML, text, a URL or a walk directory")
    return p.read_text(encoding="utf-8"), p.suffix.lower() != ".txt"


def boundary(n):
    classes = n.attrs.get("class", "")
    return n.tag in {"tr", "li"} or bool(re.search(r"card|tile|stat|metric|kpi", classes, re.I) and
        not re.search(r"(?:label|value|title|number|grid|list|container|wrapper)", classes, re.I))


def nearby(n):
    text = n.text()
    links = any(e.tag == "a" and e.attrs.get("href", "").strip() not in {"", "#"} for e in n.elements())
    if n.parent:
        for c in n.parent.children:
            if isinstance(c, str):
                text += " " + c
            elif c is not n and c.tag in {"h1", "h2", "h3", "caption", "p", "small"} and len(c.text()) < 200:
                text += " " + c.text()
                links |= any(e.tag == "a" and e.attrs.get("href", "").strip() not in {"", "#"} for e in c.elements())
    return compact(text), links


def html_units(html):
    page = Page(html)
    units = []

    def visit(n):
        if n.hidden:
            return
        nested = any(boundary(c) for c in n.elements())
        inline = n.tag in {"div", "section", "article", "main", "body"} and not any(
            e.tag in {"div", "section", "article", "p", "h1", "h2", "h3", "table", "ul", "ol", "dl"} for e in n.elements())
        if not nested and (boundary(n) or inline or n.tag in {"p", "h1", "h2", "h3", "h4", "dt", "dd", "caption"}):
            if n.text():
                context, links = nearby(n)
                cells = [e.text() for e in n.elements() if e.tag in {"td", "th", "dd"} or not any(isinstance(c, Element) for c in e.children)]
                units.append({"text": n.text(), "context": context, "source": links,
                              "place": n.place(), "parent": id(n.parent), "series": boundary(n), "cells": cells})
            return
        for c in n.children:
            if isinstance(c, Element):
                visit(c)
            elif compact(c):
                units.append({"text": compact(c), "context": nearby(n)[0], "source": nearby(n)[1],
                              "place": n.place(), "parent": id(n), "series": False, "cells": []})

    visit(page.root)
    return units


def text_units(text):
    lines = [(i, compact(s)) for i, s in enumerate(text.splitlines(), 1) if compact(s)]
    units = []
    j = 0
    while j < len(lines):
        i, s = lines[j]
        cells, end = [], j + 1
        if end < len(lines) and re.fullmatch(r"[A-Za-z][A-Za-z /:()-]*", s):
            value = lines[end][1]
            if re.fullmatch(NUMBER, value) or BROKEN.fullmatch(value) or value in {"-", "--", "0 of 0"}:
                cells, s, end = [s, value], s + " " + value, end + 1
        context = " ".join(v for _, v in lines[max(0, j - 1):end + 2])
        units.append({"text": s, "context": context, "source": False,
                      "place": f"line {i}", "parent": 0, "series": False, "cells": cells})
        j = end
    return units


def parsed_date(s):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s.replace(",", ""), fmt).date()
        except ValueError:
            pass
    return None


def metric_values(text):
    text = DATE.sub("", WINDOW.sub("", text))
    pairs = []
    for m in re.finditer(r"([A-Za-z][A-Za-z &/()'-]{0,60}?)\s*[:=]?\s*(" + NUMBER + r")", text):
        label = compact(m[1]).lower()
        if label and label not in {"source", "updated", "as of", "checked", "last run"}:
            pairs.append((label, value_key(m[2])))
    if not pairs:
        m = re.match(r"\s*(" + NUMBER + r")\s+([A-Za-z][A-Za-z ]+)$", text)
        if m:
            pairs.append((compact(m[2]).lower(), value_key(m[1])))
    return pairs


def value_key(s):
    s = compact(s).replace(",", "").replace(" ", "").replace("−", "-").lstrip("+")
    return re.sub(r"\d+\.\d+", lambda m: m[0].rstrip("0").rstrip("."), s)


def inspect_page(target, units, now):
    findings, metrics, series = [], defaultdict(list), defaultdict(list)

    def add(check, severity, u, detail):
        findings.append({"check": check, "severity": severity, "target": target,
                         "selector": u["place"], "detail": detail})

    for u in units:
        text, context = u["text"], u["context"]
        sample = text[:100]
        for m in BROKEN.finditer(text):
            if m[0] in {"None", "null"} and m[0] not in u["cells"] and not text.endswith(m[0]):
                continue
            add("broken-value", "block", u, f"{sample!r} shows {m[0]!r} as data")
        if any(c in {"-", "--"} for c in u["cells"]) or re.search(r"\w\s*[:=]?\s+-{1,2}$", text):
            add("empty-value", "block", u, f"{sample!r} shows a dash instead of a value next to its label")
        if re.search(r"\b0\s+of\s+0\b", text, re.I):
            add("empty-total", "block", u, f"{sample!r} shows 0 of 0")
        for m in re.finditer(r"(" + NUMBER + r")\s*%", text):
            value = float(re.sub(r"[^\d.+-]", "", m[1].replace("−", "-")))
            label = re.split(r"[%!?;]", text[:m.start()])[-1][-65:]
            if not re.search(r"[A-Za-z]", label):
                label = text[m.end():m.end() + 45]
            if value > 100 and re.search(r"\b(?:share|of total|rate|percent of)\b", label, re.I):
                add("percentage-over-100", "block", u, f"{sample!r} shows {value:g}% for a share/rate of a total")
        if re.search(r"\b" + COUNT + r"\s*[:=]?\s*[-−]\s*\d|[-−]\s*\d[\d,]*\s+" + COUNT + r"\b", text, re.I):
            add("negative-count", "block", u, f"{sample!r} shows a negative count")
        for m in DATE.finditer(text):
            day = parsed_date(m[1])
            if day and day > now:
                add("future-date", "block", u, f"{m[0]!r} is later than {now.isoformat()}")
        if METRIC.search(text) and re.search(r"\d|NaN|undefined|null|None|Infinity", text) and not WINDOW.search(context):
            add("missing-window", "review", u, f"{sample!r} has no measurement window nearby")
        if TREND.search(text) and not COMPARE.search(context):
            add("missing-comparison", "review", u, f"{sample!r} gives a trend without a comparison period")
        if SUPER.search(text) and not (u["source"] or re.search(r"https?://|\b(?:source|evidence|reference)\s*:\s*\S", context, re.I)):
            add("unsourced-claim", "review", u, f"{sample!r} makes a rank/superlative claim without a nearby source")
        windows = WINDOW.findall(text) or WINDOW.findall(context)
        window = " | ".join(compact(w).lower() for w in windows)
        pairs = metric_values(text)
        for label, value in pairs:
            metrics[label].append((value, window, u))
        if u["series"] and pairs:
            series[u["parent"]].append((pairs[0][1], u))

    for label, values in metrics.items():
        # Only a named metric (visitors, sessions, score...) blocks. Other labels are whatever words
        # sat before a number ('at', 'jun', 'q'), which on a real deck were mostly axis ticks.
        if len(re.sub(r"[^a-z]", "", label)) < 4:
            continue
        severity = "block" if METRIC.search(label) else "review"
        for i, (value, window, u) in enumerate(values):
            other = next((v for v, w, _ in values[:i] if v != value and (not w or not window or w == window)), None)
            if other is not None:
                add("conflicting-metric", severity, u, f"{label!r} shows both {other} and {value} without different windows")
                break
    for values in series.values():
        if len(values) < 3:
            continue
        counts = Counter(v for v, _ in values)
        repeated = [v for v, count in counts.items() if count >= 3]
        if repeated:
            add("repeated-series", "review", values[0][1], f"sibling cards/rows repeat {', '.join(repeated)} at least 3 times; check for defaults or copied data")
        nums = [float(re.sub(r"[^\d.+-]", "", v.replace("−", "-"))) for v, _ in values]
        if not repeated and all(v != 0 and v % 100 == 0 for v in nums):
            add("round-series", "review", values[0][1], "sibling cards/rows all show round hundreds; check the measured values")
    entities = defaultdict(list)
    currencies = defaultdict(list)
    for u in units:
        for country, pattern in COUNTRIES.items():
            for m in re.finditer(pattern, u["text"], re.I):
                entities[country].append((m[0], u))
        for m in re.finditer(r"(?:NZ|US|AU|CA|A|C)\$|[$£€]|\b(?:GBP|USD|NZD|AUD|CAD|EUR)\b", u["text"]):
            currency = {"£": "GBP", "€": "EUR", "$": "dollar", "NZ$": "NZD", "US$": "USD", "AU$": "AUD", "A$": "AUD", "CA$": "CAD", "C$": "CAD"}.get(m[0], m[0])
            currencies[currency].append((m[0], u))
    for groups, check in ((entities, "outlier-country"), (currencies, "outlier-currency")):
        if len(groups) < 2:
            continue
        majority = max(groups, key=lambda k: len(groups[k]))
        if len(groups[majority]) <= sum(len(v) for k, v in groups.items() if k != majority):
            continue
        for key, hits in groups.items():
            if key != majority:
                for entity, u in hits:
                    add(check, "review", u, f"{entity!r} differs from the page majority ({majority}); check the entity belongs here")
    return findings


def field_name(s):
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z])([A-Z])", r"\1_\2", s).lower()).strip("_")


def records(value):
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("data records must be JSON objects")
            yield from records(item)
    elif isinstance(value, dict):
        if any(VALUE_FIELD.search(field_name(k).replace("_", " ")) for k in value):
            yield value
        elif not any(isinstance(v, (dict, list)) for v in value.values()):
            yield value
        else:
            for v in value.values():
                if isinstance(v, (dict, list)):
                    yield from records(v)
    else:
        raise ValueError("data must be a JSON object or list of records")


def inspect_data(target, required):
    p = Path(target)
    if p.suffix.lower() == ".csv":
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        rows = list(records(json.loads(p.read_text(encoding="utf-8"))))
    findings, total, sourced, dated, both = [], 0, 0, 0, 0
    for i, row in enumerate(rows):
        fields = {field_name(k): v for k, v in row.items() if k is not None}
        if not any(VALUE_FIELD.search(k.replace("_", " ")) for k in fields):
            continue
        total += 1
        present = {k for k, v in fields.items() if v is not None and compact(str(v)).lower() not in {"", "none", "null", "-", "--"}}
        # By word, so availability_evidence and checked_on count as a source and a date.
        words = {w for k in present for w in k.split("_")} | present
        has_source = bool(words & SOURCES)
        has_date = bool(words & DATES)
        sourced += has_source
        dated += has_date
        both += has_source and has_date
        missing = (["source"] if "source" in required and not has_source else []) + (["as_of/date"] if "as_of" in required and not has_date else [])
        if missing:
            title = fields.get("name", fields.get("title", "unnamed"))
            findings.append({"check": "missing-provenance", "severity": "block", "target": target,
                             "selector": f"record {i} ({title})", "detail": "missing " + " and ".join(missing)})
    return findings, {"target": target, "records": total, "sourced": sourced, "dated": dated, "both": both}


def finish(name, noun, results, findings, out_dir, machine, extra=None):
    block = [f for f in findings if f["severity"] == "block"]
    review = [f for f in findings if f["severity"] == "review"]
    output = {"sensor": name, noun: results, "findings": findings,
              "counts": {"blocking": len(block), "review": len(review)}, "out": str(out_dir), **(extra or {})}
    (out_dir / "findings.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    if machine:
        print(json.dumps(output, indent=2))
    else:
        print(f"{name}: {len(results)} {noun}, {len(block)} blocking, {len(review)} for the reviewer")
        for c in output.get("coverage", []):
            print(f"coverage: {c['both']} of {c['records']} records sourced and dated ({c['target']})")
        for title, fs in (("blocking (clear faults, fix these)", block), ("for the reviewer (leads, judge these)", review)):
            if fs:
                print(f"\n{title}:")
                for f in fs:
                    print(f"  {f['target']}  {f['selector']}: {f['detail']}")
        if output.get("waived"):
            print("\nwaived:")
            for w in output["waived"]:
                print(f"  {w['capability']!r}: {w['reason']}")
        print(f"\nfindings: {out_dir}/findings.json")
    return 1 if block else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--out", help="findings directory (default: a new temp dir)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", default=date.today().isoformat())
    ap.add_argument("--data", action="append", nargs="+", default=[])
    ap.add_argument("--require", default="source,as_of")
    a = ap.parse_intermixed_args()
    try:
        for group in a.data:
            split = next((i for i, t in enumerate(group) if t.startswith(("http://", "https://")) or
                          Path(t).is_dir() or Path(t).suffix.lower() in {".html", ".htm", ".txt"}), len(group))
            a.targets.extend(group[split:])
            del group[split:]
            if not group:
                raise ValueError("--data needs a JSON or CSV file")
        if not a.targets:
            raise ValueError("claims needs at least one target")
        now = date.fromisoformat(a.now)
        required = {s.strip() for s in a.require.split(",") if s.strip()}
        if required - {"source", "as_of"}:
            raise ValueError("--require accepts source and as_of")
        out_dir = Path(a.out or tempfile.mkdtemp(prefix="glevels-claims-")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        pages, findings, coverage = [], [], []
        for target in a.targets:
            files = sorted((Path(target) / "text").glob("*.txt")) if Path(target).is_dir() else [target]
            if not files:
                raise ValueError(f"{target}: no text/*.txt walk evidence")
            for file in files:
                text, html = read_target(str(file))
                units = html_units(text) if html else text_units(text)
                pages.append({"target": str(file), "units": len(units)})
                findings.extend(inspect_page(str(file), units, now))
        for target in (t for group in a.data for t in group):
            fs, counts = inspect_data(target, required)
            findings.extend(fs)
            coverage.append(counts)
        return finish("claims", "pages", pages, findings, out_dir, a.json, {"coverage": coverage, "now": now.isoformat()})
    except (OSError, ValueError, csv.Error) as e:
        print(f"impossible: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
