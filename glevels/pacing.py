"""Measure .glevels/pacing.json timed phases on desktop and a 4x CPU phone.

    python3 glevels/pacing.py --repo REPO [--out DIR] [--json]

Recipes name serve, ready, url, profiles (desktop/phone), variants
(normal/reduced/nowebgl), and phases with name, seconds, action. Actions are
load, idle, scroll (to selector or bottom), scrollTo/click (selector), and drag
(selector, dx, dy, seconds). Action parameters sit beside action; an object
{type: "scroll", to: "bottom", seconds: 20} is also accepted. Scroll sends wheel
events over real time, and drag sends timed mouse moves. No synthetic clock.

Requires Python Playwright/Chromium. Each browser owns GSPACING_LOCK and waits
for 700 MB available RAM. Exit 0: no blocking findings; 1: findings; 2: impossible.
Raw Chrome traces and attributed telemetry accompany findings.json. rAF gaps
are not presentation counts: trace drops/partial presentations are separate.
"""
import argparse
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

try:
    from .pages import PageInputs
    from .spacing import free_mb
except ImportError:
    from pages import PageInputs
    from spacing import free_mb


DISPLAY_MS = 1000 / 60  # Headless Chromium's nominal display, never the page's slow median.
# API references checked 2026-11-05 (Playwright 1.58 / bundled Chromium 145):
# https://developer.chrome.com/docs/web-platform/long-animation-frames
# https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-init-script
# https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json
# Presentation fields follow DevTools' TraceEvents.ts DroppedFrame/PipelineReporter.
PROFILES = {
    "desktop": dict(viewport=dict(width=1440, height=900), device_scale_factor=1),
    "phone": dict(viewport=dict(width=390, height=844), device_scale_factor=3,
                  is_mobile=True, has_touch=True),
}
VARIANTS = ("normal", "reduced", "nowebgl")
GL_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
           "--ignore-gpu-blocklist"]

INSTRUMENT = r"""(() => {
  if (window !== window.top) return;
  const s = window.__pacing = {raf: [], loaf: [], tasks: [], observers: []};
  const script = e => ({startTime: e.startTime, duration: e.duration,
    sourceURL: e.sourceURL, sourceFunctionName: e.sourceFunctionName,
    sourceCharPosition: e.sourceCharPosition, executionStart: e.executionStart,
    invoker: e.invoker, invokerType: e.invokerType,
    forcedStyleAndLayoutDuration: e.forcedStyleAndLayoutDuration});
  s.supported = PerformanceObserver.supportedEntryTypes;
  for (const [type, key] of [['long-animation-frame', 'loaf'], ['longtask', 'tasks']]) {
    if (!s.supported.includes(type)) continue;
    const collect = entries => entries.forEach(e => s[key].push({
      startTime: e.startTime, duration: e.duration, name: e.name,
      blockingDuration: e.blockingDuration, renderStart: e.renderStart,
      styleAndLayoutStart: e.styleAndLayoutStart,
      scripts: e.scripts ? [...e.scripts].map(script) : [],
      attribution: e.attribution ? [...e.attribution].map(a => a.toJSON()) : []
    }));
    const observer = new PerformanceObserver(list => collect(list.getEntries()));
    observer.observe({type, buffered: true});
    s.observers.push({observer, collect});
  }
  function tick(timestamp) {
    s.raf.push({timestamp, callback: performance.now()});
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  s.snapshot = () => {
    s.observers.forEach(({observer, collect}) => collect(observer.takeRecords()));
    return {raf: s.raf, loaf: s.loaf, tasks: s.tasks, supported: s.supported};
  };
})()"""


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def recipe_read(path):
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError("pacing recipe must be an object")
    for key in ("serve", "ready", "url"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise ValueError(f"recipe needs {key}")
    for key, choices in (("profiles", PROFILES), ("variants", VARIANTS)):
        values = doc.setdefault(key, list(choices))
        if not isinstance(values, list) or not values or any(v not in choices for v in values) or len(set(values)) != len(values):
            raise ValueError(f"{key} must be a nonempty unique subset of {list(choices)}")
    positive(doc.get("timeout", 20), "timeout")
    if doc.get("timeout", 20) > 300:
        raise ValueError("timeout must be at most 300 seconds")
    phases = doc.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError("recipe needs phases")
    names = set()
    for i, phase in enumerate(phases):
        if not isinstance(phase, dict):
            raise ValueError("each phase must be an object")
        if isinstance(phase.get("action"), dict):
            action = dict(phase["action"])
            phase.update(action)
            phase["action"] = action.get("type")
        name = phase.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("phases need unique nonempty names")
        names.add(name)
        positive(phase.get("seconds"), "phase seconds")
        action = phase.get("action")
        if action not in ("load", "idle", "scroll", "scrollTo", "click", "drag"):
            raise ValueError(f"unknown action: {action}")
        if (i == 0) != (action == "load"):
            raise ValueError("first phase must load; subsequent phases must not navigate")
        if action in ("scrollTo", "click", "drag", "scroll"):
            key = "to" if action == "scroll" else "selector"
            if not isinstance(phase.get(key), str) or not phase[key].strip():
                raise ValueError(f"{action} needs {key}")
        if action == "drag":
            for key in ("dx", "dy"):
                value = phase.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"drag needs finite {key}")
    return doc


