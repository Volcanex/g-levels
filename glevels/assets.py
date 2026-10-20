"""Sensor: real, sourced visuals. Mechanical half of correct.assets.real-sourced.

    python3 assets.py [--manifest FILE] [--src DIR] [--out DIR] [--json]
                      [--min WxH] TARGET [TARGET ...]

Targets are URLs, HTML files, fresh g-site pages (resolved by spacing), or images.
Desktop 1440x900 and phone 390x844 share the host browser lock and memory guard.
Exit 0: no clear faults; 1: blocking provenance, placeholder, loading or size faults;
2: impossible to run. Shape, emoji and source-scan leads need a separate judge.
Each visual has its own crop; rasters also retain their original bytes. Stripped
metadata proves nothing: the judged half covers visuals that read as AI.
Data/blob assets use data:<media type>:<12-digit sha256> or blob:<type>:<hash>;
inline SVG and canvas use inline:svg:<hash> and inline:canvas:<hash> in manifests.
"""
import argparse
import fcntl
import fnmatch
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import zlib
from pathlib import Path

if __package__:
    from . import spacing, vendor
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from glevels import spacing, vendor

HERE = Path(__file__).resolve().parent
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}
LICENCES = {"CC0-1.0", "OFL-1.1", "MIT", "Apache-2.0", "ISC", "CC-BY-4.0", "own", "client-owned", "licensed"}
KINDS = {"photo", "icon", "mascot", "texture", "figure", "font", "model", "hdri", "splat", "illustration", "logo", "video", "banner"}
METHODS = {"captured", "crafted", "pipeline", "library", "sourced", "generated"}
GENERATORS = re.compile(r"OpenAI|DALL[ -]?E|gpt-image|ChatGPT|Codex|Midjourney|Stable[ _-]Diffusion|ComfyUI|Automatic1111|Firefly|Imagen|Gemini|Ideogram|\bFlux\b|Leonardo|Recraft", re.I)
HOUSE_LIBRARIES = (
    (r"(?:^|/)packages/g-tokens/fonts/[^/]+$", "OFL-1.1"),
    (r"(?:^|/)(?:assets/)?fonts/(?:InterVariable|SpaceGrotesk-Variable|JetBrainsMono-Variable)\.woff2$", "OFL-1.1"),
    (r"(?:^|/)packages/g-icons/", "MIT"),
    (r"(?:^|/)packages/g-brand/(?:mark|icon)\.svg$", "MIT"),
    (r"(?:^|/)(?:node_modules/)?@phosphor-icons/", "MIT"),
    (r"^https://cdn\.jsdelivr\.net/npm/@phosphor-icons/", "MIT"),
)
PLACEHOLDERS = {"placehold.co", "via.placeholder.com", "placeholder.com", "dummyimage.com", "picsum.photos", "loremflickr.com", "placekitten.com", "placebear.com", "fakeimg.pl", "source.unsplash.com", "i.pravatar.cc", "ui-avatars.com", "robohash.org", "api.dicebear.com"}
PLACEHOLDER_NAME = re.compile(r"(?:^|[^a-z])(?:placeholder|lorem|dummy|sample|image[-_ ]here|todo)(?:[^a-z]|$)", re.I)


def exif_text(data):
    data = data.removeprefix(b"Exif\0\0")
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return []
    endian = "<" if data[:2] == b"II" else ">"
    out, seen = [], set()
    try:
        offsets = [struct.unpack_from(endian + "I", data, 4)[0]]
        while offsets and len(seen) < 16:
            pos = offsets.pop()
            if pos in seen:
                continue
            seen.add(pos)
            count = struct.unpack_from(endian + "H", data, pos)[0]
            for i in range(min(count, 1024)):
                p = pos + 2 + i * 12
                tag, typ, n = struct.unpack_from(endian + "HHI", data, p)
                value = struct.unpack_from(endian + "I", data, p + 8)[0]
                if tag in (0x8769, 0x8825):
                    offsets.append(value)
                if typ == 2 and tag in (0x131, 0x10E, 0x9C9D):
                    raw = data[p + 8:p + 8 + n] if n <= 4 else data[value:value + n]
                    out.append(("EXIF Software" if tag == 0x131 else "EXIF description", raw.rstrip(b"\0").decode("utf-8", "replace")))
    except (struct.error, IndexError):
        pass
    return out


