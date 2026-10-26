"""The Studio gates (client nerves, egress, project contract), each tried on the original failure,
a related failure of the same class, and clean work that must pass. Each case is a tiny repo in
tmp_path, checked through the gate's real sensor command with the paths its charter pins."""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from glevels import registry
from glevels.cli import run_sensor

GATES = registry.load_gates()
CHARTERS = registry.load_charters()


@pytest.fixture(autouse=True)
def this_checkout(monkeypatch):
    # Sensor commands find studio.py through GLEVELS, so the branch under test runs, not main.
    monkeypatch.setenv("GLEVELS", str(registry.ROOT))


def entry(charter, gate):
    return next(e for e in CHARTERS[charter]["gates"] if e["id"] == gate)


def repo(tmp_path, files, git=True):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body) if isinstance(body, str) else json.dumps(body))
    if git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def sense(gate, root, charter="gsp"):
    return run_sensor(GATES[gate], entry(charter, gate), root, "")


NERVES = "correct.studio.client-nerves"
CLEAN_NERVES = {
    "CLAUDE.md": """\
        # rose-site
        Rose's portfolio. Pages live in pages/, each with content.html. The hero has a pulse
        animation on the button. Ask Gabriel through a studio ticket when the box, disk or a
        runtime secret needs him. Keys are in ~/.passport.env, never in the repo.
        """,
    "AGENTS.md": "See CLAUDE.md.\n",
    "core/api/CLAUDE.md": "Routers here mount under /api.\n",
    "core/api/AGENTS.md": "Routers here mount under /api.\n",
    # Not a nerves file, so out of scope: g-seed's README describes the G-System.
    "README.md": "One central server runs the knowledge brain and the alert spine.\n",
}


def test_nerves_catch_machinery_copied_into_a_project(tmp_path):
    files = {**CLEAN_NERVES, "CLAUDE.md": "Record what you learn with brain_note and raise infra faults with brain_alert.\n"}
    result, out = sense(NERVES, repo(tmp_path, files))
    assert result == "caught" and "brain_note" in out and "CLAUDE.md" in out


def test_nerves_catch_the_pulse_and_the_spine_in_nested_and_dot_claude_files(tmp_path):
    files = {
        **CLEAN_NERVES,
        "core/api/AGENTS.md": "Read the pulse first, then log what changed with pulse_note.\n",
        ".claude/rules/alerts.md": "Infra faults go to Gabriel's spine.\n",
    }
    result, out = sense(NERVES, repo(tmp_path, files))
    assert result == "caught"
    assert "core/api/AGENTS.md" in out and "the pulse" in out and "pulse_note" in out
    assert ".claude/rules/alerts.md" in out


def test_nerves_let_the_studios_own_create_ticket_through_but_not_brain_tools(tmp_path):
    # The studio MCP's create_ticket is how a client's Claude reaches Gabriel, so naming it is right.
    ticket = "Problems you cannot fix from your home: file them with create_ticket, for=\"gabriel\".\n"
    assert sense(NERVES, repo(tmp_path / "ok", {**CLEAN_NERVES, "CLAUDE.md": ticket})) == ("met", "4 files")
    brain = ticket + "Then record the fix with brain_note.\n"
    result, out = sense(NERVES, repo(tmp_path / "leak", {**CLEAN_NERVES, "CLAUDE.md": brain}))
    # One catch, on the brain_note line, and none for create_ticket.
    assert result == "caught" and out.count(": names ") == 1 and "(brain_note)" in out


def test_nerves_pass_clean_project_nerves(tmp_path):
    assert sense(NERVES, repo(tmp_path, CLEAN_NERVES)) == ("met", "4 files")


EGRESS = "correct.security.egress-closed"


def test_egress_catches_star(tmp_path):
    result, out = sense(EGRESS, repo(tmp_path, {"studio.json": {"views": [], "egress": ["*"]}}))
    assert result == "caught" and "allows every host" in out


@pytest.mark.parametrize("bad, why", [
    ("*.com", "top-level domain"),
    ("*.co.uk", "registry suffix"),
    ("*.workers.dev", "platform"),
    ("*.github.io", "platform"),
    ("api.*.com", "wildcard pattern"),
    ("10.0.0.0/8", "not a host name"),
    ("https://example.com/x", "not a host name"),
])
def test_egress_catches_wildcards_that_amount_to_everything(tmp_path, bad, why):
    result, out = sense(EGRESS, repo(tmp_path, {"studio.json": {"egress": ["api.stripe.com", bad]}}))
    assert result == "caught" and why in out and "api.stripe.com" not in out


