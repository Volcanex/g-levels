import functools
import http.server
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from email.message import EmailMessage
from pathlib import Path

import pytest

from glevels import readerctx

FIXTURES = Path(__file__).parent / "fixtures-readerctx"
SCRIPT = Path(readerctx.__file__)


def run(tmp_path, *args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path / "out"), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=600)


@pytest.mark.parametrize("fixture,trigger,blocks", [
    ("walkthrough.html", "never been walked through", True),
    ("fixed.eml", "We fixed", False), ("fixed.txt", "We fixed", False),
])
def test_original_and_related_process_leaks(tmp_path, fixture, trigger, blocks):
    # "We fixed" is right in a reply to the customer who reported the bug, so it is a lead.
    process = run(tmp_path, "--json", FIXTURES / fixture)
    assert process.returncode == (1 if blocks else 0), process.stdout + process.stderr
    result = json.loads(process.stdout)
    found = result["blocking"] if blocks else result["review"]
    assert any(f["check"] == "process-leak" and f["trigger"] == trigger for f in found)
    assert all(f["locator"].startswith(str(FIXTURES / fixture) + ":") for f in found)
    assert (tmp_path / "out/findings.json").is_file()
    assert (tmp_path / "out/critic.md").is_file()


def test_deck_strings_and_json_locations(tmp_path):
    process = run(tmp_path, "--json", FIXTURES / "deck.json")
    assert process.returncode == 1
    result = json.loads(process.stdout)
    assert [f["trigger"] for f in result["blocking"]] == ["{company}"]
    assert result["blocking"][0]["locator"].endswith(':$["slides"][0]["title"]')
    assert any(f["trigger"] == "ARR" for f in result["review"])


def test_customer_email_passes_and_packet_contains_only_reader_context(tmp_path):
    background = tmp_path / "reader.txt"
    background.write_text("A customer waiting for an order.")
    process = run(tmp_path, "--json", "--reader", background, "--task", "Arrange delivery.", FIXTURES / "customer.eml")
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["blocking"] == []
    assert result["review"] == []
    packet = (tmp_path / "out/critic.md").read_text()
    assert "Your Friday delivery" in packet
    assert "A customer waiting for an order." in packet
    assert "Arrange delivery." in packet
    assert readerctx.RUBRIC in packet
    for absent in (str(FIXTURES), "customer.eml", "bookings@example.test", "Content-Type", "locator"):
        assert absent not in packet


def test_html_preserves_order_inline_sentences_and_hides_private_text():
    copy = readerctx.html_copy('''<title>Welcome</title><meta name="description" content="Book your visit.">
<h1>Choose a date</h1><p>We <strong>fixed</strong> the walkthrough. Reply <a href="/book">to book</a>.</p>
<!-- We patched it. --><script>We patched it.</script><style>We patched it.</style>
<div hidden><p>We patched it.</p></div><p aria-hidden="true">We patched it.</p>
<div style="DISPLAY : none !important"><p>We patched it.</p></div>
<ul><li>Your first choice</li><li>Your second choice</li></ul><button>Book now</button>
<img alt="A garden in spring"><label>Your name</label>''', "page.html")
    assert [" ".join(p["text"].split()) for p in copy] == [
        "Welcome", "Book your visit.", "Choose a date", "We fixed the walkthrough. Reply to book.",
        "Your first choice", "Your second choice", "Book now", "A garden in spring", "Your name",
    ]
    blocking, review = readerctx.analyse(copy)
    assert blocking == []
    leaks = [f for f in review if f["check"] == "process-leak"]
    assert len(leaks) == 1
    assert leaks[0]["quote"] == "We fixed the walkthrough."
    assert leaks[0]["locator"] == "page.html:2"


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
def test_email_decoding(tmp_path, encoding):
    message = EmailMessage()
    message["Subject"] = "Your café booking"
    message.set_content("We fixed the walkthrough.\n", cte=encoding)
    message.add_attachment(b"We patched the pipeline.", maintype="text", subtype="plain", filename="private.txt")
    path = tmp_path / "booking.eml"
    path.write_bytes(message.as_bytes())
    copy = readerctx.read_target(str(path))
    assert copy[0]["text"] == "Your café booking"
    assert len([f for f in readerctx.analyse(copy)[1] if f["check"] == "process-leak"]) == 1


@pytest.mark.parametrize("text", [
    "The steps have never been tested.", "The steps haven't been checked.", "These haven't been tested.",
    "It hasn't been verified.", "TODO: replace the placeholder.", "FIXME: this is TBD.", "This says lorem ipsum.",
])
def test_blocking_process_triggers(text):
    blocking, _ = readerctx.analyse(readerctx.paragraphs(text, "copy.txt"))
    assert any(f["check"] == "process-leak" for f in blocking)


