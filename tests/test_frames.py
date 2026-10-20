import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from glevels import frames


@pytest.fixture(autouse=True)
def dependencies():
    frames.dependencies()


def save(directory, arrays):
    directory.mkdir(parents=True, exist_ok=True)
    for i, array in enumerate(arrays):
        Image.fromarray(array.astype(np.uint8)).save(directory / f"{i:03d}.png")
    return directory


def smooth_sequence(faults=False):
    y, x = np.mgrid[:144, :240]
    arrays = []
    for i in range(12):
        plane = 105 + 12 * np.sin((x - 2 * i) / 15) + 8 * np.cos(y / 17)
        rgb = np.repeat(plane[:, :, None], 3, axis=2)
        if faults:
            rgb[24:72, 24:72] += 90 if i == 4 else 0
            rgb[72:120, 144:216] += 32 if i % 2 else -32
            if i >= 8:
                rgb += 30
        arrays.append(np.clip(rgb, 0, 255).astype(np.uint8))
    return arrays


def run(directory, out, **kwargs):
    return frames.analyse(frames.sequences(directory.resolve()), directory.resolve(), out, **kwargs)


def test_continuous_pop_shimmer_lighting_and_clean_control(tmp_path):
    clean = save(tmp_path / "clean", smooth_sequence())
    broken = save(tmp_path / "broken", smooth_sequence(faults=True))
    assert run(clean, tmp_path / "clean-out")["findings"] == []
    data = run(broken, tmp_path / "broken-out")
    kinds = {f["kind"] for f in data["findings"]}
    assert {"single-frame-spike", "temporal-shimmer", "lighting-jump"} <= kinds
    pop = next(f for f in data["findings"] if f["kind"] == "single-frame-spike" and f["frames"][1] == "004.png"
               and f["box"][0] < 100)
    assert pop["severity"] == "block"
    x, y, w, h = pop["box"]
    assert x <= 24 < x + w and y <= 24 < y + h
    assert len(pop["frames"]) == 3
    assert run(broken, tmp_path / "stills-out", stills=True)["findings"] == []


def test_crop_pixels_stay_native_and_zoom_is_nearest_neighbour(tmp_path):
    directory = save(tmp_path / "input", smooth_sequence(faults=True))
    data = run(directory, tmp_path / "out")
    for finding in data["findings"]:
        x, y, w, h = finding["box"]
        left, top, cw, ch = finding["context_box"]
        native = np.array(Image.open(finding["crops"]["native"]))
        zoom = np.array(Image.open(finding["crops"]["zoom"]))
        assert native.shape == (ch, cw * len(finding["frames"]), 3)
        tight = []
        for i, name in enumerate(finding["frames"]):
            original = np.array(Image.open(directory / name))
            assert np.array_equal(native[:, i * cw:(i + 1) * cw], original[top:top + ch, left:left + cw])
            tight.append(original[y:y + h, x:x + w])
        expected = np.repeat(np.repeat(np.concatenate(tight, axis=1), 3, axis=0), 3, axis=1)
        assert np.array_equal(zoom, expected)


def test_colour_faults_and_black_are_blocking(tmp_path):
    textured = smooth_sequence()[0]
    magenta = textured.copy()
    magenta[30:40, 40:50] = [255, 0, 255]
    directory = save(tmp_path / "input", [np.zeros_like(textured), np.full_like(textured, 127), magenta])
    data = run(directory, tmp_path / "out", stills=True)
    assert [f["kind"] for f in data["findings"]].count("invalid-frame") == 2
    missing = next(f for f in data["findings"] if f["kind"] == "missing-texture")
    assert missing["box"] == [40, 30, 10, 10]
    assert all(f["severity"] == "block" for f in data["findings"])


