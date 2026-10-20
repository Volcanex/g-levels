import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from glevels import runtime

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures-walk"


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="glevels-runtime-test-") as directory:
        yield Path(directory)


@pytest.fixture(scope="module", autouse=True)
def browser_available():
    if not shutil.which("node"):
        pytest.skip("node is missing")
    check = subprocess.run(["node", "-e", """
const {createRequire} = require('node:module');
const path = require('node:path');
const req = createRequire(path.join(process.env.GDESIGN || '/home/gabriel/g-design', 'package.json'));
const {chromium} = req('playwright');
process.exit(require('node:fs').existsSync(chromium.executablePath()) ? 0 : 1);
"""], capture_output=True, text=True)
    if check.returncode:
        pytest.skip("Playwright or Chromium is missing: " + check.stderr[-300:])


def sensor(out, *targets, extra=()):
    return subprocess.run([sys.executable, str(ROOT / "glevels/runtime.py"), "--out", str(out),
                           *extra, *map(str, targets)], cwd=ROOT, capture_output=True, text=True, timeout=700)


def load(out, name="findings.json"):
    return json.loads((out / name).read_text())


def test_clean_passes_and_captures_evidence(tmp_path):
    out = tmp_path / "clean"
    run = sensor(out, FIXTURES / "clean.html")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "0 blocking" in run.stdout
    result = load(out)
    assert result["blocking"] == []
    walk = load(out, "walk.json")
    state = walk["states"][0]
    assert (out / state["screenshot"]).read_bytes().startswith(b"\x89PNG")
    assert "Clean page" in (out / state["text"]).read_text()
    assert state["fps"] > 0
    assert next(c for c in state["controls"] if c["name"] == "Working button")["selector"] == "#working"
    assert next(c for c in state["controls"] if c["name"] == "Hidden button")["visible"] is False


@pytest.fixture(scope="module")
def broken_run():
    with tempfile.TemporaryDirectory(prefix="glevels-runtime-broken-") as directory:
        out = Path(directory)
        run = sensor(out, FIXTURES / "broken.html")
        assert run.returncode == 1, run.stdout + run.stderr
        yield out, run


def test_broken_reports_load_error_fetch_and_visible_error(broken_run):
    out, run = broken_run
    assert {f["kind"] for f in load(out)["blocking"]} >= {"pageerror", "http", "errortext"}
    assert "fixture load exploded" in run.stdout
    assert "HTTP 404" in run.stdout and "data.json" in run.stdout
    assert "Unexpected error" in run.stdout
    assert all(f["sameOrigin"] for f in load(out)["blocking"])


def test_frozen_fails_and_keeps_longtask_evidence(tmp_path):
    out = tmp_path / "frozen"
    run = sensor(out, FIXTURES / "frozen.html")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "freeze: main thread did not answer in 20s" in run.stdout
    assert any(f["kind"] == "freeze" for f in load(out)["blocking"])
    assert any(f["kind"] == "longtask" for f in load(out)["review"])


def test_dead_journey_fails(tmp_path):
    out = tmp_path / "dead"
    journey = tmp_path / "dead.json"
    journey.write_text(json.dumps([{"click": "#dead"}]))
    run = sensor(out, FIXTURES / "dead.html", extra=("--journey", str(journey)))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "dead control" in run.stdout
    assert any(f["kind"] == "dead" for f in load(out)["blocking"])
    assert load(out, "walk.json")["edges"][-1]["outcome"] == "error"


def test_journey_success_and_failures(tmp_path):
    out = tmp_path / "journey"
    journey = tmp_path / "journey.json"
    journey.write_text(json.dumps([{"name": "Working button check"}, {"click": "#working"},
                                   {"expect": "Button worked"}, {"expect": "Missing text"}, {"click": "#absent"}]))
    run = sensor(out, FIXTURES / "clean.html", extra=("--journey", str(journey)))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "expected visible text" in run.stdout and "#absent" in run.stdout
    edges = load(out, "walk.json")["edges"]
    assert edges[2]["outcome"] == "dom"
    assert edges[3]["outcome"] == "none"
    assert len([f for f in load(out)["blocking"] if f["kind"] == "journey"]) == 2


def test_compare_old_new_and_fixed(tmp_path, broken_run):
    old, _ = broken_run
    same = tmp_path / "same"
    run = sensor(same, FIXTURES / "broken.html", extra=("--compare", str(old)))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "0 new" in run.stdout and "OLD" in run.stdout
    assert not load(same)["regressions"]
    assert not load(same)["fixed"]
    journey = tmp_path / "regression.json"
    journey.write_text(json.dumps([{"expect": "A missing result"}]))
    new = tmp_path / "new"
    run = sensor(new, FIXTURES / "broken.html", extra=("--compare", str(old / "findings.json"), "--journey", str(journey)))
    assert run.returncode == 1, run.stdout + run.stderr
    result = load(new)
    assert {f["status"] for f in result["blocking"]} == {"NEW", "OLD"}
    assert [f["kind"] for f in result["regressions"]] == ["journey"]
    fixed = tmp_path / "fixed"
    run = sensor(fixed, FIXTURES / "clean.html", extra=("--compare", str(old)))
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(load(fixed)["fixed"]) == len(load(old)["blocking"])


