"""Sensor for correct.content.recipe-recorded: every content-lab piece carries a piece.json that
matches the lab's CONTRACT.md, the media it names, a recipe.md and a non-empty brief.

    python3 contentpiece.py <lab root | pieces dir | one piece dir>

Exit 0 when every piece passes, 1 with one line per fault, 2 when there is nothing to check.
Stdlib only: image sizes are read from the file headers."""
import json
import re
import struct
import sys
from pathlib import Path

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FORMATS = {"image", "video", "svg", "html"}
RESULTS = {"met", "attempted", "waived", "impossible"}
STATUSES = {"candidate", "rejected"}
BANNED = {"—": "em dash", "·": "middle dot", "&mdash;": "em dash", "&middot;": "middle dot"}
MAX_EDGE = 4096
MAX_VIDEO = 30 * 1024 * 1024
NETWORK = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import\s+["']?(?:https?:)?//""", re.I)


def image_size(p):
    b = p.read_bytes()[:65536] if p.stat().st_size > 65536 else p.read_bytes()
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        c = b[12:16]
        if c == b"VP8X":
            return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if c == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if c == b"VP8L":
            v = int.from_bytes(b[21:25], "little")
            return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
    if b[:2] == b"\xff\xd8":
        data = p.read_bytes()
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def glyphs(text, where, faults):
    for g, name in BANNED.items():
        if g in text:
            faults.append(f"{where} has a {name}")