def test_unlit_black_and_new_exact_black_area_are_review_leads(tmp_path):
    base = smooth_sequence()[0]
    dark = base.copy()
    dark[40:44, 30:180] = 0
    source = save(tmp_path / "source", [dark])
    baseline = save(tmp_path / "baseline", [base])
    data = run(source, tmp_path / "out", stills=True, baseline=baseline)
    assert {f["kind"] for f in data["findings"]} == {"unlit-black", "new-black-area"}
    assert data["findings"][0]["kind"] == "new-black-area"
    assert all(f["severity"] == "review" for f in data["findings"])
    assert all(Path(f["baseline_crops"]["native"]).is_file() for f in data["findings"])
    assert not run(baseline, tmp_path / "reverse", stills=True, baseline=source)["findings"]
    thick = base.copy()
    thick[40:100, 50:100] = 0
    door = save(tmp_path / "door", [thick])
    assert not run(door, tmp_path / "door-out", stills=True)["findings"]


def test_bright_hollow_band_and_dark_colours(tmp_path):
    rgb = np.full((120, 160, 3), 160, dtype=np.uint8)
    rgb[20:100, 30:130] = 0
    rgb[35:85, 45:115] = 200
    directory = save(tmp_path / "band", [rgb])
    findings = run(directory, tmp_path / "out", stills=True)["findings"]
    assert any(f["kind"] == "unlit-black" for f in findings)
    rgb[20:100, 30:130] = [24, 3, 3]
    directory = save(tmp_path / "red", [rgb])
    assert run(directory, tmp_path / "red-out", stills=True)["findings"] == []


@pytest.mark.parametrize("colour", ([17, 3, 3], [3, 17, 3], [20, 20, 20], [4, 4, 4]))
def test_near_black_colours_are_never_leads(tmp_path, colour):
    rgb = smooth_sequence()[0]
    rgb[40:44, 30:180] = colour
    source = save(tmp_path / "source", [rgb])
    baseline = save(tmp_path / "baseline", [smooth_sequence()[0]])
    assert run(source, tmp_path / "out", stills=True, baseline=baseline)["findings"] == []


def test_black_threshold_and_baseline_shift_tolerance(tmp_path):
    base = smooth_sequence()[0]
    base[40:44, 30:180] = 3
    baseline = save(tmp_path / "baseline", [base])
    for dx, dy in ((1, 0), (0, 2), (2, 2)):
        shifted = smooth_sequence()[0]
        shifted[40 + dy:44 + dy, 30 + dx:180 + dx] = 0
        source = save(tmp_path / f"shift-{dx}-{dy}", [shifted])
        findings = run(source, tmp_path / f"out-{dx}-{dy}", stills=True, baseline=baseline)["findings"]
        assert {f["kind"] for f in findings} == {"unlit-black"}
    shifted[80:84, 30:180] = 0
    source = save(tmp_path / "new", [shifted])
    findings = run(source, tmp_path / "new-out", stills=True, baseline=baseline)["findings"]
    new = [f for f in findings if f["kind"] == "new-black-area"]
    assert len(new) == 1 and new[0]["box"] == [30, 80, 150, 4]


def test_manifest_order_camera_and_subdirectory_boundaries(tmp_path):
    root = tmp_path / "input"
    save(root / "first", smooth_sequence()[:2])
    save(root / "second", smooth_sequence()[2:4])
    assert [r["name"] for r in frames.sequences(root)] == ["first", "second"]
    (root / "frames.json").write_text(json.dumps({"sequences": [
        {"name": "custom", "frames": ["second/001.png", "first/000.png"], "camera": "moving"}]}))
    row = frames.sequences(root)[0]
    assert row["camera"] == "moving"
    assert row["frames"] == [root / "second/001.png", root / "first/000.png"]
    (root / "frames.json").write_text(json.dumps({"sequences": [
        {"name": "bad", "frames": ["../outside.png"]}]}))
    with pytest.raises(ValueError, match="invalid frame"):
        frames.sequences(root)


