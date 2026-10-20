"""Offline g-agent-run stand-in. Scenarios vary the explored space, not the runner."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--role")
ap.add_argument("--prompt-file")
ap.add_argument("--workdir")
ap.add_argument("--out")
ap.add_argument("--meta")
ap.add_argument("--allow-claude", action="store_true")
a = ap.parse_args()
prompt = Path(a.prompt_file).read_text()
role = prompt.splitlines()[0].split()[-1].lower()
scenario = os.environ.get("IDEAS_SCENARIO", "saturate")
config = json.loads(Path(os.environ["G_AGENT_PROVIDERS"]).read_text())
spec = config["roles"]["design"][0]
assert "value" not in config
assert config["effort"]["design"] == "high"
assert config["sandbox"]["design"] == "read-only"
assert len(config["roles"]["design"]) == 1
assert spec == ("codex:gpt-6-astra" if role == "probe" else "claude:opus")
Path(a.meta).write_text(json.dumps({"provider": spec, "attempts": [{"effort": "high"}]}))
if os.environ.get("IDEAS_CALLS"):
    with open(os.environ["IDEAS_CALLS"], "a") as f:
        f.write(json.dumps({"role": role, "prompt": prompt}) + "\n")
round_match = re.search(r"^Round: (\d+)$", prompt, re.M)
round_no = int(round_match[1]) if round_match else 0
if scenario == "all-fail" or scenario == "partial-fail" and "Framing: a power user\n" in prompt:
    sys.exit("fixture agent unavailable")
if scenario == "judge-fail" and role == "judge":
    Path(a.out).write_text("not JSON")
    sys.exit(0)
if scenario == "retry" and role == "generator" and a.out.endswith("-1.txt"):
    Path(a.out).write_text("not JSON")
    sys.exit(0)
if role in ("generator", "probe"):
    assert "Canonical ideas:" not in prompt and "Raw ideas:" not in prompt
    ideas = []
    for i in range(5):
        topic = round_no * 10 + i if scenario == "budget" else i
        if role == "probe" and scenario in ("probe-three", "probe-one"):
            if i < (3 if scenario == "probe-three" else 1):
                topic = 100 + i
        ideas.append({"title": f"Idea {topic}", "who": "marketing leads",
                      "what": f"Explain campaign signal {topic}. Show its source and a comparison.",
                      "why_now": "The results page has no comparison.", "surfaces": ["results page"],
                      "interprets": "Puts the result in context."})
    data = {"ideas": ideas}
elif role == "judge":
    canonical_text, raw_text = prompt.split("Canonical ideas:\n")[1].split("\n\nRaw ideas:\n")
    canonical, raw = json.loads(canonical_text), json.loads(raw_text)
    by_title = {c["title"]: c["id"] for c in canonical}
    decisions = []
    for idea in raw:
        if idea["title"] in by_title:
            decisions.append({"raw": idea["id"], "dup_of": by_title[idea["title"]]})
        else:
            decisions.append({"raw": idea["id"], "new": {"title": idea["title"], "summary": idea["what"],
                              "who": idea["who"], "surfaces": idea["surfaces"]}})
            by_title[idea["title"]] = f"c{len(by_title) + 1}"
    if scenario == "unattributed":
        decisions += [{"new": {"title": "Invented", "summary": "No source", "who": "nobody", "surfaces": ["nowhere"]}},
                      {"raw": "r999.1.1", "new": {"title": "Invented", "summary": "Wrong round", "who": "nobody", "surfaces": ["nowhere"]}}]
    data = {"decisions": decisions}
else:
    assert role == "rank"
    canonical = json.loads(prompt.split("Canonical ideas:\n")[1])
    count = int(re.search(r"Return the best (\d+) ids", prompt)[1])
    data = {"shortlist": [{"id": c["id"], "reason": "Explains a result on the existing page."}
                          for c in reversed(canonical[-count:])]} if count else {"shortlist": []}
    if scenario == "rank-invents":
        data["shortlist"][0]["id"] = "c999"
text = json.dumps(data)
Path(a.out).write_text("Here is the answer:\n```json\n" + text + "\n```\n" if role in ("generator", "probe") else text)
