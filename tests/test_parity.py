import json
import fcntl
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from glevels import parity

FIXTURES = Path(__file__).parent / "fixtures-parity"
SCRIPT = Path(parity.__file__)
SIBLINGS = [FIXTURES / f"{name}.html" for name in ("visibility", "brand", "competitor", "pr")]


def run(tmp_path, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path / "out"), *map(str, args)],
                          capture_output=True, text=True)


def test_lone_control_is_a_lead_until_required(tmp_path):
    p = run(tmp_path, *SIBLINGS)
    assert p.returncode == 0, p.stdout + p.stderr
    for word in ("for the reviewer (", "'depth' (1 of 4)", "visibility.html", "bare list/table"):
        assert word in p.stdout
    data = json.loads((tmp_path / "out/findings.json").read_text())
    assert data["counts"] == {"blocking": 0, "review": 2}
    assert not p.stderr
    p = run(tmp_path, "--json", "--require", "depth|sample depth", *SIBLINGS)
    assert p.returncode == 1, p.stdout + p.stderr
    blocks = [f for f in json.loads(p.stdout)["findings"] if f["severity"] == "block"]
    assert len(blocks) == 3 and all(f["check"] == "required" and "1 of 4" in f["detail"] for f in blocks)


def test_required_capability_nobody_has_is_a_lead(tmp_path):
    p = run(tmp_path, "--json", "--require", "teleport", *SIBLINGS)
    assert p.returncode == 0, p.stdout + p.stderr
    assert any(f["selector"] == "teleport" and f["severity"] == "review" for f in json.loads(p.stdout)["findings"])


def test_lone_exception_still_reports_interpretation(tmp_path):
    p = run(tmp_path, "--except", FIXTURES / "except.txt", *SIBLINGS)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "waived:" in p.stdout and "experiment" in p.stdout
    assert "for the reviewer" in p.stdout
    p = run(tmp_path, "--json", "--except", FIXTURES / "except.txt", *SIBLINGS)
    data = json.loads(p.stdout)
    assert data["counts"]["blocking"] == 0
    assert data["waived"][0]["capability"] == "depth"
    assert data == json.loads((tmp_path / "out/findings.json").read_text())


def test_gap_at_three_of_four(tmp_path):
    p = run(tmp_path, "--json", *SIBLINGS[1:], FIXTURES / "gap.html")
    assert p.returncode == 0, p.stdout + p.stderr
    fs = json.loads(p.stdout)["findings"]
    assert len(fs) == 1
    assert fs[0]["check"] == "gap" and fs[0]["selector"] == "export" and fs[0]["severity"] == "review"
    assert "3 of 4" in fs[0]["detail"]
    p = run(tmp_path, "--json", "--require", "export", *SIBLINGS[1:], FIXTURES / "gap.html")
    assert p.returncode == 1, p.stdout + p.stderr
    fs = json.loads(p.stdout)["findings"]
    assert len(fs) == 1 and fs[0]["check"] == "required" and "3 of 4" in fs[0]["detail"]


def test_clean_sibling_surfaces(tmp_path):
    p = run(tmp_path, "--json", *SIBLINGS[1:])
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)["findings"] == []


def test_controls_labels_headings_and_explicit_capabilities():
    html = '''<h2>Results (12)</h2><h3>Sources</h3>
    <button aria-label="Run audits (2)">Go</button>
    <a class="btn" href="/export">Exports</a><a href="/about">About</a>
    <label for="depth">Depth</label><select id="depth"><option>Deep</option></select>
    <label><input type="checkbox">Notifications</label>
    <label for="search">Search</label><input id="search">
    <span id="manual">Manual control</span><button aria-labelledby="manual">Toggle</button>
    <div role="switch" aria-label="Auto runs"></div>
    <div data-capability="Schedule"></div><button hidden>Hidden action</button>
    <input type="hidden" name="token"><script>undefined</script>'''
    surface = parity.html_surface("fixture", html)
    assert set(surface["capabilities"]) == {"run audit", "export", "depth", "notification", "search", "manual control", "auto run", "schedule"}
    assert set(surface["sections"]) == {"result", "source"}


def test_normalise_counts_and_simple_plurals():
    assert parity.normalise("Run audits (2)!") == "run audit"
    assert parity.normalise("Capabilities: 12") == "capability"
    assert parity.normalise("Status / analysis") == "status analysis"


def test_interpretation_must_be_near_results():
    s = parity.html_surface("fixture", '<nav><p>Next step: choose an audit</p></nav><section><h2>Results</h2><table><tr><td>12</td></tr></table></section>')
    assert s["bare_results"]
    s = parity.html_surface("fixture", '<section><h2>Results</h2><p>Because Acme appears most often, focus on the other brands.</p><ul><li>12</li></ul></section>')
    assert not s["bare_results"]


@pytest.mark.parametrize("html", ['<h1>Results</h1><ul><li>Acme 12</li></ul>', '<p>Results</p><table><tr><td>12</td></tr></table>'])
def test_result_titles_and_text_labels_need_interpretation(html):
    assert parity.html_surface("fixture", html)["bare_results"]


