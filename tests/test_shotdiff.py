import pytest

Image = pytest.importorskip("PIL.Image")

from glevels import shotdiff  # noqa: E402


def png(path, size=(10, 10), colour=(255, 255, 255, 255), dots=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", size, colour)
    for xy, c in dots:
        img.putpixel(xy, c)
    img.save(path)


def test_reports_share_size_and_missing_files_and_exits_zero(tmp_path, capsys):
    before, after = tmp_path / "before", tmp_path / "after"
    png(before / "iphone/home-light.png")
    png(after / "iphone/home-light.png")
    png(before / "mac/inbox-dark.png")
    png(after / "mac/inbox-dark.png", dots=[((0, 0), (254, 255, 255, 255)), ((1, 0), (0, 0, 0, 255))])
    png(before / "web/settings.png")
    png(after / "web/settings.png", size=(10, 12))
    png(before / "web/gone.png")
    png(after / "web/added.png")
    code = shotdiff.main([str(before), str(after)])
    out = capsys.readouterr().out
    assert code == 0
    lines = {line.split()[0]: " ".join(line.split()[1:]) for line in out.splitlines()}
    assert lines["iphone/home-light.png"] == "0.0000% changed (0 of 100 px), largest channel change 0"
    assert lines["mac/inbox-dark.png"] == "2.0000% changed (2 of 100 px), largest channel change 255"
    assert lines["web/settings.png"] == "size differs: 10x10 before, 10x12 after"
    assert "missing after: web/gone.png" in out and "new after:     web/added.png" in out
    assert "compared 3: 1 identical, 1 changed, 1 size differs, 0 unreadable; missing after 1, new after 1" in out


def test_one_level_change_and_alpha_count(tmp_path, capsys):
    before, after = tmp_path / "b", tmp_path / "a"
    png(before / "x.png", colour=(10, 10, 10, 255))
    png(after / "x.png", colour=(10, 10, 10, 255), dots=[((5, 5), (10, 10, 10, 254))])
    assert shotdiff.main([str(before), str(after)]) == 0
    assert "1.0000% changed (1 of 100 px), largest channel change 1" in capsys.readouterr().out


def test_unreadable_file_is_reported_not_fatal(tmp_path, capsys):
    before, after = tmp_path / "b", tmp_path / "a"
    png(before / "x.png")
    after.mkdir()
    (after / "x.png").write_bytes(b"\x89PNG not really")
    assert shotdiff.main([str(before), str(after)]) == 0
    assert "x.png  unreadable:" in capsys.readouterr().out


def test_cannot_run_without_two_directories_of_pngs(tmp_path, capsys):
    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()
    assert shotdiff.main([str(tmp_path / "empty1"), str(tmp_path / "empty2")]) == 2
    assert shotdiff.main([str(tmp_path / "empty1"), str(tmp_path / "missing")]) == 2
    assert shotdiff.main([str(tmp_path / "empty1")]) == 2
    assert shotdiff.main(["", ""]) == 2
