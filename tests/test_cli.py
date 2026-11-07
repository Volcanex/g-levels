import argparse
import json
import os
import subprocess
import sys

import pytest

from glevels import cli, registry


def run(*args, log):
    env = dict(os.environ, GLEVELS_LOG=str(log))
    return subprocess.run([sys.executable, "-m", "glevels", *args], cwd=registry.ROOT, env=env,
                          capture_output=True, text=True)


def test_record_needs_honesty_then_grade_counts_as_taste(tmp_path):
    log = tmp_path / "r.jsonl"
    assert run("record", "correct.secrets.argv", "met", ".", log=log).returncode != 0
    assert run("record", "correct.secrets.argv", "met", ".", "--by", "reviewer",
               "--honest", "read every unit added", log=log).returncode == 0
    rid = json.loads(log.read_text())["id"]
    assert run("grade", rid, "disagree", "--note", "missed a cron line", log=log).returncode == 0
    out = run("counts", log=log).stdout
    assert "0/1" in out
    assert rid not in run("sample", log=log).stdout


def test_brief_feeds_gates_and_review_names_only_review_gates(tmp_path):
    log = tmp_path / "r.jsonl"
    out = run("brief", "--charter", "g-site", log=log).stdout
    assert "correct.copy.voice" in out and "Silence is the only failure" in out
    review = run("brief", "--charter", "g-system", "--review", ".", log=log).stdout
    assert "correct.secrets.argv" in review and "correct.secrets.git-history" not in review


def test_exit_code_only_means_impossible_when_the_gate_says_so(tmp_path):
    from glevels.cli import run_sensor
    gate = {"sensor": {"run": "exit 2"}}
    assert run_sensor(gate, {}, tmp_path, "")[0] == "caught"
    gate["sensor"]["cannot_run_exit"] = 2
    assert run_sensor(gate, {}, tmp_path, "")[0] == "impossible"
    assert run_sensor({"sensor": {"run": "true"}}, {}, tmp_path, "")[0] == "met"


@pytest.fixture
def candidate_repo(tmp_path, monkeypatch):
    repo = tmp_path / "candidate"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "page.html").write_text("<p>Candidate</p>")
    subprocess.run(["git", "-C", str(repo), "add", "page.html"], check=True)
    gate = {"id": "correct.test.honesty", "version": 1, "remediation": "Check the candidate",
            "sensor": {"type": "command", "run": "test -f {file}"}}
    entry = {"id": gate["id"], "version": 1, "paths": ["*.html"], "blocking": True}
    monkeypatch.setattr(cli, "LOG", tmp_path / "results.jsonl")
    monkeypatch.setattr(registry, "load_gates", lambda: {gate["id"]: gate})
    monkeypatch.setattr(registry, "load_charters", lambda: {
        "test": {"id": "test", "repos": [repo.name], "gates": [entry]}})
    return repo, gate, entry


def check_candidate(repo):
    return cli.cmd_check(argparse.Namespace(path=str(repo), all=False, gate=None))


def review_candidate(repo, gate, result="met", by="reviewer"):
    return cli.cmd_record(argparse.Namespace(path=str(repo), gate=gate["id"], result=result,
                                           note="Reviewed the page", honest="Read the candidate", by=by))


@pytest.mark.parametrize("case", ["empty-index", "unmatched-inputs"])
def test_in_scope_gate_without_inputs_is_impossible(candidate_repo, capsys, case):
    repo, gate, entry = candidate_repo
    if case == "empty-index":
        subprocess.run(["git", "-C", str(repo), "rm", "--cached", "-q", "page.html"], check=True)
    else:
        entry["paths"] = ["*.css"]
    assert check_candidate(repo) == 1
    row, = cli.rows()
    assert (row["result"], row["note"]) == ("impossible", "no inputs")
    assert "no inputs" in capsys.readouterr().out


