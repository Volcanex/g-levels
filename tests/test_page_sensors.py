import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from glevels import assets, claims, frames, onsystem, parity, rendered, spacing


@pytest.mark.parametrize("sensor", [spacing, onsystem, assets])
def test_non_page_is_rejected_before_browser_lock(sensor, tmp_path, monkeypatch, capsys):
    source = tmp_path / "main.js"
    source.write_text("export default 1")
    monkeypatch.setattr(sys, "argv", [sensor.__file__, "--out", str(tmp_path / "out"), str(source)])
    monkeypatch.setattr(spacing.fcntl, "flock", lambda *args: pytest.fail("invalid page took the browser lock"))
    with pytest.raises(SystemExit) as error:
        sensor.main()
    assert error.value.code == 2
    assert "is not a page" in capsys.readouterr().out


def test_preflight_validates_all_targets_before_starting_a_server(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text("<h1>Hello</h1>")
    source = tmp_path / "main.js"
    source.write_text("export default 1")
    monkeypatch.setattr(spacing, "serve", lambda *args: pytest.fail("started server before rejecting target"))
    with pytest.raises(SystemExit):
        spacing.resolve([str(page), str(source)], tmp_path)


def test_running_page_finds_the_manifest_of_the_repo_that_serves_it(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "assets.toml").write_text("")
    monkeypatch.delenv("GLEVELS_REPO", raising=False)
    assert assets.find_manifest("http://127.0.0.1:1/") is None
    monkeypatch.setenv("GLEVELS_REPO", str(tmp_path))
    assert assets.find_manifest("http://127.0.0.1:1/") == tmp_path / "assets.toml"


def fake_capture(url, out):
    (out / "text").mkdir(parents=True)
    text = "Visitors NaN" if url.endswith("/bad") else "Visitors 12 last 7 days"
    (out / "text/one.txt").write_text(text)
    (out / "walk.json").write_text(json.dumps({"states": [{"url": url, "text": "text/one.txt",
        "controls": [{"name": "Export", "visible": not url.endswith("/bad")}], "headings": []}]}))
    return out


def test_claims_judges_rendered_text_instead_of_empty_app_shell(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GLEVELS_RENDERED", "1")
    monkeypatch.setattr(rendered, "capture", fake_capture)
    monkeypatch.setattr(sys, "argv", ["claims", "--out", str(tmp_path), "http://fixture/bad"])
    assert claims.main() == 1
    assert "NaN" in capsys.readouterr().out
    result = json.loads((tmp_path / "findings.json").read_text())
    assert any(f["check"] == "broken-value" for f in result["findings"])


def test_parity_judges_rendered_controls_and_keeps_missing_capability_blocking(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GLEVELS_RENDERED", "1")
    monkeypatch.setattr(rendered, "capture", fake_capture)
    monkeypatch.setattr(sys, "argv", ["parity", "--out", str(tmp_path), "--require", "Export",
                                     "http://fixture/good", "http://fixture/bad"])
    assert parity.main() == 1
    output = capsys.readouterr().out
    assert "http://fixture/bad" in output
    assert "export" in output.lower()


def test_static_urls_do_not_start_a_browser_without_page_inputs(monkeypatch):
    monkeypatch.delenv("GLEVELS_RENDERED", raising=False)
    assert not rendered.requested("http://fixture/bad")
    monkeypatch.setenv("GLEVELS_RENDERED", "1")
    assert rendered.requested("http://fixture/bad")
    assert not rendered.requested("index.html")


def test_render_timeout_starts_after_acquiring_the_browser_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("GSPACING_LOCK", str(tmp_path / "browser.lock"))
    held = []
    monkeypatch.setattr(rendered.fcntl, "flock", lambda *args: held.append(True))
    def run(command, **kwargs):
        assert held == [True]
        assert kwargs["env"]["GLEVELS_WALK_LOCKED"] == "1"
        fake_capture("http://fixture/good", tmp_path / "out")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(rendered.subprocess, "run", run)
    assert rendered.capture("http://fixture/good", tmp_path / "out") == tmp_path / "out"


def test_frames_recipe_uses_the_managed_server_url(tmp_path, monkeypatch):
    (tmp_path / ".glevels").mkdir()
    (tmp_path / ".glevels/frames.json").write_text(json.dumps({"captures": [
        {"name": "menu", "url": "{url}#/home", "path": "move.mjs"}]}))
    monkeypatch.setenv("GLEVELS_READY_URL", "http://127.0.0.1:43210/")
    seen = []
    def capture(args, out):
        seen.append(args.source)
        return {"findings": [], "sequences": [], "notes": [], "tiles": []}, 0
    monkeypatch.setattr(frames, "check_frames", capture)
    out = tmp_path / "out"
    out.mkdir()
    args = argparse.Namespace(repo=str(tmp_path), recipe=None, json=True, frames=24, fps=30, viewport="390x844")
    assert frames.recipe_run(args, out) == 0
    assert seen == ["http://127.0.0.1:43210/#/home"]
