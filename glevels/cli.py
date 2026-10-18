"""glevels: gates and charters. See CLAUDE.md for the model."""

import argparse
import collections
import datetime
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from . import registry

LOG = Path(os.environ.get("GLEVELS_LOG", registry.ROOT / "var" / "results.jsonl"))


def record(gate, version, charter, repo, rel, result, note="", by=None):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "gate": gate, "version": version,
           "charter": charter, "repo": repo, "path": rel, "result": result, "note": note,
           "by": by or os.environ.get("GLEVELS_BY", "agent")}
    row["id"] = row_id(row)
    with LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def row_id(row):
    return hashlib.sha1(json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True).encode()).hexdigest()[:8]


def rows():
    if not LOG.exists():
        return []
    out = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    for r in out:
        r.setdefault("id", row_id(r))
    return out


def target(path):
    path = Path(path).resolve()
    repo = registry.repo_of(path)
    rel = "" if path == repo else str(path.relative_to(repo))
    return repo, rel


def files_under(repo, rel, globs):
    import fnmatch
    base = repo / rel if rel else repo
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "--", str(base)], capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if any(fnmatch.fnmatch(f, g) for g in globs)]


def fill(cmd, **values):
    for k, v in values.items():
        cmd = cmd.replace("{" + k + "}", str(v))
    return cmd


def run_sensor(gate, entry, repo, rel):
    """Returns (result, output): met, caught, or impossible when every failure exits with the sensor's
    cannot_run_exit. A command containing {file} runs once per tracked file in scope."""
    cmd = gate["sensor"]["run"]
    if "{file}" in cmd:
        runs = [(f, subprocess.run(fill(cmd, file=repo / f, repo=repo), shell=True, capture_output=True, text=True))
                for f in files_under(repo, rel, entry.get("paths", ["**"]))]
    else:
        runs = [(rel, subprocess.run(fill(cmd, repo=repo, path=repo / rel), shell=True, capture_output=True, text=True))]
    fails = [(f, p) for f, p in runs if p.returncode]
    if not fails:
        return "met", f"{len(runs)} files"
    cannot = gate["sensor"].get("cannot_run_exit")
    result = "impossible" if cannot and all(p.returncode == cannot for _, p in fails) else "caught"
    return result, "\n\n".join(f"{f}\n{(p.stdout + p.stderr).strip()[-800:]}" for f, p in fails)


def cmd_check(a):
    gates, charters = registry.load_gates(), registry.load_charters()
    repo, rel = target(a.path)
    cid, scoped = registry.in_scope(repo, rel, charters)
    if cid is None:
        print("no charter covers this path")
        return 0
    triggers = set(registry.TRIGGERS) if a.all else {"every-run"}
    caught = 0
    print(f"{repo.name}/{rel}  charter {cid}")
    for e in scoped:
        g = gates[e["id"]]
        if a.gate and g["id"] not in a.gate:
            continue
        if g.get("trigger", "every-run") not in triggers and not a.gate:
            continue
        tag = " (blocking)" if e.get("blocking") else ""
        if e["waiver"]:
            record(g["id"], g["version"], e["charter"], repo.name, rel, "waived", e["waiver"]["reason"])
            print(f"  waived     {g['id']}{tag}: {e['waiver']['reason']}")
        elif g["sensor"]["type"] == "command" or "run" in g["sensor"]:
            # A review gate with a run command has a mechanical half: it runs here, and its catch is
            # logged, then the rubric still goes to an independent reviewer.
            result, out = run_sensor(g, e, repo, rel)
            review = g["sensor"]["type"] == "review"
            if result == "met":
                if not review:
                    record(g["id"], g["version"], e["charter"], repo.name, rel, "met")
                print(f"  {'mechanical' if review else 'met       '} {g['id']}{tag}{' passed' if review else ''}")
            else:
                caught += result == "caught"
                record(g["id"], g["version"], e["charter"], repo.name, rel, result, out[:500])
                print(f"  {result:<10} {g['id']}{tag}\n    fix: {g['remediation'].strip()}\n    {out.replace(chr(10), chr(10) + '    ')}")
            if review:
                print(f"  review     {g['id']}{tag}: {g['sensor']['rubric'].strip()}\n"
                      f"    then: glevels record {g['id']} met|attempted|impossible {a.path} --honest \"...\"")
        elif g["sensor"]["type"] == "review":
            print(f"  review     {g['id']}{tag}: {g['sensor']['rubric'].strip()}\n"
                  f"    then: glevels record {g['id']} met|attempted|impossible {a.path} --honest \"...\"")
        else:
            print(f"  hook       {g['id']}{tag}: enforced at tool time by {g['sensor']['hook']}")
    print(f"\nEvery gate: {registry.HONESTY}")
    return 1 if caught else 0


