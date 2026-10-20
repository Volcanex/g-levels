"""Sensor: reader context and process leaks. Gate correct.copy.reader-context.

    python3 readerctx.py [--json] [--out DIR] [--reader TEXT|FILE] [--task TEXT]
                        [--knows "term, term"] [--glossary FILE] [--critic]
                        [--model opus] [--render] TARGET [TARGET ...]

Read HTML, Markdown, text, email, JSON, walk output directories or URLs. --render
uses walk.mjs when present. Browser runs share spacing.py's lock and memory guard.
findings.json, critic.md and any screenshots
go in --out (default: a new temp dir, printed at the end).

Exit 0: no clear fault. Exit 1: clear faults, listed under "blocking". Exit 2:
could not run, printed as "impossible: ...". Leads under "for the reviewer" and
the isolated critic's verdict never fail a run alone. This does not score voice.
"""
import argparse
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = set("UK US AI OK FAQ PDF URL CEO EU GB VAT PM AM TV".split())
SKIP_KEYS = set("id type src href url class style color icon layout template image slug key".split())
OPENERS = set("A An The This That These Those We Our Your You I It As In To Use Try Choose Visit Book Reply Hi Thanks".split())
DATES = set("Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August September October November December".split())
RUBRIC = '''You are this reader. 1) In one sentence: what is this asking or offering you, and what happens next? 2) Quote every phrase you would need knowledge you lack to understand. 3) Quote every sentence that explains something you already know. 4) Quote every sentence about how the sender made, fixed, tested or checked things that you do not need. 5) Quote anything that sounds untrue, evasive or suspicious from where you sit. Answer as JSON: {"gist": str, "findings": [{"quote": exact words from the copy, "problem": "assumes"|"over-explains"|"process"|"suspicious"|"unclear-ask", "why": str}], "verdict": "met"|"attempted"}. Quote exactly; a quote that is not in the copy is discarded.'''
# Blocking: words that are never right in copy a customer reads.
PROCESS = [
    r"\bnever been\s+(?:walked|tested|tried|run|checked)(?:\s+through)?\b",
    r"\b(?:not yet|haven['’]t been|hasn['’]t been|(?:has|have|had) not been)\s+(?:tested|verified|checked|tried|walked)(?:\s+through)?\b",
    r"(?-i:\b(?:TODO|FIXME|TBD)\b)|\blorem ipsum\b",
]
INTERNALS = [
    r"\$?\{\{?\s*[\w.-]+\s*\}\}?|%s|<[A-Za-z][\w.-]*>",
    r"(?<![\w:/])/(?:opt|home|root|tmp|var|etc|srv|usr)/[^\s,;]+",
    r"\b(?:undefined|NaN)\b|\[object Object\]",
    r"https?://[^\s]+[?&](?:_+[\w-]*|debug|preview|internal|token|api_key|access_token|auth|branch|commit|staging|env)=[^\s]*",
]
# Leads: right in some products and some letters (a reply to the customer who reported the bug,
# a product about AI agents, a developer walkthrough), wrong in most customer copy.
PROCESS_LEADS = [
    r"\b(?:we|I|our team)\s+(?:just\s+)?(?:fixed|patched|rewrote|refactored|migrated|redeployed|rebuilt|reworked)\b",
    r"\b(?:subagents?|LLMs?|GPT(?:-\w+)?|Claude|Codex|prompts?|pipelines?|commits?|branches|branch|pull requests?|staging|placeholders?)\b",
    r"\bfirst look\b(?=.*\b(?:we|our|I|scan|review|test|walkthrough)\b)|\b(?:we|our|I|scan|review|test|walkthrough)\b.*?\bfirst look\b",
]
INTERNAL_LEADS = [
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b[a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+\b|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b",
    r"(?<![\w:/])\.{1,2}/[^\s,;]+|\b[\w-]+\.(?:py|json)\b|\bnull\b",
]


def normalise(text):
    return " ".join(text.translate(str.maketrans("‘’“”", "''\"\"")).split())


def paragraphs(text, source, start=1, markdown=False):
    result, pending, line_number = [], [], start

    def flush():
        if pending:
            result.append({"text": "\n".join(pending), "locator": f"{source}:{line_number}"})
            pending.clear()

    for number, line in enumerate(text.splitlines(), start):
        boundary = markdown and re.match(r"\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", line)
        heading = markdown and re.match(r"\s*#{1,6}\s", line)
        if not line.strip() or boundary:
            flush()
        if line.strip():
            if not pending:
                line_number = number
            if markdown:
                line = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", line)
                line = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", line)
            pending.append(line)
            if heading:
                flush()
    flush()
    return result


class CopyHTML(HTMLParser):
    # Copy also lives in bare divs and spans (a card's text is often <div class="card-text">).
    selected = set("""title h1 h2 h3 h4 h5 h6 p li button a label div span strong em b small td th dd dt
                      figcaption blockquote caption summary section article main aside""".split())
    blocks = set("title h1 h2 h3 h4 h5 h6 p li button label div section article header footer nav ul ol".split())
    void = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source, self.stack, self.parts, self.result = source, [], [], []
        self.line = 1

    def flush(self):
        text = "".join(self.parts)
        if text.strip():
            self.result.append({"text": text, "locator": f"{self.source}:{self.line}"})
        self.parts.clear()

    def add(self, text):
        if not self.parts:
            self.line = self.getpos()[0]
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # Code stays in the copy, marked: inline as `code`, blocks as their own paragraph. The
        # mechanical checks skip it (a template or path there is the point), the critic reads it,
        # because a sentence like "end the route in `/*`" means nothing with the code cut out.
        if tag == "pre" and not (self.stack and self.stack[-1][1]):
            self.flush()
            self.stack.append((tag, False, True))
            self.code = True
            return
        if tag in ("code", "kbd", "samp") and not (self.stack and self.stack[-1][1]):
            self.stack.append((tag, False, True))
            if not getattr(self, "code", False):
                self.add("`")
            return
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in ("script", "style", "template", "noscript")
                  or "hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true"
                  or bool(re.search(r"display\s*:\s*none\b", attrs.get("style") or "", re.I)))
        if not hidden:
            if tag in self.blocks:
                self.flush()
            if tag == "meta" and str(attrs.get("name", "")).lower() == "description":
                self.flush()
                self.add(attrs.get("content") or "")
                self.flush()
            if tag == "img" and attrs.get("alt"):
                self.flush()
                self.add(attrs["alt"])
                self.flush()
            if tag == "br":
                self.add("\n")
        if tag not in self.void:
            capture = tag in self.selected or bool(self.stack and self.stack[-1][2])
            self.stack.append((tag, hidden, capture))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is not None and tag == "pre" and getattr(self, "code", False):
            text = "".join(self.parts)
            self.parts.clear()
            if text.strip():
                self.result.append({"text": text, "locator": f"{self.source}:{self.line}", "code": True})
            self.code = False
            del self.stack[index:]
            return
        if index is not None and tag in ("code", "kbd", "samp") and not getattr(self, "code", False):
            self.add("`")
            del self.stack[index:]
            return
        if index is not None:
            if not self.stack[index][1] and (tag in self.blocks or tag == "a" and not any(x[2] for x in self.stack[:index])):
                self.flush()
            del self.stack[index:]

    def handle_data(self, data):
        if self.stack and not self.stack[-1][1] and self.stack[-1][2]:
            self.add(data)