def metadata(data):
    """Read provenance packets, without treating compressed picture pixels as metadata."""
    packets, dimensions = [], None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos = 8
        while pos + 12 <= len(data):
            n = int.from_bytes(data[pos:pos + 4], "big")
            kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
            if len(body) != n:
                break
            if kind == b"IHDR" and n >= 8:
                dimensions = list(struct.unpack(">II", body[:8]))
            try:
                if kind == b"tEXt":
                    key, text = body.split(b"\0", 1)
                    packets.append(("PNG " + key.decode("latin1"), text.decode("latin1")))
                elif kind == b"zTXt":
                    key, text = body.split(b"\0", 1)
                    packets.append(("PNG " + key.decode("latin1"), zlib.decompress(text[1:]).decode("utf-8", "replace")))
                elif kind == b"iTXt":
                    key, text = body.split(b"\0", 1)
                    compressed, method, rest = text[0], text[1], text[2:]
                    language, translated, text = rest.split(b"\0", 2)
                    if compressed:
                        text = zlib.decompress(text)
                    packets.append(("PNG " + key.decode("latin1"), text.decode("utf-8", "replace")))
                elif kind == b"eXIf":
                    packets.extend(exif_text(body))
            except (ValueError, IndexError, zlib.error):
                pass
            pos += n + 12
    elif data.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 4 <= len(data) and data[pos] == 255:
            marker = data[pos + 1]
            if marker in (0xDA, 0xD9):
                break
            if marker == 255:
                pos += 1
                continue
            n = int.from_bytes(data[pos + 2:pos + 4], "big")
            if n < 2:
                break
            body = data[pos + 4:pos + 2 + n]
            if marker == 0xE1:
                if body.startswith(b"Exif\0\0"):
                    packets.extend(exif_text(body))
                else:
                    packets.append(("JPEG XMP", body.decode("utf-8", "replace")))
            if marker in (0xC0, 0xC1, 0xC2) and len(body) >= 5:
                h, w = struct.unpack(">HH", body[1:5])
                dimensions = [w, h]
            pos += n + 2
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        pos = 12
        while pos + 8 <= len(data):
            kind = data[pos:pos + 4]
            n = int.from_bytes(data[pos + 4:pos + 8], "little")
            body = data[pos + 8:pos + 8 + n]
            if kind == b"EXIF":
                packets.extend(exif_text(body))
            elif kind == b"XMP ":
                packets.append(("WebP XMP", body.decode("utf-8", "replace")))
            elif kind == b"VP8X" and len(body) >= 10:
                dimensions = [int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1]
            pos += n + 8 + n % 2
    hits = []
    for label, text in packets:
        if "trainedAlgorithmicMedia" in text or "compositeWithTrainedAlgorithmicMedia" in text:
            hits.append(label + ": XMP DigitalSourceType trained algorithmic media")
        names = sorted(set(m.group() for m in GENERATORS.finditer(text)))
        if names:
            hits.append(label + ": " + ", ".join(names))
        if label.lower() == "png parameters":
            hits.append("PNG parameters: Automatic1111 / diffusion parameters")
    c2pa = b"jumb" in data or b"c2pa" in data
    if c2pa:
        for match in re.finditer(rb"jumb|c2pa", data):
            text = data[max(0, match.start() - 64):match.start() + 32768].decode("utf-8", "replace")
            names = sorted(set(m.group() for m in GENERATORS.finditer(text)))
            if names:
                hits.append("C2PA/JUMBF generator claim: " + ", ".join(names))
    return {"generated": bool(hits), "evidence": list(dict.fromkeys(hits)), "c2pa": c2pa,
            "dimensions": dimensions, "packets": [{"field": k, "value": v[:500]} for k, v in packets]}