@pytest.mark.parametrize("text", [
    "We just patched your website.", "I rewrote your report.", "Our team migrated the website.",
    "Our agent used a prompt.", "A subagent used GPT.", "Claude and Codex checked this.",
    "Use the pipeline on this branch.", "The pull request is on staging.", "Our first look found three issues.",
])
def test_process_leads_do_not_block(text):
    # Right in some letters and products (a reply to the reporter, a product about AI agents).
    blocking, review = readerctx.analyse(readerctx.paragraphs(text, "copy.txt"))
    assert not any(f["check"] == "process-leak" for f in blocking)
    assert any(f["check"] == "process-leak" for f in review)


@pytest.mark.parametrize("text,trigger", [
    ("Hello {{company}}.", "{{company}}"), ("Hello {company}.", "{company}"),
    ("Hello ${company}.", "${company}"), ("Hello %s.", "%s"), ("Hello <company>.", "<company>"),
    ("Read /opt/app/config.", "/opt/app/config."), ("This is undefined.", "undefined"),
    ("This is NaN.", "NaN"), ("This is [object Object].", "[object Object]"),
    ("Visit https://example.test/?debug=1 now.", "https://example.test/?debug=1"),
])
def test_internal_triggers(text, trigger):
    blocking, _ = readerctx.analyse(readerctx.paragraphs(text, "copy.txt"))
    assert any(f["trigger"] == trigger for f in blocking)


@pytest.mark.parametrize("text,trigger", [
    ("Read app.py today.", "app.py"), ("Read config.json today.", "config.json"),
    ("Open ./secret today.", "./secret"), ("Set API_KEY now.", "API_KEY"),
    ("Read account_name here.", "account_name"), ("Read accountName here.", "accountName"),
    ("This is null.", "null"),
])
def test_internal_leads(text, trigger):
    # Developer walkthroughs name files and variables on purpose.
    blocking, review = readerctx.analyse(readerctx.paragraphs(text, "copy.txt"))
    assert blocking == []
    assert any(f["trigger"] == trigger for f in review)


def test_leads_do_not_block_and_knowledge_is_respected(tmp_path):
    path = tmp_path / "copy.txt"
    path.write_text("ARR is growing. As you know, CRM (customer relationship management) helps.\n\n"
                    "Use Cloud Desk today. Cloud Desk saves time. We checked every page.\n\n"
                    "One. Two. Three. Four. Five. Six.\n\n" + "word " * 31 + ".")
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("ARR: annual recurring revenue\n")
    process = run(tmp_path, "--json", "--knows", "CRM", "--glossary", glossary, path)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    checks = {f["check"] for f in result["review"]}
    assert checks == {"assumed-acronym", "assumed-term", "assumed-product", "over-explaining", "known-definition",
                      "insider-framing", "long-paragraph", "long-sentence"}
    assert not any(f["check"] == "assumed-acronym" and f["trigger"] == "CRM" for f in result["review"])


def test_first_use_expansions_and_common_acronyms():
    for text in ("Annual recurring revenue (ARR) is growing. ARR pays the bills.",
                 "ARR (annual recurring revenue) is growing.", "Your UK PDF is ready."):
        assert readerctx.analyse(readerctx.paragraphs(text, "copy.txt")) == ([], [])
    _, review = readerctx.analyse(readerctx.paragraphs("ARR is growing. Annual recurring revenue (ARR) pays the bills.", "copy.txt"))
    assert any(f["trigger"] == "ARR" for f in review)


def test_markdown_walk_directory_and_line_numbers(tmp_path):
    path = tmp_path / "copy.md"
    path.write_text("# Your next step\n\nChoose [a date](https://example.test).\nWe fixed the walkthrough.\n")
    copy = readerctx.read_target(str(path))
    assert copy[1]["text"] == "Choose a date.\nWe fixed the walkthrough."
    assert readerctx.analyse(copy)[1][0]["locator"] == f"{path}:4"
    text = tmp_path / "walk/text"
    text.mkdir(parents=True)
    (text / "002.txt").write_text("Second page text.")
    (text / "001.txt").write_text("First page text.")
    assert [p["text"] for p in readerctx.read_target(str(text.parent))] == ["First page text.", "Second page text."]


def test_long_reader_text_is_not_treated_as_a_filename(tmp_path):
    background = "A customer waiting for an order. " * 20
    process = run(tmp_path, "--reader", background, FIXTURES / "customer.eml")
    assert process.returncode == 0, process.stdout + process.stderr
    assert background.strip() in (tmp_path / "out/critic.md").read_text()


def test_markdown_heading_is_separate_and_walk_order_uses_manifest(tmp_path):
    copy = readerctx.paragraphs("# Your next step\nChoose a date.", "copy.md", markdown=True)
    assert [p["text"] for p in copy] == ["Your next step", "Choose a date."]
    text = tmp_path / "text"
    text.mkdir()
    (text / "s10.txt").write_text("The later page.")
    (text / "s2.txt").write_text("The earlier page.")
    assert [p["text"] for p in readerctx.read_target(str(tmp_path))] == ["The earlier page.", "The later page."]
    (tmp_path / "walk.json").write_text(json.dumps({"states": [{"text": "text/s10.txt"}, {"text": "text/s2.txt"}]}))
    assert [p["text"] for p in readerctx.read_target(str(tmp_path))] == ["The later page.", "The earlier page."]


