"""Sensor: motion rhythm, with evidence for gate correct.motion.rhythm.

    python3 glevels/motion.py [--out DIR] [--json] [--script SCRIPT.mjs]
        [--narrative SELECTOR]... [--tokens tokens.css] [--viewport WxH]
        [--duration MS] [--seed N] TARGET

TARGET uses spacing.resolve: a URL, HTML served with its relative assets, or a
g-site pages/<dir>/content.html compiled and proxied through GSPACING_BASE.
The optional script
exports async (page, mark); await mark('name') before each interaction. Duration
is the observation window after load, each automatic action, and script return;
30+ second windows are supported with frames streamed to disk, not held in RAM.
--seed N replaces Math.random before page scripts, in both fresh contexts.
Normal and reduced-motion runs use the real clock. Declared animations are then
paused and sought separately. findings.json, per-step videos and consecutive,
unscaled frame strips land in --out (default: a printed temporary directory).

Requires Node, g-design's Playwright, ffmpeg, numpy and Pillow. Exit 0 means no
clear faults, 1 means blocking faults, 2 means impossible to run. Reviewer leads
never fail a run; frame pacing on this SwiftShader host is always only a lead.
"""
import argparse
import fcntl
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from .spacing import free_mb, resolve
except ImportError:
    from spacing import free_mb, resolve

HERE = Path(__file__).resolve().parent
TOKENS = Path('/home/gabriel/g-design/packages/g-tokens/dist/tokens.css')
LOCK = Path(tempfile.gettempdir()) / 'glevels-browser.lock'
MIN_FREE_MB = 600  # Chromium needs headroom on this shared host.
FRAME_MS = 1000 / 60  # Measure travel in nominal display frames, independent of host speed.
VISUAL_TRAVEL = .98  # Sub-pixel spring settling is outside the visible duration.
JUMP_LEAD = .35  # A large frame step deserves a human look.
JUMP_BLOCK = .60  # Teleporting most of the travel is a clear fault.
UI_MIN_MS, UI_MAX_MS, SHEET_MAX_MS = 80, 300, 500  # House feedback range and sheet exception.
NARRATIVE_MIN_MS = 400  # Reading and storytelling need more time than feedback.
RAMP_RATIO = .5  # Endpoint speed/energy above half the mean suggests a cliff.
CLOCKWORK_CV = 1  # A memoryless random process has CV 1; natural sequences burst and pause.
READ_MIN_MS = 60  # Faster discrete reveals are difficult to read.
PIXEL_DIFF = 8  # Ignore small JPEG/luma changes rather than calling noise motion.
DECLARED_PAD = 8  # Anti-aliasing and shadows can extend beyond a declared target's box.
TOKEN_TIME_TOLERANCE = .02  # Allow serialization rounding when comparing token durations.
TOKEN_CURVE_TOLERANCE = .015  # Rounded CSS easing lists should still match their source token.
OVERSHOOT_LEAD, WOBBLE_LEAD = .05, .10  # Small spring settling is expected; larger reversals need a look.
ENERGY_FLOOR, ENERGY_ACTIVE = .002, .02  # Require motion above noise and a small fraction of the peak.
EPISODE_GAP_MS = FRAME_MS * 2.5  # A quiet gap separates discrete visible changes.
DECLARED_COVERAGE = .8  # Mostly declared regions belong to the curve analysis.
DROPPED_INTERVAL_RATIO = 1.5  # Long rAF intervals are pacing leads on this renderer.
LAYOUT = re.compile(r'^(width|height|top|left|right|bottom|margin.*|padding.*|inset.*|flex.*|grid.*)$')
MOVEMENT = {'transform', 'translate', 'scale', 'rotate', 'left', 'top', 'right', 'bottom',
            'width', 'height', 'scrollTop', 'scrollLeft', 'offsetDistance'}


def dependencies():
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f'numpy and Pillow are required: {e}') from e
    return np, Image