def html_copy(text, source):
    parser = CopyHTML(source)
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser.result


def json_copy(value, source, path="$"):
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() not in SKIP_KEYS:
                result.extend(json_copy(child, source, path + "[" + json.dumps(key) + "]"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(json_copy(child, source, f"{path}[{index}]"))
    elif isinstance(value, str) and len(value.split()) >= 3:
        result.append({"text": value, "locator": f"{source}:{path}"})
    return result


def read_target(target):
    if target.startswith(("http://", "https://")):
        with urllib.request.urlopen(target, timeout=30) as response:
            return html_copy(response.read().decode(response.headers.get_content_charset() or "utf-8"), target)
    path = Path(target)
    if path.is_dir():
        manifest = path / "walk.json"
        files = ([path / state["text"] for state in json.loads(manifest.read_text())["states"]] if manifest.is_file()
                 else sorted((path / "text").glob("*.txt"), key=lambda f: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", f.name)]))
        if not files:
            raise ValueError(f"no text/*.txt in {target}")
        return [p for file in files for p in read_target(str(file))]
    if path.suffix.lower() == ".eml":
        raw = path.read_bytes()
        message = BytesParser(policy=policy.default).parsebytes(raw)
        raw_lines = raw.decode("utf-8", errors="replace").splitlines()
        subject_line = next((i for i, line in enumerate(raw_lines, 1) if line.lower().startswith("subject:")), 1)
        body_line = next((i + 1 for i, line in enumerate(raw_lines, 1) if not line), 1)
        result = paragraphs(str(message.get("Subject", "")), target, subject_line)
        for part in message.walk():
            if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                result.extend(paragraphs(part.get_content(), target, body_line))
        return result
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".html", ".htm"):
        return html_copy(text, target)
    if path.suffix.lower() == ".json":
        return json_copy(json.loads(text), target)
    if path.suffix.lower() not in (".md", ".txt"):
        raise ValueError(f"unsupported copy file: {target}")
    return paragraphs(text, target, markdown=path.suffix.lower() == ".md")


def render_urls(urls, out_dir):
    if shutil.which("node") is None:
        raise ValueError("node is not installed")
    with open(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        memory = Path("/proc/meminfo").read_text()
        mb = int(re.search(r"MemAvailable:\s+(\d+)", memory)[1]) // 1024
        minimum = int(os.environ.get("GSPACING_MIN_FREE_MB", "600"))
        if mb < minimum:
            raise ValueError(f"only {mb} MB of memory free, a browser needs {minimum}. Try again when the host is quieter")
        render_dir = out_dir / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        walk = HERE / "walk.mjs"
        command = ["node", str(walk), "--mode", "smoke", "--out", str(render_dir), *urls]
        process = subprocess.run(command, env=dict(os.environ, GLEVELS_WALK_LOCKED="1"),
                                 capture_output=True, text=True, timeout=300)
        if process.returncode:
            raise ValueError(f"browser run failed: {(process.stderr or process.stdout).strip()[-600:]}")
        states = json.loads((render_dir / "walk.json").read_text())["states"]
        if len(states) != len(urls):
            raise ValueError("browser did not capture every target")
        result = {}
        for url, state in zip(urls, states):
            failures = [e["message"] for e in state.get("errors", []) if e.get("kind") in ("action", "freeze", "navigation")]
            if failures or not state.get("url", "").startswith(("http://", "https://")):
                raise ValueError(f"browser could not read {url}: {'; '.join(failures) or state.get('url')}")
            result[url] = read_target(str(render_dir / state["text"]))
        return result


def sentences(paragraph):
    text = paragraph["text"]
    start = 0
    for match in re.finditer(r'''[.!?]+["'”’)]*(?=\s|$)|$''', text):
        end = match.end()
        if text[start:end].strip():
            leading = len(text[start:end]) - len(text[start:end].lstrip())
            locator = paragraph["locator"]
            if re.search(r":\d+$", locator):
                source, line = locator.rsplit(":", 1)
                locator = f"{source}:{int(line) + text[:start + leading].count(chr(10))}"
            yield {"text": " ".join(text[start:end].split()), "locator": locator}
        start = end


def expanded(term, text):
    escaped = re.escape(term)
    before = re.search(r"([A-Za-z][A-Za-z -]+)\s*\(" + escaped + r"\)", text)
    after = re.search(r"\b" + escaped + r"\s*\(([A-Za-z][A-Za-z -]+)\)", text)
    return any("".join(word[0] for word in re.findall(r"[A-Za-z]+", match[1]))[-len(term):].upper() == term
               for match in (before, after) if match)


def analyse(copy, knows=None, glossary=None):
    block, review = [], []
    known = {term.casefold() for term in knows or []}
    seen, prior, products = set(), "", {}

    def add(items, sentence, check, trigger, detail):
        items.append({"check": check, "locator": sentence["locator"], "quote": sentence["text"],
                      "trigger": trigger, "detail": detail})

    for paragraph in copy:
        if paragraph.get("code"):
            continue
        lines = list(sentences(paragraph))
        for sentence in lines:
            text = re.sub(r"`[^`]*`", "`code`", sentence["text"])
            for items, check, patterns in ((block, "process-leak", PROCESS), (block, "internal", INTERNALS),
                                           (review, "process-leak", PROCESS_LEADS), (review, "internal", INTERNAL_LEADS)):
                for pattern in patterns:
                    match = re.search(pattern, text, re.I if check == "process-leak" else 0)
                    if match:
                        add(items, sentence, check, match[0], "sender's process" if check == "process-leak" else "unfilled template or internal detail")
            for match in re.finditer(r"\b[A-Z]{2,6}\b", text):
                term = match[0]
                if term not in seen and term not in COMMON and term.casefold() not in known and not expanded(term, text):
                    add(review, sentence, "assumed-acronym", term, "no expansion at first use")
                seen.add(term)
            for term, meaning in (glossary or {}).items():
                match = re.search(r"\b" + re.escape(term) + r"\b", text, re.I)
                if match and term.casefold() not in known and normalise(meaning).casefold() not in normalise(prior + " " + text[:match.start()]).casefold():
                    add(review, sentence, "assumed-term", term, "used before its plain meaning")
            for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", text):
                words = match[0].split()
                while words and words[0] in OPENERS:
                    words.pop(0)
                if words and " ".join(words) not in DATES:
                    products.setdefault(" ".join(words), []).append(sentence)
            match = re.match(r"(?:This means|In other words|Note that|It['’]s worth noting|Simply put|To be clear|As you know)\b", text, re.I)
            if match:
                add(review, sentence, "over-explaining", match[0], "check whether the reader needs this explanation")
            for term in knows or []:
                match = re.search(r"\b" + re.escape(term) + r"\s*\([^)]+\)", text, re.I)
                if match:
                    add(review, sentence, "known-definition", match[0], "defines a term this reader knows")
            match = re.search(r"\b(?:every page|we checked|sampled|our scan|the walkthrough)\b", text, re.I)
            if match:
                add(review, sentence, "insider-framing", match[0], "check whether this method matters to the reader")
            words = len(text.split())
            if words > 30:
                add(review, sentence, "long-sentence", str(words), f"{words} words, over 30")
            prior += " " + text
        if len(lines) > 5:
            add(review, {"text": " ".join(paragraph["text"].split()), "locator": paragraph["locator"]},
                "long-paragraph", str(len(lines)), f"{len(lines)} sentences, over 5")
    full = " ".join(p["text"] for p in copy)
    for term, uses in products.items():
        explanation = re.search(r"\b" + re.escape(term) + r"(?:\s*\([^)]+\)|\s+(?:is|are|means|lets|helps|provides|offers)\b|,\s+(?:a|an|the)\b)", full)
        if len(uses) > 1 and term.casefold() not in known and not explanation:
            add(review, uses[0], "assumed-product", term, "recurring name has no explanation")
    return block, review


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    result = json.loads(text)
    if isinstance(result, dict) and isinstance(result.get("result"), str):
        if result.get("is_error"):
            raise ValueError(result["result"])
        return parse_json(result["result"])
    return result


def critic(packet, copy, model):
    command = os.environ.get("GLEVELS_CRITIC_CMD")
    args = shlex.split(command) if command else [
        "claude", "--print", "--tools", "", "--model", model, "--output-format", "json",
        "--setting-sources", "", "--safe-mode", "--strict-mcp-config", "--no-session-persistence",
        "--system-prompt", "You are the reader described in the prompt. Follow its rubric using only the supplied copy.",
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="glevels-readerctx-critic-") as tmp:
            prompt = Path(tmp) / "critic.md"
            shutil.copyfile(packet, prompt)
            env = {key: value for key, value in os.environ.items()
                   if key not in ("CLAUDECODE", "CLAUDE_CODE_ADDITIONAL_DIRECTORIES", "CLAUDE_CODE_SYSTEM_PROMPT")}
            process = subprocess.run(args, input=prompt.read_text(), cwd=tmp, env=env,
                                     capture_output=True, text=True, timeout=90)
        if process.returncode:
            raise ValueError((process.stderr or process.stdout).strip()[-500:] or f"exit {process.returncode}")
        result = parse_json(process.stdout)
        if not isinstance(result, dict) or not isinstance(result.get("findings"), list) or not isinstance(result.get("gist"), str):
            raise ValueError("critic did not return gist and findings JSON")
        verified, dropped = [], 0
        for finding in result["findings"]:
            quote = finding.get("quote") if isinstance(finding, dict) else None
            if not isinstance(quote, str) or not quote.strip() or normalise(quote) not in normalise(copy):
                dropped += 1
            elif finding.get("problem") in ("assumes", "over-explains", "process", "suspicious", "unclear-ask"):
                verified.append(finding)
        return {"gist": result["gist"], "findings": verified, "verdict": result.get("verdict"), "dropped_quotes": dropped}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"impossible": str(error)}


def report(result):
    print(f"readerctx: {result['sentences']} sentences, {len(result['blocking'])} blocking, {len(result['review'])} for the reviewer")
    for key, title in (("blocking", "blocking (clear faults, fix these)"), ("review", "for the reviewer (leads, use judgement)")):
        if result[key]:
            print(f"\n{title}:")
            for finding in result[key]:
                print(f"  {finding['locator']} {finding['check']}: {finding['detail']} (trigger: {finding['trigger']}) — {finding['quote']}")
    if "critic" in result:
        review = result["critic"]
        if "impossible" in review:
            print(f"\ncritic: impossible: {review['impossible']}")
        else:
            print(f"\ncritic:\n  gist: {review['gist']}")
            for finding in review["findings"]:
                print(f"  {finding['problem']}: {finding['quote']} — {finding.get('why', '')}")
            print(f"  discarded fabricated quotes: {review['dropped_quotes']}")
    print(f"\nfindings: {result['out']}/findings.json\ncritic packet: {result['out']}/critic.md")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reader", default="")
    ap.add_argument("--task", default="")
    ap.add_argument("--knows")
    ap.add_argument("--glossary")
    ap.add_argument("--critic", action="store_true")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    try:
        out_dir = Path(args.out or tempfile.mkdtemp(prefix="glevels-readerctx-")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        reader = args.reader
        try:
            if reader and Path(reader).is_file():
                reader = Path(reader).read_text()
        except OSError as error:
            if error.errno != errno.ENAMETOOLONG:
                raise
        glossary = {}
        if args.glossary:
            for line in Path(args.glossary).read_text().splitlines():
                if line.strip():
                    term, meaning = line.split(":", 1)
                    glossary[term.strip()] = meaning.strip()
        urls = list(dict.fromkeys(t for t in args.targets if t.startswith(("http://", "https://")))) if args.render and (HERE / "walk.mjs").is_file() else []
        rendered = render_urls(urls, out_dir) if urls else {}
        copy = []
        for target in args.targets:
            if target in urls:
                copy.extend(rendered[target])
            else:
                copy.extend(read_target(target))
        text = "\n\n".join("```\n" + p["text"].strip("\n") + "\n```" if p.get("code") else p["text"].strip() for p in copy)
        packet = out_dir / "critic.md"
        packet.write_text(f"Copy:\n{text}\n\nReader background:\n{reader}\n\nReader task:\n{args.task}\n\n{RUBRIC}\n")
        blocking, review = analyse(copy, [t.strip() for t in args.knows.split(",") if t.strip()] if args.knows is not None else None, glossary)
        result = {"blocking": blocking, "review": review, "sentences": sum(len(list(sentences(p))) for p in copy), "out": str(out_dir)}
        if args.critic:
            result["critic"] = critic(packet, text, args.model)
        (out_dir / "findings.json").write_text(json.dumps(result, indent=2) + "\n")
    except (OSError, ValueError, LookupError, subprocess.TimeoutExpired) as error:
        if args.json:
            print(json.dumps({"impossible": str(error)}))
        else:
            print(f"impossible: {error}")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        report(result)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