def test_tree_edges_states_and_safe_skips(tmp_path):
    server, base = runtime.serve(FIXTURES / "tree")
    out = tmp_path / "tree"
    try:
        run = subprocess.run(["node", str(ROOT / "glevels/walk.mjs"), "--mode", "tree", "--safe",
                              "--settle", "50", "--out", str(out), base + "/index.html"],
                             cwd=ROOT, capture_output=True, text=True, timeout=700)
    finally:
        server.shutdown()
        server.server_close()
    assert run.returncode == 0, run.stdout + run.stderr
    walk = load(out, "walk.json")
    assert {s["title"] for s in walk["states"]} == {"Option tree", "Settings", "Quiet page"}
    assert not any("panel=analytics" in s["url"] for s in walk["states"])
    throwing = next(e for e in walk["edges"] if e["action"]["name"] == "Break on click")
    assert throwing["outcome"] == "error"
    assert any(e["kind"] == "pageerror" and "fixture click exploded" in e["message"] for e in throwing["errors"])
    quiet = next(s for s in walk["states"] if s["title"] == "Quiet page")
    assert [c["name"] for c in quiet["controls"]] == ["Home"]
    settings = next(s for s in walk["states"] if s["title"] == "Settings")
    assert next(c for c in settings["controls"] if c["name"] == "Notifications")["checked"] is False
    edges = [e for e in walk["edges"] if e["from"] == settings["id"]]
    assert len([e for e in edges if e["action"]["kind"] == "select"]) == 2
    assert next(e for e in edges if e["action"]["name"] == "Notifications")["outcome"] == "dom"
    skipped = {s["name"]: s["reason"] for s in walk["skipped"]}
    assert "safe" in skipped["Delete account"]
    assert "safe" in skipped["Send profile"]
    assert "outside entry origins" in skipped["External link"]
    assert "outside entry origins" in skipped["External button"]
    assert all(s["url"].startswith(base) for s in walk["states"])
    assert "non-HTTP" in skipped["Email"]
    assert walk["budget"]["hit"] is False


def test_tree_local_form_and_query_entry(tmp_path):
    server, base = runtime.serve(FIXTURES / "tree")
    out = tmp_path / "local-tree"
    try:
        run = subprocess.run(["node", str(ROOT / "glevels/walk.mjs"), "--mode", "tree", "--settle", "30",
                              "--max-actions", "20", "--out", str(out), base + "/settings.html", base + "/index.html?panel=analytics"],
                             cwd=ROOT, capture_output=True, text=True, timeout=700)
    finally:
        server.shutdown()
        server.server_close()
    assert run.returncode == 0, run.stdout + run.stderr
    walk = load(out, "walk.json")
    assert walk["budget"]["actions"] == len(walk["edges"]) <= 20
    assert any("Analytics" in (out / s["text"]).read_text() for s in walk["states"])
    submitted = [e for e in walk["edges"] if e["action"]["kind"] == "submit"]
    assert submitted and any("name=Review" in s["url"] for s in walk["states"])


def test_json_verdict_and_blank_page(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('<!doctype html><title>Blank</title><link rel="icon" href="data:,">')
    out = tmp_path / "blank"
    run = sensor(out, tmp_path, extra=("--json",))
    assert run.returncode == 1, run.stdout + run.stderr
    assert json.loads(run.stdout)["blocking"][0]["kind"] == "blank"


def test_warning_and_missing_image_are_reviewer_leads(tmp_path):
    page = tmp_path / "review.html"
    page.write_text('<!doctype html><title>Review leads</title><link rel="icon" href="data:,">'
                    '<h1>Review leads</h1><img src="missing.png" alt="Missing illustration">'
                    '<script>console.warn("fixture warning")</script>')
    out = tmp_path / "review"
    run = sensor(out, page)
    assert run.returncode == 0, run.stdout + run.stderr
    assert not load(out)["blocking"]
    assert {f["kind"] for f in load(out)["review"]} >= {"console", "http"}
    assert load(out, "walk.json")["edges"][0]["outcome"] == "navigate"


def test_memory_guard_refuses_before_browser_launch(tmp_path):
    env = dict(os.environ, GSPACING_MIN_FREE_MB="999999")
    run = subprocess.run([sys.executable, str(ROOT / "glevels/runtime.py"), "--out", str(tmp_path),
                          str(FIXTURES / "clean.html")], env=env, capture_output=True, text=True, timeout=700)
    assert run.returncode == 2
    assert "impossible:" in run.stdout and "memory free" in run.stdout
    assert not (tmp_path / "walk.json").exists()


def test_reviewer_findings_do_not_block():
    errors = [{"kind": "console", "level": "warning", "message": "review warning", "sameOrigin": True},
              {"kind": "console", "level": "error", "message": "third party", "sameOrigin": False},
              {"kind": "http", "status": 404, "resourceType": "image", "message": "missing image", "sameOrigin": True},
              {"kind": "http", "status": 503, "resourceType": "script", "message": "third party", "sameOrigin": False}]
    state = {"id": "s1", "url": "http://localhost/test", "errors": errors, "fps": 12,
             "animatingCanvas": True, "longtasks": [1200]}
    result = runtime.findings({"states": [state], "edges": []}, [state["url"]], [state["url"]])
    assert result["blocking"] == []
    assert {f["kind"] for f in result["review"]} == {"console", "http", "fps", "longtask"}