def parse_tokens(file):
    css = re.sub(r'/\*.*?\*/', '', Path(file).read_text(), flags=re.S)
    values = {}
    for name, value in re.findall(r'(--[\w-]+)\s*:\s*([^;{}]+);', css):
        values.setdefault(name, value)

    def resolve(value, seen=()):
        def replace(m):
            name = m[1]
            return resolve(values[name], (*seen, name)) if name in values and name not in seen else m[0]
        return re.sub(r'var\((--[\w-]+)\)', replace, value).strip()

    durations, easings = {}, {}
    for name, raw in values.items():
        value = resolve(raw)
        m = re.fullmatch(r'([\d.]+)(ms|s)', value)
        if m and ('dur' in name or 'motion' in name):
            durations[name] = float(m[1]) * (1000 if m[2] == 's' else 1)
        if ('ease' in name or 'spring' in name) and re.match(r'cubic-bezier\(|linear\(|^(ease[\w-]*|linear)$', value):
            easings[name] = value
    if not durations or not easings:
        raise ValueError(f'no motion durations/easings in {file}')
    return {'durations': durations, 'easings': easings}


def easing_function(value):
    names = {'ease': (.25, .1, .25, 1), 'ease-in': (.42, 0, 1, 1),
             'ease-out': (0, 0, .58, 1), 'ease-in-out': (.42, 0, .58, 1)}
    value = value.strip()
    if value == 'linear':
        return lambda x: x
    if value in ('step-start', 'step-end'):
        value = 'steps(1, ' + ('start)' if value == 'step-start' else 'end)')
    if value.startswith('steps('):
        args = value[6:-1].split(',')
        count, mode = int(args[0]), (args[1].strip() if len(args) > 1 else 'end')
        mode = {'start': 'jump-start', 'end': 'jump-end'}.get(mode, mode)
        if count < 1 or mode == 'jump-none' and count < 2:
            raise ValueError(f'invalid easing: {value}')

        def steps(x):
            n = math.floor(x * count + 1e-10) + (mode in ('jump-start', 'jump-both'))
            denominator = count + (mode == 'jump-both') - (mode == 'jump-none')
            return min(1, max(0, n / denominator))
        return steps
    if value.startswith('linear('):
        points = []
        for part in value[7:-1].split(','):
            words = part.split()
            y = float(words[0])
            positions = [float(w[:-1]) / 100 for w in words[1:]]
            points.extend([[x, y] for x in positions] or [[None, y]])
        if len(points) < 2:
            raise ValueError(f'invalid easing: {value}')
        points[0][0] = 0 if points[0][0] is None else points[0][0]
        points[-1][0] = 1 if points[-1][0] is None else points[-1][0]
        previous = points[0][0]
        for p in points[1:]:
            if p[0] is not None:
                p[0] = max(previous, p[0])
                previous = p[0]
        i = 0
        while i < len(points) - 1:
            j = i + 1
            while points[j][0] is None:
                j += 1
            for k in range(i + 1, j):
                points[k][0] = points[i][0] + (points[j][0] - points[i][0]) * (k - i) / (j - i)
            i = j

        def linear(x):
            i = 0
            while i < len(points) - 2 and x >= points[i + 1][0]:
                i += 1
            a, b = points[i:i + 2]
            return b[1] if b[0] == a[0] else a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])
        return linear
    if value in names:
        x1, y1, x2, y2 = names[value]
    elif value.startswith('cubic-bezier('):
        x1, y1, x2, y2 = map(float, value[13:-1].split(','))
    else:
        raise ValueError(f'unsupported easing: {value}')

    def bezier(x):
        low, high = 0, 1
        for _ in range(35):
            t = (low + high) / 2
            v = 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t ** 2 * x2 + t ** 3
            if v < x:
                low = t
            else:
                high = t
        return 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3
    return bezier


def visual_duration(duration, easing):
    fn = easing_function(easing)
    return duration * next((i / 4000 for i in range(4001) if fn(i / 4000) >= VISUAL_TRAVEL), 1)


