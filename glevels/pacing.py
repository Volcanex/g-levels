"""Measure frame pacing on online g-runner devices, with Playwright fallback.

    python3 glevels/pacing.py --repo REPO [--device auto|NAME|none] [--out DIR] [--json]

Recipes name serve, ready, url, profiles (desktop/phone), variants
(normal/reduced/nowebgl), and phases with name, seconds, action. Actions are
load, idle, scroll (to selector or bottom), scrollTo/click (selector), and drag
(selector, dx, dy, seconds). Action parameters sit beside action; an object
{type: "scroll", to: "bottom", seconds: 20} is also accepted. Scroll sends wheel
events over real time, and drag sends timed mouse moves. No synthetic clock.

Devices run first through g-runner; --device none forces Playwright fallback.
runner_query=gate=frames uses the page driver and window.__gsFrames in fallback.
Each fallback browser owns GSPACING_LOCK and waits for 900 MB available RAM. Exit 0: no blocking findings; 1: findings; 2: impossible.
Analysed trace evidence and attributed telemetry accompany findings.json.
Raw traces are deleted after analysis unless --keep-traces is passed. rAF gaps
are not presentation counts: trace drops/partial presentations are separate.
"""
import argparse
from contextlib import contextmanager
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
G_RUN = [sys.executable, "/home/gabriel/g-runner/bin/g-run"]
MAC_RUNNER = Path(__file__).resolve().parents[1] / "scripts/mac-runner"
MAC_BUSY = 3  # scripts/mac-runner refused before launching (another measurement holds the Mac)
FRAME_PHASES = ["intro", "hero", "scroll", "iso", "work", "features"]


def thresholds(doc=None):
    gate = Path(__file__).resolve().parents[1] / "gates/correct.motion.frame-pacing.toml"
    limits = tomllib.loads(gate.read_text())["thresholds"]
    for key, value in (doc or {}).get("thresholds", {}).items():
        if key not in limits:
            raise ValueError(f"unknown pacing threshold: {key}")
        limits[key] = positive(value, key)
    return limits

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
    for key in ("url",):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise ValueError(f"recipe needs {key}")
    if any(key in doc for key in ("serve", "ready")):
        for key in ("serve", "ready"):
            if not isinstance(doc.get(key), str) or not doc[key].strip():
                raise ValueError(f"recipe needs {key}")
    for key, choices in (("profiles", PROFILES), ("variants", VARIANTS)):
        values = doc.setdefault(key, list(choices))
        if not isinstance(values, list) or not values or any(v not in choices for v in values) or len(set(values)) != len(values):
            raise ValueError(f"{key} must be a nonempty unique subset of {list(choices)}")
    positive(doc.get("timeout", 20), "timeout")
    if doc.get("timeout", 20) > 300:
        raise ValueError("timeout must be at most 300 seconds")
    thresholds(doc)
    for key, default, maximum in (("runner_duration", 70, 1800), ("runner_timeout", 120, 3600),
                                  ("runner_wait", 150, 3600), ("frames_timeout", 60, 300)):
        value = positive(doc.setdefault(key, default), key)
        if value > maximum:
            raise ValueError(f"{key} must be at most {maximum} seconds")
    if doc["runner_timeout"] < doc["runner_duration"] or doc["runner_duration"] < 1:
        raise ValueError("runner duration must be >= 1 and <= runner timeout")
    if "runner_query" in doc:
        query = doc["runner_query"]
        if not isinstance(query, str) or not query or query.startswith("?") or "#" in query:
            raise ValueError("runner_query must be a query string, e.g. gate=frames")
        doc.setdefault("phases", [dict(name=name) for name in FRAME_PHASES])
        if not isinstance(doc["phases"], list):
            raise ValueError("phases must be a list")
        names = [p.get("name") for p in doc["phases"] if isinstance(p, dict)]
        if not names or len(names) != len(doc["phases"]) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
            raise ValueError("phases need unique nonempty names")
        return doc
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
        if "serve" not in self.doc:
            self.url = self.doc["url"]
            if urlsplit(self.url).scheme not in ("http", "https") or not urlsplit(self.url).hostname:
                raise ValueError("recipe url must be HTTP(S)")
            return
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


