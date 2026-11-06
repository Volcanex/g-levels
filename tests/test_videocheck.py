import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

from glevels import videocheck as vc


def encode(path, positions, fps=60, crf=18, block_frame=None):
    y, x = np.mgrid[:800, :320]
    plane = 115 + 45 * np.sin(y / 13) + 25 * np.cos(y / 7 + x / 19)
    plane += 30 * ((x // 9 + y // 5) % 2)
    image = np.stack([plane, plane * .8 + 20, plane * .6 + 35], axis=2)
    image = np.clip(image, 0, 255)
    command = ["ffmpeg", "-v", "error", "-y", "-threads", "2", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-s", "320x180", "-r", str(fps), "-i", "pipe:0",
               "-an", "-c:v", "libx264", "-threads", "2", "-filter_threads", "2",
               "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(path)]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        for index, position in enumerate(positions):
            top = int(position)
            fraction = position - top
            frame = (1 - fraction) * image[top:top + 180] + fraction * image[top + 1:top + 181]
            if block_frame is not None and index >= block_frame:
                block = frame[24:56, 24:88]
                mean = block.mean(axis=1, keepdims=True)
                contrast = np.minimum(mean, 255 - mean) * .9
                # Preserve row means so the block does not impersonate a scroll jump.
                block[:, :32] = mean + contrast
                block[:, 32:] = mean - contrast
            process.stdin.write(frame.astype(np.uint8).tobytes())
        process.stdin.close()
        error = process.stderr.read().decode()
        assert process.wait() == 0, error
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    directory = tmp_path_factory.mktemp("video")
    outward = 300 * (1 - np.cos(np.linspace(0, np.pi, 91))) / 2
    positions = np.r_[np.zeros(15), outward, np.full(15, 300), outward[::-1], np.zeros(15)]
    clean = encode(directory / "clean.mp4", positions)
    original = encode(directory / "30fps.mp4", positions[::2], fps=30)
    doubled = directory / "doubled.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-i", str(original),
                    "-vf", "fps=60", "-an", "-c:v", "libx264", "-threads", "2",
                    "-filter_threads", "2", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(doubled)], check=True)
    duplicate = positions.copy()
    duplicate[60] = duplicate[59]
    jump = positions.copy()
    jump[60:75] += 60
    return dict(clean=clean, original=original, doubled=doubled,
                duplicate=encode(directory / "duplicate.mp4", duplicate),
                jump=encode(directory / "jump.mp4", jump), directory=directory,
                positions=positions)


def kinds(result):
    return {row["kind"] for row in result["findings"]}


def test_original_30fps_and_doubled_container_fail(clips):
    assert "frame-rate" in kinds(vc.inspect(clips["original"]))
    result = vc.inspect(clips["doubled"])
    assert result["r_frame_rate"] == result["avg_frame_rate"] == "60/1"
    assert "duplicate-frame" in kinds(result)
    assert result["duplicates"] > 60
    assert result["mpdecimate_dropped"] >= result["duplicates"]


def test_related_duplicate_and_jump_fail(clips):
    duplicated = vc.inspect(clips["duplicate"])
    assert "duplicate-frame" in kinds(duplicated)
    assert any(60 in row.get("frames", []) for row in duplicated["findings"])
    jumped = vc.inspect(clips["jump"])
    assert {"motion-spike", "scroll-discontinuity"} & kinds(jumped)


def test_clean_eased_round_trip_with_holds_passes(clips):
    result = vc.inspect(clips["clean"])
    assert result["findings"] == []
    assert result["frames"] == 227
    assert result["motion_method"] == "vertical-shift"
    assert result["duplicates"] == 0
    assert len(result["holds"]) >= 3
    assert result["longest_hold_frames"] >= 15
    assert result["ramp_in_frames"] > 2
    assert result["ramp_out_frames"] > 2
    assert result["loop_seam"]["px_frame"] == pytest.approx(0, abs=.1)
    assert result["peak_px_frame"] == pytest.approx(300 * np.pi / 180, rel=.2)


def test_cli_multiple_files_json_and_unreadable(clips):
    script = str(Path(vc.__file__))
    process = subprocess.run([sys.executable, script, str(clips["clean"]),
                              str(clips["original"]), "--json"], capture_output=True, text=True)
    assert process.returncode == 1
    results = json.loads(process.stdout)
    assert not results[0]["findings"] and results[1]["findings"]
    missing = clips["directory"] / "missing.mp4"
    process = subprocess.run([sys.executable, script, str(clips["original"]), str(missing),
                              "--json"], capture_output=True, text=True)
    assert process.returncode == 2
    assert "error" in json.loads(process.stdout)[1]


def test_missing_tool_is_impossible(clips, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert vc.main([str(clips["clean"]), "--json"]) == 2


def test_loop_seam_and_no_loop(clips):
    outward = clips["positions"][:121]
    path = encode(clips["directory"] / "one-way.mp4", outward)
    assert "loop-seam" in kinds(vc.inspect(path))
    assert vc.inspect(path, loop=False)["findings"] == []


def test_abrupt_start_and_stop(clips):
    positions = np.r_[np.zeros(15), np.arange(0, 300, 4), np.full(15, 296)]
    path = encode(clips["directory"] / "abrupt.mp4", positions)
    assert {"uneased-start", "uneased-stop"} <= kinds(vc.inspect(path, loop=False))


def test_bethdarroch_slow_scroll_stays_one_bout_per_move():
    vc.dependencies()
    # decode(video, 160, 100) from bethdarroch-retry.mp4: 800x500, 1140 frames,
    # 640 CSS px sine scrolls at frames 60-540 and 612-1092, with holds.
    fixture = Path(__file__).parent / "fixtures" / "bethdarroch-motion.npz"
    with np.load(fixture) as data:
        result = vc.motion(data["differences"], data["shifts"], data["reliable"],
                           5, 500, 60, data["seam"], True)
    assert result["findings"] == []
    assert result["ramp_in_frames"] > 2
    assert result["ramp_out_frames"] > 2


@pytest.mark.parametrize("hard_start", [False, True])
def test_slow_scroll_ramps(hard_start):
    vc.dependencies()
    move = np.full(241, .18) if hard_start else .18 * np.sin(np.linspace(0, np.pi, 241))
    shifts = np.r_[np.zeros(15), move, np.zeros(15)]
    result = vc.motion(shifts * 20, shifts, np.ones(len(shifts), dtype=bool),
                       5, 500, 60, (0., 0., True), False)
    assert result["motion_method"] == "vertical-shift"
    assert result["peak_px_frame"] < 1
    if hard_start:
        assert {"uneased-start", "uneased-stop"} <= kinds(result)
    else:
        assert result["findings"] == []


def test_slow_bouts_still_separate_at_a_sustained_hold():
    vc.dependencies()
    eased = .18 * np.sin(np.linspace(0, np.pi, 241))
    shifts = np.r_[np.zeros(15), eased, np.zeros(15), np.full(60, .18), np.zeros(15)]
    result = vc.motion(shifts * 20, shifts, np.ones(len(shifts), dtype=bool),
                       5, 500, 60, (0., 0., True), False)
    assert {"uneased-start", "uneased-stop"} <= kinds(result)
    assert any(row["kind"] == "uneased-start" and row["frames"] == [272]
               for row in result["findings"])


def test_container_audio_and_pixel_format(clips):
    path = clips["directory"] / "audio-tail-moov.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-i", str(clips["clean"]),
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest",
                    "-c:v", "copy", "-c:a", "aac", "-threads", "2", str(path)], check=True)
    assert {"audio-stream", "faststart"} <= kinds(vc.inspect(path))
    path = clips["directory"] / "444.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-i", str(clips["clean"]),
                    "-an", "-c:v", "libx264", "-threads", "2", "-filter_threads", "2",
                    "-pix_fmt", "yuv444p", "-movflags", "+faststart", str(path)], check=True)
    assert "video-stream" in kinds(vc.inspect(path))