def curve_metrics(times, values):
    np, _ = dependencies()
    data = np.asarray(values, dtype=float)
    if len(data) < 2 or data.ndim != 2:
        return None
    delta = data[-1] - data[0]
    span = np.ptp(data, axis=0)
    moving = span > 1e-5
    if not moving.any():
        return None
    curves = []
    for k in np.flatnonzero(moving):
        travel = abs(delta[k]) if abs(delta[k]) > span[k] * .05 else span[k]
        sign = -1 if delta[k] < 0 else 1
        curves.append((data[:, k] - data[0, k]) * sign / travel)
    curves = np.asarray(curves).T
    steps = np.abs(np.diff(curves, axis=0))
    progress = curves[:, int(np.argmax(span[moving]))]
    speed = np.abs(np.diff(progress)) / np.maximum(np.diff(times), 1e-6)
    mean = 1 / max(times[-1] - times[0], 1e-6)
    reached = np.flatnonzero(progress >= VISUAL_TRAVEL)
    return {'max_frame_step': round(float(steps.max()), 5),
            'start_velocity_ratio': round(float(speed[0] / mean), 4),
            'end_velocity_ratio': round(float(speed[-1] / mean), 4),
            'overshoot': round(float(max(0, curves.max() - 1, -curves.min())), 5),
            'wobble': round(float(np.maximum(0, -np.diff(progress)).sum()), 5),
            'visual_ms': round(float(times[reached[0]] - times[0]) if len(reached) else times[-1] - times[0], 2),
            'progress': [round(float(p), 5) for p in progress]}


def finding(check, detail, selector='(page)', metrics=None, severity='review', **extra):
    return dict(check=check, severity=severity, selector=selector, detail=detail, metrics=metrics or {}, **extra)


def animation_findings(a, tokens, reduced=False, entrance=False):
    out = []
    props = a['properties']
    movement = a.get('movementObserved', bool(MOVEMENT.intersection(props)))
    duration = a['duration']
    metrics = a.get('metrics') or {}
    visual = metrics.get('visual_ms', a.get('visual_ms', duration))
    sheet_token = any('sheet' in name and abs(duration - d) <= max(2, d * TOKEN_TIME_TOLERANCE)
                      for name, d in tokens['durations'].items())  # Declared with the sheet's own token.
    ceiling = SHEET_MAX_MS if a.get('sheet') or sheet_token else UI_MAX_MS
    common = dict(selector=a['selector'], metrics={'total_ms': duration, 'visual_ms': visual,
                  'delay_ms': a['delay'], 'easing': a['easing'], **metrics}, rect=a.get('rect'),
                  start=a.get('firstSeen'), end=a.get('lastSeen'))

    def add(check, detail, severity='review'):
        out.append(finding(check, detail, severity=severity, **common))

    if reduced:
        if movement and (duration > 50 or metrics.get('movement_frames', 0) > 1):
            add('reduced-motion', 'movement still runs with prefers-reduced-motion: reduce')
        return out
    if a['iterations'] == 'infinite':
        if not a.get('indicator'):
            add('looping', 'infinite motion on non-indicator content')
        return out
    if not a.get('narrative'):
        if visual > ceiling:
            # An entrance on load answers no input, so the feedback ceiling is a lead there.
            add('ui-duration', f'UI visual duration {visual:.1f}ms (total {duration:g}ms), ceiling {ceiling}ms'
                + ('; page entrance: judge it as feedback or mark it narrative' if entrance else ''),
                'block' if visual > 2 * ceiling and not entrance else 'review')
        elif visual < UI_MIN_MS and duration > 0:
            add('snappy', f'UI visual duration {visual:.1f}ms (total {duration:g}ms), below {UI_MIN_MS}ms')
        if duration and not any(abs(duration - d) <= max(2, d * TOKEN_TIME_TOLERANCE) for d in tokens['durations'].values()):
            add('off-token-duration', f'total duration {duration:g}ms is outside the motion tokens')
        fn = easing_function(a['easing'])
        if not any(max(abs(fn(i / 20) - easing_function(e)(i / 20)) for i in range(21)) < TOKEN_CURVE_TOLERANCE
                   for e in tokens['easings'].values()):
            add('off-token-easing', f"easing {a['easing']} is outside the motion tokens")
    elif visual < NARRATIVE_MIN_MS:
        add('narrative-fast', f'narrative visual duration {visual:.1f}ms (total {duration:g}ms), below {NARRATIVE_MIN_MS}ms')
    layout = [p for p in props if LAYOUT.match(p)]
    if layout:
        add('animated-layout', 'animated layout properties: ' + ', '.join(layout))
    if movement and (a['easing'] == 'linear' or a['easing'].startswith(('steps(', 'step-'))):
        add('movement-easing', f"movement uses {a['easing']}")
    if metrics:
        jump = metrics['max_frame_step']
        if movement and duration >= 200 and jump > JUMP_LEAD:
            add('jump', f'{jump:.3f} of travel in one nominal 16.7ms frame', 'block' if jump > JUMP_BLOCK else 'review')
        if movement and (metrics['end_velocity_ratio'] > RAMP_RATIO or
                         a.get('narrative') and metrics['start_velocity_ratio'] > RAMP_RATIO):
            ends = 'both ends' if a.get('narrative') else 'the end'
            add('no-ramp', f"movement needs easing at {ends}; start/mean {metrics['start_velocity_ratio']:.2f}, end/mean {metrics['end_velocity_ratio']:.2f}")
        if metrics['overshoot'] > OVERSHOOT_LEAD or metrics['wobble'] > WOBBLE_LEAD:
            add('wobble', f"overshoot {metrics['overshoot']:.3f}, reverse travel {metrics['wobble']:.3f}")
    if a.get('hover') and a.get('hover_fraction', 0) > .03 + 1e-6:
        add('hover-movement', f"hover translates or scales by {a['hover_fraction']:.1%}, above 3%")
    return out


