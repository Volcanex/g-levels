import json

import pytest

from glevels import connections

MANIFEST = {"hosts": [{"id": "h"}], "projects": [{"id": "g-studio"}], "services": [{"id": "brain-mcp"}],
            "system": [{"id": "gcore"}], "dependencies": [{"id": "ntfy"}], "workspaces": [{"number": 0}]}
GOOD = """
[tool.gcore]
depends = ["ntfy"]

[[tool.gcore.connects]]
to = "g-studio"
via = "http"
flow = "reads owner home and projects through /api/app/*"

[[tool.gcore.connects]]
to = "gcore"
via = "file"
flow = "reads manifest.json for the topology"
dir = "in"
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv("GCORE_MANIFEST", str(manifest))
    r = tmp_path / "repo"
    r.mkdir()
    return r


def run(repo, text, capsys):
    if text is not None:
        (repo / "pyproject.toml").write_text(text)
    code = connections.main([str(repo)])
    return code, capsys.readouterr().out


def test_good_declaration_passes(repo, capsys):
    code, out = run(repo, GOOD, capsys)
    assert code == 0, out
    assert "out g-studio via http" in out and "in gcore via file" in out


def test_empty_connects_is_a_positive_assertion(repo, capsys):
    code, out = run(repo, '[tool.gcore]\ndepends = []\nconnects = []\n', capsys)
    assert code == 0 and "declares no connections" in out


def test_missing_connects_fails(repo, capsys):
    code, out = run(repo, '[tool.gcore]\ndepends = ["ntfy"]\n', capsys)
    assert code == 1 and "has no connects" in out


def test_missing_table_and_missing_pyproject_fail(repo, capsys):
    assert run(repo, '[project]\nname = "x"\n', capsys)[0] == 1
    (repo / "pyproject.toml").unlink()
    code, out = run(repo, None, capsys)
    assert code == 1 and "no pyproject.toml" in out


@pytest.mark.parametrize("entry, problem", [
    ('to = "g-sudio"\nvia = "http"\nflow = "x"', "'g-sudio' is not an id in the manifest"),
    ('to = "h"\nvia = "carrier-pigeon"\nflow = "x"', "via 'carrier-pigeon' is not one of"),
    ('to = "h"\nflow = "x"', "no via"),
    ('to = "ntfy"\nvia = "http"\nflow = "  "', "no flow"),
    ('via = "ssh"\nflow = "x"', "no to"),
    ('to = "brain-mcp"\nvia = "mcp"\nflow = "x"\ndir = "sideways"', "dir 'sideways' is not one of"),
    ('to = "brain-mcp"\nvia = "mcp"\nflow = "x"\ndirection = "in"', "unknown key direction"),
])
def test_malformed_entries_fail_naming_the_problem(repo, capsys, entry, problem):
    code, out = run(repo, f"[tool.gcore]\ndepends = []\n\n[[tool.gcore.connects]]\n{entry}\n", capsys)
    assert code == 1 and problem in out, out


def test_connects_must_be_tables_and_toml_must_parse(repo, capsys):
    assert "must be a list of tables" in run(repo, '[tool.gcore]\nconnects = "g-studio"\n', capsys)[1]
    assert "is not a table" in run(repo, '[tool.gcore]\nconnects = ["g-studio"]\n', capsys)[1]
    assert "does not parse" in run(repo, '[tool.gcore\n', capsys)[1]


def test_unreadable_manifest_cannot_run(repo, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("GCORE_MANIFEST", str(tmp_path / "nowhere.json"))
    code, out = run(repo, GOOD, capsys)
    assert code == 2 and "cannot read the manifest" in out
