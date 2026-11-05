import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

from glevels import videocheck as vc


def encode(path, positions, fps=60, crf=18):
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
        for position in positions:
            top = int(position)
            fraction = position - top
            frame = (1 - fraction) * image[top:top + 180] + fraction * image[top + 1:top + 181]
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