def rhythm_metrics(events):
    groups = {}
    for e in events:
        if e.get('reveal', True) and not e.get('occluded') and e.get('onscreen', True):
            groups.setdefault(e['selector'], []).append(e['time'])
    out = []
    for selector, times in groups.items():
        times = sorted(set(times))
        if len(times) < 2:
            continue
        gaps = [b - a for a, b in zip(times, times[1:])]
        mean = sum(gaps) / len(gaps)
        cv = (sum((v - mean) ** 2 for v in gaps) / len(gaps)) ** .5 / mean if mean else 0
        out.append(dict(selector=selector, events=len(times), intervals_ms=gaps, mean_ms=mean, cv=cv,
                        start=times[0], end=times[-1]))
    return out


def event_findings(events):
    out = []
    for metrics in rhythm_metrics(events):
        selector, start, end = (metrics[k] for k in ('selector', 'start', 'end'))
        if metrics['events'] >= 8 and metrics['cv'] < CLOCKWORK_CV:
            out.append(finding('clockwork-rhythm',
                               f"{metrics['events']} reveals, interval CV {metrics['cv']:.3f}: more even than random: real sequences come in bursts and pauses",
                               selector, metrics, start=start, end=end))
        if metrics['mean_ms'] < READ_MIN_MS:
            out.append(finding('too-fast-to-read', f"events average {metrics['mean_ms']:.1f}ms apart, below {READ_MIN_MS}ms",
                               selector, metrics, start=start, end=end))
    return out