def find_manifest(target, explicit=None, src=None):
    if explicit:
        return Path(explicit).resolve()
    start = Path(src).resolve() if src else None
    if not str(target).startswith(("http://", "https://")):
        start = Path(target).resolve().parent
    if not start:
        return None
    for directory in (start, *start.parents):
        if (directory / "assets.toml").is_file():
            return directory / "assets.toml"
        if (directory / ".git").exists():
            break
    return None


def manifest_errors(entries):
    errors = []
    for i, entry in enumerate(entries, 1):
        tag = f"asset[{i}] {entry.get('match', '(no match)')}"
        if not entry.get("match") or not isinstance(entry.get("match"), str):
            errors.append(tag + ": missing match")
        if not isinstance(entry.get("kind"), str) or entry["kind"] not in KINDS:
            errors.append(tag + ": unknown kind")
        if not isinstance(entry.get("method"), str) or entry["method"] not in METHODS:
            errors.append(tag + ": unknown method")
        if not isinstance(entry.get("licence"), str) or entry["licence"] not in LICENCES:
            errors.append(tag + ": unknown licence")
        if not isinstance(entry.get("source"), str) or not entry["source"].strip():
            errors.append(tag + ": no source")
        if str(entry.get("licence", "")).startswith("CC-BY") and not (isinstance(entry.get("credit"), str) and entry["credit"].strip()):
            errors.append(tag + ": CC-BY needs credit")
        if entry.get("method") == "generated" and not approved(entry):
            errors.append(tag + ": generated asset needs approval for mood board or store art; icons, logos, mascots, illustrations and figures are not approved roles")
    return errors


def approved(entry):
    return bool(entry and entry.get("method") == "generated" and entry.get("approved_role") in ("mood board", "store art")
                and entry.get("kind") not in ("icon", "logo", "mascot", "illustration", "figure"))


def candidates(asset, target, manifest):
    url = asset["url"]
    parsed = urllib.parse.urlsplit(url)
    values = [url, urllib.parse.unquote(parsed.path).lstrip("/"), asset.get("key", "")]
    if not str(target).startswith(("http://", "https://")) and parsed.scheme in ("http", "https"):
        page = urllib.parse.urlsplit(asset.get("page_url", ""))
        relative = os.path.relpath(urllib.parse.unquote(parsed.path), str(Path(urllib.parse.unquote(page.path)).parent)) if page.path else urllib.parse.unquote(parsed.path).lstrip("/")
        local = (Path(target).resolve().parent / relative).resolve()
        values.append(str(local))
        if manifest:
            values.append(os.path.relpath(local, manifest.parent))
        for parent in Path(target).resolve().parents:
            if (parent / ".git").exists():
                values.append(os.path.relpath(local, parent))
                break
    return values


def covering(asset, target, manifest, entries):
    values = candidates(asset, target, manifest)
    for entry in entries:
        match = entry.get("match", "")
        if isinstance(match, str) and match and any(fnmatch.fnmatchcase(v, match) or
                (match.startswith(("http://", "https://")) and v.startswith(match)) for v in values):
            return entry
    for pattern, licence in HOUSE_LIBRARIES:
        if any(re.search(pattern, v) for v in values):
            return {"kind": "font" if licence == "OFL-1.1" else "icon", "method": "library", "source": "g-design / Phosphor", "licence": licence}
    if asset.get("house"):
        return {"kind": "icon", "method": "library", "source": "g-design icon glyph", "licence": "MIT"}
    return None


