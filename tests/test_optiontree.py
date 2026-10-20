import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from glevels import optiontree

FIXTURES = Path(__file__).parent / "fixtures-optiontree"
SENSOR = Path(optiontree.__file__)


@pytest.fixture
def walk():
    return json.loads((FIXTURES / "walk.json").read_text())


def clean_walk():
    return {"entry": ["http://127.0.0.1/"], "states": [
        {"id": "home", "url": "http://127.0.0.1/", "title": "Home", "depth": 0,
         "text": "Welcome home.", "controls": [], "errors": [], "forms": []}
    ], "edges": [], "budget": {"hit": False}}


def test_blocking_faults_and_unreached_states(walk):
    result = optiontree.analyse(walk, FIXTURES, routes=["/", "/settings", "/linked", "/missing"])
    faults = "\n".join(result["blocking"])
    assert "clicking 'Add task' on Tasks" in faults
    assert "led to an error: Unexpected error" in faults
    assert "'Save draft' on Tasks" in faults and "nothing happened" in faults
    assert "Task failure" in faults and "error or blank state" in faults
    assert "Blank screen" in faults and "blank state reached" in faults
    assert "unreachable route /missing" in faults
    assert "unreachable route /linked" not in faults
    assert "unreachable route /settings" not in faults
    assert "Disabled action" not in faults and "Secret report" not in faults
    assert "Unreachable orphan" not in faults and "unrelated exception" not in faults


@pytest.mark.parametrize("error,blocks", [
    ({"kind": "pageerror", "message": "exception", "sameOrigin": False}, True),
    ({"kind": "console", "message": "console error", "sameOrigin": True}, True),
    ({"kind": "console", "message": "third party error", "sameOrigin": False}, False),
    ({"kind": "console", "message": "console warning", "sameOrigin": True, "level": "warning"}, False),
    ({"kind": "http", "message": "server error", "status": 503, "sameOrigin": True}, True),
    ({"kind": "http", "message": "third party server", "status": 503, "sameOrigin": False}, False),
    ({"kind": "http", "message": "missing image", "status": 404, "sameOrigin": True}, False),
    ({"kind": "errortext", "message": "Unexpected error"}, True),
    ({"kind": "blank", "message": "blank screen"}, True),
    ({"kind": "requestfailed", "message": "request interrupted", "sameOrigin": True}, False),
])
def test_edge_error_kinds(walk, error, blocks):
    edge = walk["edges"][1]
    edge.update(outcome="dom", errors=[error])
    result = optiontree.analyse(walk, FIXTURES)
    faults = [s for s in result["blocking"] if "'Save draft'" in s]
    assert bool(faults) == blocks


def test_error_outcome_without_details(walk):
    walk["edges"][1].update(outcome="error", errors=[])
    assert any("'Save draft'" in s and "action failed" in s for s in optiontree.analyse(walk, FIXTURES)["blocking"])


def test_leads_and_implied_one_hop(walk):
    result = optiontree.analyse(walk, FIXTURES)
    leads = "\n".join(result["review"])
    assert "Empty workspace" in leads and "empty state with no control offered" in leads
    assert "no enabled controls offered" in leads
    assert "Final settings" in leads and "only return to already-seen states" in leads
    assert "Secret report" in leads and "hidden in every observed state" in leads
    assert "'Home' on Final settings" in leads and "more than 4 decisions" in leads
    assert '"You can export invoices."' in leads
    assert '"You can filter archived tasks."' in leads
    assert '"Choose a theme."' not in leads
    assert "crawl stopped on budget: 5 enabled controls went untried" in leads
    assert result["budget"]["untried"] == 5


def test_empty_state_with_disabled_or_hidden_controls(walk):
    empty = next(s for s in walk["states"] if s["id"] == "empty")
    empty["controls"] = [{"name": "Add record", "visible": False}, {"name": "Connect", "visible": True, "disabled": True}]
    assert any("Empty workspace" in s and "empty state with no control" in s for s in optiontree.analyse(walk, FIXTURES)["review"])