def check_piece(d):
    f = []
    pj = d / "piece.json"
    if not pj.is_file():
        return [f"{d.name}: no piece.json"]
    try:
        p = json.loads(pj.read_text())
    except ValueError as e:
        return [f"{d.name}: piece.json is not valid JSON ({e})"]
    req = ["id", "title", "type", "format", "files", "aspect", "summary", "recipe", "gates", "created"]
    for k in req:
        if k not in p:
            f.append(f"missing field {k}")
    if p.get("id") != d.name:
        f.append(f"id {p.get('id')!r} does not match folder {d.name!r}")
    if not KEBAB.match(str(p.get("id", ""))):
        f.append("id is not kebab-case")
    for k in ("title", "summary"):
        v = p.get(k)
        if not isinstance(v, str) or not v.strip():
            f.append(f"{k} is empty")
        else:
            if not v.strip()[0].isupper():
                f.append(f"{k} does not start with a capital")
            glyphs(v, k, f)
    if not isinstance(p.get("type"), str) or not KEBAB.match(p.get("type", "")):
        f.append("type must be a kebab-case string")
    fmt = p.get("format")
    if fmt not in FORMATS:
        f.append(f"format {fmt!r} not one of {sorted(FORMATS)}")
    if not isinstance(p.get("aspect"), str) or not re.match(r"^\d+(\.\d+)?:\d+(\.\d+)?$", p.get("aspect", "")):
        f.append("aspect must look like 16:9")
    files = p.get("files")
    if not isinstance(files, list) or not files:
        f.append("files must be a non-empty list")
        files = []
    media = list(files)
    if fmt in ("video", "html"):
        if not p.get("poster"):
            f.append(f"a {fmt} piece needs a poster")
        else:
            media.append(p["poster"])
    for name in media:
        mp = (d / str(name)).resolve()
        try:
            mp.relative_to(d.resolve())
        except ValueError:
            f.append(f"file {name} escapes the piece folder")
            continue
        if not mp.is_file():
            f.append(f"file {name} does not exist")
            continue
        ext = mp.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            size = image_size(mp)
            if size is None:
                f.append(f"{name}: cannot read image size")
            elif max(size) > MAX_EDGE:
                f.append(f"{name}: {size[0]}x{size[1]} exceeds {MAX_EDGE}px long edge")
        elif ext in (".gif", ".bmp", ".tif", ".tiff"):
            f.append(f"{name}: images must be PNG, WebP or JPG")
        elif ext == ".mp4" and mp.stat().st_size > MAX_VIDEO:
            f.append(f"{name}: {mp.stat().st_size // 1048576} MB exceeds 30 MB")
        elif ext in (".html", ".htm") and fmt == "html":
            if NETWORK.search(mp.read_text(errors="replace")):
                f.append(f"{name}: loads from the network, html pieces must be self-contained")
    if fmt == "video" and files and not any(str(x).lower().endswith(".mp4") for x in files):
        f.append("a video piece needs an MP4 in files")
    r = p.get("recipe") if isinstance(p.get("recipe"), dict) else {}
    if not r:
        f.append("recipe must be an object")
    else:
        if not isinstance(r.get("brief"), str) or len(r.get("brief", "").strip()) < 40:
            f.append("recipe.brief is empty or too short to be the verbatim brief")
        instr = r.get("instructions")
        if not instr or not (d / str(instr)).is_file():
            f.append(f"recipe.instructions {instr!r} does not exist")
        elif len((d / str(instr)).read_text(errors="replace").strip()) < 200:
            f.append(f"{instr} is too short to remake the piece from")
        if not (d / "recipe.md").is_file():
            f.append("no recipe.md")
        agents = r.get("agents")
        if not isinstance(agents, list) or not agents or not all(isinstance(a, dict) and a.get("role") and a.get("model") for a in agents):
            f.append("recipe.agents must list {role, model} objects")
        if not isinstance(r.get("tools"), list) or not r.get("tools"):
            f.append("recipe.tools must be a non-empty list")
        if not isinstance(r.get("generated_pixels"), bool):
            f.append("recipe.generated_pixels must be true or false")
        for k in ("cost_usd", "wall_minutes"):
            if not isinstance(r.get(k), (int, float)) or isinstance(r.get(k), bool) or r.get(k) < 0:
                f.append(f"recipe.{k} must be a non-negative number")
        if not isinstance(r.get("attempts"), int) or r.get("attempts", 0) < 1:
            f.append("recipe.attempts must be a positive integer")
    g = p.get("gates")
    if not isinstance(g, dict):
        f.append("gates must be an object")
    else:
        for gid, v in g.items():
            if not isinstance(v, dict) or v.get("result") not in RESULTS:
                f.append(f"gates.{gid}.result must be one of {sorted(RESULTS)}")
    j = p.get("judge")
    if j is not None:
        s = j.get("score") if isinstance(j, dict) else None
        if not isinstance(s, (int, float)) or isinstance(s, bool) or not 1 <= s <= 10:
            f.append("judge.score must be 1 to 10")
        elif "status" in p:
            want = "candidate" if s >= 7 else "rejected"
            if p["status"] != want:
                f.append(f"status {p['status']!r} disagrees with judge score {s} (want {want})")
        if isinstance(j, dict) and not str(j.get("notes", "")).strip():
            f.append("judge.notes is empty")
    if "status" in p and p["status"] not in STATUSES:
        f.append(f"status must be one of {sorted(STATUSES)}")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", str(p.get("created", ""))):
        f.append("created must be an ISO 8601 timestamp with a zone")
    return [f"{d.name}: {x}" for x in f]


def pieces_in(path):
    path = Path(path).resolve()
    if (path / "piece.json").is_file():
        return [path]
    base = path / "pieces" if (path / "pieces").is_dir() else path
    return sorted(x for x in base.iterdir() if x.is_dir() and not x.name.startswith((".", "_")))


def main(argv):
    if len(argv) != 2 or not Path(argv[1]).is_dir():
        print("usage: contentpiece.py <lab root | pieces dir | piece dir>")
        return 2
    ps = pieces_in(argv[1])
    if not ps:
        print("no pieces to check")
        return 2
    faults = [x for d in ps for x in check_piece(d)]
    for x in faults:
        print(x)
    print(f"{len(ps)} pieces, {len(faults)} faults")
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
