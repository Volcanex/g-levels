import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from glevels import spacing


FIXTURES = Path(__file__).parent / "fixtures-spacing"


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    gdesign = Path(os.environ.get("GDESIGN", "/home/gabriel/g-design"))
    if not (gdesign / "node_modules/playwright/package.json").is_file():
        pytest.skip("g-design Playwright is not installed")
    browser = subprocess.run(["node", "-e", "console.log(require('playwright').chromium.executablePath())"],
                             cwd=gdesign, capture_output=True, text=True)
    if browser.returncode or not Path(browser.stdout.strip()).is_file():
        pytest.skip("g-design Chromium is not installed")
    root = tmp_path_factory.mktemp("spacing")
    cache = {}

    def run(page, *flags):
        key = page, flags
        if key not in cache:
            out = root / str(len(cache))
            p = subprocess.run([sys.executable, str(spacing.HERE / "spacing.py"), "--out", str(out),
                                *flags, str(FIXTURES / page)], capture_output=True, text=True, timeout=240)
            results = json.loads((out / "findings.json").read_text()) if (out / "findings.json").exists() else []
            cache[key] = p, results
        return cache[key]

    return run


def test_occlusion_blocks_and_has_bottom_screenshot(render):
    p, results = render("occlusion.html", "--viewports", "phone=390x844", "--states", "loaded")
    assert p.returncode == 1, p.stdout + p.stderr
    found = [f for f in results[0]["findings"] if f["check"] == "fixed-sticky-occlusion"]
    assert any(f["severity"] == "block" and 'p#last' in f["selector"] and "bottom of the page" in f["detail"] for f in found)
    assert len(results[0]["screenshots"]) >= 2
    assert all(Path(s).is_file() for s in results[0]["screenshots"])


def test_states_are_leads_and_loaded_is_clean(render):
    p, results = render("states.html", "--viewports", "phone=390x844")
    assert p.returncode == 0, p.stdout + p.stderr
    by_state = {r["state"]: r for r in results}
    assert set(by_state) == {"loaded", "loading", "error", "long-text"}
    assert not any(f["severity"] == "block" for r in results for f in r["findings"])
    assert not by_state["loaded"]["findings"]
    assert any(f["check"] == "state-layout-changes" for f in by_state["loading"]["findings"])
    assert any(f["check"] == "raw-failure-text" for f in by_state["error"]["findings"])
    assert any(f["check"] == "text-spills-out" for f in by_state["long-text"]["findings"])
    assert all(r["screenshots"] for r in results)
    assert "long text" in p.stdout


def test_steps_compare_titles_and_skip_request_states(render):
    p, results = render("deck.html", "--viewports", "phone=390x844", "--steps", "2")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "text-touches-box" in p.stdout
    assert "titles-move-between-siblings" in p.stdout
    assert "step 1" in p.stdout and "step 2" in p.stdout
    assert p.stdout.count("loading/error skipped") == 1
    assert [r["render"] for r in results] == ["loaded", "step 1", "step 2", "long text"]
    assert all(r["screenshots"] for r in results)


def test_clean_reserved_layout_at_all_widths_states_and_steps(render):
    p, results = render("clean.html", "--steps", "2", "--landscape")
    assert p.returncode == 0, p.stdout + p.stderr
    loaded = [r for r in results if r["render"] == "loaded"]
    assert [r["viewport"] for r in loaded] == ["phone-s 320x568", "phone 390x844", "tablet 768x1024",
                                              "desktop 1440x900", "phone-land 844x390"]
    assert len(results) == 18
    assert all(r["vp"] == "phone" for r in results if r["state"] != "loaded")
    assert not any(f["check"] in ("fixed-sticky-occlusion", "raw-failure-text", "state-layout-changes")
                   for r in results for f in r["findings"])
    assert "titles-move-between-siblings" not in p.stdout
    assert all(r["screenshots"] for r in results)


@pytest.mark.parametrize("page,check", [("original.html", "text-touches-box"), ("related.html", "page-scrolls-sideways")])
def test_existing_failures_still_block(render, page, check):
    p, _ = render(page, "--viewports", "phone=390x844", "--states", "loaded")
    assert p.returncode == 1, p.stdout + p.stderr
    assert check in p.stdout


def test_squeezed_icon_buttons_block(render):
    p, results = render("squeezed.html", "--viewports", "desktop=1440x900", "--states", "loaded")
    squeezed = [f for r in results for f in r["findings"] if f["check"] == "icon-button-squeezed"]
    assert p.returncode == 1 and len(squeezed) == 2, p.stdout + p.stderr


