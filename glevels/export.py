"""Read-only JSON view of the registry, the results log, the backlog and the level summary.

g-command-web's levels page calls snapshot() on each request. Nothing here writes:
grading goes through `glevels grade`, so there is one writer for the log.
project_view() gives a project's charter, pinned gates and latest states without private detail.

    python3 -m glevels.export            the whole snapshot as JSON
    python3 -m glevels.export sample     only the ungraded verdicts

The backlog and the level summary live in var/ (gitignored, private) as dated files.
The newest of each is read. Either can be missing, and the snapshot says so.
"""

import collections
import json
import sys
from pathlib import Path

from . import registry
from .cli import LOG, rows

VAR = registry.ROOT / "var"
COUNTED = ("caught",) + registry.RESULTS
GRADABLE = ("met", "attempted", "caught", "impossible")


def _newest(pattern):
    found = sorted(VAR.glob(pattern))
    return found[-1] if found else None


def _graded_ids(log):
    notes = ((r.get("note") or "") for r in log if r.get("result") == "grade")
    return {n.split()[1] for n in notes if len(n.split()) > 1}


def registry_view():
    """Gates with their taxonomy, charters with extends, and every pin as a link."""
    gates, charters = registry.load_gates(), registry.load_charters()
    out_gates = []
    for gid, g in sorted(gates.items()):
        kind, domain, name = (gid.split(".", 2) + ["", ""])[:3] if gid.count(".") >= 2 else ("", "", gid)
        out_gates.append({
            "id": gid, "kind": g.get("kind"), "prefix": kind, "domain": domain, "name": name,
            "version": g.get("version"), "trigger": g.get("trigger", "every-run"),
            "sensor": g.get("sensor", {}).get("type"), "skill": g.get("skill"),
            "principle": g.get("principle", ""),
        })
    out_charters, links = [], []
    for cid, c in sorted(charters.items()):
        out_charters.append({
            "id": cid, "title": c.get("title", cid), "extends": c.get("extends", []),
            "repos": c.get("repos", []), "principles": c.get("principles", []),
            "chain": [k["id"] for k in registry.chain(cid, charters)],
        })
        for e in c.get("gates", []):
            g = gates.get(e["id"], {})
            waivers = [{"repo": x.get("repo", "*"), "reason": x.get("reason", ""), "review": str(x.get("review", "")),
                        "approved_by": x.get("approved_by", "")}
                       for x in c.get("exceptions", []) if x["gate"] == e["id"]]
            links.append({"charter": cid, "gate": e["id"], "version": e.get("version"),
                          "current": e.get("version") == g.get("version"), "blocking": bool(e.get("blocking")),
                          "paths": e.get("paths", ["**"]), "exceptions": waivers})
        # An exception can sit in a narrower charter than the pin it waives.
        pinned_here = {e["id"] for e in c.get("gates", [])}
        for x in c.get("exceptions", []):
            if x["gate"] not in pinned_here:
                links.append({"charter": cid, "gate": x["gate"], "version": None, "current": True, "blocking": False,
                              "paths": x.get("paths", ["**"]), "waiver_only": True,
                              "exceptions": [{"repo": x.get("repo", "*"), "reason": x.get("reason", ""),
                                              "review": str(x.get("review", "")),
                                              "approved_by": x.get("approved_by", "")}]})
    problems, warnings = registry.lint(gates, charters)
    return {"gates": out_gates, "charters": out_charters, "links": links,
            "lint": {"problems": problems, "warnings": warnings}}


