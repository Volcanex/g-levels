import json
from contextlib import nullcontext
import subprocess
import sys

import pytest

from glevels import pacing


def payload(**changes):
    return dict(gs_frames=1, viewport=dict(w=390, h=844, dpr=3), webgl=True, reduced=False,
                phases=[dict(id=name, ms=6000, count=358, fps=59.7, p50=16.7, p95=17.4,
                             worst=33.1, over_20=2, over_33=0, over_50=0,
                             longtasks=1, longtask_ms=63) for name in pacing.FRAME_PHASES], **changes)


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    secret = "TEST-SECRET-" + "x" * 40
    token = tmp_path / "token"
    token.write_text(secret)
    monkeypatch.setenv("RUNNER_TOKEN_FILE", str(token))
    monkeypatch.delenv("RUNNER_TOKEN", raising=False)
    state_path = tmp_path / "state.json"
    calls_path = tmp_path / "calls.jsonl"
    job_path = tmp_path / "job.json"
    monkeypatch.setenv("FAKE_STATE", str(state_path))
    monkeypatch.setenv("FAKE_CALLS", str(calls_path))
    monkeypatch.setenv("FAKE_JOB", str(job_path))
    script = tmp_path / "g-run"
    script.write_text('''import json, os, sys
from pathlib import Path
secret = Path(os.environ['RUNNER_TOKEN_FILE']).read_text()
assert secret not in repr(sys.argv)
with Path(os.environ['FAKE_CALLS']).open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
print(Path(os.environ['FAKE_STATE' if '--status' in sys.argv else 'FAKE_JOB']).read_text())
''')
    monkeypatch.setattr(pacing, "G_RUN", [sys.executable, str(script)])
    info = dict(ua="iPhone Safari", gpu=dict(adapter="Apple GPU"))
    device = dict(name="iphone", online=True, seen=100000, info=info)
    state_path.write_text(json.dumps(dict(now=100001, devices=[device])))
    job = dict(id="job-1", state="done", result=dict(ok=True, posted=payload(),
               frames=dict(fps=1), device_info=info))
    job_path.write_text(json.dumps(job))
    folder = tmp_path / ".glevels"
    folder.mkdir()
    (folder / "pacing.json").write_text(json.dumps(dict(url="https://example.org/?other=1#anchor", runner_query="gate=frames")))
    return dict(root=tmp_path, state=state_path, job=job_path, calls=calls_path, secret=secret)


def invoke(fake, capsys, *args):
    result = pacing.main(["--repo", str(fake["root"]), "--out", str(fake["root"] / "out"), *args])
    captured = capsys.readouterr()
    assert fake["secret"] not in captured.out + captured.err + fake["calls"].read_text()
    data = json.loads((fake["root"] / "out/findings.json").read_text())
    return result, captured, data


def test_device_judges_posted_and_preserves_query(fake_runner, capsys):
    result, captured, data = invoke(fake_runner, capsys)
    assert result == 0
    assert data["measured_on"] == "g-runner device iphone"
    assert data["runs"][0]["phases"][1]["fps"] == 59.7  # Ignore parent's 1 fps.
    assert "Apple GPU" in captured.out and "iPhone Safari" in captured.out
    calls = [json.loads(line) for line in fake_runner["calls"].read_text().splitlines()]
    assert calls[0] == ["--status", "--json"]
    assert "https://example.org/?other=1&gate=frames#anchor" in calls[1]


def test_real_page_drop_is_a_finding_even_with_fast_parent(fake_runner, capsys):
    job = json.loads(fake_runner["job"].read_text())
    job["result"]["frames"]["fps"] = 60
    phase = job["result"]["posted"]["phases"][4]
    phase.update(fps=30, over_20=358, over_33=358, p50=33.4, p95=33.5, worst=33.5)
    fake_runner["job"].write_text(json.dumps(job))
    result, captured, data = invoke(fake_runner, capsys)
    assert result == 1
    assert "work" in captured.out and "100.00%" in captured.out
    assert data["runs"][0]["phases"][4]["findings"]