class RecipeServer(PageInputs):
    """Use pages' process-group cleanup and signal handling for the pacing recipe."""
    def __init__(self, repo, doc, path):
        super().__init__(repo)
        self.doc, self.path, self.present = doc, path, True

    def start(self):
        # Never collide with the studio's live development port.
        while True:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                if port != 8240:
                    break
        def expand(value):
            return value.replace("{port}", str(port)).replace("{repo}", str(self.repo))
        ready, self.url = expand(self.doc["ready"]), expand(self.doc["url"])
        for url in (ready, self.url):
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or any(c.isspace() for c in url):
                raise ValueError(f"invalid URL: {url}")
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(expand(self.doc["serve"]), shell=True, cwd=self.repo,
                                        stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + self.doc.get("timeout", 20)
        reason = "no response"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(f"server exited {self.process.returncode}: {self.log.read().decode(errors='replace')[-2000:]}")
            try:
                with urllib.request.urlopen(ready, timeout=1) as response:
                    if 200 <= response.status < 300:
                        return
            except (OSError, urllib.error.URLError) as error:
                reason = str(error)
            time.sleep(.1)
        raise RuntimeError(f"server not ready: {ready}: {reason}")


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    lower = math.floor(index)
    return round(values[lower] + (values[math.ceil(index)] - values[lower]) * (index - lower), 2)


def overlap(a, b, start, duration):
    return max(0, min(b, start + duration) - max(a, start))


def trace_events(path):
    """Parse Chrome's traceEvents array with bounded memory, regardless of line breaks."""
    decoder = json.JSONDecoder()
    with path.open() as source:
        buffer = ""
        while True:
            chunk = source.read(65536)
            if not chunk:
                raise RuntimeError("trace has no traceEvents array")
            buffer += chunk
            match = re.search(r'"traceEvents"\s*:\s*\[', buffer)
            if match:
                buffer = buffer[match.end():]
                break
        while True:
            buffer = buffer.lstrip(" \n\r\t,")
            if buffer.startswith("]"):
                return
            try:
                event, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = source.read(65536)
                if not chunk:
                    raise RuntimeError("incomplete traceEvents array")
                buffer += chunk
                continue
            yield event
            buffer = buffer[end:]


def trace_read(path, sync_ms):
    """Read a streamed Chrome JSON trace one event at a time, keeping only evidence."""
    events, sync, main = [], None, None
    for event in trace_events(path):
        name = event.get("name", "")
        if name == "__pacing_sync":
            sync, main = event["ts"] / 1000 - sync_ms, (event["pid"], event["tid"])
        if name == "DroppedFrame" or name == "PipelineReporter" or (
            event.get("ph") == "X" and event.get("dur", 0) / 1000 > DISPLAY_MS
            and name in ("RunTask", "ThreadControllerImpl::RunTask", "FunctionCall", "Layout", "Paint", "UpdateLayoutTree")
        ):
            events.append(event)
    if sync is None:
        raise RuntimeError("Chrome trace has no clock sync marker")
    tasks, drops, reporters = [], [], []
    for event in events:
        start = event.get("ts", 0) / 1000 - sync
        if event["name"] in ("DroppedFrame", "PipelineReporter") and event.get("pid") != main[0]:
            continue  # This page's renderer, not unrelated browser/display surfaces.
        if event["name"] == "DroppedFrame":
            drops.append(dict(startTime=start, partial=bool(event.get("args", {}).get("hasPartialUpdate")),
                              args=event.get("args", {})))
        elif event["name"] == "PipelineReporter":
            args = event.get("args", {})
            reporter = args.get("frame_reporter") or args.get("chrome_frame_reporter")
            if reporter and reporter.get("state"):
                reporters.append(dict(startTime=start, **reporter))
        elif (event.get("pid"), event.get("tid")) == main:
            tasks.append(dict(startTime=start, duration=event["dur"] / 1000, name=event["name"],
                              args=event.get("args", {})))
    return dict(tasks=tasks, dropped=drops, reporters=reporters,
                presentation_supported=bool(drops or reporters))


def analyse_phase(window, telemetry, trace, errors, profile):
    start, end = window["start"], window["end"]
    raf = telemetry["raf"]
    # Assign a boundary-spanning gap to the phase in which the callback arrived.
    pairs = [(a["timestamp"], b["timestamp"], a["callback"], b["callback"])
             for a, b in zip(raf, raf[1:]) if start <= b["callback"] < end]
    intervals = [b - a for a, b, _, _ in pairs]
    loaf = [e for e in telemetry["loaf"] if overlap(start, end, e["startTime"], e["duration"]) and e["duration"] > 50]
    tasks = [e for e in telemetry["tasks"] if overlap(start, end, e["startTime"], e["duration"])]
    busy = trace["tasks"] + telemetry["tasks"]
    # Only actual script execution counts; a LoAF's wall duration can include
    # waiting on software raster/GPU work. Trace tasks cover sub-50ms jank too.
    busy += [script for frame in telemetry["loaf"] for script in frame.get("scripts", [])]
    drops = [(a, b, ca, cb) for a, b, ca, cb in pairs if b - a > DISPLAY_MS * 1.5]
    # Chromium may deliver a stale display timestamp after the blocking task,
    # followed almost immediately by another rAF. Include the display interval
    # as well as callback delivery time, or those real stalls appear GPU-bound.
    main_drops = sum(any(overlap(a, max(b, cb), e["startTime"], e["duration"]) > DISPLAY_MS for e in busy)
                     for a, b, _, cb in drops)
    phase_errors = [e for e in errors if start <= e["time"] < end]
    count = len(pairs)
    dropped_percent = 100 * len(drops) / count if count else 0
    main_percent = 100 * main_drops / count if count else 0
    findings = []
    if window["action"] != "load":
        limit = 100 if profile == "phone" else 50
        if any(e["duration"] > limit for e in loaf):
            findings.append(f"long animation frame > {limit} ms")
        if main_percent > (2 if profile == "phone" else 1):
            findings.append(f"main-thread dropped intervals {main_percent:.2f}%")
    if any(e["kind"] == "pageerror" for e in phase_errors):
        findings.append("page error")
    if count < 2:
        raise RuntimeError(f"too few rAF samples in {window['name']}")
    phase_trace = {key: [e for e in trace[key] if start <= e["startTime"] < end]
                   for key in ("dropped", "reporters")}
    # Prefer explicit DroppedFrame events; PipelineReporter is the fallback on
    # versions exporting only reporter states. Never sum both representations.
    trace_partial = sum(e["partial"] for e in phase_trace["dropped"])
    trace_dropped = len(phase_trace["dropped"]) - trace_partial
    if not trace["dropped"]:
        trace_dropped = sum(e["state"] == "STATE_DROPPED" for e in phase_trace["reporters"])
        trace_partial = sum(e["state"] == "STATE_PRESENTED_PARTIAL" for e in phase_trace["reporters"])
    classification = "main-thread" if main_drops else "unattributed/raster-or-GPU candidate" if drops else "steady"
    return dict(**window, frame_count=sum(start <= e["callback"] < end for e in raf),
                intervals=len(intervals), interval_ms={key: percentile(intervals, q) for key, q in
                                                     (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))},
                dropped_count=len(drops), dropped_percent=round(dropped_percent, 3),
                main_thread_dropped_count=main_drops, main_thread_dropped_percent=round(main_percent, 3),
                other_dropped_count=len(drops) - main_drops, classification=classification,
                long_animation_frames=loaf, long_tasks=tasks, errors=phase_errors,
                trace_dropped=trace_dropped if trace["presentation_supported"] else None,
                trace_partial=trace_partial if trace["presentation_supported"] else None,
                trace_reporters=phase_trace["reporters"], findings=findings)