def results_view(reg=None):
    """Counts per gate, per day per gate, silent gates and Gabriel's agreement rate."""
    reg = reg or registry_view()
    log = rows()
    per_gate = collections.defaultdict(collections.Counter)
    per_day = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    last = {}
    for r in log:
        g = r["gate"]
        if r["result"] == "grade":
            per_gate[g]["graded"] += 1
            per_gate[g]["agreed"] += (r.get("note") or "").startswith("agree")
            continue
        per_gate[g][r["result"]] += 1
        per_day[g][r["ts"][:10]][r["result"]] += 1
        last[g] = max(last.get(g, ""), r["ts"])
    triggers = {g["id"]: g["trigger"] for g in reg["gates"]}
    hooks = {g["id"] for g in reg["gates"] if g["sensor"] == "hook"}
    pinned = {l["gate"] for l in reg["links"] if not l.get("waiver_only")}
    gates = []
    for gid in sorted(set(triggers) | set(per_gate)):
        n = per_gate[gid]
        gates.append({
            "gate": gid, "trigger": triggers.get(gid), "pinned": gid in pinned, "registered": gid in triggers,
            "counts": {k: n[k] for k in COUNTED}, "total": sum(n[k] for k in COUNTED),
            "graded": n["graded"], "agreed": n["agreed"],
            "taste": round(n["agreed"] / n["graded"], 3) if n["graded"] else None,
            "last": last.get(gid),
            # Silent: pinned, meant to fire every run, and nothing in the log. A hook enforces at tool
            # time and logs only when it breaks, so it is never silent.
            "silent": gid in pinned and triggers.get(gid) == "every-run" and gid not in last and gid not in hooks,
            "days": [{"day": d, **{k: c[k] for k in COUNTED}} for d, c in sorted(per_day[gid].items())],
        })
    days = sorted({d for g in per_day.values() for d in g})
    total = collections.Counter()
    for gid in per_gate:
        total.update({k: per_gate[gid][k] for k in COUNTED + ("graded", "agreed")})
    return {"log": str(LOG), "rows": len(log), "days": days, "gates": gates, "totals": dict(total)}


def project_view(names, gates=None, charters=None, log=None):
    """The narrowest charter and latest gate states across a project's repo names."""
    gates = registry.load_gates() if gates is None else gates
    charters = registry.load_charters() if charters is None else charters
    log = rows() if log is None else log
    cid, scoped, lineage = None, [], []
    for name in names:
        candidate, entries = registry.in_scope(Path("/nonexistent") / name, "", charters)
        ancestors = registry.chain(candidate, charters) if candidate is not None else []
        if len(ancestors) > len(lineage):
            cid, scoped, lineage = candidate, entries, ancestors
    latest = {}
    for r in log:
        if r["result"] == "grade" or r["repo"] not in names:
            continue
        gid = r["gate"]
        if gid not in latest or r["ts"] > latest[gid]["ts"]:
            latest[gid] = r
    out_gates = []
    counts = dict.fromkeys(registry.RESULTS + ("caught", "silent", "not-run", "hook"), 0)
    blocking = {"total": 0, "silent": 0, "failing": 0}
    for e in scoped:
        g, r = gates[e["id"]], latest.get(e["id"])
        trigger = g.get("trigger", "every-run")
        sensor = g["sensor"]["type"]
        waived = bool(e["waiver"])
        if r is not None:
            state = r["result"]
        elif waived:
            state = "waived"
        elif sensor == "hook":
            state = "hook"
        else:
            state = "silent" if trigger == "every-run" else "not-run"
        out_gates.append({
            "gate": e["id"], "charter": e["charter"], "version": e["version"],
            "trigger": trigger, "sensor": sensor, "blocking": bool(e.get("blocking")),
            "waived": waived, "state": state,
            "last": r["ts"] if r is not None else None, "result": r["result"] if r is not None else None,
        })
        counts[state] = counts.get(state, 0) + 1
        if e.get("blocking"):
            blocking["total"] += 1
            blocking["silent"] += state == "silent"
            blocking["failing"] += state in ("caught", "impossible")
    return {"repos": names, "charter": cid,
            "chain": [{"id": c["id"], "title": c.get("title", c["id"])} for c in lineage],
            "gates": out_gates, "counts": counts, "blocking": blocking}


def projects_view(projects, gates=None, charters=None, log=None):
    """Compact project summaries from one registry and log read."""
    gates = registry.load_gates() if gates is None else gates
    charters = registry.load_charters() if charters is None else charters
    log = rows() if log is None else log
    out = {}
    for pid, names in projects.items():
        view = project_view(names, gates, charters, log)
        out[pid] = {k: view[k] for k in ("charter", "counts", "blocking")}
        out[pid]["total"] = len(view["gates"])
    return out