def test_in_scope_gate_with_clean_inputs_still_passes(candidate_repo):
    repo, _, _ = candidate_repo
    assert check_candidate(repo) == 0
    assert [row["result"] for row in cli.rows()] == ["met"]


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("mechanical", [True, False])
def test_review_without_verdict_is_attempted(candidate_repo, capsys, blocking, mechanical):
    repo, gate, entry = candidate_repo
    gate["sensor"] = {"type": "review", "rubric": "Inspect the page"}
    if mechanical:
        gate["sensor"]["run"] = "test -f {file}"
    entry["blocking"] = blocking
    assert check_candidate(repo) == int(blocking)
    row, = cli.rows()
    assert row["result"] == "attempted"
    assert "missing" in row["note"] and "review" in row["note"]
    assert gate["id"] in capsys.readouterr().out


@pytest.mark.parametrize("case", ["tracked-edit", "staged-edit", "new-file", "gate-version", "worker", "legacy"])
def test_review_for_another_candidate_does_not_clear_gate(candidate_repo, case):
    repo, gate, _ = candidate_repo
    gate["sensor"].update(type="review", rubric="Inspect the page")
    if case == "legacy":
        cli.record(gate["id"], gate["version"], "test", repo.name, "", "met", by="reviewer")
    else:
        assert review_candidate(repo, gate, by="worker" if case == "worker" else "reviewer") == 0
    if case in ("tracked-edit", "staged-edit"):
        (repo / "page.html").write_text("<p>Changed candidate</p>")
        if case == "staged-edit":
            subprocess.run(["git", "-C", str(repo), "add", "page.html"], check=True)
    elif case == "new-file":
        (repo / "extra.html").write_text("<p>Unreviewed page</p>")
    elif case == "gate-version":
        gate["version"] += 1
    assert check_candidate(repo) == 1
    assert cli.rows()[-1]["result"] == "attempted"
    assert "missing" in cli.rows()[-1]["note"]


@pytest.mark.parametrize("mechanical", [True, False])
def test_reviewed_clean_candidate_still_passes(candidate_repo, mechanical):
    repo, gate, _ = candidate_repo
    gate["sensor"].update(type="review", rubric="Inspect the page")
    if not mechanical:
        del gate["sensor"]["run"]
    assert check_candidate(repo) == 1
    assert review_candidate(repo, gate) == 0
    assert check_candidate(repo) == 0
    assert check_candidate(repo) == 0
    assert cli.rows()[-1]["result"] == "met"


@pytest.mark.parametrize("verdict", ["attempted", "impossible"])
def test_latest_reviewer_verdict_still_blocks(candidate_repo, verdict):
    repo, gate, _ = candidate_repo
    gate["sensor"].update(type="review", rubric="Inspect the page")
    assert review_candidate(repo, gate) == 0
    assert review_candidate(repo, gate, verdict) == 0
    assert check_candidate(repo) == 1
    assert cli.rows()[-1]["result"] == verdict


def test_mechanical_failure_still_blocks_reviewed_candidate(candidate_repo):
    repo, gate, _ = candidate_repo
    gate["sensor"].update(type="review", rubric="Inspect the page", run="exit 1")
    assert review_candidate(repo, gate) == 0
    assert check_candidate(repo) == 1
    assert cli.rows()[-1]["result"] == "caught"


FIXTURES = registry.ROOT / "tests" / "fixtures-spacing"


@pytest.mark.skipif(not os.environ.get("GLEVELS_BROWSER_TESTS"), reason="needs Playwright, set GLEVELS_BROWSER_TESTS=1")
def test_spacing_blocks_the_failure_fixtures():
    spacing = registry.ROOT / "glevels" / "spacing.py"
    for page in ("original.html", "related.html"):
        p = subprocess.run([sys.executable, str(spacing), str(FIXTURES / page)], capture_output=True, text=True)
        assert p.returncode == 1, p.stdout[-500:]
    p = subprocess.run([sys.executable, str(spacing), str(FIXTURES / "related.html"), str(FIXTURES / "related-sibling.html")],
                       capture_output=True, text=True)
    assert p.returncode == 1