def wait_until(page, deadline):
    page.wait_for_timeout(max(0, (deadline - time.monotonic()) * 1000))


def action_run(page, phase):
    action, seconds = phase["action"], phase["seconds"]
    if action == "scrollTo":
        page.locator(phase["selector"]).first.evaluate("el => el.scrollIntoView({behavior: 'instant', block: 'start'})")
    deadline = time.monotonic() + seconds
    if action == "click":
        page.locator(phase["selector"]).first.click()
    elif action in ("scroll", "drag"):
        if action == "scroll":
            target = page.evaluate("selector => selector === 'bottom' ? document.documentElement.scrollHeight - innerHeight : document.querySelector(selector).getBoundingClientRect().top + scrollY", phase["to"])
            origin = page.evaluate("scrollY")
            box = None
        else:
            locator = page.locator(phase["selector"]).first
            locator.scroll_into_view_if_needed()
            box = locator.bounding_box()
            if not box:
                raise ValueError(f"drag target is not visible: {phase['selector']}")
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
        started = deadline - seconds
        sent = 0
        try:
            while time.monotonic() < deadline:
                # Input acknowledgement can itself wait behind a busy renderer.
                # Skip overdue ticks rather than extending a 20s scroll into
                # minutes by trying to send every missed wheel event afterwards.
                tick = math.floor((time.monotonic() - started) * 30) + 1
                fraction = min(1, tick / (seconds * 30))
                if action == "scroll":
                    desired = (target - origin) * fraction
                    page.mouse.wheel(0, desired - sent)
                    sent = desired
                else:
                    page.mouse.move(x + phase["dx"] * fraction, y + phase["dy"] * fraction)
                wait_until(page, started + seconds * fraction)
            if action == "scroll" and abs(target - origin - sent) > .01:
                page.mouse.wheel(0, target - origin - sent)
            elif action == "drag":
                page.mouse.move(x + phase["dx"], y + phase["dy"])
        finally:
            if box:
                page.mouse.up()
    wait_until(page, deadline)


