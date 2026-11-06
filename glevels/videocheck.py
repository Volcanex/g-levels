"""Check shipped video timing, encoding, repeated frames and motion continuity.

Exit 0: clean. Exit 1: faults. Exit 2: cannot inspect at least one input.
Only a few small grayscale frames are kept; timing and motion series are O(frames).

Automatically reads <video>.events.json when present, for example:
{"fps": 60, "events": [{"kind": "ui", "frame": 212, "label": "click hero image"},
                       {"kind": "route", "frame": 455, "label": "open version 3"}]}
The capture script records 0-based decoded frames when it dispatches actions.
UI windows are [frame, frame + 50], inclusive. Optional ui "until" is an inclusive
end frame that may only shorten the window. Route cuts use [frame - 1, frame + 3].
UI feedback is judged on the live app by correct.motion.rhythm. Narrative scroll,
camera and cursor motion must still ramp. Invalid sidecars are impossible.
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys


BPP_FLOOR = 0.02
STILL_DIFFERENCE = 0.25  # Mean luma levels out of 255; tolerates small codec noise.
HOLD_FRAMES = 6  # At 60 fps, a pause lasts at least 100 ms, unlike a short stutter.
# Fixed capture windows, never sidecar settings. 50 frames at 60 fps cover the
# slowest house spring token, --spring-slow-dur at 825 ms, including its settle.
UI_EVENT_FRAMES = 50
# A route cut can straddle the dispatched action by one frame and settle in three.
ROUTE_EVENT_BEFORE = 1
ROUTE_EVENT_AFTER = 3
EVENT_FINDINGS = {
    "ui": {"uneased-start", "uneased-stop", "motion-spike"},
    "route": {"motion-spike", "scroll-discontinuity", "uneased-start", "uneased-stop"},
}


def dependencies():
    global np
    import numpy as np
    for tool in ("ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            raise OSError(f"{tool} not found")


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError(f"{command[0]}: {result.stderr.strip()[-1200:]}")
    return result.stdout


def atoms(path):
    """Read top-level MP4 boxes by their lengths; never search compressed payloads."""
    found = []
    size = path.stat().st_size
    with path.open("rb") as source:
        offset = 0
        while offset < size:
            source.seek(offset)
            header = source.read(8)
            if len(header) != 8:
                raise ValueError("truncated MP4 box header")
            length, kind = struct.unpack(">I4s", header)
            minimum = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise ValueError("truncated extended MP4 box")
                length = struct.unpack(">Q", extended)[0]
                minimum = 16
            elif length == 0:
                length = size - offset
            if length < minimum or offset + length > size:
                raise ValueError("invalid MP4 box length")
            found.append(kind.decode("ascii", errors="replace"))
            offset += length
    return found


def step(previous, current):
    difference = float(np.abs(current - previous).mean())
    a, b = previous.mean(axis=1), current.mean(axis=1)
    limit = max(1, len(a) // 4)
    target = b[limit:-limit]
    windows = np.lib.stride_tricks.sliding_window_view(a, len(target))
    errors = ((windows - target) ** 2).mean(axis=1)
    index = int(errors.argmin())
    shift = float(index - limit)
    if 0 < index < len(errors) - 1:
        left, centre, right = errors[index - 1:index + 2]
        curvature = left - 2 * centre + right
        if curvature > 1e-6:
            shift += float(np.clip((left - right) / (2 * curvature), -.5, .5))
    contrast = float(target.std())
    reliable = (0 < index < len(errors) - 1 and contrast > 2 and
                float(errors[index]) ** .5 < contrast * .35)
    return difference, shift, reliable


def decode(path, width, height):
    command = ["ffmpeg", "-v", "error", "-xerror", "-threads", "2", "-i", str(path),
               "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "2",
               "-vf", f"scale={width}:{height}:flags=area,format=gray", "-threads", "2",
               "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"]
    differences, shifts, reliable = [], [], []
    first = previous = None
    count = 0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            while True:
                raw = process.stdout.read(width * height)
                if not raw:
                    break
                if len(raw) != width * height:
                    raise ValueError("incomplete decoded frame")
                current = np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)
                if first is None:
                    first = current
                else:
                    diff, shift, good = step(previous, current)
                    differences.append(diff)
                    shifts.append(shift)
                    reliable.append(good)
                previous = current
                count += 1
            error = process.stderr.read().decode(errors="replace")
            if process.wait() or count < 3:
                raise ValueError(f"cannot decode at least three frames: {error[-1200:]}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return count, np.array(differences), np.array(shifts), np.array(reliable), step(previous, first)


def runs(mask):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist()))


def motion(differences, shifts, reliable, scale, native_height, fps, seam, loop):
    findings = []

    def fault(kind, detail, frames):
        findings.append(dict(kind=kind, detail=detail, frames=frames))

    quiet = differences <= STILL_DIFFERENCE
    holds, duplicates = [], []
    for start, end in runs(quiet):
        if end - start >= HOLD_FRAMES:
            holds.append(dict(start_frame=start, end_frame=end, frames=end - start + 1))
        elif start > 0 and end < len(quiet):
            before = differences[max(0, start - 3):start].max()
            after = differences[end:end + 3].max()
            if min(before, after) > STILL_DIFFERENCE * 4:
                duplicates.extend(range(start + 1, end + 1))
    if duplicates:
        fault("duplicate-frame", f"{len(duplicates)} repeated frames interrupt motion", duplicates)
    moving = ~quiet
    vertical = bool(moving.any() and reliable[moving].mean() >= .6 and
                    np.median(np.abs(shifts[moving])) >= .12)
    signed = shifts if vertical else differences / 255 * native_height / scale
    speed = np.abs(signed)
    speed[quiet] = 0
    for index, value in enumerate(differences):
        neighbours = np.r_[differences[max(0, index - 3):index], differences[index + 1:index + 4]]
        if len(neighbours) and value > max(2, float(np.median(neighbours)) * 4):
            fault("motion-spike", f"difference {value:.2f} is far above neighbouring steps", [index + 1])
    if vertical:
        for index in range(1, len(speed)):
            local = np.abs(signed[max(0, index - 3):index + 4])
            change = abs(float(signed[index] - signed[index - 1]))
            if change > max(.75, float(np.median(local)) * .65):
                fault("scroll-discontinuity", f"vertical speed changes by {change * scale:.2f} px/frame",
                      [index, index + 1])
    active = speed > max(.12, float(np.percentile(speed, 95)) * .05)
    # Subpixel row-profile estimates can cross the activity floor for many frames
    # during a continuous slow scroll. Merge across slower steps and short codec-
    # quiet gaps, but keep bouts separate when a sustained hold ends the move.
    for start, end in runs(~active):
        if (start > 0 and end < len(active) and
                all(b - a < HOLD_FRAMES for a, b in runs(quiet[start:end]))):
            active[start:end] = True
    ramps_in, ramps_out = [], []
    for start, end in runs(active):
        segment = speed[start:end]
        peak = float(segment.max())
        low = np.flatnonzero(segment >= .1 * peak)
        high = np.flatnonzero(segment >= .8 * peak)
        ramp_in, ramp_out = int(high[0] - low[0]), int(low[-1] - high[-1])
        ramps_in.append(ramp_in)
        ramps_out.append(ramp_out)
        if (start > 0 or not loop) and ramp_in <= 2:
            fault("uneased-start", f"10%-80% speed ramp takes {ramp_in} frames", [start + 1])
        if (end < len(speed) or not loop) and ramp_out <= 2:
            fault("uneased-stop", f"80%-10% speed ramp takes {ramp_out} frames", [end])
    seam_diff, seam_shift, _ = seam
    seam_signed = seam_shift if vertical else seam_diff / 255 * native_height / scale
    seam_speed = abs(seam_signed)
    if loop:
        ordinary = float(np.percentile(np.r_[differences[:5], differences[-5:]], 95))
        boundary_steps = [float(np.median(signed[:3])), float(np.median(signed[-3:]))]
        discontinuous = any(abs(seam_signed - edge) > max(.75, abs(edge) * .65) for edge in boundary_steps)
        if seam_diff > max(2, ordinary * 3) or discontinuous:
            fault("loop-seam", f"last-to-first difference {seam_diff:.2f}, speed {seam_speed * scale:.2f} px/frame",
                  [len(differences), 0])
    return dict(findings=findings, holds=holds, duplicates=len(duplicates),
                repeated_steps=int(quiet.sum()), longest_hold_frames=max((h["frames"] for h in holds), default=0),
                ramp_in_frames=min(ramps_in, default=0), ramp_out_frames=min(ramps_out, default=0),
                motion_method="vertical-shift" if vertical else "difference-equivalent (proxy, not displacement)",
                peak_px_frame=float(speed.max()) * scale, mean_px_frame=float(speed.mean()) * scale,
                peak_px_second=float(speed.max()) * scale * fps,
                mean_px_second=float(speed.mean()) * scale * fps,
                loop_seam=dict(checked=loop, difference=seam_diff, px_frame=seam_speed * scale))


def load_events(path, fps, count):
    sidecar = Path(str(path) + ".events.json")
    if not sidecar.exists() and not sidecar.is_symlink():
        return []
    try:
        data = json.loads(sidecar.read_text())
        if not isinstance(data, dict) or set(data) != {"fps", "events"}:
            raise ValueError("need exactly fps and events")
        if type(data["fps"]) not in (int, float) or Fraction(str(data["fps"])) != fps:
            raise ValueError(f"fps must match the clip's actual frame rate {fps}")
        if not isinstance(data["events"], list):
            raise ValueError("events must be a list")
        for index, event in enumerate(data["events"]):
            if (not isinstance(event, dict) or not {"kind", "frame", "label"} <= set(event)
                    or set(event) - {"kind", "frame", "label", "until"}):
                raise ValueError(f"event {index}: need kind, frame, label and optional ui until")
            if not isinstance(event["kind"], str) or event["kind"] not in EVENT_FINDINGS:
                raise ValueError(f"event {index}: unknown kind")
            frame = event["frame"]
            if type(frame) is not int or not 0 <= frame < count:
                raise ValueError(f"event {index}: frame must be a decoded index in [0, {count - 1}]")
            if not isinstance(event["label"], str) or not event["label"].strip():
                raise ValueError(f"event {index}: label must be a nonempty string")
            if "until" in event:
                until = event["until"]
                if (event["kind"] != "ui" or type(until) is not int
                        or not frame <= until <= min(frame + UI_EVENT_FRAMES, count - 1)):
                    raise ValueError(f"event {index}: until may only shorten the fixed ui window")
        return data["events"]
    except (OSError, ValueError) as error:
        raise ValueError(f"events sidecar {sidecar}: {error}") from error


def exempt_events(result):
    findings, exempt = [], []
    for finding in result["findings"]:
        frames = finding.get("frames", [])
        for event in result["events"]:
            if finding["kind"] not in EVENT_FINDINGS[event["kind"]] or not frames:
                continue
            frame = event["frame"]
            if event["kind"] == "ui":
                start, end = frame, event.get("until", frame + UI_EVENT_FRAMES)
            else:
                start, end = frame - ROUTE_EVENT_BEFORE, frame + ROUTE_EVENT_AFTER
            if all(start <= index <= end for index in frames):
                exempt.append(dict(finding, event={key: event[key] for key in ("kind", "frame", "label")}))
                break
        else:
            findings.append(finding)
    result.update(findings=findings, exempt=exempt)


def inspect(path, loop=True, fps=60):
    dependencies()
    path = Path(path).resolve(strict=True)
    metadata = json.loads(run(["ffprobe", "-v", "error", "-threads", "2", "-show_streams",
                               "-show_format", "-of", "json", str(path)]))
    videos = [s for s in metadata["streams"] if s["codec_type"] == "video"]
    if not videos:
        raise ValueError("no video stream")
    stream = videos[0]
    width, height = int(stream["width"]), int(stream["height"])
    if width <= 0 or height <= 0:
        raise ValueError("invalid video dimensions")
    result = dict(file=str(path), width=width, height=height, codec=stream.get("codec_name"),
                  pixel_format=stream.get("pix_fmt"), r_frame_rate=stream.get("r_frame_rate"),
                  avg_frame_rate=stream.get("avg_frame_rate"), findings=[], events=[], exempt=[])

    def fault(kind, detail):
        result["findings"].append(dict(kind=kind, detail=detail))

    if len(videos) != 1 or result["codec"] != "h264" or result["pixel_format"] != "yuv420p":
        fault("video-stream", "need exactly one H.264 yuv420p video stream")
    result["audio_streams"] = sum(s["codec_type"] == "audio" for s in metadata["streams"])
    if result["audio_streams"]:
        fault("audio-stream", f"{result['audio_streams']} audio streams; shipped background video must be silent")
    mp4 = "mp4" in metadata["format"].get("format_name", "").split(",")
    boxes = atoms(path) if mp4 else []
    result["faststart"] = "moov" in boxes and "mdat" in boxes and boxes.index("moov") < boxes.index("mdat")
    if not result["faststart"]:
        fault("faststart", "need MP4 with moov before mdat")
    expected = Fraction(fps)
    actual_fps = Fraction(stream.get("avg_frame_rate", "0/1"))
    if actual_fps <= 0:
        raise ValueError("cannot establish the stream's actual frame rate")
    result["fps"] = float(actual_fps)
    if any(Fraction(stream.get(key, "0/1")) != expected for key in ("r_frame_rate", "avg_frame_rate")):
        fault("frame-rate", f"r_frame_rate={result['r_frame_rate']}, avg_frame_rate={result['avg_frame_rate']}; need {fps}/1")
    timing = json.loads(run(["ffprobe", "-v", "error", "-threads", "2", "-select_streams", "v:0",
                             "-show_frames", "-show_entries", "frame=pts,best_effort_timestamp",
                             "-of", "json", str(path)]))["frames"]
    pts = [frame.get("pts") for frame in timing]
    time_base = Fraction(stream["time_base"])
    deltas = [Fraction(b - a) * time_base for a, b in zip(pts, pts[1:]) if a is not None and b is not None]
    result["pts_deltas"] = sorted({str(delta) for delta in deltas})
    result["pts_faults"] = sum(delta != 1 / expected for delta in deltas)
    if len(deltas) != len(timing) - 1 or result["pts_faults"]:
        fault("frame-timing", f"{result['pts_faults']} wrong PTS deltas or missing PTS; need exactly 1/{fps} s each")
    sample_width = min(width, 160)
    sample_height = max(8, round(height * sample_width / width / 2) * 2)
    count, differences, shifts, reliable, seam = decode(path, sample_width, sample_height)
    result["frames"] = count
    result["events"] = load_events(path, actual_fps, count)
    duration = Fraction(stream["duration_ts"]) * time_base if "duration_ts" in stream else None
    result["duration"] = float(duration) if duration is not None else None
    result["nb_frames"] = stream.get("nb_frames")
    if (int(stream.get("nb_frames", 0)) != count or len(timing) != count or duration != count / actual_fps):
        fault("frame-count", f"nb_frames={result['nb_frames']}, decoded={count}, PTS={len(timing)}, duration={duration}; need frames/actual fps")
    bitrate = int(stream.get("bit_rate", 0))
    result.update(bitrate=bitrate, bpp_frame=bitrate / (width * height * float(actual_fps)), bpp_floor=BPP_FLOOR)
    if result["bpp_frame"] < BPP_FLOOR:
        fault("bitrate", f"{result['bpp_frame']:.5f} bits/pixel/frame below {BPP_FLOOR:.3f} floor")
    progress = run(["ffmpeg", "-v", "error", "-xerror", "-threads", "2", "-i", str(path),
                    "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "2",
                    "-vf", f"scale={sample_width}:{sample_height}:flags=area,mpdecimate",
                    "-threads", "2", "-fps_mode", "passthrough", "-progress", "pipe:1",
                    "-nostats", "-f", "null", "-"])
    kept = [int(line.split("=", 1)[1]) for line in progress.splitlines() if line.startswith("frame=")]
    if not kept:
        raise ValueError("mpdecimate did not report a frame count")
    result["mpdecimate_dropped"] = count - kept[-1]
    measured = motion(differences, shifts, reliable, height / sample_height, height, float(actual_fps), seam, loop)
    result["findings"].extend(measured.pop("findings"))
    result.update(measured)
    exempt_events(result)
    return result


def report(result):
    print(f"\n{result['file']}")
    print(f"  events={len(result['events'])}; exempt findings={len(result['exempt'])}")
    if "error" in result:
        print(f"  impossible: {result['error']}")
        return
    print(f"  {result['width']}x{result['height']} {result['codec']} {result['pixel_format']}; "
          f"audio={result['audio_streams']}; faststart={result['faststart']}")
    print(f"  fps r={result['r_frame_rate']} avg={result['avg_frame_rate']}; "
          f"frames={result['frames']} nb_frames={result['nb_frames']}; duration={result['duration']} s; "
          f"PTS deltas={','.join(result['pts_deltas'])}; wrong deltas={result['pts_faults']}")
    print(f"  repeats={result['repeated_steps']}; isolated duplicates={result['duplicates']}; "
          f"mpdecimate dropped={result['mpdecimate_dropped']}; holds={len(result['holds'])}; "
          f"longest hold={result['longest_hold_frames']} frames")
    print(f"  speed ({result['motion_method']}): peak={result['peak_px_frame']:.3f} px/frame "
          f"({result['peak_px_second']:.2f} px/s), mean={result['mean_px_frame']:.3f} px/frame "
          f"({result['mean_px_second']:.2f} px/s); ramps in/out={result['ramp_in_frames']}/{result['ramp_out_frames']} frames")
    print(f"  bitrate={result['bitrate']} bit/s; bits/pixel/frame={result['bpp_frame']:.5f} (floor {BPP_FLOOR:.3f}); "
          f"loop seam={'checked' if result['loop_seam']['checked'] else 'disabled'} "
          f"difference={result['loop_seam']['difference']:.3f}, {result['loop_seam']['px_frame']:.3f} px/frame")
    for hold in result["holds"]:
        print(f"  hold: frames {hold['start_frame']}-{hold['end_frame']} ({hold['frames']} frames)")
    for finding in result["findings"]:
        frames = finding.get("frames", [])
        print(f"  FAULT {finding['kind']}: {finding['detail']}" +
              (f"; frames {frames[:12]}" + (f" (+{len(frames) - 12} more)" if len(frames) > 12 else "") if frames else ""))
    for finding in result["exempt"]:
        event = finding["event"]
        reason = ("ui-feedback, judged by correct.motion.rhythm" if event["kind"] == "ui"
                  else "route cut, g-motion rule 11")
        print(f"  EXEMPT {finding['kind']}: {finding['detail']}, frames {finding['frames']}, "
              f"event {event['kind']} at frame {event['frame']}: {event['label']} ({reason})")
    print(f"  {'FAULT' if result['findings'] else 'clean'}: {len(result['findings'])} findings")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--loop", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--fps", type=int, default=60)
    args = parser.parse_args(argv)
    if args.fps <= 0:
        parser.error("--fps must be positive")
    results = []
    for video in args.videos:
        try:
            results.append(inspect(video, loop=args.loop, fps=args.fps))
        except (ImportError, OSError, ValueError, KeyError, TypeError, MemoryError, ZeroDivisionError,
                subprocess.SubprocessError) as error:
            results.append(dict(file=video, error=str(error), findings=[], events=[], exempt=[]))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            report(result)
    return 2 if any("error" in r for r in results) else 1 if any(r["findings"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