def test_pose_manifest_is_exclusive_and_fallback_ignores_ancillary_images(tmp_path):
    root = save(tmp_path / "input", smooth_sequence()[:3])
    for name in ("contact.png", "contact-full.png", "diff-before.png", "000.judge.png"):
        shutil.copyfile(root / "000.png", root / name)
    assert [p.name for p in frames.frame_files(root)] == ["000.png", "001.png", "002.png"]
    (root / "frames.json").write_text(json.dumps({"sequences": [
        {"name": "ignored", "frames": ["000.png"]}]}))
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"poses": [{"file": "002.png"}, {"file": "001.png"}]}))
    assert frames.sequences(root)[0]["frames"] == [root / "002.png", root / "001.png"]
    manifest.write_text(json.dumps({"poses": [{"file": "../outside.png"}]}))
    with pytest.raises(ValueError, match="invalid frame"):
        frames.sequences(root)
    manifest.write_text(json.dumps({"poses": []}))
    with pytest.raises(ValueError, match="no frames"):
        frames.sequences(root)


def test_report_caps_ranked_leads_and_keeps_all_crops(tmp_path, capsys, monkeypatch):
    kinds = ["lighting-jump", "temporal-shimmer", "single-frame-spike", "unlit-black", "new-black-area"]
    save(tmp_path / "input", [smooth_sequence()[0]])
    findings = [(kind, "review", [30, 40, 40, 4], float(i), "test lead")
                for kind in kinds for i in range(7)]
    findings += [("invalid-frame", "block", [0, 0, 10, 10], 1.0, "test fault")]
    monkeypatch.setattr(frames, "per_frame", lambda *args: findings)
    data = run(tmp_path / "input", tmp_path / "out", stills=True)
    assert [f["rank"] for f in data["findings"]] == list(range(1, 37))
    assert [f["kind"] for f in data["findings"][:9]] == ["invalid-frame"] + ["new-black-area"] * 7 + ["unlit-black"]
    assert [f["score"] for f in data["findings"][1:8]] == list(range(6, -1, -1))
    frames.report(data, tmp_path / "out")
    output = capsys.readouterr().out
    assert sum(" score=" in line for line in output.splitlines()) == 25
    assert "lead counts: new-black-area=7, unlit-black=7, single-frame-spike=7, temporal-shimmer=7, lighting-jump=7" in output
    printed = [line for line in output.splitlines() if " score=" in line]
    assert set(kinds) <= {line.split()[0] for line in printed}
    assert all(Path(f["crops"][scale]).is_file() for f in data["findings"] for scale in ("native", "zoom"))


def test_moving_camera_floor_and_monotonic_changes(tmp_path):
    arrays = smooth_sequence()
    for i, rgb in enumerate(arrays):
        rgb[24:72, 24:72] = 105 + (6 if i % 2 else -6)
    directory = save(tmp_path / "input", arrays)
    assert any(f["kind"] == "temporal-shimmer" for f in run(directory, tmp_path / "static")["findings"])
    (directory / "frames.json").write_text(json.dumps({"sequences": [{"name": "moving", "camera": "moving",
        "frames": [p.name for p in frames.frame_files(directory)]}]}))
    assert run(directory, tmp_path / "moving")["findings"] == []
    monotonic = save(tmp_path / "monotonic", [rgb + i * 2 for i, rgb in enumerate(smooth_sequence())])
    assert run(monotonic, tmp_path / "monotonic-out")["findings"] == []


def test_scroll_compensation_keeps_lighting_change():
    rng = np.random.default_rng(4)
    plane = rng.integers(60, 200, (160, 192)).astype(np.float32)
    previous, current = plane[:144], plane[3:147]
    assert np.abs(frames.lighting_difference(previous, current, True)).max() == 0
    changed = frames.lighting_difference(previous, current + 30, True)
    assert float(changed.mean()) > frames.LIGHT_STEP
    assert np.array_equal(frames.lighting_difference(previous, current, False), current - previous)


def test_tiles_cover_odd_frame_exactly_and_every_selects_frames(tmp_path):
    directory = save(tmp_path / "input", [a[:121, :199] for a in smooth_sequence()[:3]])
    data = run(directory, tmp_path / "out", stills=True, tiles=True, every=2)
    assert len(data["tiles"]) == 8
    for name in ("000.png", "002.png"):
        original = np.array(Image.open(directory / name))
        reconstructed = np.zeros_like(original)
        for tile in (t for t in data["tiles"] if t["frame"] == name):
            x, y, w, h = tile["box"]
            crop = np.array(Image.open(tile["path"]))
            assert crop.shape == (h, w, 3)
            reconstructed[y:y + h, x:x + w] = crop
        assert np.array_equal(original, reconstructed)


