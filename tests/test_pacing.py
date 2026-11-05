import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from glevels import pacing


def recipe(**changes):
    doc = dict(serve="python3 -m http.server {port} --bind 127.0.0.1",
               ready="http://127.0.0.1:{port}/", url="http://127.0.0.1:{port}/",
               profiles=["desktop", "phone"], variants=["normal"],
               phases=[dict(name="load", action="load", seconds=1),
                       dict(name="idle", action="idle", seconds=5),
                       dict(name="scroll", action="scroll", to="bottom", seconds=4)])
    return dict(doc, **changes)


def write_recipe(root, doc):
    folder = root / ".glevels"
    folder.mkdir(exist_ok=True)
    path = folder / "pacing.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("change", [
    {"profiles": []}, {"profiles": ["tablet"]}, {"profiles": ["phone", "phone"]},
    {"variants": ["missing"]}, {"timeout": 301},
    {"phases": []}, {"phases": [dict(name="x", action="idle", seconds=1)]},
    {"phases": [dict(name="x", action="load", seconds=float("nan"))]},
    {"phases": [dict(name="x", action="load", seconds=1), dict(name="y", action="drag", seconds=1)]},
])
def test_invalid_recipe_is_impossible(tmp_path, change):
    with pytest.raises(ValueError):
        pacing.recipe_read(write_recipe(tmp_path, recipe(**change)))


def test_action_object_and_default_variants(tmp_path):
    doc = recipe(phases=[dict(name="load", seconds=1, action={"type": "load"}),
                        dict(name="scroll", seconds=4, action={"type": "scroll", "to": "bottom", "seconds": 2})])
    del doc["variants"]
    result = pacing.recipe_read(write_recipe(tmp_path, doc))
    assert result["variants"] == list(pacing.VARIANTS)
    assert result["phases"][1]["seconds"] == 2


def telemetry(gaps, loaf=None, tasks=None):
    raf, stamp = [], 0
    for gap in [0, *gaps]:
        stamp += gap
        raf.append(dict(timestamp=stamp, callback=stamp))
    return dict(raf=raf, loaf=loaf or [], tasks=tasks or [])


def test_fixed_display_and_main_thread_correlation():
    data = telemetry([33.33] * 30)
    window = dict(name="idle", action="idle", start=0, end=1100)
    trace = dict(tasks=[], dropped=[], reporters=[], presentation_supported=False)
    slow = pacing.analyse_phase(window, data, trace, [], "phone")
    assert slow["dropped_count"] == 30  # A slow page cannot redefine the display rate.
    assert slow["dropped_percent"] == 100
    assert slow["main_thread_dropped_count"] == 0
    assert slow["trace_dropped"] is None
    assert slow["findings"] == []
    trace["tasks"] = [dict(startTime=100, duration=30)]
    blocked = pacing.analyse_phase(window, data, trace, [], "phone")
    assert blocked["main_thread_dropped_percent"] > 2
    assert blocked["findings"]


def test_load_exemption_errors_and_attribution():
    script = dict(startTime=100, duration=80, sourceURL="fixture.js",
                  sourceFunctionName="stall", invoker="Window.setInterval")
    data = telemetry([16.67] * 30, loaf=[dict(startTime=100, duration=80, scripts=[script])])
    trace = dict(tasks=[], dropped=[], reporters=[], presentation_supported=True)
    window = dict(name="idle", action="idle", start=0, end=600)
    result = pacing.analyse_phase(window, data, trace, [], "desktop")
    assert result["findings"] == ["long animation frame > 50 ms"]
    assert result["long_animation_frames"][0]["scripts"][0] == script
    assert not pacing.analyse_phase(dict(window, action="load"), data, trace, [], "desktop")["findings"]
    error = dict(kind="pageerror", time=10, message="failed")
    assert "page error" in pacing.analyse_phase(dict(window, action="load"), data, trace, [error], "phone")["findings"]


def test_delayed_raf_timestamp_is_correlated_with_the_stall():
    # Chromium can deliver an old display timestamp just after a blocking task,
    # then deliver the next rAF almost immediately. Callback-to-callback time
    # alone misses the busy span that caused this long display interval.
    data = dict(raf=[dict(timestamp=588.7, callback=589),
                     dict(timestamp=605.4, callback=677.9),
                     dict(timestamp=672, callback=679.2)], loaf=[],
                tasks=[dict(startTime=597.4, duration=80)])
    trace = dict(tasks=[], dropped=[], reporters=[], presentation_supported=False)
    phase = pacing.analyse_phase(dict(name="idle", action="idle", start=0, end=1000), data, trace, [], "phone")
    assert phase["dropped_count"] == 1
    assert phase["main_thread_dropped_count"] == 1
    assert phase["findings"]


