import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from glevels import ideas

FIXTURES = Path(__file__).parent / "fixtures-ideas"
ROOT = Path(__file__).resolve().parent.parent
GATE = "design.product.feature-ideas"


def run_sensor(tmp_path, scenario="saturate", *args):
    out = tmp_path / "out"
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, GLEVELS_AGENT_CMD=shlex.join([sys.executable, str(FIXTURES / "agent.py")]),
               G_AGENT_PROVIDERS=str(FIXTURES / "providers.json"), IDEAS_SCENARIO=scenario,
               IDEAS_CALLS=str(calls))
    p = subprocess.run([sys.executable, "-m", "glevels.ideas", GATE,
                        "--brief", str(FIXTURES / "brief.txt"), "--workdir", str(ROOT),
                        "--out", str(out), "--framings", "|".join(ideas.FRAMINGS), *args],
                       env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)
    return p, out, calls


def test_saturation_then_empty_probe_is_met(tmp_path):
    p, out, calls = run_sensor(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "round 1: 5 new\nround 2: 0 new\nprobe: 0 new" in p.stdout
    assert "verdict: met" in p.stdout
    assert "glevels record design.product.feature-ideas met" in p.stdout
    assert "--honest" in p.stdout
    findings = json.loads((out / "findings.json").read_text())
    assert findings["probe_new"] == 0
    canonical = json.loads((out / "ideas.json").read_text())
    assert len(canonical) == 5
    assert all(len(c["raw"]) == 9 for c in canonical)
    assert len({r["framing"] for c in canonical for r in c["raw"]}) == 5
    assert {r["round"] for r in canonical[0]["raw"]} == {1, 2, 3}
    assert all(r["interprets"] for c in canonical for r in c["raw"])
    report = (out / "shortlist.md").read_text()
    assert report.startswith("1. Idea 4.")
    assert "Came up 9 times across 3 rounds and 5 framings." in report
    logs = [json.loads(line) for line in (out / "rounds.jsonl").read_text().splitlines()]
    assert len(logs) == 13
    assert all({"round", "role", "framing", "provider", "model", "seconds", "new_count"} <= row.keys() for row in logs)
    assert {r["model"] for r in logs if r["role"] == "probe"} == {"gpt-6-astra"}
    assert {r["model"] for r in logs if r["role"] != "probe"} == {"opus"}
    prompts = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(c["role"] == "judge" for c in prompts) == 3
    for call in prompts:
        if call["role"] in ("generator", "probe"):
            assert "Canonical ideas:" not in call["prompt"]


def test_budget_with_new_ideas_is_attempted(tmp_path):
    p, out, calls = run_sensor(tmp_path, "budget", "--rounds", "2", "--shortlist", "3")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "round 2: 5 new" in p.stdout
    assert "budget ran out before saturation" in p.stdout
    assert "verdict: attempted" in p.stdout
    assert "DESIGN PROBE" not in calls.read_text()
    assert len(json.loads((out / "findings.json").read_text())["shortlist"]) == 3


@pytest.mark.parametrize("scenario,count,verdict", [("probe-three", 3, "attempted"), ("probe-one", 1, "met")])
def test_probe_measures_new_space(tmp_path, scenario, count, verdict):
    p, out, _ = run_sensor(tmp_path, scenario)
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"probe: {count} new" in p.stdout
    assert f"verdict: {verdict}" in p.stdout
    findings = json.loads((out / "findings.json").read_text())
    assert findings["probe_new"] == count
    if count == 3:
        assert "probe added 3 ideas: the generator ran out, not the space" in p.stdout


def test_judge_cannot_add_an_idea_without_current_raw_citation(tmp_path):
    p, out, _ = run_sensor(tmp_path, "unattributed")
    assert p.returncode == 0, p.stdout + p.stderr
    canonical = json.loads((out / "ideas.json").read_text())
    assert len(canonical) == 5
    assert not any(c["title"] == "Invented" for c in canonical)
    assert "dropped 2 invalid decisions" in p.stdout
    logs = [json.loads(line) for line in (out / "rounds.jsonl").read_text().splitlines()]
    assert sum(row.get("dropped", 0) for row in logs) == 6


def test_non_design_gate_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ideas, "load_gates", lambda: {"correct.test.example": {"kind": "correcting"}})
    assert ideas.main(["correct.test.example", "--brief", str(FIXTURES / "brief.txt"),
                       "--workdir", str(tmp_path)]) == 2
    assert "impossible: correct.test.example is not a design gate" in capsys.readouterr().out