def sample(n=6, gate=None):
    """Ungraded verdicts, newest first: the pool `glevels sample` draws from, in a stable order."""
    log = rows()
    graded = _graded_ids(log)
    pool = [r for r in log if r["result"] in GRADABLE and r["id"] not in graded and (not gate or r["gate"] == gate)]
    # Repeated runs log identical verdicts. Show one of each, newest first, taking gates in turn
    # so a busy gate cannot fill the whole sample.
    seen, by_gate = set(), collections.OrderedDict()
    for r in pool[-200:][::-1]:
        sig = (r["gate"], r.get("repo"), r.get("path"), r["result"], (r.get("note") or "").strip())
        if sig not in seen:
            seen.add(sig)
            by_gate.setdefault(r["gate"], []).append(r)
    pool, queues = [], list(by_gate.values())
    while len(pool) < n and any(queues):
        for q in queues:
            if q and len(pool) < n:
                pool.append(q.pop(0))
    return [dict({k: r.get(k) for k in ("id", "ts", "gate", "version", "charter", "repo", "path", "result", "by")},
                 note=(r.get("note") or "")[:800]) for r in pool]


def backlog_view(reg=None):
    """The newest candidate-gate backlog, ranked, with each cluster matched to a registered gate."""
    path = _newest("backlog-*.json")
    if path is None:
        return None
    d = json.loads(path.read_text())
    reg = reg or registry_view()
    by_name = {g["name"]: g["id"] for g in reg["gates"]}
    aliases = d.get("registry_snapshot", {}).get("current_id_aliases", {})
    ids = {g["id"] for g in reg["gates"]}
    clusters = []
    for c in d.get("clusters", []):
        built = by_name.get(c["class"]) or (aliases.get(c["class"]) if aliases.get(c["class"]) in ids else None)
        draft = c.get("draft_gate") or {}
        clusters.append({
            "class": c["class"], "rank": c.get("rank"), "count": c["count"], "kind": c["kind"],
            "kind_counts": c.get("kind_counts", {}), "charter_group": c.get("charter_group"),
            "projects": c.get("project_counts", {}), "recommendation": c.get("recommendation"),
            "overlap": [{"gate": aliases.get(o.get("gate"), o.get("gate")), "coverage": o.get("coverage")}
                        for o in c.get("existing_gate_overlap", []) if isinstance(o, dict)],
            "principle": draft.get("principle", ""), "built": built,
            "examples": [e.get("prompt", "")[:160] for e in c.get("examples", [])[:2]],
        })
    groups = [{k: g.get(k) for k in ("id", "rank", "prompt_count", "cluster_count", "kind_counts", "clusters",
                                       "proposed_scope", "proposed_action")}
              for g in d.get("charter_groups", [])]
    none = [{"class": c["class"], "count": c["count"]} for c in d.get("none_classes", [])]
    return {"file": path.name, "created": d.get("created_date"), "summary": d.get("summary", {}),
            "input": {k: d.get("input", {}).get(k) for k in ("selected_rows", "total_rows", "time_range")},
            "clusters": clusters, "groups": groups, "needed_him": none}


def levels_view():
    """Where Gabriel works: level share and correction rate per ISO week."""
    path = _newest("levels-summary-*.json")
    if path is None:
        return None
    d = json.loads(path.read_text())
    weeks = [{k: w.get(k) for k in ("iso_week", "week_start", "partial_week", "prompts", "sessions",
                                      "level_counts", "level_shares", "corrections", "correction_rate",
                                      "l2_l3_share")}
             for w in d.get("by_week", [])]
    m = d.get("method", {})
    return {"file": path.name, "generated_at": d.get("generated_at"), "window": d.get("window", {}),
            "levels": m.get("level_order", []), "unit": m.get("unit"), "weeks": weeks,
            "totals": {k: d.get("totals", {}).get(k) for k in ("prompts", "sessions", "level_counts", "level_shares",
                                                                "corrections", "correction_rate", "l2_l3_share")},
            "trend": d.get("trend", {}), "validation": {k: d.get("validation", {}).get(k) for k in
                                                         ("sample_size", "level_agreement", "correction_agreement")}}


def snapshot(n=6):
    reg = registry_view()
    return {"registry": reg, "results": results_view(reg), "sample": sample(n),
            "backlog": backlog_view(reg), "levels": levels_view()}


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if what == "project":
        out = project_view(sys.argv[2:])
    else:
        out = sample() if what == "sample" else snapshot()
    json.dump(out, sys.stdout, indent=1, default=str)
    print()