def test_trace_presentation_and_main_thread_filter(tmp_path):
    path = tmp_path / "trace.json"
    events = [
        dict(name="__pacing_sync", ts=1000000, pid=1, tid=2),
        dict(name="RunTask", ph="X", ts=1010000, dur=30000, pid=1, tid=2),
        dict(name="RunTask", ph="X", ts=1010000, dur=90000, pid=1, tid=3),
        dict(name="DroppedFrame", ts=1020000, pid=1, args=dict(frameSeqId=1, hasPartialUpdate=True)),
        dict(name="PipelineReporter", ts=1020000, pid=1, args=dict(chrome_frame_reporter=dict(
            state="STATE_PRESENTED_PARTIAL", reason="REASON_MAIN_THREAD"))),
    ]
    path.write_text('{"traceEvents":[\n' + "".join(json.dumps(e) + ",\n" for e in events) + '{}]}\n')
    trace = pacing.trace_read(path, 100)
    assert len(trace["tasks"]) == 1
    assert trace["tasks"][0]["startTime"] == 110
    assert trace["dropped"][0]["partial"]
    assert trace["reporters"][0]["state"] == "STATE_PRESENTED_PARTIAL"
    phase = pacing.analyse_phase(dict(name="idle", action="idle", start=0, end=600),
                                 telemetry([16.67] * 30), trace, [], "desktop")
    assert phase["trace_partial"] == 1
    assert phase["trace_dropped"] == 0  # Partially presented is not wholly dropped.
    trace["dropped"] = []
    phase = pacing.analyse_phase(dict(name="idle", action="idle", start=0, end=600),
                                 telemetry([16.67] * 30), trace, [], "desktop")
    assert phase["trace_partial"] == 1
    assert phase["trace_dropped"] == 0
    events[-1]["args"]["frame_reporter"] = events[-1]["args"].pop("chrome_frame_reporter")
    events.append(dict(events[-1], pid=999))  # Another surface must not double the count.
    path.write_text(json.dumps(dict(traceEvents=events)))
    trace = pacing.trace_read(path, 100)
    assert len(trace["reporters"]) == 1
    assert trace["presentation_supported"]


def test_trace_stream_across_chunk_boundaries(tmp_path):
    events = [dict(name="large", args=dict(text='quotes" and ] brackets' * 5000)), dict(name="last")]
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(dict(traceEvents=events)))
    assert list(pacing.trace_events(path)) == events
    path.write_text('{"traceEvents":[{"name":"incomplete"')
    with pytest.raises(RuntimeError, match="incomplete"):
        list(pacing.trace_events(path))


BASE = """<!doctype html><html><head><link rel="icon" href="data:,"><style>
body {margin:0; height:6000px} #box {position:fixed; top:20px; width:40px;
height:40px; background:coral; will-change:transform; animation:slide 2s ease-in-out infinite alternate}
@keyframes slide {to {transform:translateX(180px)}}
</style></head><body><div id="box"></div><script>
let frames=0; function light(){frames++; requestAnimationFrame(light)} requestAnimationFrame(light);
"""
FIXTURES = {
    "busy": """setInterval(function busyLoop(){const start=performance.now();
      while(performance.now()-start<80){}},500);""",
    "layout": """const container=document.createElement('div'); container.style.display='flex';
      container.style.flexWrap='wrap'; document.body.append(container);
      for(let i=0;i<400;i++){const node=document.createElement('div'); node.textContent='layout';
        node.style.width='30px'; container.append(node)}
      let n=0; addEventListener('scroll', function forcedLayout(){const start=performance.now();
        do{container.style.width=(300+(n++%50))+'px'; window.heightSink=container.offsetHeight}
        while(performance.now()-start<80)});""",
    "clean": "",
}


@pytest.mark.parametrize("case", ["busy", "layout", "clean"])
def test_browser_failure_classes_and_clean_both_profiles(tmp_path, case):
    # Each subprocess owns the shared lock and memory guard, including in a full suite run.
    pytest.importorskip("playwright.sync_api")
    write_recipe(tmp_path, recipe())
    (tmp_path / "index.html").write_text(BASE + FIXTURES[case] + "</script></body></html>")
    out = tmp_path / "out"
    process = subprocess.run([sys.executable, str(Path(pacing.__file__)), "--repo", str(tmp_path), "--out", str(out)],
                             env=dict(os.environ, PLAYWRIGHT_BROWSERS_PATH="/home/gabriel/.cache/ms-playwright",
                                      GSPACING_LOCK="/tmp/g-studio-browser.lock"),
                             capture_output=True, text=True, timeout=7200)
    assert process.returncode == (0 if case == "clean" else 1), process.stdout + process.stderr
    runs = json.loads((out / "findings.json").read_text())["runs"]
    assert {r["profile"] for r in runs} == {"desktop", "phone"}
    for run in runs:
        assert Path(run["trace"]).is_file()
        assert not run["errors"]
        if case == "clean":
            assert not any(p["findings"] for p in run["phases"])
        else:
            phase = next(p for p in run["phases"] if p["name"] == ("idle" if case == "busy" else "scroll"))
            assert phase["findings"], process.stdout
            assert phase["main_thread_dropped_count"] > 0
            scripts = [s for e in phase["long_animation_frames"] for s in e["scripts"]]
            assert any(s["sourceURL"] and s["invoker"] for s in scripts)
            if case == "layout":
                assert any(s["forcedStyleAndLayoutDuration"] > 0 for s in scripts)