def test_components_merge_branches_and_measure_perimeter():
    mask = np.array([[1, 0, 1], [1, 1, 1], [0, 0, 1]], dtype=bool)
    assert frames.components(mask) == [{"box": [0, 0, 3, 3], "area": 6, "perimeter": 14, "near": 0}]


def test_partial_tiles_count_real_area_and_shimmer_evidence_is_local(tmp_path):
    values = np.full((2, 2), 90.0)
    assert list(frames.hot_boxes(values, frames.SPIKE_LEAD, (25, 25)))[0][2] == 625
    arrays = []
    for i in range(20):
        rgb = smooth_sequence()[0].copy()
        rgb[24:72, 24:72] = 100 + (32 if i % 2 else -32) if i < 10 else 100
        rgb[72:120, 144:192] = 100 + (32 if i % 2 else -32) if i >= 10 else 100
        arrays.append(rgb)
    directory = save(tmp_path / "input", arrays)
    shimmer = [f for f in run(directory, tmp_path / "out")["findings"] if f["kind"] == "temporal-shimmer"]
    assert len(shimmer) == 2
    for f in shimmer:
        middle = int(Path(f["frames"][1]).stem)
        assert middle < 10 if f["box"][0] < 100 else middle >= 10


def test_frame_dimensions_baseline_mismatch_and_missing_reference(tmp_path):
    arrays = smooth_sequence()[:2]
    source = save(tmp_path / "input", [arrays[0], arrays[1][:72]])
    with pytest.raises(ValueError, match="dimensions change"):
        run(source, tmp_path / "out")
    assert run(source, tmp_path / "stills", stills=True)["findings"] == []
    base = save(tmp_path / "baseline", [arrays[0][:72]])
    with pytest.raises(ValueError, match="baseline frame dimensions"):
        run(source, tmp_path / "mismatch", stills=True, baseline=base)
    data = run(source, tmp_path / "missing", stills=True, baseline=tmp_path / "no-baseline")
    assert len(data["notes"]) == 2


def cli(*args):
    return subprocess.run([sys.executable, str(Path(frames.__file__)), *map(str, args)], capture_output=True, text=True)


@pytest.mark.parametrize("explicit", (False, True))
def test_recipe_repo_capture_commands_paths_and_mesh_subprocess(tmp_path, explicit):
    repo = tmp_path / "repo"
    save(repo / "input", smooth_sequence()[:1])
    (repo / "triangle.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 3 2\n")
    command = shlex.join([sys.executable, "-c", "import shutil; shutil.copytree('input', 'shots/{run}')"])
    recipe = {"captures": [
        {"name": "generated", "run": command, "frames": "shots/{run}", "stills": True, "baseline": "input"},
        {"name": "existing", "frames": "input", "stills": True}],
        "meshes": [{"run": shlex.join([sys.executable, "-c", "from pathlib import Path; Path('mesh-ran').touch()"]),
                    "files": ["triangle.obj"]}]}
    path = repo / ".glevels/frames.json"
    path.parent.mkdir()
    path.write_text(json.dumps(recipe))
    args = ["--recipe", path] if explicit else []
    result = cli("--repo", repo, *args, "--out", tmp_path / "out", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert [part["exit"] for part in data["parts"]] == [0, 0, 0]
    assert len(list((repo / "shots").iterdir())) == 1
    assert (repo / "mesh-ran").is_file()
    assert len({part["out"] for part in data["parts"]}) == 3
    for part in data["parts"]:
        assert (Path(part["out"]) / "findings.json").is_file()
    assert "meshcheck: 1 inputs" in (Path(data["parts"][-1]["out"]) / "stdout.txt").read_text()
    repeated = cli("--repo", repo, *args, "--out", tmp_path / "out2", "--json")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["run"] != data["run"]
    assert len(list((repo / "shots").iterdir())) == 2


@pytest.mark.parametrize("codes, expected", [([2, 2], 2), ([2, 0], 1), ([2, 1], 1), ([0, 0], 0), ([0, 3], 1)])
def test_recipe_exit_aggregation_and_failed_capture_skips_stale_frames(tmp_path, codes, expected):
    source = save(tmp_path / "input", smooth_sequence()[:1])
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps({"captures": [
        {"name": f"capture-{i}", "run": f"exit {code}", "frames": str(source), "stills": True}
        for i, code in enumerate(codes)]}))
    result = cli("--recipe", recipe, "--out", tmp_path / "out", "--json")
    assert result.returncode == expected, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert [p["exit"] for p in data["parts"]] == [2 if c == 2 else 1 if c else 0 for c in codes]
    for part, code in zip(data["parts"], codes):
        if code:
            assert not (Path(part["out"]) / "judge").exists()
            assert ("impossible" if code == 2 else "capture failed") in part["error"]
            failure = json.loads((Path(part["out"]) / "findings.json").read_text())
            assert bool(failure["findings"]) == (code != 2)