def frame_energy(files, exclusions=None, changed_pixels=None):
    np, Image = dependencies()
    previous = None
    energies, boxes = [], []
    for index, file in enumerate(files):
        with Image.open(file) as im:
            frame = np.asarray(im.convert('L'), dtype=np.int16)
        if previous is None:
            energies.append(0.)
            boxes.append(None)
            if changed_pixels is not None:
                changed_pixels.append(0)
        else:
            diff = np.abs(frame - previous)
            for box in exclusions[index] if exclusions else []:
                x, y, w, h = box
                diff[max(0, math.floor(y - DECLARED_PAD)):min(diff.shape[0], math.ceil(y + h + DECLARED_PAD)),
                     max(0, math.floor(x - DECLARED_PAD)):min(diff.shape[1], math.ceil(x + w + DECLARED_PAD))] = 0
            ys, xs = np.nonzero(diff > PIXEL_DIFF)
            if changed_pixels is not None:
                changed_pixels.append(len(xs))
            energies.append(float(np.where(diff > PIXEL_DIFF, diff, 0).mean()))
            boxes.append([int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if len(xs) >= 4 else None)
        previous = frame
    return energies, boxes


def union_boxes(boxes):
    boxes = [b for b in boxes if b]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None


def episode_findings(energies, times, boxes):
    if not energies or max(energies) == 0:
        return []
    threshold = max(ENERGY_FLOOR, max(energies) * ENERGY_ACTIVE)
    episodes, active = [], []
    for i, energy in enumerate(energies):
        if energy > threshold and boxes[i]:
            active.append(i)
        elif active:
            episodes.append(active)
            active = []
    if active:
        episodes.append(active)
    out = []
    for episode in episodes:
        first, last = episode[0], episode[-1]
        measured = [energies[i] * (FRAME_MS / max(FRAME_MS, times[i] - times[i - 1]))
                    if n else energies[i] for n, i in enumerate(episode)]
        peak = max(measured)
        mean = sum(measured) / len(measured)
        edge = min(2, len(measured))  # The first/last raster interval can contain only part of a frame's travel.
        start_energy = sum(measured[:edge]) / edge
        stop_energy = sum(measured[-edge:]) / edge
        box = union_boxes([boxes[i] for i in episode])
        metrics = {'energy_peak': peak, 'energy_mean': mean, 'frames': len(episode), 'box': box,
                   'endpoint_frames': edge, 'start_ratio': start_energy / mean, 'stop_ratio': stop_energy / mean,
                   'elapsed_ms': times[last] - times[first]}
        common = dict(metrics=metrics, box=box, start=times[max(0, first - 1)], end=times[last])
        if len(episode) == 1:
            out.append(finding('instant', 'whole visible change in one captured frame', **common))
        elif start_energy / mean > RAMP_RATIO or stop_energy / mean > RAMP_RATIO:
            out.append(finding('no-ramp', f"no ramp: episode starts at {metrics['start_ratio']:.0%} and stops at {metrics['stop_ratio']:.0%} of mean energy", **common))
    return out


def overlaps_declared(f, animations):
    box = f.get('box')
    if not box:
        return False
    for a in animations:
        r = a.get('rect')
        if not r or a.get('firstSeen', 0) > f['end'] + FRAME_MS or a.get('lastSeen', 0) < f['start'] - FRAME_MS:
            continue
        overlap = max(0, min(box[2], r[0] + r[2]) - max(box[0], r[0])) * max(0, min(box[3], r[1] + r[3]) - max(box[1], r[1]))
        if overlap / max(1, (box[2] - box[0]) * (box[3] - box[1])) > DECLARED_COVERAGE:
            return True
    return False


def content_findings(step):
    """A discrete line in a running sequence is not the whole watched region."""
    sequences = {m['selector'] for m in rhythm_metrics(step['events'])}  # Two or more discrete reveals.
    units = {selector: sum(e.get('count', 1) for e in step['events'] if e['selector'] == selector and
                           e.get('reveal', True) and not e.get('occluded'))
             for selector in sequences}  # Visible reveals; a cleared or covered DOM is not part of the sequence.
    out = []
    for e in step['events']:
        if not e.get('instant') or e.get('feedback') or e.get('occluded') or not e.get('onscreen', True):
            continue  # Content covered by an overlay (a loading screen) changes unseen.
        if e['selector'] in sequences and e.get('count', 1) * 2 < units[e['selector']]:
            continue
        out.append(finding('instant', 'content appears, disappears or is replaced in one captured frame',
                           e['selector'], {'content_units': e.get('count', 1)},
                           severity='block' if e.get('narrative') else 'review',
                           rect=e.get('rect'), start=e['time'] - FRAME_MS, end=e['time'] + FRAME_MS))
    return out


def reduced_content_findings(step):
    """Reduced motion swaps movement for a crossfade; a one-frame swap of watched content is a lead."""
    return [dict(f, check='reduced-instant', severity='review',
                 detail='content appears or is replaced in one captured frame under prefers-reduced-motion: reduce; crossfade it')
            for f in content_findings(step)]


def collapse_findings(findings):
    grouped = {}
    for f in findings:
        key = f['check'], f['selector']
        f.setdefault('steps', [f['step']])
        if key not in grouped:
            grouped[key] = f
            continue
        first = grouped[key]
        for name in f['steps']:
            if name not in first['steps']:
                first['steps'].append(name)
        first.setdefault('evidence', []).append({k: f[k] for k in ('step', 'video', 'strips', 'metrics', 'reduced') if k in f})
        first['occurrences'] = first.get('occurrences', 1) + f.get('occurrences', 1)
        if f['severity'] == 'block' and first['severity'] != 'block':
            first['evidence'].append({k: first[k] for k in ('step', 'video', 'strips', 'metrics')})
            for field in ('severity', 'detail', 'metrics', 'step', 'video', 'strips', 'crop_box', 'frame_range',
                          'rect', 'box', 'start', 'end'):
                if field in f:
                    first[field] = f[field]
    return list(grouped.values())


def region_findings(episodes, step, viewport_area):
    out = []
    for f in episodes:
        if f['check'] != 'instant':
            out.append(f)
            continue
        box = f['box']
        if step.get('changed_pixels'):
            counts = [count for frame, count in zip(step['frames'], step['changed_pixels'])
                      if f['start'] < frame['time'] <= f['end']]
            if max(counts, default=0) < viewport_area * .02:
                continue
        if (box[2] - box[0]) * (box[3] - box[1]) < viewport_area * .02:
            continue
        if step['name'] == 'load' and f['start'] <= max(step['frames'][0]['time'], step.get('loadEnd') or 0,
                                                     step.get('firstPaint') or 0) + FRAME_MS:
            continue  # First paint: the page rendering itself up to its load event.
        if any(f['start'] - FRAME_MS <= e['time'] <= f['end'] + FRAME_MS for e in step['events']):
            continue  # Content analysis knows whether these are individual lines or a bulk reveal.
        for region in step.get('narrativeRegions', []):
            x, y, w, h = region['rect']
            if x <= box[0] and y <= box[1] and x + w >= box[2] and y + h >= box[3]:
                if any(m['selector'] == region['selector'] and m['events'] >= 8 for m in rhythm_metrics(step['events'])):
                    f = None  # Semantic events distinguish a burst from bulk completion in this sequence.
                    break
                f.update(selector=region['selector'], severity='block')
                break
        if f:
            out.append(f)
    return out


def frame_strips(f, step, out_dir, number):
    _, Image = dependencies()
    frames = step['frames']
    if not frames:
        return []
    start, end = f.get('start') or step['start'], f.get('end') or step['end']
    indices = [i for i, x in enumerate(frames) if start - FRAME_MS <= x['time'] <= end + FRAME_MS]
    if not indices:
        indices = [min(range(len(frames)), key=lambda i: abs(frames[i]['time'] - start))]
    lo, hi = max(0, min(indices) - 1), min(len(frames), max(indices) + 2)
    hi = min(hi, lo + 8)  # A consecutive excerpt; the full sequence remains in the video.
    box = f.get('box')
    if not box and f.get('rect'):
        x, y, w, h = f['rect']
        box = [math.floor(x), math.floor(y), math.ceil(x + w), math.ceil(y + h)]
    with Image.open(frames[lo]['file']) as im:
        width, height = im.size
    box = box or [0, 0, width, height]
    box = [max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])]
    if box[2] <= box[0] or box[3] <= box[1]:
        box = [0, 0, width, height]
    w, h = box[2] - box[0], box[3] - box[1]
    paths = []
    for first in range(lo, hi, 8):
        stop = min(first + 8, hi)
        strip = Image.new('RGB', (w * (stop - first), h))
        for i in range(first, stop):
            with Image.open(frames[i]['file']) as im:
                strip.paste(im.crop(box), ((i - first) * w, 0))
        path = out_dir / f'lead-{number:04d}-{first:05d}.png'
        strip.save(path)
        paths.append(str(path))
    f['frame_range'] = [lo, hi - 1]
    f['crop_box'] = box
    return paths