def test_stub_critic_is_isolated_and_fabricated_quotes_are_dropped(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text('''import json, os, pathlib, sys
prompt = sys.stdin.read()
assert sorted(p.name for p in pathlib.Path.cwd().iterdir()) == ["critic.md"]
assert pathlib.Path("critic.md").read_text() == prompt
assert "walkthrough.html" not in prompt
print("```json\\n" + json.dumps({"gist": "Arrange a call.", "findings": [
    {"quote": "those steps have never been walked through end to end against a live site", "problem": "process", "why": "This is the sender's method."},
    {"quote": "The sky is green", "problem": "suspicious", "why": "Invented."}
], "verdict": "met"}) + "\\n```")
''')
    env = dict(os.environ, GLEVELS_CRITIC_CMD=shlex.join([sys.executable, str(stub)]))
    process = run(tmp_path, "--json", "--critic", FIXTURES / "walkthrough.html", env=env)
    assert process.returncode == 1, process.stdout + process.stderr
    critic = json.loads(process.stdout)["critic"]
    assert critic["gist"] == "Arrange a call."
    assert critic["dropped_quotes"] == 1
    assert len(critic["findings"]) == 1


def test_critic_failure_does_not_change_mechanical_result(tmp_path):
    env = dict(os.environ, GLEVELS_CRITIC_CMD="/readerctx-no-such-critic")
    process = run(tmp_path, "--critic", FIXTURES / "customer.eml", env=env)
    assert process.returncode == 0
    assert "critic: impossible:" in process.stdout


def test_critic_normalises_quotes_and_cli_json_envelope(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text('''import json, sys
sys.stdin.read()
print(json.dumps({"result": json.dumps({"gist": "Reply.", "findings": [
    {"quote": "It's  ready", "problem": "suspicious", "why": "A question."}
], "verdict": "attempted"})}))
''')
    monkeypatch.setenv("GLEVELS_CRITIC_CMD", shlex.join([sys.executable, str(stub)]))
    packet = tmp_path / "critic.md"
    packet.write_text("It’s ready")
    result = readerctx.critic(packet, "It’s ready", "opus")
    assert len(result["findings"]) == 1
    assert result["dropped_quotes"] == 0


@pytest.mark.parametrize("args", [["missing.txt"], ["--json", "missing.txt"]])
def test_impossible_input(tmp_path, args):
    process = run(tmp_path, *args)
    assert process.returncode == 2
    assert "impossible" in process.stdout
    assert "Traceback" not in process.stderr


@pytest.fixture
def site():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_url_fetch_without_browser(tmp_path, site):
    process = run(tmp_path, "--json", site + "/walkthrough.html")
    assert process.returncode == 1, process.stdout + process.stderr
    assert any(f["trigger"] == "never been walked through" for f in json.loads(process.stdout)["blocking"])


def test_rendered_url_uses_real_chromium(tmp_path, site):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    check = subprocess.run(["node", "--input-type=module", "-e", '''
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
const require = createRequire(path.join(process.env.GDESIGN || '/home/gabriel/g-design', 'package.json'));
const { chromium } = require('playwright');
process.exit(fs.existsSync(chromium.executablePath()) ? 0 : 1);
'''], capture_output=True, text=True)
    if check.returncode:
        pytest.skip("Playwright or Chromium is missing: " + check.stderr)
    process = run(tmp_path, "--json", "--render", site + "/render.html")
    assert process.returncode == 1, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert any(f["trigger"] == "never been walked through" for f in result["blocking"])
    assert not any("patched" in f["quote"] for f in result["blocking"] + result["review"])
    assert list((tmp_path / "out").rglob("*.png"))


def test_browser_memory_guard(tmp_path, site):
    env = dict(os.environ, GSPACING_MIN_FREE_MB="999999999", GSPACING_LOCK=str(tmp_path / "guard.lock"))
    process = run(tmp_path, "--render", site + "/render.html", env=env)
    assert process.returncode == 2
    assert "impossible: only" in process.stdout


def test_code_stays_in_the_copy_but_out_of_the_checks():
    html = ("<div class='card-text'>Add a route ending in <code>/*</code> so the homepage is covered.</div>"
            "<pre>export default { fetch: (r) => fetch(r) } // {{token}} /home/x</pre>"
            "<p>Then search the page for <code>{{site_id}}</code>.</p>")
    copy = readerctx.html_copy(html, "page.html")
    assert copy[0]["text"] == "Add a route ending in `/*` so the homepage is covered."
    assert any(p.get("code") and "{{token}}" in p["text"] for p in copy)
    block, _ = readerctx.analyse(copy)
    assert block == []