def test_recipe_keeps_running_after_analysis_is_impossible(tmp_path):
    source = save(tmp_path / "input", [np.zeros((24, 24, 3), dtype=np.uint8)])
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps({"captures": [
        {"name": "broken-first", "frames": str(source), "stills": True},
        {"name": "absent", "frames": str(tmp_path / "absent")},
        {"name": "broken", "frames": str(source), "stills": True}]}))
    result = cli("--recipe", path, "--out", tmp_path / "out", "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert [p["exit"] for p in data["parts"]] == [1, 2, 1]
    assert len(data["findings"]) == 2
    assert json.loads((tmp_path / "out/broken/findings.json").read_text())["findings"][0]["kind"] == "invalid-frame"


def test_recipe_print_limit_applies_across_captures(tmp_path):
    rgb = smooth_sequence()[0]
    rgb[40:44, 30:180] = 0
    source = save(tmp_path / "input", [rgb] * 15)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps({"captures": [
        {"name": name, "frames": str(source), "stills": True} for name in ("first", "second")]}))
    result = cli("--recipe", path, "--out", tmp_path / "out")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sum(" score=" in line for line in result.stdout.splitlines()) == 25
    data = json.loads((tmp_path / "out/findings.json").read_text())
    assert len(data["findings"]) == 30
    assert {f["part"] for f in data["findings"]} == {"first", "second"}
    assert [f["rank"] for f in data["findings"]] == list(range(1, 31))


def test_missing_and_empty_recipe_are_impossible(tmp_path):
    result = cli("--repo", tmp_path)
    assert result.returncode == 2
    assert result.stdout.strip() == f"impossible: no .glevels/frames.json in {tmp_path}: add a recipe that captures this product's frames"
    path = tmp_path / "empty.json"
    path.write_text('{"captures": [], "meshes": []}')
    assert cli("--recipe", path).returncode == 2


def test_recipe_browser_options_resolve_from_repo(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "move.mjs").write_text("export default async () => {}")
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps({"captures": [{"name": "web", "url": "https://example.com",
        "path": "move.mjs", "frames_n": 4, "fps": 12, "viewport": "240x144", "stills": True}]}))
    def capture(args, out):
        assert args.source == "https://example.com" and args.path == str(repo / "move.mjs")
        assert (args.frames, args.fps, args.viewport) == (4, 12, "240x144")
        return save(out / "frames", smooth_sequence()[:4])
    monkeypatch.setattr(frames, "capture", capture)
    monkeypatch.setattr(sys, "argv", ["frames.py", "--recipe", str(recipe), "--repo", str(repo),
                                     "--out", str(tmp_path / "out"), "--json"])
    assert frames.main() == 0
    assert json.loads(capsys.readouterr().out)["parts"][0]["exit"] == 0


