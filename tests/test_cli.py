import json
import os
import subprocess
import sys

from glevels import registry


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