def test_stress_still_blocks_sideways_scrolling(render):
    p, results = render("related.html", "--viewports", "phone=390x844", "--states", "long-text")
    assert p.returncode == 1, p.stdout + p.stderr
    assert any(f["check"] == "page-scrolls-sideways" and f["severity"] == "block" for f in results[0]["findings"])
    assert all(f["severity"] == "review" for f in results[0]["findings"]
               if f["check"] not in ("page-scrolls-sideways", "text-overlaps-text"))


def test_stress_still_blocks_text_overlap(render):
    p, results = render("original.html", "--viewports", "phone=390x844", "--states", "long-text")
    assert p.returncode == 1, p.stdout + p.stderr
    assert any(f["check"] == "text-overlaps-text" and f["severity"] == "block" for f in results[0]["findings"])


def test_optional_detector_filters_and_reports_failure_once(monkeypatch):
    calls = []
    def detect(url, viewport):
        calls.append((url, viewport))
        return [{"antipattern": "cramped-padding", "selector": "button", "name": "Cramped padding"},
                {"antipattern": "overused-font", "selector": "body", "name": "Font"}], None
    monkeypatch.setattr(spacing.vendor, "impeccable", detect)
    monkeypatch.setattr(spacing, "free_mb", lambda: 4096)
    results = [{"url": "http://fixture/", "vp": vp, "viewport": f"{vp} {size}", "render": "loaded", "findings": []}
               for vp, size in (("phone", "390x844"), ("desktop", "1440x900"), ("tablet", "768x1024"))]
    spacing.impeccable(results)
    assert calls == [("http://fixture/", "390x844"), ("http://fixture/", "1440x900")]
    assert [f["check"] for r in results for f in r["findings"]] == ["impeccable: cramped-padding"] * 2
    monkeypatch.setattr(spacing.vendor, "impeccable", lambda *a: (None, "optional browser unavailable"))
    spacing.impeccable(results)
    notes = [n for r in results for n in r.get("notes", [])]
    assert notes == ["impeccable: did not run: optional browser unavailable"]


def test_plain_files_keep_parent_relative_assets(tmp_path):
    import urllib.request

    (tmp_path / "pages").mkdir()
    (tmp_path / "assets").mkdir()
    page = tmp_path / "pages/card.html"
    page.write_text('<link rel="stylesheet" href="../assets/site.css"><p>A card</p>')
    (tmp_path / "assets/site.css").write_text("body { padding: 24px }")
    urls, servers = spacing.resolve([str(page)], tmp_path / "build")
    try:
        assert urls[0].endswith("/pages/card.html")
        css = urllib.request.urlopen(urls[0].replace("pages/card.html", "assets/site.css")).read().decode()
        assert "padding: 24px" in css
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_canvas_auth_wait_and_custom_step_key(render, tmp_path):
    import http.server
    import threading

    class App(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("X-Fixture") != "fixture-only-token":
                self.send_error(401)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((FIXTURES / "app.html").read_bytes())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": [{"origin": origin,
                      "localStorage": [{"name": "fixture", "value": "present"}]}]}))
    out = tmp_path / "out"
    try:
        p = subprocess.run([sys.executable, str(spacing.HERE / "spacing.py"), "--out", str(out),
                            "--viewports", "phone=390x844", "--states", "loaded", "--steps", "1", "--step-key", "n",
                            "--wait-for", "#ready", "--storage-state", str(state), "--header", "X-Fixture: fixture-only-token",
                            origin + "/app.html"], capture_output=True, text=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
    assert p.returncode == 0, p.stdout + p.stderr
    results = json.loads((out / "findings.json").read_text())
    assert len(results) == 2
    assert '"Ready"' in results[0]["landmarkSamples"]["h1"]
    assert '"Next"' in results[1]["landmarkSamples"]["h1"]
    assert p.stdout.count("canvas page: geometry only sees the HTML over the canvas") == 2
    assert any(f["check"] == "fixed-elements-overlap" and f["severity"] == "review" for f in results[0]["findings"])
    assert "fixture-only-token" not in p.stdout + p.stderr + (out / "findings.json").read_text()


def test_each_deck_compares_its_own_titles_and_ignores_title_width():
    results = []
    for url in ("first", "second"):
        for step, y, width in ((0, 32, 100), (1, 128, 240), (2, 32, 240)):
            results.append({"url": url, "vp": "phone", "state": "loaded", "render": f"step {step}", "step": step,
                            "landmarks": {"screen-title": [32, y, width, 32]}})
    findings = spacing.compare_landmarks(results)
    assert len(findings) == 2
    assert {f["url"] for f in findings} == {"first", "second"}
    assert all(f["render"] == "step 1" and "96px" in f["detail"] for f in findings)
