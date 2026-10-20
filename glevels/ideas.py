"""Sensor: explore a design gate's ideas, merge them, and check saturation with a probe.

    python3 -m glevels.ideas GATE_ID --brief FILE --workdir DIR [--out DIR]
        [--rounds N] [--per-round 3] [--shortlist 12] [--framings 'a|b|c']
        [--dry-run] [--json]

Strong independent generators and one judge produce a ranked shortlist for Gabriel.
The probe uses a different model family. Budget exhaustion is attempted, not met.
shortlist.md, ideas.json, rounds.jsonl and findings.json go in --out (default: a new
temp dir, printed at the end). No results are recorded automatically.

Exit 0: no clear fault, including attempted exploration. Exit 1: clear faults under
"blocking". Exit 2: could not run, printed as "impossible: ...". Leads under "for the
reviewer" need judgement and never fail the run alone. --dry-run calls no models.
"""
import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .registry import load_gates

CONTEXT = Path("/home/gabriel/genv/context")
FRAMINGS = ["a first-time user", "a power user", "a rival product's PM",
            "the owner's own taste", "a sceptical customer paying for it"]
NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def budget(text):
    match = re.search(r"(?:at most\s+)?(\d+|" + "|".join(NUMBERS[1:]) + r")\s+rounds?\b", text.lower())
    if not match:
        return 4
    word = match[1]
    return int(word) if word.isdigit() else NUMBERS.index(word)


def framings_for(framings, round_no, count):
    return [framings[((round_no - 1) * count + i) % len(framings)] for i in range(count)]


def extract_json(text, key):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data
    raise ValueError(f"no JSON object with a {key} list")


def text_fields(data, fields):
    return all(isinstance(data.get(k), str) and data[k].strip() for k in fields)


def surfaces_ok(value):
    return bool(value) and (isinstance(value, str) or
                            isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value))


def check_ideas(data):
    ideas = data["ideas"]
    if not 5 <= len(ideas) <= 12:
        raise ValueError("generator must return 5-12 ideas")
    for idea in ideas:
        if (not isinstance(idea, dict) or not text_fields(idea, ("title", "who", "what", "why_now"))
                or not surfaces_ok(idea.get("surfaces")) or not isinstance(idea.get("interprets"), str)):
            raise ValueError("generator idea is missing title, who, what, why_now, surfaces or interprets")


def provider_configs(tmp):
    source = Path(os.environ.get("G_AGENT_PROVIDERS", CONTEXT / "providers.json"))
    base = json.loads(source.read_text())
    models_path = source.with_name("models.json")
    models = json.loads((models_path if models_path.is_file() else CONTEXT / "models.json").read_text())
    known = {m["id"] for m in models.get("models", [])}
    known.update(m["model"] for m in models.get("codex_ladder", []))
    probe = "gpt-6-astra" if "gpt-6-astra" in known else "gpt-6.1-sol"
    if probe not in known:
        raise ValueError("models.json knows neither gpt-6-astra nor gpt-6.1-sol for the probe")
    configs = {}
    for name, spec in (("opus", "claude:opus"), ("probe", f"codex:{probe}")):
        config = copy.deepcopy(base)
        # provider_order falls back to roles when value is absent, including on retries.
        config.pop("value", None)
        config.setdefault("roles", {})["design"] = [spec]
        config.setdefault("effort", {})["design"] = "high"
        config.setdefault("sandbox", {})["design"] = "read-only"
        path = tmp / f"providers-{name}.json"
        path.write_text(json.dumps(config, indent=2) + "\n")
        configs[name] = {"path": path, "spec": spec, "config": config}
    return configs