def test_cli_exit_codes_and_json(tmp_path):
    clean = save(tmp_path / "clean", smooth_sequence()[:2])
    result = cli("--json", "--out", tmp_path / "out", clean)
    assert result.returncode == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == json.loads((tmp_path / "out/findings.json").read_text())
    broken = save(tmp_path / "broken", [np.zeros((24, 24, 3), dtype=np.uint8)])
    result = cli("--out", tmp_path / "broken-out", broken)
    assert result.returncode == 1
    assert "blocking (clear faults" in result.stdout and "invalid-frame" in result.stdout
    assert cli("--out", tmp_path / "empty", tmp_path / "absent").returncode == 2
    assert cli("--every", 0, clean).returncode == 2
    assert cli("--out", clean / "nested", clean).returncode == 2


def test_dependencies_and_memory_failure_exit_impossible(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["frames.py", "unused"])
    def missing():
        raise ImportError("numpy unavailable")
    monkeypatch.setattr(frames, "dependencies", missing)
    assert frames.main() == 2
    assert "impossible: numpy unavailable" in capsys.readouterr().out
    monkeypatch.setattr(frames, "dependencies", lambda: None)
    monkeypatch.setattr(frames, "prepare", lambda *args: (_ for _ in ()).throw(MemoryError("no memory")))
    monkeypatch.setattr(sys, "argv", ["frames.py", "--out", str(tmp_path / "out"), "unused"])
    assert frames.main() == 2
    assert "impossible: no memory" in capsys.readouterr().out


def test_low_memory_guard_prevents_browser_launch(tmp_path, monkeypatch):
    script = tmp_path / "path.mjs"
    script.write_text("export default async () => {}")
    monkeypatch.setattr(frames, "LOCK", tmp_path / "browser.lock")
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path: "MemAvailable: 500000 kB" if str(path) == "/proc/meminfo" else read_text(path))
    def launch(*args, **kwargs):
        pytest.fail("a browser was launched without enough memory")
    monkeypatch.setattr(subprocess, "run", launch)
    args = SimpleNamespace(path=str(script), source="https://example.com")
    with pytest.raises(ValueError, match="only 488 MB available"):
        frames.capture(args, tmp_path)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
def test_video_extracts_every_frame_without_dropping(tmp_path):
    directory = save(tmp_path / "input", smooth_sequence()[:4])
    video = tmp_path / "input.mp4"
    encoded = subprocess.run(["ffmpeg", "-v", "error", "-framerate", "12", "-i", str(directory / "%03d.png"),
                              "-c:v", "mpeg4", str(video)], capture_output=True, text=True)
    assert encoded.returncode == 0, encoded.stderr
    result = cli("--json", "--out", tmp_path / "out", video)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["sequences"][0]["frames"] == 4
    assert len(list((tmp_path / "out/frames").glob("*.png"))) == 4


def moving_manifest(directory):
    (directory / "frames.json").write_text(json.dumps({
        "camera": "moving", "frames": [p.name for p in frames.frame_files(directory)]}))
    return directory


def parallax_sequence(count=9):
    rng = np.random.default_rng(54)
    back = rng.integers(65, 155, (144, 240 + count * 4), dtype=np.uint8)
    front = rng.integers(65, 155, (72, 240 + count * 52), dtype=np.uint8)
    arrays = []
    for i in range(count):
        plane = back[:, i * 4:i * 4 + 240].copy()
        plane[48:120] = front[:, i * 52:i * 52 + 240]
        arrays.append(np.repeat(plane[:, :, None], 3, axis=2))
    return arrays


def test_moving_parallax_has_no_spikes_shimmer_or_lighting(tmp_path):
    source = moving_manifest(save(tmp_path / "input", parallax_sequence()))
    findings = run(source, tmp_path / "out")["findings"]
    assert not [f for f in findings if f["kind"] in {
        "single-frame-spike", "temporal-shimmer", "lighting-jump"}]