@pytest.mark.parametrize("posted", [None, {}, {"phases": []}])
def test_parent_frames_without_page_payload_are_impossible(fake_runner, capsys, posted):
    job = json.loads(fake_runner["job"].read_text())
    job["result"].update(posted=posted, frames=dict(fps=60))
    fake_runner["job"].write_text(json.dumps(job))
    result, captured, data = invoke(fake_runner, capsys)
    assert result == 2
    assert data["runs"][0]["reason"] == "page posted no frame stats"
    assert "impossible: page posted no frame stats" in captured.out


@pytest.mark.parametrize("selected", ["auto", "none", "missing"])
def test_no_online_device_falls_back_with_note(fake_runner, capsys, monkeypatch, selected):
    fleet = json.loads(fake_runner["state"].read_text())
    fleet["devices"][0]["seen"] = fleet["now"] - 30000  # Strict boundary even if online is stale.
    fake_runner["state"].write_text(json.dumps(fleet))
    calls = []
    monkeypatch.setattr(pacing, "fallback_capture", lambda *a: calls.append(a) or [])
    # --device none never invokes the CLI, so the call log may not exist.
    fake_runner["calls"].touch()
    result, captured, data = invoke(fake_runner, capsys, "--device", selected)
    assert result == 0 and len(calls) == 1
    assert "other=1&gate=frames" in calls[0][0]
    assert data["measured_on"] == "playwright fallback, no paired device online"
    assert "measured_on: playwright fallback, no paired device online" in captured.out
    if selected == "none":
        assert fake_runner["calls"].read_text() == ""


def test_all_online_devices_are_measured(fake_runner, capsys):
    fleet = json.loads(fake_runner["state"].read_text())
    fleet["devices"].append(dict(fleet["devices"][0], name="android"))
    fake_runner["state"].write_text(json.dumps(fleet))
    result, _, data = invoke(fake_runner, capsys)
    assert result == 0
    assert [r["device"] for r in data["runs"]] == ["iphone", "android"]


def test_malformed_and_ambiguous_stats_do_not_pass(fake_runner, capsys):
    job = json.loads(fake_runner["job"].read_text())
    job["result"]["posted"]["phases"][2]["over_20"] = 100
    fake_runner["job"].write_text(json.dumps(job))
    result, _, data = invoke(fake_runner, capsys)
    assert result == 2
    assert "25 ms drop rate unresolved" in data["runs"][0]["reason"]
    job["result"]["posted"]["phases"] = job["result"]["posted"]["phases"][:-1]
    fake_runner["job"].write_text(json.dumps(job))
    result, _, data = invoke(fake_runner, capsys)
    assert result == 2
    assert "missing or out of order" in data["runs"][0]["reason"]


def test_cli_failure_does_not_echo_a_secret(fake_runner, monkeypatch, capsys):
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, fake_runner["secret"], fake_runner["secret"])
    monkeypatch.setattr(pacing.subprocess, "run", fail)
    assert pacing.main(["--repo", str(fake_runner["root"])]) == 2
    captured = capsys.readouterr()
    assert fake_runner["secret"] not in captured.out + captured.err


def test_thresholds_shared_by_summary_and_trace(tmp_path):
    doc = dict(phases=[dict(name=n) for n in pacing.FRAME_PHASES], thresholds=dict(phone_dropped_percent=3))
    stats = payload()
    stats["phases"][1].update(over_20=10, over_33=10)
    assert not pacing.posted_run(stats, doc, dict(ua="iPhone"))["phases"][1]["findings"]
    doc["thresholds"]["phone_dropped_percent"] = 2
    assert pacing.posted_run(stats, doc, dict(ua="iPhone"))["phases"][1]["findings"]


def test_managed_mac_restarts_and_stops_each_measurement(fake_runner, capsys, monkeypatch):
    fleet = json.loads(fake_runner["state"].read_text())
    fleet["devices"][0]["name"] = "mac-m3"
    fake_runner["state"].write_text(json.dumps(fleet))
    tool = fake_runner["root"] / "mac-runner"
    log = fake_runner["root"] / "mac-calls"
    tool.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nwith Path({str(log)!r}).open('a') as f: f.write(sys.argv[1]+'\\n')\n")
    tool.chmod(0o700)
    monkeypatch.setattr(pacing, "MAC_RUNNER", tool)
    invoke(fake_runner, capsys)
    job = json.loads(fake_runner["job"].read_text())
    job["result"]["posted"] = {}
    fake_runner["job"].write_text(json.dumps(job))
    invoke(fake_runner, capsys)
    assert log.read_text().splitlines() == ["start", "stop", "start", "stop"]