def test_implied_options_capped_and_hidden_controls_do_not_satisfy(walk):
    walk["states"][0]["text"] = "\n".join(f"You can download document{i}." for i in range(30))
    walk["states"][0]["controls"].append({"name": "Download", "visible": False})
    result = optiontree.analyse(walk, FIXTURES)
    assert len([s for s in result["review"] if "implied option" in s]) == 15


def test_defaults_include_switch_buttons_and_selected_options(walk):
    defaults = "\n".join(optiontree.analyse(walk, FIXTURES)["defaults"])
    for expected in ("Analytics: off by default", "Alerts: on by default", "Daily: on by default", "Theme: System by default", "Access: Private by default"):
        assert expected in defaults


def test_defaults_first_sight_and_controls_later_visible(walk):
    later = copy.deepcopy(walk["states"][0])
    later.update(id="later", depth=1, url="http://127.0.0.1:8000/?step=2", text="A second view.")
    for control in later["controls"]:
        control["visible"] = True
        if control.get("name") == "Analytics":
            control["checked"] = True
    walk["states"].append(later)
    walk["edges"].append({"from": "home", "to": "later", "action": {"kind": "click", "name": "Next"}, "outcome": "dom"})
    result = optiontree.analyse(walk, FIXTURES)
    assert len([s for s in result["defaults"] if "Analytics:" in s]) == 1
    assert not any("Secret report" in s and "hidden in every" in s for s in result["review"])


def test_branching_uses_shortest_decisions_not_navigation(walk):
    result = optiontree.analyse(walk, FIXTURES)
    decisions = {s["id"]: s["decisions"] for s in result["branching"]["states"]}
    assert decisions["settings"] == 0 and decisions["advanced"] == 1
    assert decisions["final"] == 5 and result["branching"]["median"] == 1
    assert any("branching needs 5 decisions" in s for s in result["review"])
    walk["edges"].append({"from": "home", "to": "final", "action": {"kind": "link", "name": "Shortcut"}, "outcome": "navigate"})
    result = optiontree.analyse(walk, FIXTURES)
    assert next(s["decisions"] for s in result["branching"]["states"] if s["id"] == "final") == 0
    assert not any("'Home'" in s and "more than 4" in s for s in result["review"])


def test_source_switches_and_declared_routes(walk):
    leads = "\n".join(optiontree.analyse(walk, FIXTURES, src=FIXTURES / "src")["review"])
    assert "?analytics= into the URL (read at options.js:1)" in leads
    assert leads.count("?analytics=") == 1
    for param in ("token", "mode", "segment", "debug"):
        assert f"?{param}=" in leads
    for param in ("visible", "search", "Theme"):
        assert f"?{param}=" not in leads
    for route in ("/undiscovered", "/react-only", "/server-only", "/dart-only"):
        assert f"route {route} declared at" in leads
    assert "route /settings declared at" not in leads
    assert "/task/:id" not in leads and "/dynamic/{id}" not in leads


def test_source_ignores_dependencies_and_route_origin(walk, tmp_path):
    deps = tmp_path / "node_modules"
    deps.mkdir()
    (deps / "options.js").write_text("params.get('dependency');")
    assert optiontree.scan_source(tmp_path) == ([], [], [])
    walk["states"][0]["controls"].append({"href": "https://elsewhere.invalid/missing", "name": "Elsewhere"})
    assert any("unreachable route /missing" in s for s in optiontree.analyse(walk, FIXTURES, routes=["/missing"])["blocking"])


def test_tree_includes_states_controls_outcomes_and_shots(walk, tmp_path):
    tree = optiontree.tree_markdown(walk, FIXTURES, tmp_path)
    for expected in ("Tasks (http", "Add task -> error: Task failure", "Save draft -> none: Tasks", "Secret report -> none", "Export history -> hidden", "Visible option -> untried", "Disabled action -> none", "shots/home.png", "seen above", "Final settings"):
        assert expected in tree


def test_persona_packet_json_and_toml(tmp_path):
    packet = optiontree.persona_markdown(FIXTURES / "persona.toml", "http://fallback/")
    assert "Entry URL: http://127.0.0.1:8000/" in packet
    assert "Add my first task" in packet and optiontree.JUDGE in packet
    assert "analytics=1" not in packet and "blocking" not in packet
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({"reader": "New user", "tasks": ["Join the team"], "secret": "omit this"}))
    packet = optiontree.persona_markdown(path, "http://fallback/")
    assert "http://fallback/" in packet and "Join the team" in packet and "omit this" not in packet