def capture(playwright, url, doc, profile, variant, out):
    """Called with the shared lock held, once per fresh browser/process pair."""
    browser = playwright.chromium.launch(args=GL_ARGS + (["--disable-webgl", "--disable-webgl2"] if variant == "nowebgl" else []))
    chromium_version = browser.version
    try:
        options = dict(PROFILES[profile], reduced_motion="reduce" if variant == "reduced" else "no-preference")
        if profile == "phone":
            options["user_agent"] = ("Mozilla/5.0 (Linux; Android 13; Pixel 6a) AppleWebKit/537.36 "
                                     f"(KHTML, like Gecko) Chrome/{browser.version} Mobile Safari/537.36")
        context = browser.new_context(**options)
        context.add_init_script(INSTRUMENT)
        page = context.new_page()
        page.set_default_timeout(120000)
        errors = []
        active = {"name": "load"}
        def record(kind, message):
            # Browser callbacks cannot synchronously evaluate page JS. Host monotonic
            # time is converted to the page clock after the run, including load errors.
            errors.append(dict(kind=kind, message=message, host=time.monotonic(), phase=active["name"]))
        page.on("pageerror", lambda error: record("pageerror", str(error)))
        page.on("console", lambda message: record("console", message.text) if message.type == "error" else None)
        page.on("dialog", lambda dialog: dialog.dismiss())
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4 if profile == "phone" else 1})
        trace_path = out / f"{profile}-{variant}.trace.json"
        with trace_path.open("w") as stream:
            complete = []
            cdp.on("Tracing.tracingComplete", lambda data: complete.append(data))
            cdp.send("Tracing.start", {"categories": "-*,devtools.timeline,blink.user_timing,toplevel,disabled-by-default-devtools.timeline.frame",
                                       "transferMode": "ReturnAsStream", "options": "recordContinuously"})
            windows = []
            for phase in doc["phases"]:
                print(f"pacing: {profile}/{variant}/{phase['name']}", file=sys.stderr, flush=True)
                active["name"] = phase["name"]
                if phase["action"] == "load":
                    navigation = time.monotonic()
                    response = page.goto(url, wait_until="commit", timeout=120000)
                    if response and response.status >= 400:
                        record("pageerror", f"HTTP {response.status}")
                    start = 0
                    wait_until(page, navigation + phase["seconds"])
                else:
                    start = page.evaluate("performance.now()")
                    action_run(page, phase)
                end = page.evaluate("performance.now()")
                windows.append(dict(name=phase["name"], action=phase["action"], start=start, end=end))
            # Deliver trailing observer entries; they are still assigned by their own timestamps.
            page.wait_for_timeout(100)
            telemetry = page.evaluate("window.__pacing.snapshot()")
            if not {"long-animation-frame", "longtask"}.issubset(telemetry["supported"]):
                raise RuntimeError("Chromium lacks long-animation-frame or longtask observers")
            sync_ms = page.evaluate("performance.mark('__pacing_sync').startTime")
            clock_host = time.monotonic()
            clock_page = page.evaluate("performance.now()")
            cdp.send("Tracing.end")
            deadline = time.monotonic() + 120
            while not complete and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            if not complete or complete[0].get("dataLossOccurred"):
                raise RuntimeError("Chrome trace incomplete or lost events")
            handle = complete[0]["stream"]
            try:
                while True:
                    chunk = cdp.send("IO.read", {"handle": handle, "size": 1048576})
                    if chunk.get("base64Encoded"):
                        raise RuntimeError("expected an uncompressed JSON trace stream")
                    stream.write(chunk["data"])
                    if chunk.get("eof"):
                        break
            finally:
                cdp.send("IO.close", {"handle": handle})
        # Probe a new, unattached canvas only after measurement; do not affect the recipe.
        renderer = page.evaluate("""() => {
          const gl = document.createElement('canvas').getContext('webgl');
          if (!gl) return null;
          const ext = gl.getExtension('WEBGL_debug_renderer_info');
          const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
          gl.getExtension('WEBGL_lose_context')?.loseContext();
          return renderer;
        }""")
        if variant == "nowebgl" and renderer is not None:
            raise RuntimeError("WebGL-disabled control still has a WebGL context")
        browser.close()
        browser = None  # Release Chromium's RAM before reading and analysing its trace.
        trace = trace_read(trace_path, sync_ms)
        for error in errors:
            error["time"] = clock_page + (error.pop("host") - clock_host) * 1000
        phases = [analyse_phase(w, telemetry, trace, errors, profile) for w in windows]
        # Keep event-phase tags as well: host-to-page clock conversion can straddle a boundary.
        for error in errors:
            phase = next(p for p in phases if p["name"] == error["phase"])
            if error not in phase["errors"]:
                phase["errors"].append(error)
            if error["kind"] == "pageerror" and "page error" not in phase["findings"]:
                phase["findings"].append("page error")
        (out / f"{profile}-{variant}.telemetry.json").write_text(json.dumps(telemetry))
        return dict(profile=profile, variant=variant, chromium=chromium_version, renderer=renderer,
                    cpu_throttle=4 if profile == "phone" else 1, context=options, phases=phases,
                    errors=errors, trace=str(trace_path), trace_sync_ms=sync_ms,
                    presentation_supported=trace["presentation_supported"])
    finally:
        if browser:
            browser.close()