def analyse_run(run, tokens, out_dir):
    np, Image = dependencies()
    findings = []
    for step in run['steps']:
        if step['name'] == 'load':
            while len(step['frames']) > 1:
                with Image.open(step['frames'][0]['file']) as im:
                    low, high = im.convert('L').getextrema()
                if low < 254:
                    break
                step['frames'].pop(0)
        animations = step['animations']
        leads = []
        for a in animations:
            if a.get('sampleError'):
                leads.append(finding('unmeasured-animation', a['sampleError'], a['selector'], rect=a.get('rect'),
                                     start=a.get('firstSeen'), end=a.get('lastSeen')))
            if a.get('samples'):
                a['metrics'] = curve_metrics(a['sampleTimes'], a.pop('samples'))
            if a.get('movementSamples'):
                measured = curve_metrics(a['sampleTimes'], a.pop('movementSamples'))
                if measured:
                    movement_frames = sum(abs(b - c) > 1e-5 for b, c in zip(measured['progress'], measured['progress'][1:]))
                    a['metrics'] = {**(a.get('metrics') or {}), **measured, 'movement_frames': movement_frames}
            a['easing_visual_ms'] = round(visual_duration(a['duration'], a['easing']), 2)
            a['visual_ms'] = (a.get('metrics') or {}).get('visual_ms', a['easing_visual_ms'])
            leads.extend(animation_findings(a, tokens, run['reduced'], entrance=step['name'] == 'load'))
        files = [f['file'] for f in step['frames']]
        times = [f['time'] for f in step['frames']]
        exclusions = [[a['rect'] for a in animations if a.get('rect') and
                       a['firstSeen'] - FRAME_MS <= t <= a['lastSeen'] + FRAME_MS] for t in times]
        for t, excluded in zip(times, exclusions):
            excluded.extend(i['rect'] for i in step.get('interactions', []) if i.get('feedbackBox') and
                            0 <= t - i['time'] <= 1000)
        changed_pixels = []
        energies, boxes = frame_energy(files, exclusions, changed_pixels)
        step['changed_pixels'] = changed_pixels
        step['energy'] = energies
        step['moving_boxes'] = boxes
        if not run['reduced']:
            for hover in step.get('hovers', []):
                if hover['fraction'] > .03 + 1e-6:
                    leads.append(finding('hover-movement', f"hover translates or scales by {hover['fraction']:.1%}, above 3%",
                                         hover['selector'], {'hover_fraction': hover['fraction']}, rect=hover['rect'],
                                         start=hover['time'], end=hover['end']))
            with Image.open(files[0]) as im:
                viewport_area = im.width * im.height
            episodes = region_findings([f for f in episode_findings(energies, times, boxes)
                                        if not overlaps_declared(f, animations)], step, viewport_area)
            leads.extend(episodes)
            leads.extend(content_findings(step))
            step['rhythm'] = rhythm_metrics(step['events'])
            events = event_findings(step['events'])
            leads.extend(events)
            intervals = np.diff(step['raf'])
            if len(intervals):
                median = float(np.median(intervals))
                dropped = int(np.count_nonzero(intervals > median * DROPPED_INTERVAL_RATIO))
                step['pacing'] = {'median_ms': median, 'dropped': dropped, 'longest_ms': float(intervals.max())}
                if dropped:
                    leads.append(finding('frame-pacing', f'{dropped} rAF intervals exceed 1.5 × median {median:.1f}ms; SwiftShader headless pacing is a lead, never blocking', metrics=step['pacing']))
            if step['loaf']:
                longest = max(step['loaf'], key=lambda x: x['duration'])
                leads.append(finding('long-animation-frame', f"longest LoAF {longest['duration']:.1f}ms; SwiftShader headless pacing is a lead, never blocking", metrics=longest))
        else:
            leads.extend(reduced_content_findings(step))
            for selector in step.get('smoothScroll') or []:
                leads.append(finding('reduced-scroll', 'scroll-behavior: smooth remains with prefers-reduced-motion: reduce', selector))
        grouped = {}
        for f in leads:
            key = f['check'], f['selector'], f['severity']
            if key in grouped:
                first = grouped[key]
                first.setdefault('related_metrics', []).append(f['metrics'])
                first['occurrences'] = first.get('occurrences', 1) + 1
                rects = [x for x in (first.get('rect'), f.get('rect')) if x]
                if rects:
                    box = union_boxes([[x, y, x + w, y + h] for x, y, w, h in rects])
                    first['rect'] = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
                continue
            grouped[key] = f
        for f in grouped.values():
            f.update(step=step['name'], reduced=run['reduced'], video=step['video'])
            if not f.get('rect') and not f.get('box'):
                f['box'] = union_boxes(boxes)
            f['strips'] = frame_strips(f, step, out_dir, len(findings) + run.get('finding_offset', 0))
            findings.append(f)
    return collapse_findings(findings)


