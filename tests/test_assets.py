import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from glevels import assets

FIXTURES = Path(__file__).parent / "fixtures-assets"
ROOT = Path(__file__).resolve().parents[1]


def png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png_with(kind=b"tEXt", body=b"Software\0gpt-image-1"):
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + png_chunk(kind, body) + png_chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + png_chunk(b"IEND", b"")


@pytest.mark.parametrize("kind,body", [
    (b"tEXt", b"Software\0gpt-image-1"),
    (b"zTXt", b"Software\0\0" + zlib.compress(b"ComfyUI")),
    (b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0<Iptc4xmpExt:DigitalSourceType>trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>"),
    (b"iTXt", b"Software\0\1\0\0\0" + zlib.compress(b"Midjourney")),
    (b"tEXt", b"parameters\0steps: 20, sampler: Euler"),
])
def test_png_metadata(kind, body):
    info = assets.metadata(png_with(kind, body))
    assert info["generated"]
    assert info["evidence"]
    assert info["dimensions"] == [1, 1]


def software_exif(name):
    value = name.encode() + b"\0"
    return b"Exif\0\0II" + struct.pack("<HIH", 42, 8, 1) + struct.pack("<HHII", 0x131, 2, len(value), 26) + b"\0" * 4 + value


@pytest.mark.parametrize("name", ["OpenAI", "DALL-E", "ChatGPT", "Codex", "Stable Diffusion", "Firefly", "Imagen", "Gemini", "Ideogram", "Flux", "Leonardo", "Recraft", "Automatic1111"])
def test_jpeg_exif(name):
    packet = software_exif(name)
    info = assets.metadata(b"\xff\xd8\xff\xe1" + struct.pack(">H", len(packet) + 2) + packet + b"\xff\xd9")
    assert info["generated"]
    assert name in " ".join(info["evidence"])


def test_jpeg_webp_xmp_and_c2pa():
    xmp = b'<xmp Iptc4xmpExt:DigitalSourceType="compositeWithTrainedAlgorithmicMedia"/>'
    jpeg = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp + b"\xff\xd9"
    assert assets.metadata(jpeg)["generated"]
    def webp_chunk(kind, value):
        return kind + struct.pack("<I", len(value)) + value + (b"\0" if len(value) % 2 else b"")
    body = b"WEBP" + webp_chunk(b"EXIF", software_exif("Recraft")) + webp_chunk(b"XMP ", xmp)
    info = assets.metadata(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert info["generated"] and len(info["evidence"]) == 2
    assert assets.metadata(b"\0\0\0\x30jumb c2pa claim_generator OpenAI")["generated"]
    assert assets.metadata(b"\0\0\0\x10jumb c2pa no generator")["c2pa"]


def test_clean_and_truncated_metadata():
    assert not assets.metadata((FIXTURES / "clean/photo.png").read_bytes())["generated"]
    for body in (b"", b"RIFF", b"\xff\xd8", png_with()[:22]):
        assert not assets.metadata(body)["generated"]


def test_manifest_rules():
    good = dict(match="*", kind="photo", method="captured", source="camera", licence="own")
    assert not assets.manifest_errors([good])
    for update, phrase in [({"licence": "unknown"}, "unknown licence"), ({"source": ""}, "no source"), ({"licence": "CC-BY-4.0"}, "credit"), ({"method": "generated"}, "approval"), ({"method": "generated", "kind": "icon", "approved_role": "mood board"}, "approval"), ({"licence": "licensed", "source": ""}, "no source")]:
        assert any(phrase in x for x in assets.manifest_errors([dict(good, **update)]))
    assert not assets.manifest_errors([dict(good, method="generated", approved_role="store art")])
    assert any("no source" in e for e in assets.manifest_errors([dict(good, source=None)]))
    assert any("credit" in e for e in assets.manifest_errors([dict(good, licence="CC-BY-4.0", credit=None)]))
    for key in ("kind", "method", "licence"):
        assert any("unknown " + key in e for e in assets.manifest_errors([dict(good, **{key: []})]))
    assert not assets.manifest_errors([dict(good, licence="CC-BY-4.0", credit="Fixture author")])


def test_manifest_discovery_and_matching(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (tmp_path / "assets.toml").write_text("")
    page = root / "nested/page.html"
    page.parent.mkdir()
    page.write_text("")
    assert assets.find_manifest(page) is None
    manifest = root / "assets.toml"
    manifest.write_text("")
    assert assets.find_manifest(page) == manifest
    assert assets.find_manifest("https://example.test", src=root) == manifest
    assert assets.find_manifest("https://example.test") is None
    entry = dict(match="static/img/*.png", source="own")
    assert assets.covering({"url": "http://localhost/static/img/george.png"}, page, manifest, [entry]) == entry
    assert assets.covering({"url": "https://cdn.jsdelivr.net/npm/@phosphor-icons/web@2/src/bold/Phosphor-Bold.woff2"}, page, None, [])["method"] == "library"
    assert assets.covering({"url": "data:image/png:123abc"}, page, manifest, [dict(match="data:image/png:*", source="own")])
    entry = dict(match="nested/photo.png", source="own")
    assert assets.covering({"url": "http://localhost/nested/photo.png", "page_url": "http://localhost/nested/page.html"}, page, manifest, [entry]) == entry


def test_source_scan():
    scan = assets.scan_source(FIXTURES / "source")
    assert scan["primitives"] == 4
    assert scan["real_materials"] == 2
    assert scan["sites"][0]["line"] == 1
    assert assets.scan_source(FIXTURES / "clean")["primitives"] == 0


def test_source_scan_blocks_self_declared_stand_ins_but_skips_bundles():
    scan = assets.scan_source(FIXTURES / "standin")
    assert [(s["file"], s["line"], s["placeholder"]) for s in scan["placeholders"]] == [("remote.js", 4, "stand-in")]


def test_generated_approval_and_minimum():
    target = str(FIXTURES / "clean/photo.png")
    asset = dict(url="http://localhost/photo.png", original=str(FIXTURES / "clean/photo.png"), natural=[800, 600], selector="img", sample="Material", rect=[0, 0, 800, 600])
    def render():
        return [dict(target_index=0, url="http://localhost/", viewport="desktop 1440x900", assets=[dict(asset)], findings=[])]
    assert not assets.analyse(render(), [target], minimum=(800, 600))
    assert any(f["check"] == "minimum-size" for f in assets.analyse(render(), [target], minimum=(1200, 630)))
    assert assets.approved(dict(method="generated", kind="photo", approved_role="store art"))
    assert not assets.approved(dict(method="generated", kind="icon", approved_role="mood board"))


def test_approved_generated_photo_is_quiet(tmp_path):
    manifest = tmp_path / "assets.toml"
    manifest.write_text('[[asset]]\nmatch="*"\nkind="photo"\nmethod="generated"\nsource="gpt-image-1 fixture"\nlicence="own"\napproved_role="store art"\n')
    target = str(FIXTURES / "generated.png")
    renders = [dict(target_index=0, url="http://fixture/", viewport="desktop 1440x900", findings=[],
                    assets=[dict(url="http://fixture/generated.png", original=target)])]
    assert not assets.analyse(renders, [target], manifest_arg=str(manifest))
    assert renders[0]["assets"][0]["metadata"]["generated"]


def test_optional_detector_filters_and_reports_failure_once(monkeypatch):
    renders = [dict(target_index=i, viewport="desktop 1440x900", assets=[], pageHeight=900) for i in range(2)]
    monkeypatch.setattr(assets.spacing, "free_mb", lambda: 4096)
    monkeypatch.setattr(assets.vendor, "impeccable", lambda *a, **kw: (None, "optional tool missing"))
    leads = assets.impeccable(renders, ["a", "b"], ["http://a", "http://b"])
    assert len(leads) == 1
    assert leads[0]["detail"] == "impeccable: did not run: optional tool missing"
    assert leads[0]["severity"] == "review"
    def detect(*args, **kwargs):
        assert args[1] == "1440x900" and kwargs == {"scope": None}
        return [{"antipattern": "broken-image", "snippet": "Missing photo"},
                {"antipattern": "overused-font", "snippet": "Font"}], None
    monkeypatch.setattr(assets.vendor, "impeccable", detect)
    assert len(assets.impeccable(renders, ["a", "b"], ["http://a", "http://b"])) == 2
    monkeypatch.setattr(assets.spacing, "free_mb", lambda: 0)
    leads = assets.impeccable(renders, ["a", "b"], ["http://a", "http://b"])
    assert len(leads) == 1 and "memory" in leads[0]["detail"]


def browser_available():
    if not shutil.which("node"):
        return False
    result = subprocess.run(["node", "-e", "const p=require('/home/gabriel/g-design/node_modules/playwright');process.exit(require('fs').existsSync(p.chromium.executablePath())?0:1)"], capture_output=True)
    return result.returncode == 0


@pytest.mark.skipif(not browser_available(), reason="node or house Playwright/Chromium is unavailable")
@pytest.mark.parametrize("fixture,expected", [("placeholder.html", 1), ("clean/index.html", 0), ("generated/index.html", 1), ("related/index.html", 1), ("impeccable.html", 1)])
def test_browser_fixture(fixture, expected, tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "glevels/assets.py"), "--out", str(tmp_path), str(FIXTURES / fixture)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == expected, result.stdout + result.stderr
    data = json.loads((tmp_path / "findings.json").read_text())
    checks = {f["check"] for f in data["findings"]}
    if fixture == "placeholder.html":
        assert {"placeholder", "empty-slot", "primitive-svg", "emoji-icon", "upscaled", "generated", "no-manifest"} <= checks
        for render in data["renders"]:
            images = [a for a in render["assets"] if a.get("selector") in ("#soft-raster", "#generated-raster")]
            assert len(images) == 2
            assert all(Path(a["original"]).is_file() and Path(a["crop"]).is_file() for a in images)
    elif fixture == "clean/index.html":
        assert not (checks - {"impeccable", "retina-resolution"})
        assert all(a.get("manifest") for r in data["renders"] for a in r["assets"])
    elif fixture == "generated/index.html":
        assert {"manifest", "generated"} <= checks
    elif fixture == "impeccable.html":
        assert {"broken-media", "upscaled"} <= checks
        assert "NaN" not in result.stdout
        assert all(f["severity"] == "review" for f in data["findings"] if f["check"] == "impeccable")
        assert all(f["rect"] for f in data["findings"] if f["check"] == "impeccable" and "did not run" not in f["detail"])
    else:
        assert {"empty-slot", "primitive-svg", "broken-media", "upscaled", "emoji-icon"} <= checks
        all_assets = [a for r in data["renders"] for a in r["assets"]]
        assert any(a["url"].startswith("data:image/png:") for a in all_assets)
        assert any(a["url"].startswith("blob:") for a in all_assets)
        assert any(a["url"].endswith("object.glb") for a in all_assets)
        assert any(f["check"] == "broken-media" and f["selector"] == "#corrupt-background" for f in data["findings"])
        assert any(f["check"] == "emoji-icon" and f["selector"] == "#pseudo-emoji" for f in data["findings"])