def scan_source(directory):
    root = Path(directory).resolve()
    sites, loads = [], 0
    skipped = {"node_modules", "dist", "build", "vendor", ".git", "test", "tests", "__tests__"}
    primitive = re.compile(r"\b(?:Box|Sphere|Circle|Cylinder|Cone|Plane)Geometry\s*\(")
    real = re.compile(r"\b(?:TextureLoader|KTX2Loader|GLTFLoader|RGBELoader|EXRLoader)\s*\(|@sparkjsdev/spark|\b\w*Splat\w*Loader\s*\(|[\w./-]+\.(?:hdr|exr)\b|\b(?:map|normalMap|roughnessMap|aoMap)\s*[:=]")
    for base, directories, files in os.walk(root):
        directories[:] = sorted(d for d in directories if d not in skipped)
        for name in sorted(files):
            f = Path(base) / name
            if f.suffix.lower() not in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".glsl") or re.search(r"\.(?:test|spec|min)\.|[-.][0-9a-f]{8,}\.m?js$", name):
                continue
            raw = f.read_text(errors="replace")
            text = re.sub(r"/\*.*?\*/|(?m:^\s*//[^\n]*)", lambda m: "\n" * m.group().count("\n"), raw, flags=re.S)
            loads += len(list(real.finditer(text)))
            for match in primitive.finditer(text):
                nearby = text[max(0, match.start() - 400):match.end() + 700]
                if not re.search(r"\b(?:map|normalMap|roughnessMap|aoMap)\s*[:=]", nearby):
                    line = text.count("\n", 0, match.start()) + 1
                    site = {"file": str(f.relative_to(root)), "line": line, "sample": match.group()}
                    # the author's own comment calls it a stand-in: a placeholder, not a judgement call
                    said = re.search(r"\b(?:stand-?in|placeholder)\b", "\n".join(raw.splitlines()[max(0, line - 25):line]), re.I)
                    if said:
                        site["placeholder"] = said.group()
                    sites.append(site)
            for match in re.finditer(r"\bcreateImageBitmap\s*\(|\.decode\s*\(", text):
                nearby = text[max(0, match.start() - 400):match.end() + 400]
                if re.search(r"\.(?:png|jpe?g|webp|avif)\b|\bphoto\b", nearby, re.I):
                    loads += 1
            for match in re.finditer(r"\.(?:arc|fillRect)\s*\(|\bfunction\s+draw(?:Face|Tree|Car)\s*\(|\b(?:const|let)\s+draw(?:Face|Tree|Car)\s*=", text, re.I):
                nearby = text[max(0, match.start() - 500):match.end() + 500]
                helper = "draw" in match.group().lower()
                if re.search(r"particle|sprite|draw(?:Face|Tree|Car)", nearby, re.I) and (not helper or re.search(r"\.(?:arc|fillRect|moveTo|lineTo|ellipse)\s*\(", nearby)):
                    sites.append({"file": str(f.relative_to(root)), "line": text.count("\n", 0, match.start()) + 1, "sample": match.group()})
    stand_ins = [s for s in sites if s.get("placeholder")]
    return {"root": str(root), "primitives": len(sites), "real_materials": loads, "sites": (stand_ins + [s for s in sites if not s.get("placeholder")])[:10],
            "placeholders": stand_ins}


def finding(severity, check, detail, asset=None, **extra):
    asset = asset or {}
    return dict(severity=severity, check=check, detail=detail, selector=asset.get("selector", "page"),
                sample=asset.get("sample", ""), rect=asset.get("rect"), viewport=asset.get("viewport", "all"), **extra)


def impeccable(results, targets, urls):
    leads, noted = [], False
    for i, url in enumerate(urls):
        render = next(r for r in results if r["target_index"] == i and r["viewport"].startswith("desktop"))
        try:
            if spacing.free_mb() < spacing.MIN_FREE_MB:
                found, why = None, "not enough free memory for the optional browser"
            else:
                found, why = vendor.impeccable(url, "1440x900", scope=None)
        except Exception as e:
            found, why = None, f"{type(e).__name__} while running the optional detector"
        if why and not noted:
            leads.append(finding("review", "impeccable", "impeccable: did not run: " + why,
                                 target=targets[i]))
            noted = True
        for f in found or []:
            if f.get("antipattern") not in IMPECCABLE_IDS:
                continue
            snippet = str(f.get("snippet", ""))
            matches = [asset for asset in render["assets"] if asset["selector"] == f.get("selector") or
                       (len(asset.get("sample", "")) > 3 and asset["sample"].lower() in snippet.lower())]
            element = matches[0] if len(matches) == 1 else {}
            leads.append(finding("review", "impeccable", f"impeccable: {f.get('name', f['antipattern'])}: {snippet}",
                                 {"selector": f.get("selector") or element.get("selector", "html"),
                                  "sample": element.get("sample") or snippet[:60],
                                  "rect": f.get("rect") or element.get("rect") or [0, 0, 1440, render["pageHeight"]],
                                  "viewport": "desktop 1440x900"}, target=targets[i], antipattern=f["antipattern"]))
    return leads