def cmd_record(a):
    gates, charters = registry.load_gates(), registry.load_charters()
    if a.gate not in gates:
        sys.exit(f"unknown gate {a.gate}")
    if a.result in ("attempted", "impossible") and not a.note:
        sys.exit(f"{a.result} needs --note saying what is missing or why")
    if a.result in ("met", "attempted") and not a.honest:
        sys.exit(f"{a.result} needs --honest: one line on how you know the principle holds, not just the sensor.\n"
                 f"{registry.HONESTY}")
    repo, rel = target(a.path)
    cid, scoped = registry.in_scope(repo, rel, charters)
    entry = next((e for e in scoped if e["id"] == a.gate), None)
    if entry is None:
        sys.exit(f"{a.gate} is not in scope at {a.path} (charter {cid})")
    note = " | ".join(x for x in (a.note, a.honest and f"honest: {a.honest}") if x)
    record(a.gate, gates[a.gate]["version"], entry["charter"], repo.name, rel, a.result, note, by=a.by)
    if a.result == "impossible":
        print("recorded. impossible does not ship: tell Gabriel, or raise it at charter review if the charter allows")
    return 0


def cmd_status(a):
    """Latest result per in-scope gate. A gate with no result is the one failure: silence."""
    gates, charters = registry.load_gates(), registry.load_charters()
    repo, rel = target(a.path)
    cid, scoped = registry.in_scope(repo, rel, charters)
    latest = {}
    for r in rows():
        if r["repo"] == repo.name and (not rel or r["path"].startswith(rel)):
            latest[r["gate"]] = r
    silent = 0
    print(f"{repo.name}/{rel}  charter {cid}")
    for e in scoped:
        r = latest.get(e["id"])
        trig = gates[e["id"]].get("trigger", "every-run")
        if r is None:
            silent += trig == "every-run" and gates[e["id"]]["sensor"]["type"] != "hook"
        print(f"  {r['result'] if r else 'silent':10} {e['id']:24} {trig:10} {r['ts'] if r else ''}")
    return 1 if silent else 0


def cmd_counts(a):
    """Counts go up: per charter, gate and repo. Notes stay at the gate (glevels notes)."""
    c = collections.defaultdict(collections.Counter)
    for r in rows():
        if a.charter and r["charter"] != a.charter:
            continue
        key = (r["charter"], r["gate"], r["repo"])
        if r["result"] == "grade":
            c[key]["graded"] += 1
            c[key]["agreed"] += r["note"].startswith("agree")
        else:
            c[key][r["result"]] += 1
    cols = ("caught",) + registry.RESULTS
    print(f"{'charter':14} {'gate':36} {'repo':14} " + " ".join(f"{x:>10}" for x in cols) + "      taste")
    for (ch, g, rp), n in sorted(c.items()):
        taste = f"{n['agreed']}/{n['graded']}" if n["graded"] else "-"
        print(f"{ch:14} {g:36} {rp:14} " + " ".join(f"{n[x]:>10}" for x in cols) + f" {taste:>10}")
    print("\ntaste = Gabriel agreed with the gate's verdict / results he graded (glevels sample).")
    return 0


def cmd_notes(a):
    for r in rows():
        if r["gate"] == a.gate and r["note"] and (not a.repo or r["repo"] == a.repo):
            print(f"{r['ts']}  {r['repo']}/{r['path']}  {r['result']}: {r['note']}")
    return 0