def analyse_phase(window, telemetry, trace, errors, profile, limits=None):
    limits = limits or thresholds()
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
        limit = limits[f"{profile}_long_frame_ms"]
        if any(e["duration"] > limit for e in loaf):
            findings.append(f"long animation frame > {limit} ms")
        if main_percent > limits[f"{profile}_dropped_percent"]:
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


def capture(playwright, url, doc, profile, variant, out, keep_traces=False):
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
            payload = None
            if doc.get("runner_query"):
                response = page.goto(url, wait_until="commit", timeout=120000)
                if response and response.status >= 400:
                    record("pageerror", f"HTTP {response.status}")
                try:
                    handle = page.wait_for_function("window.__gsFrames?.gs_frames === 1 && window.__gsFrames.done === true",
                                                    timeout=doc["frames_timeout"] * 1000)
                    handle.dispose()
                except Exception:
                    raise RuntimeError("page posted no frame stats (window.__gsFrames timeout)") from None
                snapshot = page.evaluate("({payload: window.__gsFrames, end: performance.now()})")
                payload = snapshot["payload"]
                posted_run(payload, doc, {}, profile, hardware=False)  # Validate the shared page contract.
                if payload["reduced"] != (variant == "reduced") or (variant == "nowebgl" and payload["webgl"]):
                    raise RuntimeError("page did not honour reduced motion/WebGL control")
                start = max(0, snapshot["end"] - sum(p["ms"] for p in payload["phases"]))
                for phase in payload["phases"]:
                    end = start + phase["ms"]
                    windows.append(dict(name=phase["id"], action="load" if phase["id"] in ("intro", "load") else "page", start=start, end=end))
                    start = end
            for phase in ([] if payload else doc["phases"]):
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
        try:
            trace = trace_read(trace_path, sync_ms)
        finally:
            if not keep_traces:
                trace_path.unlink(missing_ok=True)
        for error in errors:
            error["time"] = clock_page + (error.pop("host") - clock_host) * 1000
            if payload:
                error["phase"] = next((w["name"] for w in windows if error["time"] < w["end"]), windows[-1]["name"])
        phases = [analyse_phase(w, telemetry, trace, errors, profile, thresholds(doc)) for w in windows]
        if payload:
            for phase, stats in zip(phases, payload["phases"]):
                phase.update(stats)
        # Keep event-phase tags as well: host-to-page clock conversion can straddle a boundary.
        for error in errors:
            phase = next(p for p in phases if p["name"] == error["phase"])
            if error not in phase["errors"]:
                phase["errors"].append(error)
            if error["kind"] == "pageerror" and "page error" not in phase["findings"]:
                phase["findings"].append("page error")
        (out / f"{profile}-{variant}.telemetry.json").write_text(json.dumps(telemetry))
        result = dict(profile=profile, variant=variant, chromium=chromium_version, renderer=renderer,
                    cpu_throttle=4 if profile == "phone" else 1, context=options, phases=phases,
                    errors=errors, trace=str(trace_path) if keep_traces else None,
                    trace_retained=keep_traces, trace_evidence=trace, trace_sync_ms=sync_ms,
                    presentation_supported=trace["presentation_supported"])
        if payload:
            result.update(posted=payload, device_info=dict(ua=options.get("user_agent", "Chromium desktop"), webgl2_renderer=renderer),
                          phase_timing="trace windows reconstructed from page durations at completion poll; boundary uncertainty up to a polling interval")
        return result
    finally:
        if browser:
            browser.close()
        if not keep_traces:
            (out / f"{profile}-{variant}.trace.json").unlink(missing_ok=True)


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


def runner_call(*args):
    # g-run owns token loading. Never read, forward or print its credentials here.
    process = subprocess.run([*G_RUN, *map(str, args)], capture_output=True, text=True)
    try:
        data = json.loads(process.stdout)
    except ValueError:
        raise RuntimeError(f"g-run returned no JSON (exit {process.returncode})") from None
    if process.returncode not in (0, 1, 4):
        raise RuntimeError(f"g-run failed (exit {process.returncode})")
    return data