def analyse(results, targets, manifest_arg=None, src=None, minimum=None):
    findings, manifests, unsourced = [], {}, {}
    for i, target in enumerate(targets):
        manifest = find_manifest(target, manifest_arg, src)
        entries = []
        if manifest:
            try:
                entries = tomllib.loads(manifest.read_text()).get("asset", [])
                if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                    raise ValueError("asset must be an array of tables")
            except (OSError, ValueError) as e:
                findings.append(finding("block", "manifest", f"{manifest}: {e}", target=target))
                entries = []
            if manifest not in manifests:
                findings.extend(finding("block", "manifest", e, target=target) for e in manifest_errors(entries))
                manifests[manifest] = True
        for render in (r for r in results if r["target_index"] == i):
            render["target"] = target
            for f in render.get("findings", []):
                findings.append(dict(f, target=target, viewport=render["viewport"]))
            for asset in render.get("assets", []):
                asset["viewport"] = render["viewport"]
                asset["page_url"] = render["url"]
                entry = covering(asset, target, manifest, entries)
                asset["manifest"] = entry
                if not entry:
                    if manifest:
                        findings.append(finding("block", "unsourced", "unsourced: " + asset["url"], asset, target=target))
                    else:
                        unsourced.setdefault(target, {}).setdefault(asset.get("key", asset["url"]), asset)
                host = urllib.parse.urlsplit(asset["url"]).hostname
                if host in PLACEHOLDERS or PLACEHOLDER_NAME.search(urllib.parse.unquote(urllib.parse.urlsplit(asset["url"]).path).rsplit("/", 1)[-1]) or PLACEHOLDER_NAME.search(asset.get("alt", "")):
                    findings.append(finding("block", "placeholder", "placeholder source: " + asset["url"], asset, target=target))
                original = asset.get("original")
                if original and Path(original).suffix.lower() != ".svg":
                    info = metadata(Path(original).read_bytes())
                    asset["metadata"] = info
                    if info["generated"] and not approved(entry):
                        findings.append(finding("block", "generated", "generated provenance: " + "; ".join(info["evidence"]), asset, target=target))
                if minimum and asset.get("natural") and Path(str(target)).suffix.lower() in IMAGES:
                    w, h = asset["natural"]
                    if w < minimum[0] or h < minimum[1]:
                        findings.append(finding("block", "minimum-size", f"{w}x{h}, below required {minimum[0]}x{minimum[1]}", asset, target=target))
    for target, assets in unsourced.items():
        findings.append(finding("block", "no-manifest", f"no assets.toml: {len(assets)} assets with no provenance", target=target,
                                assets=[dict(url=a["url"], selector=a.get("selector", "request"), sample=a.get("sample", ""), rect=a.get("rect"), viewport=a.get("viewport")) for a in assets.values()]))
    return findings


