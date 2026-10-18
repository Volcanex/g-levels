"""glevels: gates and charters. See CLAUDE.md for the model."""

import argparse
import collections
import datetime
import json
import os
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
    with LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def rows():
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]


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
    """Returns (ok, output). A command containing {file} runs once per tracked file in scope."""
    cmd = gate["sensor"]["run"]
    if "{file}" in cmd:
        files = files_under(repo, rel, entry.get("paths", ["**"]))
        fails = []
        for f in files:
            p = subprocess.run(fill(cmd, file=repo / f, repo=repo), shell=True, capture_output=True, text=True)
            if p.returncode:
                fails.append(f"{f}\n{(p.stdout + p.stderr).strip()[:800]}")
        return not fails, "\n\n".join(fails) or f"{len(files)} files"
    p = subprocess.run(fill(cmd, repo=repo, path=repo / rel), shell=True, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:]


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
        elif g["sensor"]["type"] == "command":
            ok, out = run_sensor(g, e, repo, rel)
            if ok:
                record(g["id"], g["version"], e["charter"], repo.name, rel, "met")
                print(f"  met        {g['id']}{tag}")
            else:
                caught += 1
                record(g["id"], g["version"], e["charter"], repo.name, rel, "caught", out[:500])
                print(f"  caught     {g['id']}{tag}\n    fix: {g['remediation'].strip()}\n    {out.replace(chr(10), chr(10) + '    ')}")
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
    record(a.gate, gates[a.gate]["version"], entry["charter"], repo.name, rel, a.result, note)
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
            silent += trig == "every-run"
        print(f"  {r['result'] if r else 'silent':10} {e['id']:24} {trig:10} {r['ts'] if r else ''}")
    return 1 if silent else 0


def cmd_counts(a):
    """Counts go up: per charter, gate and repo. Notes stay at the gate (glevels notes)."""
    c = collections.defaultdict(collections.Counter)
    for r in rows():
        if a.charter and r["charter"] != a.charter:
            continue
        c[(r["charter"], r["gate"], r["repo"])][r["result"]] += 1
    cols = ("caught",) + registry.RESULTS
    print(f"{'charter':14} {'gate':24} {'repo':18} " + " ".join(f"{x:>10}" for x in cols))
    for (ch, g, rp), n in sorted(c.items()):
        print(f"{ch:14} {g:24} {rp:18} " + " ".join(f"{n[x]:>10}" for x in cols))
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
    sub.add_parser("map", help="the gate taxonomy and its links").set_defaults(f=cmd_map)
    sub.add_parser("lint", help="validate gates and charters").set_defaults(f=cmd_lint)
    a = ap.parse_args()
    sys.exit(a.f(a))