def test_egress_catches_a_list_that_is_not_a_list(tmp_path):
    result, out = sense(EGRESS, repo(tmp_path, {"studio.json": {"egress": "*"}}))
    assert result == "caught" and "must be a list" in out


def test_egress_passes_named_hosts_and_owned_wildcards(tmp_path):
    root = repo(tmp_path, {"studio.json": {"views": [], "egress": ["api.stripe.com", "*.sweathouse.co.uk", "*.api.example.com"]}})
    assert sense(EGRESS, root) == ("met", "1 files")


def test_egress_reads_the_studio_defaults_too(tmp_path):
    gate = GATES[EGRESS]
    defaults = {"sandbox/egress-defaults.json": {"allow": ["pypi.org", "api.anthropic.com"], "ports": [443]}}
    e = {"id": EGRESS, "paths": ["sandbox/egress-defaults.json"]}
    assert run_sensor(gate, e, repo(tmp_path / "ok", defaults), "")[0] == "met"
    opened = {"sandbox/egress-defaults.json": {"allow": ["pypi.org", "*"], "ports": [443]}}
    assert run_sensor(gate, e, repo(tmp_path / "open", opened), "")[0] == "caught"


def test_egress_cannot_judge_a_file_that_is_not_json(tmp_path):
    root = repo(tmp_path, {"studio.json": '{"egress": ["*"],'})
    assert sense(EGRESS, root)[0] == "impossible"


CONTRACT = "correct.studio.project-contract"
NERVE_PAIR = {"CLAUDE.md": "# project\n", "AGENTS.md": "# project\n"}


def test_contract_catches_a_project_without_studio_json(tmp_path):
    result, out = sense(CONTRACT, repo(tmp_path, {**NERVE_PAIR, "server.py": "app = 1\n"}))
    assert result == "caught" and "studio.json: missing" in out


def test_contract_catches_a_runtime_without_health_or_depends(tmp_path):
    files = {
        **NERVE_PAIR,
        "studio.json": {"views": [], "backend": {"port": 8080}},
        "server.py": '@app.get("/api/_health")\ndef health(): ...\n',
        "pyproject.toml": '[project]\nname = "gsp-x"\n',
    }
    result, out = sense(CONTRACT, repo(tmp_path, files))
    assert result == "caught" and "no source file serves /health" in out and "depends" in out


def test_contract_catches_missing_mirrors_and_bad_json(tmp_path):
    files = {"CLAUDE.md": "# p\n", "pages/CLAUDE.md": "# pages\n", "studio.json": '{"views": [}'}
    result, out = sense(CONTRACT, repo(tmp_path, files))
    assert result == "caught"
    assert "AGENTS.md: missing at the root" in out and "pages/AGENTS.md" in out and "does not parse" in out


def test_contract_passes_a_site_and_a_runtime(tmp_path):
    site = repo(tmp_path / "site", {**NERVE_PAIR, "studio.json": {"views": []}})
    assert sense(CONTRACT, site)[0] == "met"
    runtime = {
        **NERVE_PAIR,
        "studio.json": {"views": [], "backend": {"port": 8080}},
        "server.py": '@app.get("/health")\ndef health(): ...\n',
        "pyproject.toml": '[project]\nname = "gsp-x"\n[tool.g-core]\ndepends = []\n',
    }
    # A studio checkout has no .git in its tree, so the sensor walks it.
    assert sense(CONTRACT, repo(tmp_path / "runtime", runtime, git=False))[0] == "met"


def test_g_seed_binds_to_the_client_charter_and_gsp_repos_do_not_yet():
    cid, scoped = registry.in_scope(Path("/x/g-seed"), "", CHARTERS)
    ids = {e["id"] for e in scoped}
    assert cid == "gsp" and {NERVES, EGRESS, CONTRACT, "correct.secrets.git-history"} <= ids
    # Charters bind by exact name. When patterns land, bind gsp-* and change this line.
    assert registry.charter_for("gsp-rose", CHARTERS) == "g-system"


def test_the_studio_pins_its_own_egress_nerves_and_copy():
    cid, scoped = registry.in_scope(Path("/x/g-studio"), "", CHARTERS)
    ids = {e["id"]: e for e in scoped}
    assert cid == "g-studio"
    assert ids[EGRESS]["paths"] == ["sandbox/egress-defaults.json"] and ids[EGRESS]["blocking"]
    assert {"gstudio/mcp_server.py", "sandbox/home/instructions.md"} <= set(ids[NERVES]["paths"])
    assert "correct.copy.reader-context" in ids and CONTRACT not in ids