def report(data, out_dir):
    fs = data["findings"]
    blocks = [f for f in fs if f["severity"] == "block"]
    leads = [f for f in fs if f["severity"] == "review"]
    assets = {(r["target"], a.get("key", a["url"])) for r in data["renders"] for a in r.get("assets", [])}
    print(f"assets: {len(assets)} assets, {len(blocks)} blocking, {len(leads)} for the reviewer")
    for title, group in (("blocking (clear faults, fix these)", blocks), ("for the reviewer (leads, judge them against the crops)", leads)):
        if group:
            print("\n" + title + ":")
            for f in group:
                where = f" at {f['rect']} page px" if f.get("rect") else " (no page box)"
                print(f"  {f['target']}  {f['viewport']}  {f['selector']} \"{f['sample']}\"{where}: {f['detail']}")
                for a in f.get("assets", []):
                    print(f"    {a['url']} — {a['selector']} \"{a['sample']}\" at {a['rect']} page px, {a['viewport']}")
    print("\nasset crops, for the judge:")
    for r in data["renders"]:
        for a in r.get("assets", []):
            if not a.get("crop") and not a.get("original"):
                continue
            entry = a.get("manifest")
            provenance = json.dumps(entry, ensure_ascii=False) if entry else "no provenance"
            natural = f", natural {a['natural'][0]}x{a['natural'][1]}" if a.get("natural") else ""
            meta = a.get("metadata", {})
            evidence = "; ".join(meta.get("evidence", [])) or ("C2PA/JUMBF present" if meta.get("c2pa") else "no generator metadata; stripped metadata proves nothing")
            print(f"  {r['target']} {r['viewport']} {a.get('selector', 'request')} \"{a.get('sample', '')}\" at {a.get('rect')} page px{natural}: {a.get('crop', '(below crop size or not visible)')}")
            print(f"    {provenance}; {evidence}" + (f"; original {a['original']}" if a.get("original") else ""))
    print(f"\nfindings: {out_dir}/findings.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--manifest")
    ap.add_argument("--src")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min", dest="minimum")
    args = ap.parse_args()
    minimum = None
    if args.minimum:
        if not re.fullmatch(r"[1-9]\d*x[1-9]\d*", args.minimum):
            ap.error("--min must be WxH, for example 1200x630")
        minimum = tuple(map(int, args.minimum.split("x")))
    if args.src and not Path(args.src).is_dir():
        spacing.impossible(f"no source directory: {args.src}")
    if shutil.which("node") is None:
        spacing.impossible("node is not installed")
    out = Path(args.out or tempfile.mkdtemp(prefix="glevels-assets-")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    servers, optional = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="glevels-assets-build-") as tmp, open(spacing.LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mb = spacing.free_mb()
            if mb < spacing.MIN_FREE_MB:
                spacing.impossible(f"only {mb} MB of memory free, a browser needs {spacing.MIN_FREE_MB}. Try again when the host is quieter")
            urls = []
            for i, target in enumerate(args.targets):
                file = Path(target).resolve()
                if not target.startswith(("http://", "https://")) and file.suffix.lower() in IMAGES:
                    if not file.is_file():
                        spacing.impossible(f"no such image: {target}")
                    wrapper = Path(tmp) / f"image-{i}.html"
                    wrapper.write_text('<!doctype html><meta name="viewport" content="width=device-width"><img style="max-width:100%;height:auto" alt="' + html.escape(file.name, quote=True) + '" src="/' + urllib.parse.quote(file.name) + '">')
                    srv, base = spacing.serve({"/": wrapper}, file.parent)
                    servers.append(srv)
                    urls.append(base + "/")
                else:
                    resolved, started = spacing.resolve([target], Path(tmp))
                    urls.extend(resolved)
                    servers.extend(started)
            p = subprocess.run(["node", str(HERE / "assets.mjs"), "--out", str(out), *urls], capture_output=True, text=True, timeout=600)
            if p.returncode:
                spacing.impossible("the browser run failed: " + (p.stderr or p.stdout).strip()[-600:])
            results = json.loads((out / "findings.json").read_text())
            for render in results:
                if render.get("error"):
                    spacing.impossible(f"{args.targets[render['target_index']]} did not render: {render['error']}")
            optional = impeccable(results, args.targets, urls)
        findings = analyse(results, args.targets, args.manifest, args.src, minimum) + optional
        source = scan_source(args.src) if args.src else None
        if source:
            detail = f"{source['primitives']} primitive-drawn visuals vs {source['real_materials']} real-material loads"
            detail += "; " + "; ".join(f"{s['file']}:{s['line']} {s['sample']}" for s in source["sites"])
            findings.append(finding("review", "source-materials", detail, target=args.src))
            for site in source["placeholders"]:
                findings.append(finding("block", "placeholder", f"{site['file']}:{site['line']} {site['sample']} is a shape its own source calls a {site['placeholder']}",
                                        target=args.src))
        data = {"renders": results, "findings": findings, "source_scan": source}
        (out / "findings.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            report(data, out)
        return 1 if any(f["severity"] == "block" for f in findings) else 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        spacing.impossible(str(e))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


IMPECCABLE_IDS = {"shape-assembled-illustration", "broken-image", "buried-raster", "organic-clip-path"}

if __name__ == "__main__":
    sys.exit(main())