def run_sensor(*args):
    return subprocess.run([sys.executable, str(SENSOR), *map(str, args)], capture_output=True, text=True, timeout=180)


def test_cli_fault_exit_outputs_and_persona(tmp_path):
    result = run_sensor("--walk", FIXTURES, "--out", tmp_path, "--src", FIXTURES / "src", "--routes", FIXTURES / "routes.txt", "--persona", FIXTURES / "persona.toml")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "blocking (clear faults" in result.stdout
    assert "for the reviewer" in result.stdout and "screenshots:" in result.stdout
    for name in ("findings.json", "tree.md", "persona.md"):
        assert (tmp_path / name).is_file()
        assert str(tmp_path / name) in result.stdout


def test_cli_leads_alone_pass_and_json_matches_file(tmp_path):
    saved = tmp_path / "saved"
    saved.mkdir()
    (saved / "walk.json").write_text(json.dumps(clean_walk()))
    result = run_sensor("--walk", saved, "--out", tmp_path, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == json.loads((tmp_path / "findings.json").read_text())
    assert parsed["review"] and parsed["blocking"] == []


def test_cli_json_fault_exit_and_temp_output():
    result = run_sensor("--walk", FIXTURES, "--json")
    assert result.returncode == 1 and json.loads(result.stdout)["blocking"]
    result = run_sensor("--walk", FIXTURES)
    assert result.returncode == 1 and "/tmp/glevels-optiontree-" in result.stdout


@pytest.mark.parametrize("args", [[], ["--walk", "/nonexistent-walk"], ["--src", "/nonexistent-src", "--walk", FIXTURES], ["--deep", "-1", "--walk", FIXTURES]])
def test_cli_cannot_run(args, tmp_path):
    result = run_sensor(*args, "--out", tmp_path)
    assert result.returncode == 2
    assert result.stdout.startswith("impossible:") and "Traceback" not in result.stderr


@pytest.mark.parametrize("content", ["{bad json", "{}", '{"states": []}', "[]"])
def test_cli_bad_saved_walk(content, tmp_path):
    (tmp_path / "walk.json").write_text(content)
    result = run_sensor("--walk", tmp_path, "--out", tmp_path)
    assert result.returncode == 2 and "impossible:" in result.stdout


@pytest.fixture
def mock_crawler(monkeypatch, tmp_path):
    (tmp_path / "walk.mjs").write_text("")
    monkeypatch.setattr(optiontree, "HERE", tmp_path)
    monkeypatch.setattr(optiontree, "LOCK", tmp_path / "browser.lock")
    monkeypatch.setattr(optiontree, "free_mb", lambda: 1000)
    monkeypatch.setattr(optiontree.shutil, "which", lambda name: "/usr/bin/node")
    calls = []

    def run(cmd, **kwargs):
        assert kwargs["env"]["GLEVELS_WALK_LOCKED"] == "1"
        with open(optiontree.LOCK, "a") as lock:
            with pytest.raises(BlockingIOError):
                optiontree.fcntl.flock(lock, optiontree.fcntl.LOCK_EX | optiontree.fcntl.LOCK_NB)
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(optiontree.subprocess, "run", run)
    return calls


def test_crawl_safe_mode_and_budgets(mock_crawler, tmp_path):
    optiontree.crawl(["http://127.0.0.1/", "http://localhost/"], tmp_path, 12, 30)
    assert "--safe" not in mock_crawler[-1]
    optiontree.crawl(["https://example.invalid/", "http://localhost/"], tmp_path, 12, 30)
    assert "--safe" in mock_crawler[-1]
    assert mock_crawler[-1][2:10] == ["--mode", "tree", "--out", str(tmp_path), "--max-states", "12", "--max-actions", "30"]


def test_crawl_serves_html_files_and_directory(mock_crawler, monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kwargs):
        url = cmd[-1]
        with urlopen(url, timeout=5) as response:
            seen.append(response.read().decode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(optiontree.subprocess, "run", run)
    optiontree.crawl([str(FIXTURES / "site")], tmp_path, 5, 10)
    optiontree.crawl([str(FIXTURES / "site" / "settings.html")], tmp_path, 5, 10)
    assert "Local tasks" in seen[0] and "Local settings" in seen[1]


def test_crawl_refuses_low_memory_and_missing_node(mock_crawler, monkeypatch, tmp_path):
    monkeypatch.setattr(optiontree, "free_mb", lambda: 1)
    with pytest.raises(ValueError, match="only 1 MB"):
        optiontree.crawl(["http://localhost/"], tmp_path, 5, 10)
    assert not mock_crawler
    monkeypatch.setattr(optiontree.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="node is not installed"):
        optiontree.crawl(["http://localhost/"], tmp_path, 5, 10)


@pytest.mark.parametrize("failure", ["browser", "timeout"])
def test_crawl_failure_is_impossible(mock_crawler, monkeypatch, tmp_path, capsys, failure):
    def run(cmd, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 600)
        return SimpleNamespace(returncode=2, stderr="Chromium could not launch", stdout="")

    monkeypatch.setattr(optiontree.subprocess, "run", run)
    assert optiontree.main(["--out", str(tmp_path), "http://localhost/"]) == 2
    assert "impossible:" in capsys.readouterr().out


def test_end_to_end_local_crawl(tmp_path):
    if not (SENSOR.parent / "walk.mjs").is_file():
        pytest.skip("walk.mjs has not been supplied yet")
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    server = optiontree.http.server.ThreadingHTTPServer(("127.0.0.1", 0), optiontree.functools.partial(optiontree.Handler, directory=str(FIXTURES / "site")))
    optiontree.threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = run_sensor("--out", tmp_path, "--max-states", 8, "--max-actions", 12, f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 1, result.stdout + result.stderr
    findings = json.loads((tmp_path / "findings.json").read_text())
    assert any("Add task" in s and "Unexpected error" in s for s in findings["blocking"])
    assert any("Save draft" in s and "nothing happened" in s for s in findings["blocking"])
    assert any("export invoices" in s for s in findings["review"])
    assert (tmp_path / "walk.json").is_file() and (tmp_path / "tree.md").is_file()
    assert list((tmp_path / "shots").glob("*.png"))


def test_source_only_flags_opt_in_defaults_and_url_switches(tmp_path, capsys):
    (tmp_path / "api.py").write_text(
        "def connect(analytics: bool = Query(False), verbose: bool = Query(default=False), on: bool = Query(True)):\n"
        "    mode = request.args.get('beta')\n")
    assert optiontree.main(["--src", str(tmp_path), "--json"]) == 0
    review = json.loads(capsys.readouterr().out)["review"]
    assert any("analytics is off" in r and "api.py:1" in r for r in review)
    assert any("verbose is off" in r for r in review)
    assert not any(" on is off" in r for r in review)
    assert any("?beta=" in r for r in review)


def test_unclickable_and_download_edges_are_not_faults(walk):
    walk["edges"][1].update(outcome="unclickable", errors=[], note="locator.click: Timeout 5000ms exceeded.")
    result = optiontree.analyse(walk, FIXTURES)
    assert not any("'Save draft'" in s for s in result["blocking"])
    assert any("'Save draft'" in s and "could not click" in s for s in result["review"])
    walk["edges"][1].update(outcome="download", note=None)
    result = optiontree.analyse(walk, FIXTURES)
    assert not any("'Save draft'" in s for s in result["blocking"] + result["review"])


def test_invitations_are_short_second_person_or_imperative(walk):
    walk["states"][0]["text"] = ("Masking them out is the first fix for the next round, and it will change every number.\n"
                                 "Gates are allowed to be fuzzy, to keep the judgement where it belongs.\n"
                                 "Upgrade to see every competitor.\n" + "You can " + "really " * 30 + "export it.")
    leads = "\n".join(optiontree.analyse(walk, FIXTURES)["review"])
    assert '"Upgrade to see every competitor."' in leads
    assert "Masking them out" not in leads and "Gates are allowed" not in leads and "really really" not in leads