def test_failed_calls_are_retried_and_reported(tmp_path):
    p, out, _ = run_sensor(tmp_path, "retry")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "verdict: met" in p.stdout
    assert "generator call failed (attempt 1)" in p.stdout
    logs = [json.loads(line) for line in (out / "rounds.jsonl").read_text().splitlines()]
    assert sum(row["status"] == "failed" for row in logs) == 6


def test_partial_generator_failure_continues_without_claiming_met(tmp_path):
    p, out, _ = run_sensor(tmp_path, "partial-fail", "--rounds", "2")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "verdict: attempted" in p.stdout
    assert "failed after retry" in p.stdout
    findings = json.loads((out / "findings.json").read_text())
    assert findings["probe_new"] == 0
    assert "exploration incomplete" in findings["reason"]


@pytest.mark.parametrize("scenario,reason", [("all-fail", "whole round 1 failed"),
                                            ("judge-fail", "could not be judged after retry"),
                                            ("rank-invents", "ranking failed after retry")])
def test_unusable_runs_are_impossible(tmp_path, scenario, reason):
    p, out, _ = run_sensor(tmp_path, scenario)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "impossible:" in p.stdout and reason in p.stdout
    assert not (out / "shortlist.md").exists()


def test_machine_report(tmp_path):
    p, out, _ = run_sensor(tmp_path, "saturate", "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout) == json.loads((out / "findings.json").read_text())


def test_new_counts_are_per_call_even_with_repeated_framings(tmp_path):
    p, out, _ = run_sensor(tmp_path, "saturate", "--framings", "buyer")
    assert p.returncode == 0, p.stdout + p.stderr
    logs = [json.loads(line) for line in (out / "rounds.jsonl").read_text().splitlines()]
    assert sum(row["new_count"] for row in logs if row["round"] == 1 and row["role"] == "generator") == 5


def test_dry_run_calls_no_agent(tmp_path):
    p, _, calls = run_sensor(tmp_path, "all-fail", "--dry-run", "--framings", "buyer|owner")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "budget: 4 rounds" in p.stdout
    assert "round 1 framings: buyer | owner | buyer" in p.stdout
    assert "round 2 framings: owner | buyer | owner" in p.stdout
    assert "generator provider order: claude:opus" in p.stdout
    assert "judge provider order: claude:opus" in p.stdout
    assert "probe provider order: codex:gpt-6-astra" in p.stdout
    assert "first generator prompt:" in p.stdout
    assert not calls.exists()


def test_gate_framings_and_budget_defaults(tmp_path, monkeypatch, capsys):
    gate = {"id": GATE, "kind": "design", "principle": "Explore", "guidance": "Propose useful ideas",
            "budget": "At most two rounds.", "framings": ["client", "analyst"]}
    monkeypatch.setattr(ideas, "load_gates", lambda: {GATE: gate})
    monkeypatch.setenv("G_AGENT_PROVIDERS", str(FIXTURES / "providers.json"))
    assert ideas.main([GATE, "--brief", str(FIXTURES / "brief.txt"), "--workdir", str(tmp_path),
                       "--out", str(tmp_path / "out"), "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["budget"] == 2
    assert plan["round_framings"] == [["client", "analyst", "client"], ["analyst", "client", "analyst"]]


def test_sol_probe_fallback(tmp_path, monkeypatch):
    (tmp_path / "providers.json").write_text((FIXTURES / "providers.json").read_text())
    (tmp_path / "models.json").write_text('{"models":[{"id":"gpt-6.1-sol"}]}')
    monkeypatch.setenv("G_AGENT_PROVIDERS", str(tmp_path / "providers.json"))
    configs = ideas.provider_configs(tmp_path)
    assert configs["probe"]["spec"] == "codex:gpt-6.1-sol"
    assert configs["probe"]["config"]["effort"]["design"] == "high"


def test_provider_override_need_not_have_adjacent_models(tmp_path, monkeypatch):
    (tmp_path / "providers.json").write_text((FIXTURES / "providers.json").read_text())
    monkeypatch.setenv("G_AGENT_PROVIDERS", str(tmp_path / "providers.json"))
    monkeypatch.setattr(ideas, "CONTEXT", FIXTURES)
    configs = ideas.provider_configs(tmp_path)
    assert configs["probe"]["spec"] == "codex:gpt-6-astra"


@pytest.mark.parametrize("text,count", [("At most four rounds.", 4), ("At most 7 rounds", 7),
                                        ("No round limit specified", 4)])
def test_budget_text(text, count):
    assert ideas.budget(text) == count