def online_devices(state, selected="auto"):
    return [d for d in state["devices"] if d.get("online") and d.get("seen") is not None
            and 0 <= state["now"] - d["seen"] < 30000
            and d.get("info", {}).get("visible") is not False
            and (selected == "auto" or d["name"] == selected)]


def runner_url(url, query):
    parts = urlsplit(url)
    additions = parse_qsl(query, keep_blank_values=True)
    keys = {k for k, _ in additions}
    values = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in keys]
    return urlunsplit(parts._replace(query=urlencode(values + additions)))


def gpu_name(info):
    gpu = info.get("gpu") or {}
    return " / ".join(str(x) for x in (gpu.get("adapter"), info.get("webgl2_renderer")) if x) or "unknown adapter"


@contextmanager
def device_url(name, url):
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        yield url
        return
    if name not in ("mac", "mac-m3"):
        raise RuntimeError("loopback recipe needs a Mac tunnel or a device-reachable HTTPS URL")
    tunnel = subprocess.Popen(["ssh", "-S", "none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                               "-o", "ExitOnForwardFailure=yes", "-N", "-R",
                               f"127.0.0.1:0:127.0.0.1:{parts.port or (443 if parts.scheme == 'https' else 80)}", "mac"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(tunnel.stderr, selectors.EVENT_READ)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if selector.select(timeout=1):
                    line = tunnel.stderr.readline()
                    match = re.search(r"Allocated port (\d+)", line)
                    if match:
                        port = int(match[1])
                        if port == 8240:
                            raise RuntimeError("refusing reserved tunnel port")
                        yield urlunsplit(parts._replace(netloc=f"127.0.0.1:{port}"))
                        return
                if tunnel.poll() is not None:
                    break
        raise RuntimeError("Mac reverse tunnel did not become ready")
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()
        tunnel.stderr.close()


def posted_run(payload, doc, info, profile=None, hardware=True):
    if not isinstance(payload, dict) or payload.get("gs_frames") != 1:
        raise RuntimeError("page posted no frame stats")
    phases = payload.get("phases")
    expected = [p["name"] for p in doc["phases"]]
    if not isinstance(phases, list) or any(not isinstance(p, dict) for p in phases) or [p.get("id") for p in phases] != expected:
        raise RuntimeError("page frame phases missing or out of order")
    if not isinstance(payload.get("viewport"), dict) or any(
        not isinstance(payload.get(key), bool) for key in ("webgl", "reduced")
    ):
        raise RuntimeError("page frame stats missing viewport/webgl/reduced")
    for key in ("w", "h", "dpr"):
        positive(payload["viewport"].get(key), f"viewport {key}")
    profile = profile or ("phone" if re.search(r"iPhone|Android|Mobile", info.get("ua", ""), re.I) else "desktop")
    limits = thresholds(doc)
    judged, uncertain = [], []
    for phase in phases:
        for key in ("ms", "count", "fps", "p50", "p95", "worst"):
            positive(phase.get(key), f"{phase['id']} {key}")
        for key in ("over_20", "over_33", "over_50", "longtasks", "longtask_ms"):
            value = phase.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise RuntimeError(f"invalid {phase['id']} {key}")
        if not phase["count"] >= phase["over_20"] >= phase["over_33"] >= phase["over_50"]:
            raise RuntimeError("inconsistent page interval counters")
        lower = 100 * phase["over_33"] / phase["count"]
        upper = 100 * phase["over_20"] / phase["count"]
        findings = []
        if phase["id"] not in ("intro", "load"):
            if hardware:
                limit = limits[f"{profile}_dropped_percent"]
                if lower > limit:
                    findings.append(f"page dropped intervals >= {lower:.2f}% (limit {limit:g}%)")
                elif upper > limit:
                    uncertain.append(f"{phase['id']}: 25 ms drop rate unresolved between {lower:.2f}% and {upper:.2f}%")
                limit = limits[f"{profile}_long_frame_ms"]
                # Worst is rounded to 0.1 ms; over_50 retains its exact cutoff.
                over_limit = (limit <= 50 and phase["over_50"] > 0) or phase["worst"] - .05 > limit
                within_limit = (limit >= 50 and phase["over_50"] == 0) or phase["worst"] + .05 <= limit
                if over_limit:
                    findings.append(f"page frame interval > {limit:g} ms (over_50={phase['over_50']:g}, rounded worst {phase['worst']:g} ms)")
                elif not within_limit:
                    uncertain.append(f"{phase['id']}: {limit:g} ms frame limit unresolved from rounded worst {phase['worst']:g} ms and over_50={phase['over_50']:g}")
            # Host SwiftShader cannot assign GPU gaps to the page. Long tasks
            # still prove main-thread work, although aggregates cannot correlate drops.
            elif phase["longtask_ms"] > limits[f"{profile}_long_frame_ms"]:
                if phase["longtasks"] == 1:
                    findings.append(f"page long task {phase['longtask_ms']:g} ms")
                else:
                    uncertain.append(f"{phase['id']}: aggregate long-task time cannot establish individual stall duration")
        judged.append(dict(phase, name=phase["id"], findings=findings,
                           dropped_percent_bounds=[round(lower, 3), round(upper, 3)]))
    return dict(profile=profile, variant="reduced" if payload["reduced"] else "normal" if payload["webgl"] else "nowebgl",
                phases=judged, posted=payload, device_info=info, renderer=gpu_name(info),
                evidence="page summary; no LoAF scripts, exact 25 ms count or main-thread correlation",
                uncertainties=uncertain, status="impossible" if uncertain else "complete",
                reason="; ".join(uncertain) if uncertain else None)


def device_capture(device, url, doc):
    name = device["name"]
    run = dict(device=name, measured_on=f"g-runner device {name}", phases=[], device_info=device.get("info", {}))
    managed_mac = name in ("mac", "mac-m3")
    mac_start_attempted = False
    try:
        if managed_mac:
            mac_start_attempted = True
            process = subprocess.run([str(MAC_RUNNER), "start", "--device", name], capture_output=True, text=True)
            if process.returncode == MAC_BUSY:
                # refused before launching anything: the running Chrome belongs to another measurement
                mac_start_attempted = False
                raise RuntimeError("Mac runner busy: " + process.stdout.strip())
            if process.returncode:
                raise RuntimeError("Mac runner start impossible: " + process.stdout.strip())
            print(process.stdout.strip(), file=sys.stderr)
        with device_url(name, url) as target:
            job = runner_call("--device", name, "--duration", doc["runner_duration"],
                              "--timeout", doc["runner_timeout"], "--wait", doc["runner_wait"], "--json", target)
        result = job.get("result") or {}
        payload = result.get("posted")
        if not isinstance(payload, dict) or payload.get("gs_frames") != 1:
            raise RuntimeError("page posted no frame stats")
        if job.get("state") != "done" or result.get("ok") is not True:
            raise RuntimeError("device job did not complete successfully")
        info = result.get("device_info") or {}
        adapter = gpu_name(info)
        if not info.get("ua") or adapter == "unknown adapter" or any(s in adapter.lower() for s in ("swiftshader", "llvmpipe", "lavapipe", "software rasterizer")):
            raise RuntimeError("device has no hardware adapter/user agent: " + adapter)
        run.update(posted_run(payload, doc, info), job_id=job["id"], url=target)
    except Exception as error:
        run.update(status="impossible", reason=str(error))
    finally:
        if mac_start_attempted:
            process = subprocess.run([str(MAC_RUNNER), "stop", "--device", name], capture_output=True, text=True)
            if process.returncode:
                run.update(status="impossible", reason="Mac runner cleanup failed")
    return run


@contextmanager
def browser_slot():
    lock_path = Path(os.environ.get("GSPACING_LOCK", "/tmp/g-studio-browser.lock"))
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            subprocess.run(["free", "-m"], stdout=sys.stderr, check=True)
            if free_mb() >= 900:
                break
            print("pacing: waiting 20s for 900 MB available", file=sys.stderr, flush=True)
            time.sleep(20)
        yield


def fallback_capture(url, doc, out, keep_traces=False):
    from playwright.sync_api import sync_playwright
    runs = []
    with sync_playwright() as playwright:
        for profile in doc["profiles"]:
            for variant in doc["variants"]:
                with browser_slot():
                    print(f"pacing: {profile}/{variant}", file=sys.stderr, flush=True)
                    run = capture(playwright, url, doc, profile, variant, out, keep_traces)
                run["measured_on"] = "playwright fallback, no paired device online"
                runs.append(run)
                (out / "findings.json").write_text(json.dumps(dict(runs=runs), indent=2))
    return runs


def report(data):
    print("measured_on: " + data["measured_on"])
    if data.get("device_selection"):
        print("device selection: " + data["device_selection"])
    for run in data["runs"]:
        if run.get("device") or run.get("posted"):
            label = run.get("device") or f"{run.get('profile')}/{run.get('variant')}"
            print(f"{label}: {gpu_name(run.get('device_info', {}))}")
            print("  user agent: " + run.get("device_info", {}).get("ua", "unknown"))
            for phase in run["phases"]:
                print(f"  {phase['name']}: {phase['count']} frames / {phase['ms']:g}ms, {phase['fps']:g} fps, "
                      f"p50/p95/worst={phase['p50']:g}/{phase['p95']:g}/{phase['worst']:g}ms, "
                      f"over20/33/50={phase['over_20']}/{phase['over_33']}/{phase['over_50']}, "
                      f"longtasks={phase['longtasks']} ({phase['longtask_ms']:g}ms)")
                if phase["findings"]:
                    print("    blocking: " + "; ".join(phase["findings"]))
            if run.get("reason"):
                print(f"  {run.get('status')}: {run['reason']}")
    if any(r.get("device") or r.get("posted") for r in data["runs"]):
        print(f"pacing: {len(data['runs'])} runs; status={data['status']}; artefacts: {data['out']}")
        return
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
    parser.add_argument("--device", default="auto", metavar="NAME|auto|none")
    parser.add_argument("--keep-traces", action="store_true")
    args = parser.parse_args(argv)
    out = None
    try:
        repo = Path(args.repo).resolve()
        path = repo / ".glevels/pacing.json"
        doc = recipe_read(path)
        out = Path(args.out or tempfile.mkdtemp(prefix="glevels-pacing-")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        runs = []
        selection = "disabled by --device none" if args.device == "none" else ""
        devices = [] if args.device == "none" else online_devices(runner_call("--status", "--json"), args.device)
        if not devices and args.device != "none":
            selection = "no paired device online" if args.device == "auto" else f"requested device {args.device} offline or absent"
        measured_on = ", ".join("g-runner device " + d["name"] for d in devices) if devices else "playwright fallback, no paired device online"
        print("measured_on: " + measured_on, file=sys.stderr, flush=True)
        with RecipeServer(repo, doc, path) as server:
            server.start()
            url = runner_url(server.url, doc.get("runner_query", "gate=frames")) if devices or doc.get("runner_query") else server.url
            if devices:
                for device in devices:
                    runs.append(device_capture(device, url, doc))
                    (out / "findings.json").write_text(json.dumps(dict(measured_on=measured_on, runs=runs), indent=2))
            else:
                runs = fallback_capture(url, doc, out, args.keep_traces)
        impossible = any(r.get("status") == "impossible" for r in runs)
        blocking = any(p["findings"] for r in runs for p in r["phases"])
        status = "impossible" if impossible else "findings" if blocking else "complete"
        data = dict(runs=runs, webgl_comparison=comparisons([r for r in runs if not r.get("device")]), out=str(out),
                    measured_on=measured_on, device_selection=selection, status=status,
                    display_interval_ms=DISPLAY_MS, thresholds=thresholds(doc))
        if not devices:
            data["playwright"] = importlib.metadata.version("playwright")
        (out / "findings.json").write_text(json.dumps(data, indent=2))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            report(data)
        return 2 if impossible else int(blocking)
    except KeyboardInterrupt:
        print("impossible: pacing interrupted", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"impossible: {error}", file=sys.stderr)
        if out:
            partial = out / "findings.json"
            data = json.loads(partial.read_text()) if partial.exists() else dict(runs=[])
            data.update(status="impossible", reason=str(error), measured_on=locals().get("measured_on", "not measured"))
            partial.write_text(json.dumps(data, indent=2))
            print(f"partial artefacts: {out}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
