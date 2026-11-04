import json
import struct
import zlib

from glevels import contentpiece


def png(path, w, h):
    raw = b"".join(b"\x00" + b"\x00" * w * 3 for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def piece(tmp_path, **over):
    d = tmp_path / "pieces" / "bot-visits-chart"
    d.mkdir(parents=True)
    png(d / "main.png", 16, 9)
    (d / "recipe.md").write_text("Step by step. " * 30)
    p = {"id": "bot-visits-chart", "title": "Bot visits by engine", "type": "data-graphic", "format": "image",
         "files": ["main.png"], "aspect": "16:9", "summary": "A chart of AI bot visits for LinkedIn.",
         "recipe": {"brief": "Make a data graphic of real bot visits by engine for a LinkedIn post.",
                    "instructions": "recipe.md", "agents": [{"role": "maker", "model": "gpt-6.1-sol"}],
                    "tools": ["Playwright"], "generated_pixels": False, "cost_usd": 0, "wall_minutes": 9, "attempts": 1},
         "gates": {"correct.content.recipe-recorded": {"result": "met", "note": ""}},
         "created": "2026-11-04T12:00:00Z"}
    p.update(over)
    (d / "piece.json").write_text(json.dumps(p))
    return d


def test_clean_piece_passes(tmp_path):
    piece(tmp_path)
    assert contentpiece.main(["x", str(tmp_path)]) == 0


def test_empty_brief_fails(tmp_path):
    d = piece(tmp_path)
    p = json.loads((d / "piece.json").read_text())
    p["recipe"]["brief"] = ""
    (d / "piece.json").write_text(json.dumps(p))
    assert any("brief" in f for f in contentpiece.check_piece(d))


def test_missing_file_and_em_dash_fail(tmp_path):
    d = piece(tmp_path, files=["main.png", "gone.mp4"], title="Bots — by engine")
    faults = contentpiece.check_piece(d)
    assert any("gone.mp4" in f for f in faults) and any("em dash" in f for f in faults)


def test_video_needs_poster_and_status_matches_score(tmp_path):
    d = piece(tmp_path, format="video", judge={"score": 5, "notes": "flat"}, status="candidate")
    faults = contentpiece.check_piece(d)
    assert any("poster" in f for f in faults) and any("disagrees" in f for f in faults)