def test_moving_spike_requires_neighbours_to_agree(tmp_path):
    rng = np.random.default_rng(3)
    plane = rng.integers(70, 130, (144, 240), dtype=np.uint8)
    arrays = [np.repeat(plane[:, :, None], 3, axis=2) for _ in range(5)]
    arrays[2][48:96, 96:144] += 80
    source = moving_manifest(save(tmp_path / "spike", arrays))
    spikes = [f for f in run(source, tmp_path / "spike-out")["findings"]
              if f["kind"] == "single-frame-spike" and f["severity"] == "block"]
    assert len(spikes) == 1 and spikes[0]["frames"][1] == "002.png"
    arrays[3][48:96, 96:144] += 80
    arrays[4][48:96, 96:144] += 80
    source = moving_manifest(save(tmp_path / "step", arrays))
    assert not [f for f in run(source, tmp_path / "step-out")["findings"]
                if f["kind"] == "single-frame-spike"]
    for rgb in arrays[3:]:
        rgb[48:96, 96:144] -= 50
    source = moving_manifest(save(tmp_path / "occlusion", arrays))
    assert not [f for f in run(source, tmp_path / "occlusion-out")["findings"]
                if f["kind"] == "single-frame-spike"]


def test_moving_unexplained_motion_persistence_and_native_strips(tmp_path):
    rng = np.random.default_rng(91)
    plane = rng.integers(60, 180, (144, 240), dtype=np.uint8)
    arrays = []
    for _ in range(12):
        current = plane.copy()
        current[48:96, 96:168] = rng.integers(60, 180, (48, 72), dtype=np.uint8)
        arrays.append(np.repeat(current[:, :, None], 3, axis=2))
    source = moving_manifest(save(tmp_path / "input", arrays))
    findings = run(source, tmp_path / "out")["findings"]
    leads = [f for f in findings if f["kind"] == "unexplained-motion"]
    assert leads and all(f["severity"] == "review" for f in leads)
    for f in leads:
        assert f["box"] == [96, 48, 72, 48]
        assert "10 consecutive" in f["detail"]
        native = np.array(Image.open(f["crops"]["native"]))
        x, y, w, h = f["context_box"]
        for i, name in enumerate(f["frames"]):
            original = np.array(Image.open(source / name))
            assert np.array_equal(native[:, i * w:(i + 1) * w], original[y:y + h, x:x + w])
        x, y, w, h = f["box"]
        tight = np.concatenate([np.array(Image.open(source / p))[y:y+h, x:x+w] for p in f["frames"]], axis=1)
        assert np.array_equal(np.array(Image.open(f["crops"]["zoom"])),
                              np.repeat(np.repeat(tight, 3, axis=0), 3, axis=1))
    short = moving_manifest(save(tmp_path / "short", arrays[:4]))
    assert not [f for f in run(short, tmp_path / "short-out")["findings"] if f["kind"] == "unexplained-motion"]


