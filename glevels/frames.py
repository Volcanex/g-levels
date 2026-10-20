"""Sensor: candidate frame glitches for correct.render.frame-integrity.

    python3 glevels/frames.py [--out DIR] [--json] [--stills] [--baseline DIR]
        [--tiles] [--every N] [--path SCRIPT.mjs --frames N --fps F
        --viewport WxH] SOURCE

SOURCE is a PNG/JPG directory, video, URL or local HTML file. frames.json can
name ordered sequences and static/moving cameras; otherwise subdirectories are
separate sequences. manifest.json poses[].file takes precedence and selects only
those files, in order. Fallback scans ignore contact*, diff-* and *.judge.*.
--stills disables all temporal checks. Browser captures need
--path, whose default export is async (page, i, n). The fake clock drives timers
and animation frames, unless window.__glevelsFrame(i) supplies the engine step.

findings.json and judge/ PNGs land in --out (default: a printed temp directory).
Each finding includes frame-pixel boxes, 1:1 crops and 3x nearest-neighbour zooms.
Temporal crops join a,b,c at native resolution. --tiles adds native quadrants.
Moving cameras use local integer block matches at quarter, half and full resolution.
Fine matching samples alternate pixels; predictions and evidence stay native.
Only matching pyramids are downsampled; evidence remains native. Persistent
unexplained residuals are reviewer leads, including texture swim and animation.
Shimmer counts strong compensated reversals, not monotonic change.
Exact-black components (max channel <= 3) are ranked by
area, thinness and bright adjacency. Baselines match relative filenames, allowing
a two-pixel mask shift; new exact-black area ranks above other reviewer leads.
Transient pops track short departures and returns, including one-sided startup
recoveries with restored detail. These are review leads.
Prints at most 25 findings, interleaving lead kinds by score; findings.json retains
all findings with numeric ranks.

Recipe mode: --repo DIR reads DIR/.glevels/frames.json; --recipe FILE overrides
the recipe location. DIR defaults to the current directory for --recipe alone.
All recipe paths and shell commands resolve against DIR. Example:
    {"captures": [{"name": "tour", "run": "./capture shots/{run}",
                   "frames": "shots/{run}", "stills": true,
                   "baseline": "shots/reference"}],
     "meshes": [{"run": "./export-mesh", "files": ["build/scene.glb"]}]}
Each execution supplies a fresh {run} id in commands and paths. A capture can
instead use url, path (camera script), frames_n, fps and viewport ("1280x720")
for browser capture. Captures write --out/NAME; meshes write --out/mesh-NNN and
run the adjacent meshcheck.py in a subprocess. Optional run commands execute
before analysis: exit 2 means impossible, other nonzero exits block. Overall
exit 2 is reserved for every part being impossible; partial impossibility is 1.

Exit 0: no clear faults. Exit 1: blocking faults. Exit 2: impossible to run.
Reviewer leads alone never fail a run; a separate judge must inspect the crops.
"""
import argparse
from collections import Counter, deque
import fcntl
from functools import lru_cache, partial
import http.server
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote
import uuid

HERE = Path(__file__).resolve().parent
LOCK = Path("/tmp/glevels-browser.lock")
MIN_FREE_MB = 600  # Browser launches need at least this much available host memory.
TILE = 24  # Temporal evidence is averaged in native-pixel tiles.
SPIKE_LEAD = 14  # Minimum luma disagreement unique to the middle frame.
POP_MAX = 8  # Short departures that return are reviewer leads, never blocking.
POP_LEAD = 14  # Compensated block disagreement before checking for a return.
SPIKE_BLOCK = 48  # Strong spikes also need several hot tiles to block.
SPIKE_AREA = TILE * TILE * 4  # Small isolated spikes remain reviewer leads.
FLIP_FLOOR = 5  # Ignore tiny derivative changes in a static camera sequence.
MOVING_FLIP_FLOOR = 18  # Camera motion needs stronger reversals to suggest shimmer.
FLIP_RATE = 0.30  # Repeated reversals must occupy a substantial part of a tile.
LIGHT_STEP = 18  # Minimum abrupt mean-luma change before comparing with typical steps.
LIGHT_RATIO = 6  # Lighting steps must greatly exceed the sequence median step.
MOTION_SEARCH = 16  # Quarter-resolution radius: at least 64 native pixels per axis.
UNEXPLAINED_PERSISTENCE = 5  # Consecutive compensated triplets; a fuzzy reviewer lead.
UNEXPLAINED_RESIDUAL = 14  # Mean absolute luma left after the best rigid tile match.
BLACK = 3  # Unlit shader branches are exact black, allowing tiny channel noise.
BASELINE_RADIUS = 2  # A one- or two-pixel shift does not establish new black area.
BLANK_RANGE = 2  # A nearly single-colour image cannot establish rendered detail.
BLANK_BLACK = 3  # Tiny compression noise does not make an all-black frame usable.
MAGENTA_HIGH = 210  # Missing-texture candidates have strong red and blue channels.
MAGENTA_GREEN = 70  # Missing-texture candidates have little green.
BRIGHT = 140  # Hollow dark bands need a neighbouring bright or sky-like region.
BAND_FILL = 0.65  # Bright-adjacent dark components must leave a substantial hollow.
MIN_CLUSTER = 24  # Ignore isolated missing-texture or exact-black specks.
RIM_THINNESS = 0.18  # Perimeter/area marks thin components; bright hollow bands also qualify.
MARGIN = 16  # Native-pixel context around every evidence box.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
LEAD_KINDS = ("new-black-area", "unlit-black", "single-frame-spike", "transient-pop", "temporal-shimmer", "lighting-jump", "unexplained-motion")
MAX_PRINTED = 25
RUN_ERRORS = (ImportError, OSError, ValueError, KeyError, TypeError, MemoryError, subprocess.SubprocessError)