def cmd_gates(a):
    for g in registry.load_gates().values():
        print(f"{g['id']:36} v{g['version']}  {g.get('trigger', 'every-run'):10} "
              f"{g['sensor']['type']:8} {g['principle'].strip().splitlines()[0]}")
    return 0


def cmd_show(a):
    gates, charters = registry.load_gates(), registry.load_charters()
    item = gates.get(a.id) or charters.get(a.id)
    if item is None:
        sys.exit(f"no gate or charter {a.id}")
    print(json.dumps({k: v for k, v in item.items() if not k.startswith("_")}, indent=2, default=str))
    return 0


def gate_block(g, entry=None):
    lines = [f"### {g['id']}" + (" (blocking)" if entry and entry.get("blocking") else "")]
    lines.append(f"Principle: {' '.join(g['principle'].split())}")
    if g.get("skill"):
        lines.append(f"Load the {g['skill']} skill before starting.")
    lines.append(f"Guidance: {' '.join(g['guidance'].split())}")
    lines.append(f"Passes when: {' '.join(g['pass'].split())}")
    s = g["sensor"]
    if s["type"] == "command":
        lines.append(f"Checked by: `glevels check <path> --gate {g['id']}`. Run it before reporting done and fix every catch.")
    elif s["type"] == "review":
        lines.append(f"Checked by an independent reviewer, not you, against: {' '.join(s['rubric'].split())}")
    else:
        lines.append(f"Enforced at tool time by a hook: {s['hook']}")
    if entry and entry.get("paths"):
        lines.append(f"Applies to: {', '.join(entry['paths'])}")
    return "\n".join(lines)


def cmd_brief(a):
    """Gate guidance for a subagent brief. The orchestrator pastes it in and later runs the review."""
    gates, charters = registry.load_gates(), registry.load_charters()
    picked = {}
    for cid in a.charter or []:
        if cid not in charters:
            sys.exit(f"no charter {cid}")
        for c in registry.chain(cid, charters):
            for e in c.get("gates", []):
                picked[e["id"]] = e
    for gid in a.gate or []:
        if gid not in gates:
            sys.exit(f"no gate {gid}")
        picked.setdefault(gid, {"id": gid})
    if not picked:
        sys.exit("name at least one --charter or --gate")
    if a.review:
        print(f"## Independent review of {a.review}\n")
        print("You did not do this work. Judge it, do not fix it. For each gate, read the work and the brief, "
              "then record one result with your evidence. Ask of the worker's claims: "
              + registry.HONESTY + "\n")
        for gid, e in picked.items():
            g = gates[gid]
            if g["sensor"]["type"] != "review":
                continue
            print(f"### {gid}\nRubric: {' '.join(g['sensor']['rubric'].split())}\n"
                  f"Record: `glevels record {gid} met|attempted|impossible {a.review} --by reviewer "
                  f"--honest \"<evidence>\" [--note \"<what is missing>\"]`\n")
        return 0
    print("## Gates on this work\n")
    print("Your work is checked against these gates. Meet the principle, not just the check: "
          + registry.HONESTY + " If a gate can't be met here, say so with the reason: that is a result, not a failure. "
          "Silence is the only failure.\n")
    for gid, e in picked.items():
        print(gate_block(gates[gid], e) + "\n")
    print("Report one line per gate at the end: id, met, attempted, impossible or not run, and why.")
    return 0


def cmd_sample(a):
    """Human taste sampling: show a few recent verdicts for Gabriel to grade."""
    graded = {r["note"].split(" ", 2)[1] for r in rows() if r["result"] == "grade" and len(r["note"].split()) > 1}
    pool = [r for r in rows() if r["result"] in ("met", "attempted", "caught", "impossible")
            and r["id"] not in graded and (not a.gate or r["gate"] == a.gate)]
    pool = pool[-200:]
    for r in random.sample(pool, min(a.n, len(pool))):
        print(f"{r['id']}  {r['gate']}  {r['repo']}/{r['path']}  {r['result']}  by {r.get('by', '?')}  {r['ts']}")
        if r["note"]:
            print(f"    {' '.join(r['note'].split())[:300]}")
    print("\nGrade each: glevels grade <id> agree|disagree [--note \"why\"]")
    return 0