def test_valueless_html_attributes():
    assert parity.html_surface("fixture", '<div style><button class>Run</button></div>')["capabilities"] == ["run"]


def test_gap_threshold_includes_exactly_half_and_two_siblings():
    surfaces = [{"target": str(i), "capabilities": ["export"] if i < 2 else [], "bare_results": []} for i in range(4)]
    fs = parity.compare(surfaces, {})
    assert len(fs) == 2 and all(f["check"] == "gap" for f in fs)
    assert not parity.compare(surfaces, {"export": "deliberate"})
    assert not parity.compare(surfaces[:1] + surfaces[2:], {})[0]["check"] == "gap"


def test_two_surfaces_do_not_create_lone_faults():
    surfaces = [{"target": "a", "capabilities": ["manual control"], "bare_results": []},
                {"target": "b", "capabilities": [], "bare_results": []}]
    assert parity.compare(surfaces, {}) == []


def test_walk_states_are_individual_surfaces(tmp_path):
    p = run(tmp_path, "--json", "--require", "depth", FIXTURES / "walk")
    assert p.returncode == 1, p.stdout + p.stderr
    data = json.loads(p.stdout)
    assert len(data["surfaces"]) == 3
    assert data["counts"] == {"blocking": 2, "review": 1}
    assert data["findings"][0]["selector"] == "depth"


def test_walk_ignores_hidden_controls(tmp_path):
    walk = tmp_path / "walk"
    walk.mkdir()
    states = [{"id": str(i), "controls": [{"name": "Run audit", "visible": True}]} for i in range(3)]
    states[0]["controls"].append({"name": "Depth", "visible": False})
    (walk / "walk.json").write_text(json.dumps({"states": states}))
    assert not parity.compare(parity.walk_surfaces(str(walk)), {})


def test_real_browser_walk_evidence(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is missing")
    probe = subprocess.run(["node", "--input-type=module", "-e", '''
        import { createRequire } from "node:module";
        import path from "node:path";
        const GDESIGN = process.env.GDESIGN || "/home/gabriel/g-design";
        const require = createRequire(path.join(GDESIGN, "package.json"));
        console.log(require("playwright").chromium.executablePath());
    '''], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    if not Path(probe.stdout.strip()).is_file():
        pytest.skip("Chromium is missing")
    engine = SCRIPT.with_name("walk.mjs")
    assert engine.is_file(), "walk.mjs is needed to verify the evidence contract"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    walk = tmp_path / "browser-walk"
    base = f"http://127.0.0.1:{server.server_port}"
    env = dict(os.environ)
    try:
        with open(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            env["GLEVELS_WALK_LOCKED"] = "1"
            p = subprocess.run(["node", str(engine), "--mode", "smoke", "--settle", "0", "--out", str(walk),
                                *[f"{base}/{name}.html" for name in ("visibility", "brand", "competitor")]],
                               env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stdout + p.stderr
        assert list((walk / "shots").glob("*.png"))
        p = run(tmp_path, "--json", "--require", "depth", walk)
        assert p.returncode == 1, p.stdout + p.stderr
        data = json.loads(p.stdout)
        assert len(data["surfaces"]) == 3
        assert [(f["check"], f["selector"]) for f in data["findings"] if f["severity"] == "block"] == [("required", "depth")] * 2
        p = subprocess.run([sys.executable, str(SCRIPT.with_name("claims.py")), "--json", "--out", str(tmp_path / "claims"), str(walk)],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        assert len(json.loads(p.stdout)["pages"]) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("args", [[], ["absent.html", "other.html"], [SIBLINGS[0]]])
def test_fewer_than_two_or_unreadable_targets(tmp_path, args):
    p = run(tmp_path, *args)
    assert p.returncode == 2
    assert p.stdout.startswith("impossible:")
    assert "Traceback" not in p.stderr


def test_exception_needs_a_reason(tmp_path):
    waiver = tmp_path / "bad-except.txt"
    waiver.write_text("depth:\n")
    p = run(tmp_path, "--except", waiver, *SIBLINGS)
    assert p.returncode == 2 and "expected capability: reason" in p.stdout


def test_invalid_walk_is_impossible(tmp_path):
    walk = tmp_path / "walk"
    walk.mkdir()
    (walk / "walk.json").write_text('{"states":[{"controls":null}]}')
    p = run(tmp_path, walk)
    assert p.returncode == 2 and p.stdout.startswith("impossible:")


def test_source_mode_counts_hidden_forms(tmp_path):
    page = '<div id="form" hidden><button>Add a question</button></div>'
    (tmp_path / "a.html").write_text(page)
    (tmp_path / "b.html").write_text("<div><button>Save</button></div>")
    (tmp_path / "c.html").write_text(page)
    p = run(tmp_path, "--json", "--require", "add a question", tmp_path / "a.html", tmp_path / "b.html", tmp_path / "c.html")
    assert p.returncode == 0, p.stdout
    p = run(tmp_path, "--json", "--source", "--require", "add a question", tmp_path / "a.html", tmp_path / "b.html", tmp_path / "c.html")
    assert p.returncode == 1, p.stdout
    assert [f["target"].endswith("b.html") for f in json.loads(p.stdout)["findings"] if f["severity"] == "block"] == [True]