def dependencies():
    global np, Image
    import numpy as np
    from PIL import Image


@lru_cache(maxsize=6)
def native_frame(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def read_frame(path):
    rgb = np.array(native_frame(path))
    return rgb, rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def frame_files(directory):
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                  and not p.name.lower().startswith(("contact", "diff-")) and ".judge." not in p.name.lower())


def sequences(directory):
    directory = directory.resolve()
    manifest = directory / "frames.json"
    tour = directory / "manifest.json"
    poses = json.loads(tour.read_text()).get("poses") if tour.is_file() else None
    if isinstance(poses, list):
        rows = [{"name": directory.name, "frames": [pose["file"] for pose in poses]}]
    elif manifest.is_file():
        spec = json.loads(manifest.read_text())
        if "sequences" not in spec and isinstance(spec.get("frames"), list):
            spec = {"sequences": [{"name": directory.name, **spec}]}
        if "sequences" not in spec:
            raise ValueError('frames.json needs "sequences" (or one sequence\'s "frames" list and "camera")')
        rows = spec["sequences"]
    else:
        rows = None
    if rows is not None:
        if not isinstance(rows, list):
            raise ValueError("frames.json sequences must be a list")
        result = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("frames"), list):
                raise ValueError("frames.json needs named sequences with frame lists")
            camera = row.get("camera", "static")
            if camera not in ("static", "moving"):
                raise ValueError(f"unknown camera: {camera}")
            paths = []
            for name in row["frames"]:
                path = (directory / name).resolve()
                if directory not in path.parents or not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    raise ValueError(f"invalid frame path: {name}")
                paths.append(path)
            result.append({"name": row["name"], "camera": camera, "frames": paths})
    else:
        result = []
        direct = frame_files(directory)
        if direct:
            result.append({"name": directory.name, "camera": "static", "frames": direct})
        for child in sorted(directory.iterdir()):
            if child.is_dir() and frame_files(child):
                result.extend(sequences(child))
    if not result or any(not row["frames"] for row in result):
        raise ValueError(f"no frames in sequence: {directory}")
    return result


def components(mask, adjacent=None):
    """Four-connected components, using row runs rather than a Python pixel queue."""
    parents, runs, previous = [], [], []

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for y, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.pad(row.astype(np.int8), (1, 1))))
        current, j = [], 0
        for x0, x1 in zip(edges[::2], edges[1::2]):
            x0, x1 = int(x0), int(x1)
            i = len(parents)
            parents.append(i)
            perimeter = 2 * (x1 - x0) + 2
            while j < len(previous) and previous[j][1] <= x0:
                j += 1
            k = j
            while k < len(previous) and previous[k][0] < x1:
                left, right, other = previous[k]
                parents[root(i)] = root(other)
                perimeter -= 2 * (min(right, x1) - max(left, x0))
                k += 1
            near = int(adjacent[y, x0:x1].sum()) if adjacent is not None else 0
            runs.append((i, x0, x1, y, perimeter, near))
            current.append((x0, x1, i))
        previous = current
    groups = {}
    for i, x0, x1, y, perimeter, near in runs:
        key = root(i)
        if key not in groups:
            groups[key] = [x0, y, x1, y + 1, 0, 0, 0]
        g = groups[key]
        g[0], g[1] = min(g[0], x0), min(g[1], y)
        g[2], g[3] = max(g[2], x1), max(g[3], y + 1)
        g[4] += x1 - x0
        g[5] += perimeter
        g[6] += near
    return [{"box": [x, y, right - x, bottom - y], "area": area,
             "perimeter": perimeter, "near": near}
            for x, y, right, bottom, area, perimeter, near in groups.values()]


def tile_means(values):
    h, w = values.shape
    totals = np.add.reduceat(np.add.reduceat(values, np.arange(0, h, TILE), axis=0),
                             np.arange(0, w, TILE), axis=1)
    sizes = np.minimum(TILE, h - np.arange(0, h, TILE))[:, None] * np.minimum(TILE, w - np.arange(0, w, TILE))
    return totals / sizes


def hot_boxes(values, threshold, shape):
    h, w = shape
    for group in components(values > threshold):
        tx, ty, tw, th = group["box"]
        x, y = tx * TILE, ty * TILE
        box = [x, y, min(w, x + tw * TILE) - x, min(h, y + th * TILE) - y]
        region = values[ty:ty + th, tx:tx + tw]
        sizes = (np.minimum(TILE, h - np.arange(ty, ty + th) * TILE)[:, None] *
                 np.minimum(TILE, w - np.arange(tx, tx + tw) * TILE))
        yield box, float(region[region > threshold].mean()), int(sizes[region > threshold].sum())


def black_mask(rgb):
    return rgb.max(axis=2) <= BLACK


def dilate(mask, radius):
    h, w = mask.shape
    padded = np.pad(mask, radius)
    result = np.zeros_like(mask)
    for y in range(2 * radius + 1):
        for x in range(2 * radius + 1):
            result |= padded[y:y + h, x:x + w]
    return result


def motion_pyramid(image):
    levels = [image]
    for _ in range(2):
        padded = np.pad(levels[-1], ((0, levels[-1].shape[0] % 2),
                                    (0, levels[-1].shape[1] % 2)), mode="edge")
        levels.append((padded[::2, ::2] + padded[1::2, ::2] +
                       padded[::2, 1::2] + padded[1::2, 1::2]) * 0.25)
    return levels


