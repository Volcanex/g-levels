"""The pytest-suite gates (tenant isolation, path confinement, sandbox escape, agent tool boundary,
e2e journeys, preview parity, publish integrity), each tried on the three cases a new gate needs:
the original failure, a related failure of the same class, and clean work that must pass.

Each case is a tiny repo in tmp_path with a `.venv/bin/python` that runs this interpreter, so the
gate's real sensor command runs unchanged. Skips map to a catch for the suites that need nothing
from the host, and to impossible for those that need docker, a browser or real sites."""

import os
import shutil
import stat
import sys
import textwrap

from glevels import registry
from glevels.cli import run_sensor

GATES = registry.load_gates()

PYTEST_INI = """\
[pytest]
pythonpath = .
markers =
    sandbox: needs docker
    e2e: needs a browser
    parity: needs real sites
"""


def repo(tmp_path, files, extra_path=None):
    for rel, body in {"pytest.ini": PYTEST_INI, **files}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    env = f"PYTHONPATH={extra_path} " if extra_path else ""
    py.write_text(f"#!/bin/sh\n{env}exec {sys.executable} \"$@\"\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def sense(gate_id, root, path_env=None):
    old = os.environ.get("PATH")
    if path_env is not None:
        os.environ["PATH"] = path_env
    try:
        return run_sensor(GATES[gate_id], {}, root, "")
    finally:
        os.environ["PATH"] = old


# A route table with a rule. `leak` names a route that forgets the rule.
def studio(leak=None, extra_route=None):
    routes = ["read_file", "write_file", "publish"] + ([extra_route] if extra_route else [])
    return f"""
    STATE = {{"a": {{"index.html": "A"}}, "b": {{"index.html": "B"}}}}
    MEMBERS = {{"alice": "a", "bob": "b"}}
    ROUTES = {routes!r}

    class NotFound(Exception):
        pass

    def call(user, route, tenant, path="index.html", data="x"):
        if route != {leak!r} and MEMBERS[user] != tenant:
            raise NotFound(tenant)
        if route == "read_file":
            return STATE[tenant][path]
        STATE[tenant][path] = data
    """


MATRIX = """
import copy, pytest, studio
COVERED = ["read_file", "write_file", "publish"]

def test_matrix_covers_every_route():
    assert sorted(studio.ROUTES) == sorted(COVERED)

@pytest.mark.parametrize("route", COVERED)
def test_other_tenant_refused_and_unchanged(route):
    before = copy.deepcopy(studio.STATE)
    with pytest.raises(studio.NotFound):
        studio.call("alice", route, "b")
    assert studio.STATE == before
"""


def test_tenant_isolation_catches_a_leaking_route_and_an_uncovered_one_and_passes_clean(tmp_path):
    gate = "correct.security.tenant-isolation"
    leak = repo(tmp_path / "leak", {"studio.py": studio(leak="write_file"), "tests/security/test_matrix.py": MATRIX})
    assert sense(gate, leak)[0] == "caught"
    uncovered = repo(tmp_path / "uncovered", {"studio.py": studio(extra_route="delete_file"),
                                              "tests/security/test_matrix.py": MATRIX})
    assert sense(gate, uncovered)[0] == "caught"
    skipped = repo(tmp_path / "skipped", {"studio.py": studio(), "tests/security/test_matrix.py": MATRIX,
                                          "tests/security/test_skip.py": "import pytest\n@pytest.mark.skip('later')\ndef test_x(): pass\n"})
    assert sense(gate, skipped)[0] == "caught"
    assert sense(gate, repo(tmp_path / "clean", {"studio.py": studio(), "tests/security/test_matrix.py": MATRIX}))[0] == "met"


def tree(strict):
    resolve = "real = (root / rel).resolve()\n        if not real.is_relative_to(root.resolve()):\n            raise ValueError(rel)" if strict else "pass"
    return f"""
    import unicodedata
    from pathlib import Path

    def safe_rel(root, rel):
        rel = unicodedata.normalize("NFC", rel)
        if rel.startswith("/") or "\\x00" in rel or "\\\\" in rel or ".." in rel.split("/"):
            raise ValueError(rel)
        {resolve}
        return rel
    """


CONFINE = """
import os, pytest, tree

@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "a\\\\b", "a\\x00b"])
def test_confine_refuses_hostile(tmp_path, bad):
    with pytest.raises(ValueError):
        tree.safe_rel(tmp_path, bad)

def test_confine_refuses_symlink_escape(tmp_path):
    os.symlink("/etc", tmp_path / "out")
    with pytest.raises(ValueError):
        tree.safe_rel(tmp_path, "out/passwd")

def test_confine_allows_normal(tmp_path):
    assert tree.safe_rel(tmp_path, "pages/a.html") == "pages/a.html"
"""


def test_path_confinement_catches_symlink_escape_and_missing_tests_and_passes_clean(tmp_path):
    gate = "correct.security.path-confinement"
    files = {"tests/unit/test_tree.py": CONFINE, "tests/security/test_other.py": "def test_ok(): pass\n"}
    assert sense(gate, repo(tmp_path / "symlink", {**files, "tree.py": tree(strict=False)}))[0] == "caught"
    unnamed = {"tree.py": tree(strict=True), "tests/unit/test_tree.py": CONFINE.replace("test_confine_", "test_"),
               "tests/security/test_other.py": "def test_ok(): pass\n"}
    assert sense(gate, repo(tmp_path / "unnamed", unnamed))[0] == "caught"
    assert sense(gate, repo(tmp_path / "clean", {**files, "tree.py": tree(strict=True)}))[0] == "met"


PROBE = """
import pytest
pytestmark = pytest.mark.sandbox
NETWORK_OPEN = {open}

def test_probe_network_refused():
    assert not NETWORK_OPEN

def test_probe_setuid_refused():
    assert True
"""


def fake_docker(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    d = bin_ / "docker"
    d.write_text("#!/bin/sh\nexit 0\n")
    d.chmod(0o755)
    return f"{bin_}:/usr/bin:/bin"


def bare_path(tmp_path):
    """A PATH with the shell tools the sensor uses and no docker, even on a host that has it."""
    bin_ = tmp_path / "bare-bin"
    bin_.mkdir(parents=True, exist_ok=True)
    for tool in ("timeout", "tail", "grep"):
        src = shutil.which(tool)
        if src:
            (bin_ / tool).symlink_to(src)
    return str(bin_)


def test_sandbox_escape_is_impossible_without_docker_caught_on_escape_and_met_clean(tmp_path):
    gate = "correct.security.sandbox-escape"
    clean = {"tests/sandbox/test_probe.py": PROBE.format(open=False)}
    with_docker = fake_docker(tmp_path)
    assert sense(gate, repo(tmp_path / "nodocker", clean), path_env=bare_path(tmp_path))[0] == "impossible"
    escape = repo(tmp_path / "escape", {"tests/sandbox/test_probe.py": PROBE.format(open=True)})
    assert sense(gate, escape, path_env=with_docker)[0] == "caught"
    skipped = repo(tmp_path / "skipped", {"tests/sandbox/test_probe.py": PROBE.format(open=False)
                                          + "\n@pytest.mark.skip('docker unavailable')\ndef test_probe_disk(): pass\n"})
    assert sense(gate, skipped, path_env=with_docker)[0] == "impossible"
    assert sense(gate, repo(tmp_path / "missing", {"tests/sandbox/test_none.py": "def test_unmarked(): pass\n"}),
                 path_env=with_docker)[0] == "caught"
    assert sense(gate, repo(tmp_path / "clean", clean), path_env=with_docker)[0] == "met"


def agent_tools(bypass):
    target = "args['tenant']" if bypass else "session_tenant"
    return f"""
    import studio
    def write_file(user, session_tenant, args):
        return studio.call(user, "write_file", {target}, args["path"], args["data"])
    """


REDTEAM = """
import copy, pytest, studio, agent_tools

def test_agent_hostile_page_cannot_write_other_tenant():
    before = copy.deepcopy(studio.STATE)
    injected = {"tenant": "b", "path": "index.html", "data": "pwned"}
    try:
        agent_tools.write_file("alice", "a", injected)
    except studio.NotFound:
        pass
    assert studio.STATE["b"] == before["b"]
"""


def test_agent_tool_boundary_catches_a_tool_trusting_model_args_and_passes_clean(tmp_path):
    gate = "correct.security.agent-tool-boundary"
    leak = repo(tmp_path / "trust", {"studio.py": studio(leak="write_file"), "agent_tools.py": agent_tools(bypass=True),
                                     "tests/security/test_agent.py": REDTEAM})
    assert sense(gate, leak)[0] == "caught"
    missing = repo(tmp_path / "missing", {"studio.py": studio(), "tests/security/test_other.py": "def test_ok(): pass\n"})
    assert sense(gate, missing)[0] == "caught"
    clean = repo(tmp_path / "clean", {"studio.py": studio(leak="write_file"), "agent_tools.py": agent_tools(bypass=False),
                                      "tests/security/test_agent.py": REDTEAM})
    assert sense(gate, clean)[0] == "met"


JOURNEY = """
import pytest
pytestmark = pytest.mark.e2e

@pytest.mark.parametrize("width", [390, 1440])
def test_publish_journey(width):
    assert {ok}
"""


def test_e2e_journeys_impossible_without_a_browser_caught_on_a_broken_journey_and_met_clean(tmp_path):
    gate = "correct.product.e2e-journeys"
    fake = tmp_path / "fakepw"
    (fake / "playwright").mkdir(parents=True)
    (fake / "playwright" / "__init__.py").write_text("")
    gone = tmp_path / "nopw"
    (gone / "playwright").mkdir(parents=True)
    (gone / "playwright" / "__init__.py").write_text("raise ImportError('not installed')\n")
    nobrowser = repo(tmp_path / "nobrowser", {"tests/e2e/test_j.py": JOURNEY.format(ok=True)}, extra_path=gone)
    assert sense(gate, nobrowser)[0] == "impossible"
    broken = repo(tmp_path / "broken", {"tests/e2e/test_j.py": JOURNEY.format(ok="width != 390")}, extra_path=fake)
    assert sense(gate, broken)[0] == "caught"
    skipped = repo(tmp_path / "skipped", {"tests/e2e/test_j.py": JOURNEY.format(ok=True)
                                          + "\n@pytest.mark.skip('no chromium')\ndef test_mcp(): pass\n"}, extra_path=fake)
    assert sense(gate, skipped)[0] == "impossible"
    assert sense(gate, repo(tmp_path / "clean", {"tests/e2e/test_j.py": JOURNEY.format(ok=True)}, extra_path=fake))[0] == "met"


PARITY = """
import pytest
pytestmark = pytest.mark.parity
LIVE = {{"rose": "<p>hi</p>\\n"}}
PREVIEW = {{"rose": {preview!r}}}

@pytest.mark.parametrize("site", sorted(LIVE) or [pytest.param("none", marks=pytest.mark.skip("no real sites"))])
def test_empty_draft_preview_matches_live(site):
    assert PREVIEW[site] == LIVE[site]
"""


def test_preview_parity_catches_drift_and_is_impossible_with_no_sites_and_passes_clean(tmp_path):
    gate = "correct.product.preview-parity"
    drift = repo(tmp_path / "drift", {"tests/parity/test_p.py": PARITY.format(preview="<p>hi</p>")})
    assert sense(gate, drift)[0] == "caught"
    absurl = repo(tmp_path / "absurl", {"tests/parity/test_p.py": PARITY.format(preview="<p>hi</p>\n<img src=/preview/x>")})
    assert sense(gate, absurl)[0] == "caught"
    nosites = repo(tmp_path / "nosites", {"tests/parity/test_p.py": PARITY.format(preview="").replace('"rose": "<p>hi</p>\\n"', "")})
    assert sense(gate, nosites)[0] == "impossible"
    assert sense(gate, repo(tmp_path / "clean", {"tests/parity/test_p.py": PARITY.format(preview="<p>hi</p>\n")}))[0] == "met"


def publisher(restore):
    rollback = "for p, old in before.items():\n                live[p] = old" if restore else "pass"
    return f"""
    def publish(live, draft, build_ok):
        before = {{p: live.get(p) for p in draft}}
        for p, new in draft.items():
            live[p] = new
        if not build_ok:
            {rollback}
            return "failed"
        return "ok"
    """


PUBLISH_TESTS = """
import publisher

def test_publish_failed_build_rolls_back():
    live = {"a.html": "1", "b.html": "1"}
    assert publisher.publish(live, {"a.html": "2", "b.html": "2"}, build_ok=False) == "failed"
    assert live == {"a.html": "1", "b.html": "1"}

def test_publish_applies_whole():
    live = {"a.html": "1"}
    publisher.publish(live, {"a.html": "2"}, build_ok=True)
    assert live == {"a.html": "2"}
"""


def test_publish_integrity_catches_half_apply_and_passes_clean(tmp_path):
    gate = "correct.product.publish-integrity"
    half = repo(tmp_path / "half", {"publisher.py": publisher(restore=False), "tests/unit/test_publish.py": PUBLISH_TESTS,
                                    "tests/api/test_x.py": "def test_ok(): pass\n"})
    assert sense(gate, half)[0] == "caught"
    untested = repo(tmp_path / "untested", {"publisher.py": publisher(restore=True), "tests/unit/test_misc.py": "def test_ok(): pass\n",
                                            "tests/api/test_x.py": "def test_ok(): pass\n"})
    assert sense(gate, untested)[0] == "caught"
    clean = repo(tmp_path / "clean", {"publisher.py": publisher(restore=True), "tests/unit/test_publish.py": PUBLISH_TESTS,
                                      "tests/api/test_x.py": "def test_ok(): pass\n"})
    assert sense(gate, clean)[0] == "met"