@pytest.mark.parametrize("hardware", [True, False])
def test_first_requested_work_phase_is_not_exempt(hardware):
    stats = payload()
    stats["phases"] = stats["phases"][4:]
    stats["phases"][0].update(count=60, fps=10, p50=100, p95=200, worst=200,
                               over_20=60, over_33=60, over_50=60, longtask_ms=200)
    doc = dict(phases=[dict(name=name) for name in ("work", "features")])
    run = pacing.posted_run(stats, doc, {}, "desktop", hardware=hardware)
    assert run["phases"][0]["findings"]


@pytest.mark.parametrize("name", ["intro", "load"])
def test_only_named_intro_and_load_are_exempt(name):
    stats = payload()
    stats["phases"] = stats["phases"][4:]
    stats["phases"][1].update(id=name, worst=200, over_20=358, over_33=358, over_50=358)
    doc = dict(phases=[dict(name="work"), dict(name=name)])
    run = pacing.posted_run(stats, doc, {}, "desktop")
    assert not any(phase["findings"] for phase in run["phases"])


@pytest.mark.parametrize("name", ["mac", "mac-m3"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError, None], ids=["interrupted", "raised", "failed"])
def test_managed_mac_stops_when_start_does_not_return_success(monkeypatch, name, failure):
    calls = []

    def run(argv, **kwargs):
        assert argv == [str(pacing.MAC_RUNNER), argv[1], "--device", name]
        calls.append(argv[1])
        if argv[1] == "start":
            if failure:
                raise failure("startup interrupted after launch")
            return subprocess.CompletedProcess(argv, 1, "startup failed", "")
        assert argv[1] == "stop"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pacing.subprocess, "run", run)
    expected = pytest.raises(KeyboardInterrupt) if failure is KeyboardInterrupt else nullcontext()
    with expected:
        result = pacing.device_capture(dict(name=name), "https://example.org/", {})
        assert result["status"] == "impossible"
    assert calls == ["start", "stop"]


def test_rounded_desktop_stall_uses_exact_over_50_counter(fake_runner, capsys):
    job = json.loads(fake_runner["job"].read_text())
    job["result"]["device_info"]["ua"] = "Chrome desktop"
    job["result"]["posted"]["phases"][1].update(worst=50.0, over_33=1, over_50=1)
    fake_runner["job"].write_text(json.dumps(job))
    result, _, data = invoke(fake_runner, capsys)
    assert result == 1
    assert any("50 ms" in finding for finding in data["runs"][0]["phases"][1]["findings"])


@pytest.mark.parametrize("worst,over_50,status,blocking", [
    (100.0, 1, "impossible", False),
    (100.1, 1, "complete", True),
    (99.9, 1, "complete", False),
    (50.0, 0, "complete", False),
    (50.0, 1, "complete", False),
])
def test_phone_stall_uses_counter_and_rounding_bounds(worst, over_50, status, blocking):
    stats = payload()
    stats["phases"][1].update(worst=worst, over_33=1, over_50=over_50)
    doc = dict(phases=[dict(name=name) for name in pacing.FRAME_PHASES])
    run = pacing.posted_run(stats, doc, dict(ua="iPhone"))
    assert run["status"] == status
    assert bool(run["phases"][1]["findings"]) == blocking
    if status == "impossible":
        assert "100 ms" in run["reason"]


def test_busy_mac_is_left_alone(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1])
        assert argv[1] == "start", "a refused start must not stop another measurement's Chrome"
        return subprocess.CompletedProcess(argv, pacing.MAC_BUSY, "busy: isolated Mac runner is busy", "")

    monkeypatch.setattr(pacing.subprocess, "run", run)
    result = pacing.device_capture(dict(name="mac-m3"), "https://example.org/", {})
    assert result["status"] == "impossible" and "busy" in result["reason"]
    assert calls == ["start"]