def test_variable_pts_and_low_bitrate(clips):
    path = clips["directory"] / "vfr.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-i", str(clips["clean"]),
                    "-vf", r"setpts=PTS+gte(N\,60)/(60*TB)", "-fps_mode", "passthrough",
                    "-an", "-c:v", "libx264", "-threads", "2", "-filter_threads", "2",
                    "-crf", "18", "-movflags", "+faststart", str(path)], check=True)
    assert "frame-timing" in kinds(vc.inspect(path))
    path = encode(clips["directory"] / "mush.mp4", clips["positions"], crf=44)
    result = vc.inspect(path)
    assert result["bpp_frame"] < vc.BPP_FLOOR
    assert "bitrate" in kinds(result)


def test_fps_override_and_unreadable_file(clips):
    assert vc.inspect(clips["original"], fps=30)["findings"] == []
    bad = clips["directory"] / "broken.mp4"
    bad.write_bytes(b"not a video")
    assert vc.main([str(bad)]) == 2


def test_loop_step_preserves_scroll_direction():
    vc.dependencies()
    differences, shifts, reliable = np.full(60, 4.), np.full(60, 2.), np.ones(60, dtype=bool)
    clean = vc.motion(differences, shifts, reliable, 2, 180, 60, (4., 2., True), True)
    assert clean["findings"] == []
    reversed_step = vc.motion(differences, shifts, reliable, 2, 180, 60, (4., -2., True), True)
    assert "loop-seam" in kinds(reversed_step)