def cmd_grade(a):
    match = next((r for r in rows() if r["id"] == a.id and r["result"] != "grade"), None)
    if match is None:
        sys.exit(f"no result {a.id}")
    record(match["gate"], match["version"], match["charter"], match["repo"], match["path"], "grade",
           f"{a.verdict} {a.id}" + (f" {a.note}" if a.note else ""), by="gabriel")
    return 0


def cmd_map(a):
    """The taxonomy: every gate, its kind, sensor, trigger, skill, and the charters that pin it."""
    gates, charters = registry.load_gates(), registry.load_charters()
    print(f"{'gate':36} {'sensor':8} {'trigger':10} {'skill':16} charters")
    for gid, g in sorted(gates.items()):
        pins = []
        for c in charters.values():
            for e in c.get("gates", []):
                if e["id"] == gid:
                    waived = [x.get("repo", "*") for x in c.get("exceptions", []) if x["gate"] == gid]
                    pins.append(c["id"] + ("!" if e.get("blocking") else "") + (f" (waived: {', '.join(waived)})" if waived else ""))
            for x in c.get("exceptions", []):
                if x["gate"] == gid and not any(e["id"] == gid for e in c.get("gates", [])):
                    pins.append(f"{c['id']} waives for {x.get('repo', '*')}")
        print(f"{gid:36} {g['sensor']['type']:8} {g.get('trigger', 'every-run'):10} "
              f"{g.get('skill', '-'):16} {', '.join(pins) or '-'}")
    print("\n! = blocking. Skills are the guidance half, sensors the other.")
    return 0


def cmd_lint(a):
    problems, warnings = registry.lint(registry.load_gates(), registry.load_charters())
    for w in warnings:
        print("warn:", w)
    for p in problems:
        print("error:", p)
    print("ok" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(prog="glevels", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="run the gates in scope at a path")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--gate", action="append", help="only this gate (any trigger)")
    p.add_argument("--all", action="store_true", help="include milestone and on-demand gates")
    p.set_defaults(f=cmd_check)
    p = sub.add_parser("record", help="record a result for a review gate")
    p.add_argument("gate")
    p.add_argument("result", choices=registry.RESULTS)
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--note")
    p.add_argument("--honest", help="how you know the principle holds, not just the sensor")
    p.add_argument("--by", choices=("worker", "reviewer", "gabriel", "agent"), help="who judged it")
    p.set_defaults(f=cmd_record)
    p = sub.add_parser("status", help="latest result per gate in scope, silence flagged")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(f=cmd_status)
    p = sub.add_parser("counts", help="result counts per charter, gate and repo")
    p.add_argument("--charter")
    p.set_defaults(f=cmd_counts)
    p = sub.add_parser("notes", help="notes left at one gate")
    p.add_argument("gate")
    p.add_argument("--repo")
    p.set_defaults(f=cmd_notes)
    sub.add_parser("gates", help="list the registry").set_defaults(f=cmd_gates)
    p = sub.add_parser("show", help="print one gate or charter")
    p.add_argument("id")
    p.set_defaults(f=cmd_show)
    p = sub.add_parser("brief", help="gate guidance for a subagent brief, or --review for its reviewer")
    p.add_argument("--charter", action="append")
    p.add_argument("--gate", action="append")
    p.add_argument("--review", metavar="PATH", help="print the independent reviewer's brief for this work")
    p.set_defaults(f=cmd_brief)
    p = sub.add_parser("sample", help="recent verdicts for Gabriel to grade")
    p.add_argument("--gate")
    p.add_argument("-n", type=int, default=5)
    p.set_defaults(f=cmd_sample)
    p = sub.add_parser("grade", help="Gabriel's grade on one verdict")
    p.add_argument("id")
    p.add_argument("verdict", choices=("agree", "disagree"))
    p.add_argument("--note")
    p.set_defaults(f=cmd_grade)
    sub.add_parser("map", help="the gate taxonomy and its links").set_defaults(f=cmd_map)
    sub.add_parser("lint", help="validate gates and charters").set_defaults(f=cmd_lint)
    a = ap.parse_args()
    sys.exit(a.f(a))