def motion_translation(previous, current):
    cross = np.fft.rfft2(previous - previous.mean()) * np.conj(np.fft.rfft2(current - current.mean()))
    cross /= np.maximum(np.abs(cross), np.finfo(np.float32).eps)
    correlation = np.fft.irfft2(cross, s=current.shape)
    dy, dx = np.unravel_index(correlation.argmax(), correlation.shape)
    h, w = current.shape
    shift = np.array([dy if dy <= h // 2 else dy - h, dx if dx <= w // 2 else dx - w])
    return shift if np.abs(shift).max() <= MOTION_SEARCH * 4 else np.zeros(2, dtype=np.int32)


def motion_match(previous, current, tiles=None, tile_size=TILE):
    """Match each current tile to a rigid patch, retaining signed lighting steps."""
    h, w = current.shape
    rows, cols = (h + tile_size - 1) // tile_size, (w + tile_size - 1) // tile_size
    ty, tx = np.indices((rows, cols)).reshape(2, -1)
    if tiles is not None:
        selected = np.flatnonzero(tiles)
        ty, tx = ty[selected], tx[selected]
    shifts = np.zeros((len(ty), 2), dtype=np.int32)
    translation = motion_translation(previous, current)
    sources, targets = motion_pyramid(previous), motion_pyramid(current)
    for level in (2, 1, 0):
        source, target = sources[level], targets[level]
        height, width = target.shape
        size = tile_size // (2 ** level)
        radius = MOTION_SEARCH if level == 2 else 2
        offsets = np.array(sorted(((dy, dx) for dy in range(-radius, radius + 1)
                                  for dx in range(-radius, radius + 1)),
                                 key=lambda p: p[0] ** 2 + p[1] ** 2), dtype=np.int32)
        if level != 2:
            shifts *= 2
        pixels = np.arange(0, size, 1 if level == 2 else 2)
        for start in range(0, len(ty), 32):
            stop = min(start + 32, len(ty))
            y, x = ty[start:stop] * size, tx[start:stop] * size
            candidates = shifts[start:stop, None, :] + offsets[None, :, :]
            if level != 2:
                # Coarse sampling can alias fine detail; retain the small-motion search too.
                local = offsets
                if tile_size < TILE and level == 0:
                    local = np.array([(dy, dx) for dy in range(-8, 9) for dx in range(-8, 9)], dtype=np.int32)
                rigid = local + np.rint(translation / (2 ** level)).astype(np.int32)
                candidates = np.concatenate((np.broadcast_to(rigid, (stop - start, len(rigid), 2)),
                                             np.broadcast_to(offsets, candidates.shape), candidates), axis=1)
            sy, sx = y[:, None] + candidates[:, :, 0], x[:, None] + candidates[:, :, 1]
            th, tw = np.minimum(size, height - y), np.minimum(size, width - x)
            mask = (pixels[None, :, None] < th[:, None, None]) & (pixels[None, None, :] < tw[:, None, None])
            sample_y = sy[:, :, None, None] + pixels[None, None, :, None]
            sample_x = sx[:, :, None, None] + pixels[None, None, None, :]
            overlap = mask[:, None] & (sample_y >= 0) & (sample_y < height) & (sample_x >= 0) & (sample_x < width)
            reference = source[np.clip(sy[:, :, None, None] + pixels[None, None, :, None], 0, height - 1),
                               np.clip(sx[:, :, None, None] + pixels[None, None, None, :], 0, width - 1)]
            patch = target[np.minimum(y[:, None, None] + pixels[None, :, None], height - 1),
                           np.minimum(x[:, None, None] + pixels[None, None, :], width - 1)]
            delta = (patch[:, None] - reference) * overlap
            area = np.maximum(overlap.sum(axis=(2, 3)), 1).astype(np.float32)
            mean = delta.sum(axis=(2, 3)) / area
            cost = (np.abs(delta - mean[:, :, None, None]) * overlap).sum(axis=(2, 3)) / area
            cost += np.abs(mean)
            sampled_area = mask.sum(axis=(1, 2))
            cost[area < sampled_area[:, None] * 0.5] = np.inf
            shifts[start:stop] = candidates[np.arange(stop - start), cost.argmin(axis=1)]
    field = np.zeros((rows, cols, 2), dtype=np.int32)
    field[ty, tx] = shifts
    field = field.repeat(tile_size, axis=0).repeat(tile_size, axis=1)[:h, :w]
    y, x = np.indices((h, w))
    sy, sx = y + field[:, :, 0], x + field[:, :, 1]
    valid = (sy >= 0) & (sy < h) & (sx >= 0) & (sx < w)
    return np.where(valid, previous[np.clip(sy, 0, h - 1), np.clip(sx, 0, w - 1)], current)


def lighting_difference(previous, current, moving):
    return current - (motion_match(previous, current) if moving else previous)


def unexplained_motion(residuals, shape):
    history = np.stack(residuals)
    runs = np.zeros(history.shape, dtype=np.int32)
    for i, residual in enumerate(history):
        runs[i] = np.where(residual > UNEXPLAINED_RESIDUAL, (runs[i - 1] if i else 0) + 1, 0)
    remaining = runs >= UNEXPLAINED_PERSISTENCE
    times, rows, cols = remaining.shape
    h, w = shape
    for seed in zip(*np.nonzero(remaining)):
        if not remaining[seed]:
            continue
        pending, region = [seed], {}
        remaining[seed] = False
        while pending:
            t, y, x = pending.pop()
            region.setdefault(t, []).append((y, x))
            for nt, ny, nx in ((t - 1, y, x), (t + 1, y, x), (t, y - 1, x),
                               (t, y + 1, x), (t, y, x - 1), (t, y, x + 1)):
                if 0 <= nt < times and 0 <= ny < rows and 0 <= nx < cols and remaining[nt, ny, nx]:
                    remaining[nt, ny, nx] = False
                    pending.append((nt, ny, nx))
        best = None
        for t, positions in region.items():
            ys, xs = np.array(positions).T
            sizes = np.minimum(TILE, h - ys * TILE) * np.minimum(TILE, w - xs * TILE)
            area = int(sizes.sum())
            duration = int(runs[t, ys, xs].max())
            score = duration * area
            if best is None or score > best[0]:
                left, top = int(xs.min() * TILE), int(ys.min() * TILE)
                box = [left, top, min(w, int(xs.max() + 1) * TILE) - left,
                       min(h, int(ys.max() + 1) * TILE) - top]
                residual = float(np.average(history[t, ys, xs], weights=sizes))
                best = score, t + 1, box, duration, area, residual
        score, index, box, duration, area, residual = best
        yield index, ("unexplained-motion", "review", box, float(score),
            f"compensated residual {residual:.1f} luma, {duration} consecutive frames, {area} pixels; "
            "inspect texture swim, shimmer or animation")


def pop_match(previous, current, mask):
    subtiles = mask.repeat(2, axis=0).repeat(2, axis=1)
    rows, cols = ((size + TILE // 2 - 1) // (TILE // 2) for size in current.shape)
    return motion_match(previous, current, tiles=subtiles[:rows, :cols], tile_size=TILE // 2)


def transient_pops(active, current, index, moving, boundary):
    """Keep pre-run anchors for at most eight frames; only emit an abrupt return."""
    for position in range(len(active) - 1, -1, -1):
        anchor = active[position]
        duration = index - anchor["start"]
        if duration > POP_MAX:
            del active[position]
            continue
        mask = anchor["mask"] & dilate(boundary > POP_LEAD, 2)
        if not mask.any():
            continue
        reference = anchor["reference"]
        aligned = pop_match(current, reference, mask) if moving else current
        residual = tile_means(np.abs(reference - aligned))
        returned = mask & (residual < anchor["entry"] * 0.6) & (residual <= POP_LEAD)
        returned &= anchor["last"] - residual > anchor["entry"] * 0.4
        strength = np.where(returned, anchor["entry"] - residual, 0)
        for box, score, area in hot_boxes(strength, 0, current.shape):
            yield anchor["start"], index - 1, box, score * math.sqrt(area * duration)
        anchor["mask"][mask] &= (residual[mask] > POP_LEAD) & ~returned[mask]
        anchor["last"][mask] = residual[mask]
        if not anchor["mask"].any():
            del active[position]


def box_overlap(a, b):
    x, y, w, h = a
    bx, by, bw, bh = b
    area = max(0, min(x + w, bx + bw) - max(x, bx)) * max(0, min(y + h, by + bh) - max(y, by))
    return area / min(w * h, bw * bh)


def startup_pops(initial, moving):
    """One-sided recovery needs a gain in detail and two stable following frames."""
    for end in range(1, min(POP_MAX + 1, len(initial) - 2)):
        before, after = initial[end - 1], initial[end]
        aligned = pop_match(before, after, np.ones(tile_means(after).shape, dtype=bool)) if moving else before
        departure = tile_means(np.abs(after - aligned))
        mask = departure > POP_LEAD
        if not mask.any():
            continue
        detail = np.sqrt(np.maximum(0, tile_means(after * after) - tile_means(after) ** 2))
        old_detail = np.sqrt(np.maximum(0, tile_means(before * before) - tile_means(before) ** 2))
        mask &= detail > old_detail * 1.3 + 2
        if not mask.any():
            continue
        for future in initial[end + 1:end + 3]:
            prediction = pop_match(future, after, mask) if moving else future
            mask &= tile_means(np.abs(after - prediction)) < np.minimum(POP_LEAD, departure * 0.5)
        # A one-sided lead needs the same departure at the sequence boundary too.
        prediction = pop_match(initial[0], after, mask) if moving else initial[0]
        mask &= tile_means(np.abs(after - prediction)) > departure * 0.6
        for box, score, area in hot_boxes(np.where(mask, departure, 0), 0, after.shape):
            yield 0, end - 1, box, score * math.sqrt(area * end)


def per_frame(rgb, luma, baseline=None):
    h, w = luma.shape
    findings = []
    if float(np.ptp(rgb, axis=(0, 1)).max()) <= BLANK_RANGE or float(rgb.max()) <= BLANK_BLACK:
        findings.append(("invalid-frame", "block", [0, 0, w, h], 1.0, "blank or single-colour frame"))
    magenta = (rgb[:, :, 0] > MAGENTA_HIGH) & (rgb[:, :, 2] > MAGENTA_HIGH) & (rgb[:, :, 1] < MAGENTA_GREEN)
    for group in components(magenta):
        if group["area"] >= MIN_CLUSTER:
            findings.append(("missing-texture", "block", group["box"], float(group["area"]),
                             f"{group['area']} magenta pixels"))
    dark = black_mask(rgb)
    bright = luma > BRIGHT
    near = np.zeros_like(bright)
    near[1:] |= bright[:-1]
    near[:-1] |= bright[1:]
    near[:, 1:] |= bright[:, :-1]
    near[:, :-1] |= bright[:, 1:]
    for group in components(dark, near):
        x, y, width, height = group["box"]
        area = group["area"]
        thinness = group["perimeter"] / area
        band = group["near"] >= MIN_CLUSTER and area < width * height * BAND_FILL
        if area >= MIN_CLUSTER and max(width, height) >= TILE and (thinness > RIM_THINNESS or band):
            score = area * (1 + min(thinness, 1)) + group["near"]
            findings.append(("unlit-black", "review", group["box"], float(score),
                             f"{area} exact-black pixels, perimeter/area {thinness:.3f}, "
                             f"{group['near']} bright-adjacent; inspect unlit rim"))
    if baseline is not None:
        if baseline.shape != rgb.shape:
            raise ValueError("baseline frame dimensions differ")
        for group in components(dark & ~dilate(black_mask(baseline), BASELINE_RADIUS)):
            if group["area"] >= MIN_CLUSTER:
                findings.append(("new-black-area", "review", group["box"], float(group["area"]),
                                 f"{group['area']} NEW exact-black pixels beyond {BASELINE_RADIUS}px baseline tolerance"))
    return findings


def triplet(paths, i):
    if len(paths) < 3:
        return [paths[max(0, i - 1)], paths[i], paths[min(len(paths) - 1, i + 1)]]
    middle = min(max(i, 1), len(paths) - 2)
    return paths[middle - 1:middle + 2]


def evidence(paths, box, out, stem):
    x, y, w, h = box
    width, height = native_frame(paths[0]).size
    left, top = max(0, x - MARGIN), max(0, y - MARGIN)
    right, bottom = min(width, x + w + MARGIN), min(height, y + h + MARGIN)
    strip = Image.new("RGB", ((right - left) * len(paths), bottom - top))
    zoom = Image.new("RGB", (w * len(paths), h))
    for i, path in enumerate(paths):
        rgb = native_frame(path)
        strip.paste(rgb.crop((left, top, right, bottom)), (i * (right - left), 0))
        zoom.paste(rgb.crop((x, y, x + w, y + h)), (i * w, 0))
    crop_path, zoom_path = out / f"{stem}-1x.png", out / f"{stem}-3x.png"
    strip.save(crop_path)
    zoom.resize((zoom.width * 3, zoom.height * 3), Image.Resampling.NEAREST).save(zoom_path)
    return {"native": str(crop_path), "zoom": str(zoom_path)}, [left, top, right - left, bottom - top]


def analyse(rows, root, out, stills=False, baseline=None, tiles=False, every=1):
    native_frame.cache_clear()
    judge = out / "judge"
    judge.mkdir(parents=True, exist_ok=True)
    findings, tile_paths, notes = [], [], []

    def add(row, paths, finding):
        kind, severity, box, score, detail = finding
        stem = f"{len(findings):05d}-{kind}"
        crops, context = evidence(paths, box, judge, stem)
        item = {"id": stem, "sequence": row["name"], "kind": kind, "severity": severity,
                "frames": [str(p.relative_to(root)) for p in paths], "box": box,
                "score": round(score, 4), "detail": detail, "context_box": context, "crops": crops}
        if baseline is not None:
            references = [baseline / p.relative_to(root) for p in paths]
            if all(p.is_file() for p in references):
                item["baseline_crops"], _ = evidence(references, box, judge, stem + "-baseline")
        findings.append(item)

    for row in rows:
        paths = row["frames"]
        window = deque(maxlen=3)
        previous_diff, flips, steps, shape = None, None, [], None
        global_steps = []
        peak_flips, peak_indices = None, None
        moving = row["camera"] == "moving"
        residuals, pop_candidates, active, initial = [], [], [], []
        for i, path in enumerate(paths):
            rgb, luma = read_frame(path)
            reference = None
            if baseline is not None:
                match = baseline / path.relative_to(root)
                if match.is_file():
                    reference, _ = read_frame(match)
                else:
                    notes.append(f"no baseline for {path.relative_to(root)}")
            for finding in per_frame(rgb, luma, reference):
                add(row, [path], finding)
            if tiles and i % every == 0:
                h, w = luma.shape
                for q, (left, top, right, bottom) in enumerate([
                        (0, 0, (w + 1) // 2, (h + 1) // 2), ((w + 1) // 2, 0, w, (h + 1) // 2),
                        (0, (h + 1) // 2, (w + 1) // 2, h), ((w + 1) // 2, (h + 1) // 2, w, h)]):
                    if right <= left or bottom <= top:
                        continue
                    target = judge / f"tile-{len(tile_paths):06d}-q{q}.png"
                    Image.fromarray(rgb[top:bottom, left:right]).save(target)
                    tile_paths.append({"sequence": row["name"], "frame": str(path.relative_to(root)),
                                       "box": [left, top, right - left, bottom - top], "path": str(target)})
            if stills:
                continue
            if shape is not None and luma.shape != shape:
                raise ValueError(f"frame dimensions change in sequence {row['name']}; use --stills for independent poses")
            shape = luma.shape
            if len(initial) < POP_MAX + 3:
                initial.append(luma)
            if window:
                diff = lighting_difference(window[-1], luma, True) if moving else luma - window[-1]
                lighting = diff
                steps.append(tile_means(lighting))
                global_steps.append(float(lighting.mean()))
                outgoing = motion_match(luma, window[-1]) - window[-1] if moving else diff
                entry = tile_means(np.abs(outgoing))
                pop_candidates.extend(transient_pops(active, luma, i, moving,
                                                     np.maximum(entry, tile_means(np.abs(diff)))))
                if (entry > POP_LEAD).any():
                    if moving:
                        candidate = pop_match(luma, window[-1], entry > POP_LEAD)
                        entry = np.minimum(entry, tile_means(np.abs(window[-1] - candidate)))
                    if (entry > POP_LEAD).any():
                        active.append({"start": i, "reference": window[-1], "entry": entry,
                                       "last": entry.copy(), "mask": entry > POP_LEAD})
                if previous_diff is not None:
                    floor = MOVING_FLIP_FLOOR if moving else FLIP_FLOOR
                    reverse = ((outgoing * previous_diff) < 0) & (np.abs(outgoing) > floor) & (np.abs(previous_diff) > floor)
                    if moving:
                        aligned_a = window[-1] - previous_diff
                        aligned_c = window[-1] + outgoing
                    if flips is None:
                        flips = np.zeros(shape, dtype=np.uint32)
                    flips += reverse
                    local = tile_means(reverse.astype(np.float32))
                    if peak_flips is None:
                        peak_flips = np.full(local.shape, -1, dtype=np.float32)
                        peak_indices = np.ones(local.shape, dtype=np.int32)
                    stronger = local > peak_flips
                    peak_flips[stronger] = local[stronger]
                    peak_indices[stronger] = i - 1
                previous_diff = diff
            window.append(luma)
            if len(window) == 3:
                a, b, c = window
                if moving:
                    a, c = aligned_a, aligned_c
                disagreement = np.minimum(tile_means(np.abs(a - b)), tile_means(np.abs(b - c)))
                agreement = tile_means(np.abs(a - c))
                spike = disagreement - agreement
                if moving:
                    residuals.append(disagreement)
                    spike = np.where(agreement <= SPIKE_LEAD, spike, 0)
                for box, score, area in hot_boxes(spike, SPIKE_LEAD, shape):
                    severity = "block" if score >= SPIKE_BLOCK and area >= SPIKE_AREA else "review"
                    add(row, paths[i - 2:i + 1], ("single-frame-spike", severity, box, score,
                        f"middle frame disagrees with both neighbours by {score:.1f} luma"))
        if stills:
            continue
        pop_candidates.extend(startup_pops(initial, moving))
        accepted = []
        for first, last, box, score in sorted(pop_candidates, key=lambda p: -p[3]):
            if any(first <= end and last >= start and box_overlap(box, other) >= 0.5
                   for start, end, other in accepted):
                continue
            if first == last and any(f["sequence"] == row["name"] and f["kind"] == "single-frame-spike"
                    and f["frames"][1] == str(paths[first].relative_to(root))
                    and box_overlap(box, f["box"]) >= 0.5 for f in findings):
                continue
            accepted.append((first, last, box))
            indices = sorted({max(0, first - 1), first, last, last + 1})
            add(row, [paths[t] for t in indices], ("transient-pop", "review", box, score,
                f"run {paths[first].name}..{paths[last].name} ({last - first + 1} frames); "
                + ("one-sided recovery with restored detail; inspect sequence start" if first == 0 else
                   "departs from the compensated pre-run prediction, then returns")))
            findings[-1]["frame_range"] = [str(paths[t].relative_to(root)) for t in (first, last)]
        if residuals:
            for index, finding in unexplained_motion(residuals, shape):
                add(row, triplet(paths, index), finding)
        if flips is not None:
            rate = tile_means(flips.astype(np.float32) / (len(paths) - 2))
            for box, score, _ in hot_boxes(rate, FLIP_RATE, shape):
                x, y, w, h = box
                ys, xs = slice(y // TILE, (y + h + TILE - 1) // TILE), slice(x // TILE, (x + w + TILE - 1) // TILE)
                peaks = np.where(rate[ys, xs] > FLIP_RATE, peak_flips[ys, xs], -1)
                strongest = np.unravel_index(peaks.argmax(), peaks.shape)
                best_index = int(peak_indices[ys, xs][strongest])
                add(row, triplet(paths, best_index), ("temporal-shimmer", "review", box, score,
                    f"derivative sign-flip rate {score:.3f} over {len(paths)} frames ({row['camera']} camera)"))
        if steps:
            changes = np.abs(np.stack(steps))
            median = np.median(changes, axis=0)
            global_changes = np.abs(global_steps)
            global_median = float(np.median(global_changes))
            for i, change in enumerate(changes):
                if global_changes[i] > max(LIGHT_STEP, global_median * LIGHT_RATIO):
                    h, w = shape
                    add(row, triplet(paths, i + 1), ("lighting-jump", "review", [0, 0, w, h],
                        float(global_changes[i]), f"global mean-luma step {global_changes[i]:.1f}, median {global_median:.1f}"))
                    continue
                hot = np.where(change > np.maximum(LIGHT_STEP, median * LIGHT_RATIO), change, 0)
                for box, score, _ in hot_boxes(hot, LIGHT_STEP, shape):
                    add(row, triplet(paths, i + 1), ("lighting-jump", "review", box, score,
                        f"regional mean-luma step {score:.1f}, over {LIGHT_RATIO}x its sequence median"))
    native_frame.cache_clear()
    return {"findings": rank_findings(findings), "tiles": tile_paths, "notes": notes,
            "sequences": [{"name": r["name"], "camera": r["camera"], "frames": len(r["frames"])} for r in rows]}


def capture(args, out):
    if not args.path or not Path(args.path).is_file():
        raise ValueError("URL/HTML capture needs --path SCRIPT.mjs")
    server = None
    url = args.source
    if not url.startswith(("http://", "https://")):
        source = Path(url).resolve()
        if not source.is_file():
            raise ValueError(f"no such HTML file: {source}")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(source.parent)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/{quote(source.name)}"
    try:
        with open(LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            available = next((int(line.split()[1]) // 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                              if line.startswith("MemAvailable:")), 0)
            if available < MIN_FREE_MB:
                raise ValueError(f"only {available} MB available; browser needs {MIN_FREE_MB} MB")
            process = subprocess.run(["node", str(HERE / "frames.mjs"), "--out", str(out),
                                      "--path", str(Path(args.path).resolve()), "--frames", str(args.frames),
                                      "--fps", str(args.fps), "--viewport", args.viewport, url],
                                     capture_output=True, text=True, timeout=max(600, args.frames * 30))
            if process.returncode:
                raise ValueError(f"browser capture failed: {(process.stderr or process.stdout).strip()[-1000:]}")
    finally:
        if server:
            server.shutdown()
            server.server_close()
    return out / "frames"


def prepare(args, out):
    source = Path(args.source).resolve()
    if args.source.startswith(("http://", "https://")) or source.suffix.lower() in (".html", ".htm"):
        return capture(args, out)
    if source.is_dir():
        if source == out or source in out.parents:
            raise ValueError("--out must be outside the input directory")
        return source
    if source.is_file() and source.suffix.lower() in (".webm", ".mp4", ".mov"):
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg is not installed")
        target = out / "frames"
        target.mkdir(exist_ok=True)
        if any(target.iterdir()):
            raise ValueError(f"capture directory is not empty: {target}")
        process = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vsync", "0", str(target / "%08d.png")],
                                 capture_output=True, text=True, timeout=600)
        if process.returncode:
            raise ValueError(f"ffmpeg failed: {process.stderr.strip()[-600:]}")
        return target
    raise ValueError(f"no frame directory, video, URL or HTML file: {args.source}")


def rank_findings(findings):
    def order(finding):
        kind = finding["kind"]
        return (finding["severity"] != "block", 0 if finding["severity"] == "block" else
                LEAD_KINDS.index(kind) if kind in LEAD_KINDS else len(LEAD_KINDS), -finding["score"])
    findings = sorted(findings, key=order)
    for rank, finding in enumerate(findings, 1):
        finding["rank"] = rank
    return findings


def report(data, out):
    findings = data["findings"]
    block = [f for f in findings if f["severity"] == "block"]
    review = [f for f in findings if f["severity"] == "review"]
    print(f"frames: {len(data['sequences'])} sequences, {len(block)} blocking, {len(review)} for the reviewer")
    counts = Counter(f["kind"] for f in review)
    print("lead counts: " + (", ".join(f"{kind}={counts[kind]}" for kind in LEAD_KINDS if counts[kind]) or "none"))
    groups = {}
    for finding in review:
        groups.setdefault(finding["kind"], []).append(finding)
    queues = [deque(sorted(group, key=lambda f: -f["score"])) for group in groups.values()]
    shown = block[:MAX_PRINTED]
    while queues and len(shown) < MAX_PRINTED:
        queues.sort(key=lambda q: -q[0]["score"])
        for queue in queues:
            if len(shown) < MAX_PRINTED:
                shown.append(queue.popleft())
        queues = [queue for queue in queues if queue]
    for title, rows in [("blocking (clear faults, fix these)", [f for f in shown if f["severity"] == "block"]),
                        ("for the reviewer (leads, inspect full-resolution crops)", [f for f in shown if f["severity"] == "review"])]:
        print(f"\n{title}:")
        if not rows:
            print("  none")
        for f in rows:
            label = f"{f['part']}: " if "part" in f else ""
            if "frames" not in f:
                print(f"  {f['kind']} {label}{f['detail']}")
            else:
                print(f"  {f['kind']} {label}{', '.join(f['frames'])} at {f['box']} score={f['score']}: {f['detail']}")
    if len(findings) > MAX_PRINTED:
        print(f"  {len(findings) - MAX_PRINTED} more findings in findings.json (all ranked)")
    for note in data["notes"]:
        print(f"  note: {note}")
    crop_dirs = [Path(p["out"]) / "judge" for p in data["parts"] if p["kind"] == "capture"] if "parts" in data else [out / "judge"]
    print(f"\njudge crops: {', '.join(map(str, crop_dirs))}\nfindings: {out / 'findings.json'}")


def write_findings(data, out):
    (out / "findings.json").write_text(json.dumps(data, indent=2) + "\n")


def check_frames(args, out):
    dependencies()
    if args.every < 1 or args.frames < 1 or not math.isfinite(args.fps) or args.fps <= 0:
        raise ValueError("--every, --frames and --fps must be positive")
    dimensions = args.viewport.split("x")
    if len(dimensions) != 2 or any(int(n) <= 0 for n in dimensions):
        raise ValueError("--viewport must be positive WxH")
    baseline = Path(args.baseline).resolve() if args.baseline else None
    if baseline is not None and not baseline.is_dir():
        raise ValueError(f"no baseline directory: {baseline}")
    root = prepare(args, out)
    data = analyse(sequences(root), root, out, args.stills, baseline, args.tiles, args.every)
    data.update({"source": args.source, "out": str(out), "stills": args.stills,
                 "baseline": str(baseline) if baseline else None})
    write_findings(data, out)
    return data, int(any(f["severity"] == "block" for f in data["findings"]))


def recipe_run(args, out):
    repo = Path(args.repo or Path.cwd()).resolve()
    recipe = Path(args.recipe).resolve() if args.recipe else repo / ".glevels/frames.json"
    if not recipe.is_file() and not args.recipe:
        raise ValueError(f"no .glevels/frames.json in {repo}: add a recipe that captures this product's frames")
    doc = json.loads(recipe.read_text())
    if not isinstance(doc, dict):
        raise ValueError("recipe must be an object")
    captures, meshes = doc.get("captures", []), doc.get("meshes", [])
    if not isinstance(captures, list) or not isinstance(meshes, list) or not captures and not meshes:
        raise ValueError("recipe needs captures and/or meshes lists with at least one part")
    run_id = uuid.uuid4().hex

    def expand(value):
        if not isinstance(value, str):
            raise ValueError("recipe commands and paths must be strings")
        return value.replace("{run}", run_id)

    def resolve(value):
        return str((repo / expand(value)).resolve())

    entries, names = [], set()
    for i, entry in enumerate(captures + meshes):
        if not isinstance(entry, dict):
            raise ValueError("recipe parts must be objects")
        kind = "capture" if i < len(captures) else "mesh"
        name = entry["name"] if kind == "capture" else f"mesh-{i - len(captures) + 1:03d}"
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", "..") or name in names:
            raise ValueError(f"recipe needs unique simple part names: {name}")
        names.add(name)
        entries.append((kind, name, entry))

    parts, findings, rows, notes, tiles = [], [], [], [], []
    for kind, name, entry in entries:
        target = out / name
        target.mkdir(parents=True, exist_ok=True)
        part = {"name": name, "kind": kind, "out": str(target), "exit": 0}
        data, output = None, ""
        try:
            command = entry.get("run")
            if command is not None:
                process = subprocess.run(expand(command), shell=True, cwd=repo, capture_output=True, text=True, timeout=600)
                (target / "run.stdout.txt").write_text(process.stdout)
                (target / "run.stderr.txt").write_text(process.stderr)
                if process.returncode:
                    part["exit"] = 2 if process.returncode == 2 else 1
                    reason = "impossible" if part["exit"] == 2 else "blocking: capture failed"
                    part["error"] = f"{reason}: {name} run exited {process.returncode}: " + (process.stderr or process.stdout).strip()[-1000:]
            if not part["exit"]:
                if kind == "capture":
                    source = expand(entry["url"]) if entry.get("url") else resolve(entry["frames"])
                    capture_args = argparse.Namespace(**vars(args))
                    capture_args.source = source
                    capture_args.path = resolve(entry["path"]) if entry.get("path") else None
                    capture_args.frames = entry.get("frames_n", args.frames)
                    capture_args.fps = entry.get("fps", args.fps)
                    capture_args.viewport = entry.get("viewport", args.viewport)
                    capture_args.stills = entry.get("stills", False)
                    capture_args.baseline = resolve(entry["baseline"]) if entry.get("baseline") else None
                    data, part["exit"] = check_frames(capture_args, target)
                else:
                    files = entry["files"]
                    if not isinstance(files, list) or not files:
                        raise ValueError("mesh part needs a nonempty files list")
                    process = subprocess.run([sys.executable, str(HERE / "meshcheck.py"), "--out", str(target),
                                              *[resolve(file) for file in files]],
                                             cwd=repo, capture_output=True, text=True, timeout=600)
                    (target / "stdout.txt").write_text(process.stdout)
                    (target / "stderr.txt").write_text(process.stderr)
                    part["exit"] = 0 if process.returncode == 0 else 2 if process.returncode == 2 else 1
                    output = process.stdout + process.stderr
                    if process.returncode:
                        part["error"] = output.strip()[-1000:]
        except RUN_ERRORS as error:
            part.update(exit=2, error=f"impossible: {error}")
        if data is None and (kind == "capture" or not (target / "findings.json").is_file()):
            failure_findings = []
            if part["exit"] == 1:
                failure_findings = [{"kind": "capture-failed", "severity": "block", "score": 1.0,
                                     "detail": part["error"], "rank": 1}]
            write_findings({**part, "findings": failure_findings}, target)
            findings.extend({**finding, "part": name} for finding in failure_findings)
        if kind != "capture" and (target / "findings.json").is_file():
            for mesh in json.loads((target / "findings.json").read_text()).get("inputs", []):
                for f in mesh.get("findings", []):
                    sample = (f.get("samples") or [{}])[0]
                    findings.append({"kind": f"mesh-{f['check']}", "severity": f["severity"], "part": name,
                                     "score": float(f.get("count", 1)),
                                     "detail": f"{f.get('count', 1)} in {f.get('group', '?')}: {sample.get('detail', '')} "
                                               f"at {sample.get('location')}"})
        if data is not None:
            findings.extend({**finding, "part": name} for finding in data["findings"])
            rows.extend({**row, "name": f"{name}/{row['name']}"} for row in data["sequences"])
            notes.extend(f"{name}: {note}" for note in data["notes"])
            tiles.extend({**tile, "part": name} for tile in data["tiles"])
        parts.append(part)
        if not args.json:
            print(f"\n{name} ({kind}): exit {part['exit']}")
            if output:
                print(output.splitlines()[0])
                print(f"  mesh report: {target / 'stdout.txt'}\n  findings: {target / 'findings.json'}")
            elif part["exit"] == 2 and part.get("error"):
                print(part["error"])
    code = 2 if all(p["exit"] == 2 for p in parts) else int(any(p["exit"] for p in parts))
    summary = {"recipe": str(recipe), "repo": str(repo), "run": run_id, "out": str(out), "parts": parts, "exit": code,
               "findings": rank_findings(findings), "sequences": rows, "notes": notes, "tiles": tiles}
    write_findings(summary, out)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        report(summary, out)
        print(f"\nrecipe: {len(parts)} parts, exit {code}\nfindings: {out / 'findings.json'}")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?")
    parser.add_argument("--repo")
    parser.add_argument("--recipe")
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stills", action="store_true")
    parser.add_argument("--baseline")
    parser.add_argument("--tiles", action="store_true")
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--path")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--viewport", default="1280x720")
    args = parser.parse_args()
    try:
        if args.source and (args.repo or args.recipe):
            raise ValueError("use SOURCE or --repo/--recipe")
        if not args.source and not (args.repo or args.recipe):
            raise ValueError("provide SOURCE or --repo/--recipe")
        out = Path(args.out or tempfile.mkdtemp(prefix="glevels-frames-")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if args.repo or args.recipe:
            return recipe_run(args, out)
        data, code = check_frames(args, out)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            report(data, out)
        return code
    except RUN_ERRORS as error:
        print(f"impossible: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