def test_loop_seam_uses_the_neighbouring_holds():
    vc.dependencies()
    differences, shifts = np.full(60, 4.), np.full(60, 2.)
    differences[:10] = differences[-10:] = 0
    shifts[:10] = shifts[-10:] = 0
    result = vc.motion(differences, shifts, np.ones(60, dtype=bool), 2, 180, 60, (5., 0., True), True)
    assert "loop-seam" in kinds(result)


def write_events(path, events, fps=60):
    sidecar = Path(str(path) + ".events.json")
    sidecar.write_text(json.dumps(dict(fps=fps, events=events)))
    return sidecar


def test_ui_feedback_sidecar_exempts_local_change(clips, tmp_path, capsys):
    path = encode(tmp_path / "feedback.mp4", clips["positions"][:121], block_frame=8)
    assert vc.main([str(path), "--no-loop", "--json"]) == 1
    before = json.loads(capsys.readouterr().out)[0]
    assert "motion-spike" in kinds(before)
    event = dict(kind="ui", frame=8, label="show toolbar")
    write_events(path, [event])
    assert vc.main([str(path), "--no-loop", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert result["events"] == [event]
    assert result["exempt"] and result["findings"] == []
    assert all(row["event"] == event for row in result["exempt"])
    assert {row["kind"] for row in result["exempt"]} <= {
        "motion-spike", "uneased-start", "uneased-stop"}
    vc.report(result)
    output = capsys.readouterr().out
    assert "events=1; exempt findings=" in output
    assert "EXEMPT motion-spike:" in output
    assert "(ui-feedback, judged by correct.motion.rhythm)" in output
    assert "show toolbar" in output


def test_route_sidecar_exempts_cut(clips, tmp_path, capsys):
    positions = clips["positions"][:121].copy()
    positions[70:] += 120
    path = encode(tmp_path / "route.mp4", positions)
    assert vc.main([str(path), "--no-loop", "--json"]) == 1
    before = json.loads(capsys.readouterr().out)[0]
    assert {"motion-spike", "scroll-discontinuity"} & kinds(before)
    write_events(path, [dict(kind="route", frame=70, label="open version 3")])
    assert vc.main([str(path), "--no-loop", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert result["findings"] == [] and result["exempt"]
    vc.report(result)
    assert "(route cut, g-motion rule 11)" in capsys.readouterr().out


def test_events_leave_narrative_start_outside_window(tmp_path):
    positions = np.r_[np.zeros(70), np.arange(0, 300, 4), np.full(15, 296)]
    path = encode(tmp_path / "abrupt.mp4", positions)
    write_events(path, [dict(kind="ui", frame=0, label="click button")])
    result = vc.inspect(path, loop=False)
    assert {"uneased-start", "uneased-stop"} <= kinds(result)
    assert vc.main([str(path), "--no-loop"]) == 1


@pytest.mark.parametrize("frame,until,block,clean", [
    (10, None, 60, True),
    (10, None, 61, False),
    (10, 12, 13, False),
])
def test_ui_window_boundary_and_shortened_until(tmp_path, frame, until, block, clean):
    outward = 300 * (1 - np.cos(np.linspace(0, np.pi, 91))) / 2
    positions = np.r_[np.zeros(75), outward, np.full(15, 300)]
    path = encode(tmp_path / "boundary.mp4", positions, block_frame=block)
    event = dict(kind="ui", frame=frame, label="show toolbar")
    if until is not None:
        event["until"] = until
    write_events(path, [event])
    result = vc.inspect(path, loop=False)
    assert (not result["findings"]) == clean
    if not clean:
        assert "motion-spike" in kinds(result)
        assert any(block in row.get("frames", []) for row in result["findings"])


@pytest.mark.parametrize("payload", [
    "{",
    "[]",
    '{"fps": 60, "events": [], "window": 500}',
    '{"fps": 30, "events": []}',
    '{"fps": true, "events": []}',
    '{"fps": 60, "events": {}}',
    '{"fps": 60, "events": [null]}',
    '{"fps": 60, "events": [{"kind": "scroll", "frame": 10, "label": "scroll"}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": -1, "label": "click"}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 227, "label": "click"}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": true, "label": "click"}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 1.5, "label": "click"}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 10}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 10, "label": 5}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 10, "label": "click", "until": 61}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 10, "label": "click", "until": 9}]}',
    '{"fps": 60, "events": [{"kind": "ui", "frame": 220, "label": "click", "until": 227}]}',
    '{"fps": 60, "events": [{"kind": "route", "frame": 10, "label": "open", "until": 11}]}',
])
def test_malformed_events_are_impossible(clips, tmp_path, capsys, payload):
    path = tmp_path / "malformed.mp4"
    shutil.copyfile(clips["clean"], path)
    Path(str(path) + ".events.json").write_text(payload)
    assert vc.main([str(path), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)[0]
    assert "events" in result["error"]


def test_ui_event_cannot_exempt_duplicate(clips, tmp_path):
    path = tmp_path / "duplicate.mp4"
    shutil.copyfile(clips["duplicate"], path)
    write_events(path, [dict(kind="ui", frame=60, label="click image")])
    result = vc.inspect(path)
    assert "duplicate-frame" in kinds(result)
    assert all(row["kind"] != "duplicate-frame" for row in result["exempt"])
    assert vc.main([str(path)]) == 1


def test_ui_event_cannot_exempt_scroll_discontinuity(clips, tmp_path):
    path = tmp_path / "scroll.mp4"
    shutil.copyfile(clips["jump"], path)
    write_events(path, [dict(kind="ui", frame=59, label="click image")])
    assert "scroll-discontinuity" in kinds(vc.inspect(path))


def test_event_counts_without_sidecar(clips, capsys):
    assert vc.main([str(clips["clean"])]) == 0
    assert "events=0; exempt findings=0" in capsys.readouterr().out
    result = vc.inspect(clips["clean"])
    assert result["events"] == result["exempt"] == []


def test_exemptions_preserve_other_checks_and_window_edges():
    protected = ["duplicate-frame", "loop-seam", "bitrate", "frame-rate", "frame-timing",
                 "frame-count", "faststart", "video-stream", "audio-stream"]
    allowed = ["motion-spike", "scroll-discontinuity", "uneased-start", "uneased-stop"]
    for kind, start, end in (("ui", 10, 60), ("route", 9, 13)):
        findings = [dict(kind=name, frames=[10]) for name in protected + allowed]
        findings += [dict(kind="motion-spike", frames=frames) for frames in (
            [start], [end], [start - 1], [end + 1], [start - 1, start], [end, end + 1], [])]
        result = dict(findings=findings, events=[dict(kind=kind, frame=10, label="click")])
        vc.exempt_events(result)
        assert set(protected) <= kinds(result)
        assert ("scroll-discontinuity" in kinds(result)) == (kind == "ui")
        for edge in (start, end):
            assert any(row["kind"] == "motion-spike" and row["frames"] == [edge]
                       for row in result["exempt"])
        assert all(row in result["findings"] for row in findings[-5:])