def comparisons(runs):
    result = []
    for normal in (r for r in runs if r["variant"] == "normal"):
        control = next((r for r in runs if r["profile"] == normal["profile"] and r["variant"] == "nowebgl"), None)
        if not control:
            continue
        for a, b in zip(normal["phases"], control["phases"]):
            result.append(dict(profile=normal["profile"], phase=a["name"],
                               p95_cost_ms=round(a["interval_ms"]["p95"] - b["interval_ms"]["p95"], 2),
                               dropped_cost_pp=round(a["dropped_percent"] - b["dropped_percent"], 3),
                               loaf_cost=len(a["long_animation_frames"]) - len(b["long_animation_frames"])))
    return result


def report(data):
    print("profile variant phase frames p50/p95/p99/max(ms) drops(%) main(%) LoAF>50 longtasks trace-drop/partial errors")
    print("rAF drops use a 60 Hz display; main = correlated main-thread work. Remaining gaps are unattributed raster/GPU candidates.")
    for run in data["runs"]:
        for phase in run["phases"]:
            timing = "/".join(f"{phase['interval_ms'][k]:.1f}" for k in ("p50", "p95", "p99", "max"))
            print(f"{run['profile']} {run['variant']} {phase['name']} {phase['frame_count']} {timing} "
                  f"{phase['dropped_count']}({phase['dropped_percent']:.2f}) "
                  f"{phase['main_thread_dropped_count']}({phase['main_thread_dropped_percent']:.2f}) "
                  f"{len(phase['long_animation_frames'])} {len(phase['long_tasks'])} "
                  f"{phase['trace_dropped']}/{phase['trace_partial']} {len(phase['errors'])}")
            if phase["findings"]:
                print("  blocking: " + "; ".join(phase["findings"]))
            scripts = [s for e in phase["long_animation_frames"] for s in e.get("scripts", [])]
            for script in sorted(scripts, key=lambda s: s["duration"], reverse=True)[:3]:
                print(f"  {script['duration']:.1f}ms {script.get('sourceURL')} "
                      f"{script.get('sourceFunctionName')} [{script.get('invoker')}] "
                      f"forced-layout={script.get('forcedStyleAndLayoutDuration', 0):.1f}ms")
        print(f"  Chromium {run['chromium']}; renderer={run['renderer']}; trace presentation={run['presentation_supported']}")
        for error in run["errors"]:
            print(f"  {error['phase']} {error['kind']}: {error['message']}")
    print("WebGL cost (normal minus nowebgl; paired host measurements, not real-phone GPU speed):")
    for row in data["webgl_comparison"]:
        print(f"  {row['profile']} {row['phase']}: p95 {row['p95_cost_ms']:+.2f}ms, "
              f"drops {row['dropped_cost_pp']:+.3f}pp, LoAF {row['loaf_cost']:+d}")
    blocking = sum(bool(p["findings"]) for r in data["runs"] for p in r["phases"])
    print(f"pacing: {len(data['runs'])} runs, {blocking} blocking phases")
    print(f"artefacts: {data['out']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = None
    try:
        repo = Path(args.repo).resolve()
        path = repo / ".glevels/pacing.json"
        doc = recipe_read(path)
        from playwright.sync_api import sync_playwright
        out = Path(args.out or tempfile.mkdtemp(prefix="glevels-pacing-")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        runs = []
        with RecipeServer(repo, doc, path) as server:
            server.start()
            with sync_playwright() as playwright:
                lock_path = Path(os.environ.get("GSPACING_LOCK", "/tmp/g-studio-browser.lock"))
                with lock_path.open("a") as lock:
                    print(f"pacing: acquiring browser lock {lock_path}", file=sys.stderr, flush=True)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    for profile in doc["profiles"]:
                        for variant in doc["variants"]:
                            while True:
                                subprocess.run(["free", "-m"], stdout=sys.stderr, check=True)
                                if free_mb() >= 700:
                                    break
                                print("pacing: waiting 20s for 700 MB available", file=sys.stderr, flush=True)
                                time.sleep(20)
                            print(f"pacing: {profile}/{variant}", file=sys.stderr, flush=True)
                            runs.append(capture(playwright, server.url, doc, profile, variant, out))
                            # Persist every completed run, including if a later run cannot finish.
                            (out / "findings.json").write_text(json.dumps(dict(runs=runs), indent=2))
        data = dict(runs=runs, webgl_comparison=comparisons(runs), out=str(out),
                    display_interval_ms=DISPLAY_MS, playwright=importlib.metadata.version("playwright"))
        (out / "findings.json").write_text(json.dumps(data, indent=2))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            report(data)
        return int(any(p["findings"] for r in runs for p in r["phases"]))
    except KeyboardInterrupt:
        print("impossible: pacing interrupted", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"impossible: {error}", file=sys.stderr)
        if out:
            print(f"partial artefacts: {out}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
