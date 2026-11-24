"""The house-glyph sensor reads a script's copy, not its code (Fasra, 2026-11-24: 8,200 hits in 101 files, all code)."""
import subprocess
import sys
from pathlib import Path

from glevels import glyphs

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures-glyphs"


def run(name):
    return subprocess.run([sys.executable, str(ROOT / "glevels/glyphs.py"), str(FIXTURES / name)],
                          capture_output=True, text=True)


def test_code_comments_css_shaders_and_cookies_in_a_script_are_not_copy():
    p = run("code.js")
    assert p.returncode == 0 and not p.stdout, p.stdout


def test_banned_glyphs_in_a_scripts_strings_and_templates_are_caught_on_their_own_lines():
    p = run("copy.js")
    hits = [line.split(":")[1:3] for line in p.stdout.splitlines()]
    assert hits == [["2", " semicolon"], ["3", " em dash"], ["4", " middle dot"], ["5", " middle dot"], ["7", " semicolon"]], p.stdout
    assert p.returncode == 1


def test_a_script_keeps_its_line_numbers_and_only_its_string_text():
    text = glyphs.js_text("const a = 1; // note\nconst b = 'Words here'\n")
    assert text.split("\n")[0].strip() == "" and text.split("\n")[1].strip() == "Words here"


def test_html_is_read_as_before(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<style>a{b:c;}</style><script>x();</script><p>Fine&nbsp;here</p><p>Not fine; here</p>")
    p = subprocess.run([sys.executable, str(ROOT / "glevels/glyphs.py"), str(page)], capture_output=True, text=True)
    assert p.returncode == 1 and p.stdout.count("semicolon") == 1, p.stdout