def test_static_camera_never_calls_local_matcher(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail("static camera used local motion compensation")
    monkeypatch.setattr(frames, "motion_match", unexpected, raising=False)
    source = save(tmp_path / "input", smooth_sequence(faults=True))
    assert {f["kind"] for f in run(source, tmp_path / "out")["findings"]} == {
        "single-frame-spike", "temporal-shimmer", "lighting-jump"}


def test_unexplained_regions_require_consecutive_frames_and_rank_area():
    history = [np.zeros((4, 8), dtype=np.float32) for _ in range(14)]
    for i, residual in enumerate(history):
        residual[0, 0] = 30 if i % 2 else 0
        if i < 6:
            residual[1, 2] = 25
        if i >= 8:
            residual[1, 2] = 35
        residual[2:4, 5:8] = 20
    leads = list(frames.unexplained_motion(history, (96, 192)))
    assert len(leads) == 3
    assert sorted(finding[2] for _, finding in leads) == [
        [48, 24, 24, 24], [48, 24, 24, 24], [120, 48, 72, 48]]
    findings = frames.rank_findings([{"kind": f[0], "severity": f[1], "score": f[3]} for _, f in leads])
    assert findings[0]["score"] == 14 * 6 * frames.TILE ** 2


def test_motion_match_finds_sixty_four_pixels_and_keeps_native_dimensions():
    rng = np.random.default_rng(39)
    plane = rng.integers(60, 190, (152, 312)).astype(np.float32)
    previous, current = plane[:144, :240], plane[4:148, 64:304]
    aligned = frames.motion_match(previous, current)
    assert aligned.shape == current.shape
    assert np.array_equal(aligned[:120, :168], current[:120, :168])


def test_motion_match_does_not_exchange_flat_dark_ui_for_bright_background():
    previous = np.full((144, 240), 190, dtype=np.float32)
    previous[24:120, 24:120] = 25
    current = previous.copy()
    current[24:120, 24:32] += np.arange(96, dtype=np.float32)[:, None] / 200
    aligned = frames.motion_match(previous, current)
    assert float(np.abs(current[24:120, 24:48] - aligned[24:120, 24:48]).max()) < 1


@pytest.mark.parametrize("camera", ("static", "moving"))
def test_transient_pop_returns_after_three_frames(tmp_path, camera):
    rng = np.random.default_rng(22)
    plane = rng.integers(60, 180, (120, 192), dtype=np.uint8)
    arrays = [np.repeat(plane[:, :, None], 3, axis=2) for _ in range(16)]
    for rgb in arrays[6:9]:
        rgb[24:96, 72:144] = 210
    source = save(tmp_path / "input", arrays)
    if camera == "moving":
        moving_manifest(source)
    pops = [f for f in run(source, tmp_path / "out")["findings"] if f["kind"] == "transient-pop"]
    assert pops
    assert any(f["frame_range"] == ["006.png", "008.png"] and f["box"] == [72, 24, 72, 72] for f in pops)
    assert all(f["severity"] == "review" for f in pops)


@pytest.mark.parametrize("change", ("steady-motion", "persistent"))
def test_transient_pop_rejects_motion_and_persistent_change(tmp_path, change):
    rng = np.random.default_rng(23)
    plane = rng.integers(80, 125, (120, 192), dtype=np.uint8)
    patch = rng.integers(155, 230, (48, 48), dtype=np.uint8)
    arrays = []
    for i in range(18):
        current = plane.copy()
        if change == "steady-motion":
            current[24:72, 8 + i * 6:56 + i * 6] = patch
        elif i < 6:
            current[24:72, 72:120] = patch
        arrays.append(np.repeat(current[:, :, None], 3, axis=2))
    source = moving_manifest(save(tmp_path / "input", arrays))
    assert not [f for f in run(source, tmp_path / "out")["findings"] if f["kind"] == "transient-pop"]


@pytest.mark.parametrize("camera", ("static", "moving"))
def test_transient_pop_at_sequence_start(tmp_path, camera):
    rng = np.random.default_rng(24)
    plane = rng.integers(60, 180, (96, 168), dtype=np.uint8)
    arrays = [np.repeat(plane[:, :, None], 3, axis=2) for _ in range(12)]
    for rgb in arrays[:5]:
        rgb[24:72, 48:120] = 210
    source = save(tmp_path / "input", arrays)
    if camera == "moving":
        moving_manifest(source)
    pops = [f for f in run(source, tmp_path / "out")["findings"] if f["kind"] == "transient-pop"]
    assert any(f["frame_range"] == ["000.png", "004.png"] and f["box"] == [48, 24, 72, 48] for f in pops)


@pytest.mark.parametrize("duration", (1, 8, 9))
def test_transient_duration_limit_and_single_frame_deduplication(tmp_path, duration):
    rng = np.random.default_rng(25)
    plane = rng.integers(60, 180, (96, 168), dtype=np.uint8)
    arrays = [np.repeat(plane[:, :, None], 3, axis=2) for _ in range(22)]
    for rgb in arrays[8:8 + duration]:
        rgb[24:72, 48:120] = 210
    source = save(tmp_path / "input", arrays)
    findings = run(source, tmp_path / "out")["findings"]
    pops = [f for f in findings if f["kind"] == "transient-pop"]
    if duration == 8:
        assert any(f["frame_range"] == ["008.png", "015.png"] for f in pops)
    else:
        assert pops == []
    if duration == 1:
        assert any(f["kind"] == "single-frame-spike" for f in findings)