def resolved_orders(configs):
    runner = shutil.which("g-agent-run")
    if not runner:
        return {k: [{"spec": v["spec"], "effort": "high"}] for k, v in configs.items()}
    code = """import argparse, json, os, runpy, sys
from pathlib import Path
runner = runpy.run_path(str(Path(sys.argv[1]).resolve()))
args = argparse.Namespace(role='design', stakes='normal', allow_claude=True, claude_only=False, domain=None)
pace = os.environ.get('G_AGENT_PACE', Path.home() / '.cache/genv/compute-pace.json')
result = {}
for name, path in json.loads(sys.argv[2]).items():
    order = runner['provider_order'](args, json.loads(Path(path).read_text()), path, pace)
    result[name] = [{**p, 'effective_effort': runner['lifted'](p['spec'], p['effort'], pace)} for p in order]
print(json.dumps(result))
"""
    p = subprocess.run([sys.executable, "-c", code, runner,
                        json.dumps({k: str(v["path"]) for k, v in configs.items()})],
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise ValueError(f"could not resolve house provider order: {p.stderr.strip()[-400:]}")
    return json.loads(p.stdout)


def generator_prompt(gate, brief, framing, round_no, role):
    return (f"DESIGN {role.upper()}\nRound: {round_no}\nFraming: {framing}\n"
            f"Gate: {gate['id']}\nPrinciple: {gate['principle']}\nGuidance: {gate['guidance'].strip()}\n\n"
            "Generate 5-12 useful, distinct ideas through this framing. Work independently.\n"
            "Do not read any other generator output, judge output or previous run artifacts.\n"
            "You may read the product and screenshots named in the brief. Do not change files.\n"
            "Write plain, short proposals. Explain how raw results become meaning for the user.\n"
            "Answer JSON only, with no preamble:\n"
            '{"ideas":[{"title":"...","who":"...","what":"2-3 sentences",'
            '"why_now":"...","surfaces":["product area"],"interprets":"meaning, or empty string"}]}\n\n'
            f"Product brief:\n{brief}\n")


def judge_prompt(gate, canonical, raw, round_no):
    existing = [{k: c[k] for k in ("id", "title", "summary")} for c in canonical]
    return (f"DESIGN JUDGE\nRound: {round_no}\nGate: {gate['id']}\n"
            f"Principle: {gate['principle']}\n"
            "You are the single judge. Merge duplicates, including within this round. Never generate ideas.\n"
            "Decide each raw id exactly once. New entries must cite a raw idea from this round and only\n"
            "summarise that idea. Preserve its meaning. Use dup_of for an existing canonical id.\n"
            f"New entries receive c{len(canonical) + 1} onward in decision order; later decisions may cite them.\n"
            "Answer JSON only:\n"
            '{"decisions":[{"raw":"r2.1.3","dup_of":"c4"},{"raw":"r2.1.5",'
            '"new":{"title":"...","summary":"...","who":"...","surfaces":["..."]}}]}\n\n'
            f"Canonical ideas:\n{json.dumps(existing, ensure_ascii=False)}\n\n"
            f"Raw ideas:\n{json.dumps(raw, ensure_ascii=False)}\n")


def rank_prompt(gate, brief, canonical, count):
    entries = [{k: v for k, v in c.items() if k != "raw"} for c in canonical]
    return (f"DESIGN RANK\nGate: {gate['id']}\nPrinciple: {gate['principle']}\n"
            "You are the same judge. Rank existing canonical ids only. Never generate or rewrite ideas.\n"
            "Weigh value to the product's users, fit with what exists, and cost.\n"
            f"Return the best {min(count, len(canonical))} ids in order, with a plain one-line reason each.\n"
            'Answer JSON only: {"shortlist":[{"id":"c1","reason":"..."}]}\n\n'
            f"Product brief:\n{brief}\n\nCanonical ideas:\n{json.dumps(entries, ensure_ascii=False)}\n")


class Runner:
    def __init__(self, workdir, tmp, out, configs):
        self.workdir, self.tmp, self.out, self.configs = workdir, tmp, out, configs
        self.command = shlex.split(os.environ.get("GLEVELS_AGENT_CMD", "g-agent-run"))
        if not self.command:
            raise ValueError("GLEVELS_AGENT_CMD is empty")

    def call(self, role, round_no, framing, index, prompt, key, validate=None):
        config = self.configs["probe" if role == "probe" else "opus"]
        stem = self.tmp / f"{round_no}-{role}-{index}"
        prompt_file = stem.with_suffix(".prompt")
        prompt_file.write_text(prompt)
        rows = []
        for attempt in (1, 2):
            output = Path(f"{stem}-{attempt}.txt")
            meta = Path(f"{stem}-{attempt}.meta.json")
            row = {"round": round_no, "role": role, "framing": framing, "index": index, "attempt": attempt,
                   "provider": None, "model": None, "seconds": 0, "new_count": 0}
            started = time.monotonic()
            try:
                env = dict(os.environ, G_AGENT_PROVIDERS=str(config["path"]))
                p = subprocess.run([*self.command, "--role", "design", "--prompt-file", str(prompt_file),
                                    "--workdir", str(self.workdir), "--out", str(output), "--meta", str(meta),
                                    "--allow-claude"], env=env, capture_output=True, text=True,
                                   timeout=config["config"].get("timeout_s", {}).get("design", 3600) * 4 + 60)
                if meta.exists():
                    info = json.loads(meta.read_text())
                    spec = info.get("provider") or ""
                    row["provider"], _, model = spec.partition(":")
                    row["model"] = info.get("model") or model or None
                    attempts = info.get("attempts") or [{}]
                    row["effort"] = attempts[-1].get("effort")
                if p.returncode:
                    raise ValueError(f"exit {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
                if f"{row['provider']}:{row['model']}" != config["spec"]:
                    raise ValueError(f"agent metadata does not confirm pinned model {config['spec']}")
                data = extract_json(output.read_text(), key)
                if validate:
                    validate(data)
                row["status"] = "ok"
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                data = None
                row.update(status="failed", error=str(error))
            row["seconds"] = round(time.monotonic() - started, 3)
            rows.append(row)
            if data is not None:
                return data, rows
        return None, rows

    def log(self, rows):
        with (self.out / "rounds.jsonl").open("a") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def merge(canonical, raw, decisions):
    sources = {r["id"]: r for r in raw}
    by_id = {c["id"]: c for c in canonical}
    seen, new_sources, dropped = set(), [], 0
    for decision in decisions:
        if not isinstance(decision, dict):
            dropped += 1
            continue
        rid = decision.get("raw")
        if not isinstance(rid, str) or rid not in sources or rid in seen:
            dropped += 1
            continue
        if "new" in decision and "dup_of" not in decision:
            idea = decision["new"]
            if (not isinstance(idea, dict) or not text_fields(idea, ("title", "summary", "who"))
                    or not surfaces_ok(idea.get("surfaces"))):
                dropped += 1
                continue
            target = {k: idea[k] for k in ("title", "summary", "who", "surfaces")}
            target.update(id=f"c{len(canonical) + 1}", raw=[])
            canonical.append(target)
            by_id[target["id"]] = target
            new_sources.append(sources[rid])
        elif ("new" not in decision and isinstance(decision.get("dup_of"), str)
              and decision["dup_of"] in by_id):
            target = by_id[decision["dup_of"]]
        else:
            dropped += 1
            continue
        target["raw"].append(sources[rid])
        seen.add(rid)
    return new_sources, dropped, len(sources) - len(seen)


def run_round(runner, gate, brief, canonical, framings, round_no, count, probe=False):
    role = "probe" if probe else "generator"
    chosen = framings_for(framings, round_no, count)
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(runner.call, role, round_no, framing, i,
                               generator_prompt(gate, brief, framing, round_no, role), "ideas", check_ideas)
                   for i, framing in enumerate(chosen, 1)]
        results = [f.result() for f in futures]
    rows, raw, failures = [], [], []
    for i, (data, calls) in enumerate(results, 1):
        rows.extend(calls)
        if data is None:
            failures.append(f"round {round_no} {role} {i} ({chosen[i - 1]}) failed after retry: {calls[-1]['error']}")
            continue
        raw.extend({**idea, "id": f"r{round_no}.{i}.{j}", "round": round_no, "generator": i,
                    "framing": chosen[i - 1], "role": role,
                    "provider": calls[-1]["provider"], "model": calls[-1]["model"]}
                   for j, idea in enumerate(data["ideas"], 1))
    if not raw:
        runner.log(rows)
        raise ValueError(f"whole round {round_no} failed: " + "; ".join(failures))
    judged, calls = runner.call("judge", round_no, "", 0,
                                 judge_prompt(gate, canonical, raw, round_no), "decisions")
    rows.extend(calls)
    if judged is None:
        runner.log(rows)
        raise ValueError(f"round {round_no} could not be judged after retry: {calls[-1]['error']}")
    new_sources, dropped, omitted = merge(canonical, raw, judged["decisions"])
    for row in rows:
        if row["status"] == "ok":
            row["new_count"] = (len(new_sources) if row["role"] == "judge" else
                                sum(r["generator"] == row["index"] for r in new_sources))
    calls[-1].update(dropped=dropped, omitted=omitted)
    runner.log(rows)
    warnings = failures + [f"round {round_no} {r['role']} call failed (attempt {r['attempt']}): {r['error']}"
                           for r in rows if r["status"] == "failed"]
    if dropped or omitted:
        warnings.append(f"round {round_no} judge: dropped {dropped} invalid decisions; {omitted} raw ideas unmerged")
    (runner.out / "ideas.json").write_text(json.dumps(canonical, indent=2, ensure_ascii=False) + "\n")
    return {"round": round_no, "probe": probe, "new_count": len(new_sources), "dropped": dropped,
            "complete": not failures and not omitted, "warnings": warnings}


def check_ranking(data, canonical, count):
    ids = {c["id"] for c in canonical}
    seen = set()
    for item in data["shortlist"]:
        if (not isinstance(item, dict) or not text_fields(item, ("id", "reason"))
                or item["id"] not in ids or item["id"] in seen):
            raise ValueError("ranker must select existing canonical ids once, each with a reason")
        seen.add(item["id"])
    if len(seen) != min(count, len(ids)):
        raise ValueError("ranker did not return the requested shortlist length")


def one_line(text):
    return " ".join(text.split())


def shortlist_text(canonical, ranked):
    by_id = {c["id"]: c for c in canonical}
    lines = []
    for i, item in enumerate(ranked, 1):
        idea = by_id[item["id"]]
        raw = idea["raw"]
        surfaces = idea["surfaces"]
        surfaces = ", ".join(surfaces) if isinstance(surfaces, list) else surfaces
        rounds = len({r["round"] for r in raw})
        framings = len({r["framing"] for r in raw})
        lines.append(f"{i}. {one_line(idea['title'])}. For {one_line(idea['who'])}. "
                     f"{one_line(item['reason'])} Surfaces: {one_line(surfaces)}. "
                     f"Came up {len(raw)} times across {rounds} rounds and {framings} framings.")
    return "\n\n".join(lines) + "\n"


def execute(a, gate, brief, framings, out, tmp):
    configs = provider_configs(tmp)
    if a.dry_run:
        orders = resolved_orders(configs)
        plan = {"gate": gate["id"], "budget": a.rounds, "per_round": a.per_round,
                "shortlist": a.shortlist, "round_framings": [framings_for(framings, n, a.per_round)
                                                            for n in range(1, a.rounds + 1)],
                "probe": "one extra round after saturation; rotate framings to the next round",
                "providers": {role: orders["probe" if role == "probe" else "opus"]
                              for role in ("generator", "judge", "probe", "rank")},
                "first_prompt": generator_prompt(gate, brief, framings[0], 1, "generator")}
        if a.json:
            print(json.dumps(plan, indent=2))
        else:
            print(f"gate: {gate['id']}\nbudget: {a.rounds} rounds; {a.per_round} generators per round; one probe outside budget")
            for i, chosen in enumerate(plan["round_framings"], 1):
                print(f"round {i} framings: " + " | ".join(chosen))
            for role, order in plan["providers"].items():
                print(f"{role} provider order: " + " -> ".join(
                    f"{p['spec']} ({p.get('effective_effort', p['effort'])})" for p in order))
            print("provider pin: value removed; roles.design contains only the provider shown (high base effort)")
            print("probe framings: rotate to the round after saturation\n\nfirst generator prompt:\n" + plan["first_prompt"])
            print(f"out: {out}")
        return
    runner = Runner(Path(a.workdir).resolve(), tmp, out, configs)
    (out / "rounds.jsonl").write_text("")
    canonical, rounds, probe = [], [], None
    for n in range(1, a.rounds + 1):
        result = run_round(runner, gate, brief, canonical, framings, n, a.per_round)
        rounds.append(result)
        if not a.json:
            print(f"round {n}: {result['new_count']} new", flush=True)
        if result["new_count"] < 1 and result["complete"]:
            probe = run_round(runner, gate, brief, canonical, framings, n + 1, a.per_round, probe=True)
            if not a.json:
                print(f"probe: {probe['new_count']} new", flush=True)
            break
    verdict = "attempted"
    if probe is None:
        reason = "budget ran out before saturation"
    elif probe["new_count"] > 1:
        reason = f"probe added {probe['new_count']} ideas: the generator ran out, not the space"
    elif not all(r["complete"] for r in [*rounds, probe]):
        reason = "failed generators or unmerged raw ideas left exploration incomplete"
    else:
        verdict = "met"
        reason = f"saturation reached in round {rounds[-1]['round']}; probe added {probe['new_count']} ideas"
    ranked, calls = runner.call("rank", rounds[-1]["round"], "", 0,
                                rank_prompt(gate, brief, canonical, a.shortlist), "shortlist",
                                lambda data: check_ranking(data, canonical, a.shortlist))
    runner.log(calls)
    if ranked is None:
        raise ValueError("ranking failed after retry: " + calls[-1]["error"])
    (out / "shortlist.md").write_text(shortlist_text(canonical, ranked["shortlist"]))
    warnings = [w for r in [*rounds, *([probe] if probe else [])] for w in r["warnings"]]
    warnings.extend(f"rank call failed (attempt {r['attempt']}): {r['error']}" for r in calls if r["status"] == "failed")
    counts = ", ".join(f"{r['round']}={r['new_count']}" for r in rounds)
    note = f"rounds {counts}; probe={probe['new_count'] if probe else 'not run'}; {reason}; shortlist: {out / 'shortlist.md'}"
    command = shlex.join(["glevels", "record", gate["id"], verdict, str(Path(a.workdir).resolve()),
                          "--note", note, "--honest",
                          "<reviewer: explain how these ideas meet the principle beyond the counts>"])
    findings = {"gate": gate["id"], "verdict": verdict, "reason": reason, "rounds": rounds,
                "probe_new": probe["new_count"] if probe else None, "blocking": [],
                "for_the_reviewer": warnings, "shortlist": ranked["shortlist"],
                "record_command": command, "out": str(out)}
    (out / "findings.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False) + "\n")
    if a.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
        return
    print(f"stop: {reason}\nverdict: {verdict}")
    print("\nfor the reviewer:")
    for warning in warnings:
        print("  " + warning)
    print(f"  Review {out / 'shortlist.md'} against the brief and ideas.json; replace --honest with your evidence.")
    print("\n" + command)
    print(f"\nout: {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gate")
    ap.add_argument("--brief", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", help="output directory (default: a new temp dir)")
    ap.add_argument("--rounds", type=int)
    ap.add_argument("--per-round", type=int, default=3)
    ap.add_argument("--shortlist", type=int, default=12)
    ap.add_argument("--framings", help="independent framings, separated by |")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        gate = load_gates().get(a.gate)
        if not gate:
            raise ValueError(f"unknown gate: {a.gate}")
        if gate["kind"] != "design":
            raise ValueError(f"{a.gate} is not a design gate")
        a.rounds = a.rounds if a.rounds is not None else budget(gate.get("budget", ""))
        if min(a.rounds, a.per_round, a.shortlist) < 1:
            raise ValueError("rounds, per-round and shortlist must be positive")
        framings = a.framings.split("|") if a.framings is not None else gate.get("framings", FRAMINGS)
        if not isinstance(framings, list) or not framings or not all(isinstance(f, str) and f.strip() for f in framings):
            raise ValueError("framings must be a nonempty list of nonempty strings")
        framings = [f.strip() for f in framings]
        brief = Path(a.brief).read_text().strip()
        if not brief:
            raise ValueError("product brief is empty")
        if not Path(a.workdir).is_dir():
            raise ValueError(f"workdir is not a directory: {a.workdir}")
        out = Path(a.out or tempfile.mkdtemp(prefix="glevels-ideas-")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="glevels-ideas-agents-") as tmp:
            execute(a, gate, brief, framings, out, Path(tmp))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"impossible: {error}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