def report(result, out_dir):
    block = [f for f in result['findings'] if f['severity'] == 'block']
    review = [f for f in result['findings'] if f['severity'] != 'block']
    print(f'motion: {len(block)} blocking, {len(review)} for the reviewer')
    for title, items in [('blocking (clear faults, fix these)', block), ('for the reviewer (leads, judge against the video and frame strips)', review)]:
        print(f'\n{title}:')
        if not items:
            print('  none')
        for f in items:
            mode = ' [reduced]' if f['reduced'] else ''
            print(f"  {', '.join(f.get('steps', [f['step']]))}{mode} {f['selector']} {f['check']}: {f['detail']}")
    print('\npacing: SwiftShader headless; timing findings are leads, never blocking')
    print(f'artefacts: {out_dir}\nfindings: {out_dir / "findings.json"}')
    return 1 if block else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('target')
    ap.add_argument('--out')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--script')
    ap.add_argument('--narrative', action='append', default=[])
    ap.add_argument('--tokens', default=str(TOKENS))
    ap.add_argument('--viewport', default='1280x800')
    ap.add_argument('--duration', type=float, default=1000)
    ap.add_argument('--seed', type=int)
    a = ap.parse_args()
    servers = []
    out_dir = None
    try:
        dependencies()
        if not shutil.which('node') or not shutil.which('ffmpeg'):
            raise RuntimeError('node and ffmpeg are required')
        if not re.fullmatch(r'[1-9]\d*x[1-9]\d*', a.viewport) or not math.isfinite(a.duration) or a.duration <= 0:
            raise ValueError('viewport must be WxH and duration must be positive milliseconds')
        tokens = parse_tokens(a.tokens)
        script = Path(a.script).resolve() if a.script else None
        if script and not script.is_file():
            raise ValueError(f'no such interaction script: {script}')
        out_dir = Path(a.out or tempfile.mkdtemp(prefix='glevels-motion-')).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            urls, servers = resolve([a.target], out_dir)
        except SystemExit as e:
            raise ValueError(f'could not resolve target: {a.target}') from e
        target = urls[0]
        with open(LOCK, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mb = free_mb()
            if mb < MIN_FREE_MB:
                raise RuntimeError(f'only {mb} MB available, a browser needs {MIN_FREE_MB}')
            config = dict(target=target, out=str(out_dir), script=str(script) if script else None,
                          narrative=a.narrative, viewport=dict(zip(('width', 'height'), map(int, a.viewport.split('x')))),
                          duration=a.duration, seed=a.seed)
            config_path = out_dir / 'config.json'
            config_path.write_text(json.dumps(config))
            p = subprocess.run(['node', str(HERE / 'motion.mjs'), str(config_path)], capture_output=True, text=True,
                               timeout=max(600, a.duration / 1000 * 20 + 120))
            if p.returncode:
                raise RuntimeError((p.stderr or p.stdout).strip()[-1200:])
        result = json.loads((out_dir / 'capture.json').read_text())
        result.update(target=a.target, tokens=tokens, findings=[], pacing_note='SwiftShader headless: pacing is a lead, never blocking')
        for run in result['runs']:
            run['finding_offset'] = len(result['findings'])
            result['findings'].extend(analyse_run(run, tokens, out_dir))
        result['findings'] = collapse_findings(result['findings'])
        (out_dir / 'findings.json').write_text(json.dumps(result, indent=2))
        code = 1 if any(f['severity'] == 'block' for f in result['findings']) else 0
        if a.json:
            print(json.dumps(result, indent=2))
        else:
            report(result, out_dir)
        return code
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        print(f'impossible: {e}')
        if out_dir:
            try:
                (out_dir / 'findings.json').write_text(json.dumps({'target': a.target, 'error': str(e), 'findings': []}, indent=2))
                print(f'artefacts: {out_dir}')
            except OSError:
                pass
        return 2
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    sys.exit(main())
