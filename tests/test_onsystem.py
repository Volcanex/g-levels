"""The system sensor must catch the bad fixture and leave house components quiet."""
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from glevels import onsystem, spacing

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures-onsystem"


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    browser = subprocess.run(["node", "-e",
                              "const p=require('/home/gabriel/g-design/node_modules/playwright');"
                              "require('fs').accessSync(p.chromium.executablePath())"], capture_output=True)
    if browser.returncode:
        pytest.skip("Playwright or its Chromium is not installed")
    out = tmp_path_factory.mktemp("onsystem")
    run = subprocess.run([sys.executable, str(ROOT / "glevels/onsystem.py"), "--out", str(out),
                          str(FIXTURES / "offsystem.html"), str(FIXTURES / "onsystem.html"), str(FIXTURES / "drift.html")],
                         cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert run.returncode == 1, run.stdout + run.stderr
    return json.loads((out / "findings.json").read_text()), run.stdout


def test_offsystem_blocks(renders):
    results, output = renders
    optional_missing = any("Impeccable did not run" in note for r in results for note in r["notes"])
    bad = [r for r in results if r["url"].endswith("/offsystem.html")]
    assert len(bad) == 2
    for render in bad:
        blocks = {f["check"] for f in render["findings"] if f["severity"] == "block"}
        assert {"missing-system", "foreign-font", "raw-failure"} <= blocks
        assert blocks & {"gradient-text", "impeccable:gradient-text"}
        assert any(f["check"] == "foreign-font" and "computed stack InterVariable, Arial" in f["detail"] for f in render["findings"])
        assert any(f["check"] == "raw-failure" and f["selector"] == "body" for f in render["findings"])
        assert {"mirrored", "off-palette", "local-button"} <= set(render["counts"])
        if not any("source lint did not run" in note for note in render["notes"]):
            assert "raw-css-value" in render["counts"]
        if not optional_missing:
            assert {"impeccable:side-tab", "impeccable:bounce-easing"} <= blocks
    assert "blocking (clear faults" in output


def test_house_components_do_not_block(renders):
    results, _ = renders
    good = [r for r in results if r["url"].endswith("/onsystem.html")]
    assert len(good) == 2
    for render in good:
        assert render["system"] == "gdesign"
        assert not render.get("error")
        assert not [f for f in render["findings"] if f["severity"] == "block"]
        assert "local-button" not in render["counts"]
        assert "raw-css-value" not in render["counts"]
        assert not render["counts"], render["findings"]


def test_drift_is_a_lead_and_never_blocks_alone(renders):
    results, _ = renders
    drift = [r for r in results if r["url"].endswith("/drift.html")]
    assert len(drift) == 2
    expected = {"font-size", "font-weight", "off-palette", "border-radius", "box-shadow", "local-button",
                "local-card", "mirrored", "wall-of-text", "missing-heading", "competing-primary",
                "errors-fill-screen", "raw-css-value", "invented-token"}
    for render in drift:
        assert not render.get("error")
        assert not [f for f in render["findings"] if f["severity"] == "block"]
        wanted = expected - {"raw-css-value"} if any("source lint did not run" in note for note in render["notes"]) else expected
        assert wanted <= set(render["counts"])


def test_evidence_has_coordinates_and_viewport_tiles(renders):
    results, _ = renders
    for render in results:
        assert len(render["screenshots"]) >= 1
        for shot in render["screenshots"]:
            assert Path(shot).is_file()
            assert struct.unpack(">II", Path(shot).read_bytes()[16:24]) == tuple(map(int, render["viewport"].split()[-1].split("x")))
        assert render["tiles"][0]["y"] == 0
        assert render["tiles"][-1]["y"] == max(0, render["pageHeight"] - int(render["viewport"].split("x")[-1]))
        for finding in render["findings"]:
            assert finding["selector"]
            assert "sample" in finding
            assert len(finding["rect"]) == 4
        if any(f["check"] == "raw-css-value" for f in render["findings"]):
            assert list(Path(render["screenshots"][0]).parent.glob("*-consumer-*.css"))


@pytest.mark.parametrize("rule,category,system,expected", [
    ("gradient-text", "slop", "gdesign", "block"),
    ("side-tab", "layout", "gdesign", "block"),
    ("low-contrast", "quality", "gdesign", "review"),
    ("overused-font", "type", "gdesign", None),
    ("cream-palette", "slop", "baseline", None),
    ("cream-palette", "slop", "gdesign", "review"),
    ("em-dash-overuse", "copy", "gdesign", None),
    ("broken-image", "quality", "gdesign", None),
    ("overlap", "layout", "gdesign", None),
    ("line-length", "quality", "baseline", None),
    ("cramped-padding", "quality", "gdesign", None),
    ("straggler", "quality", "baseline", None),
])
def test_house_taste_overrides_impeccable(rule, category, system, expected):
    assert onsystem.impeccable_level({"antipattern": rule, "category": category}, system) == expected


@pytest.mark.parametrize("evidence", [{"houseStyle": True}, {"sourceOnly": True}, {"decorativeMotion": True}])
def test_house_components_and_unlocated_source_are_leads(evidence):
    assert onsystem.impeccable_level({"antipattern": "side-tab", **evidence}, "baseline") == "review"


def test_docs_cards_keep_relative_assets(tmp_path):
    profiles = json.loads((ROOT / "glevels/systems.json").read_text())
    file = "/home/gabriel/g-design/apps/docs/components/buttons.card.html"
    if not Path(file).is_file():
        pytest.skip("g-design docs are not installed")
    urls, servers = onsystem.targets([file], tmp_path, profiles)
    try:
        import urllib.request
        with urllib.request.urlopen(urls[0]) as response:
            assert b'../assets/g.css' in response.read()
        with urllib.request.urlopen(urls[0].rsplit("/components/", 1)[0] + "/assets/g.css") as response:
            assert b"--text-primary" in response.read()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_low_memory_is_impossible(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["onsystem.py", "--out", str(tmp_path), str(FIXTURES / "onsystem.html")])
    monkeypatch.setattr(onsystem.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(spacing, "LOCK", tmp_path / "browser.lock")
    monkeypatch.setattr(spacing, "free_mb", lambda: 0)
    with pytest.raises(SystemExit) as exc:
        onsystem.main()
    assert exc.value.code == 2
    assert "impossible: only 0 MB" in capsys.readouterr().out


def test_optional_browser_checks_memory_before_each_launch(monkeypatch):
    memory = iter([spacing.MIN_FREE_MB, 0])
    calls = []
    monkeypatch.setattr(spacing, "free_mb", lambda: next(memory))
    monkeypatch.setattr(onsystem.vendor, "impeccable", lambda *args: (calls.append(args) or [], None))
    results = onsystem.impeccable(["http://localhost/"], onsystem.DEFAULT_VIEWPORTS)
    assert calls == [("http://localhost/", "1440x900")]
    assert results[0]["why"] is None
    assert "not enough free memory" in results[1]["why"]


def test_optional_tool_failure_is_a_lead(monkeypatch):
    monkeypatch.setattr(spacing, "free_mb", lambda: spacing.MIN_FREE_MB)
    monkeypatch.setattr(onsystem.vendor, "impeccable", lambda *args: (None, "npm is not installed"))
    results = onsystem.impeccable(["http://localhost/"], onsystem.DEFAULT_VIEWPORTS)
    assert all(r["findings"] == [] and r["why"] == "npm is not installed" for r in results)


def test_invalid_viewport_is_impossible(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["onsystem.py", "--viewports", "oops", "http://localhost/"])
    with pytest.raises(SystemExit) as exc:
        onsystem.main()
    assert exc.value.code == 2
    assert "impossible: invalid viewports" in capsys.readouterr().out
