from pathlib import Path

from glevels import registry

GATES = registry.load_gates()
CHARTERS = registry.load_charters()


def test_registry_lints_clean():
    problems, _ = registry.lint(GATES, CHARTERS)
    assert problems == []


def test_covers_files_and_parent_dirs():
    globs = ["pages/*/content.html"]
    assert registry.covers("pages/charter/content.html", globs)
    assert registry.covers("pages/charter", globs)
    assert registry.covers("pages", globs)
    assert not registry.covers("pages/charter/config.json", globs)
    assert not registry.covers("static", globs)


def test_narrow_charter_extends_and_waives():
    cid, scoped = registry.in_scope(Path("/x/g-site"), "pages/charter", CHARTERS)
    ids = {e["id"]: e for e in scoped}
    assert cid == "g-site"
    assert "copy-voice" in ids and "no-secrets" in ids
    assert ids["service-owner-files"]["waiver"]["approved_by"] == "Gabriel"
    assert ids["no-secrets"]["waiver"] is None


def test_other_repos_get_the_system_charter_only():
    cid, scoped = registry.in_scope(Path("/x/anything"), "", CHARTERS)
    assert cid == "g-system"
    assert "copy-voice" not in {e["id"] for e in scoped}
    assert all(e["waiver"] is None for e in scoped)


def test_lint_catches_pin_drift_and_bare_exceptions():
    charters = {"c": {"id": "c", "repos": ["*"], "gates": [{"id": "no-secrets", "version": 9}],
                      "exceptions": [{"gate": "no-secrets", "reason": "x"}]}}
    problems, _ = registry.lint(GATES, charters)
    assert any("pins no-secrets v9" in p for p in problems)
    assert any("missing review, approved_by" in p for p in problems)


def test_design_gates_name_their_stopping_rule():
    bad = dict(GATES["feature-ideas"])
    del bad["saturation"]
    problems, _ = registry.lint({"feature-ideas": bad}, {})
    assert any("saturation" in p for p in problems)


def test_lint_enforces_links():
    bad = dict(GATES["copy-voice"], skill="no-such-skill")
    problems, _ = registry.lint({"copy-voice": bad}, {})
    if registry.SKILLS.is_dir():
        assert any("skill no-such-skill not found" in p for p in problems)
    charters = {"c": {"id": "c", "repos": ["*"], "gates": [],
                      "exceptions": [{"gate": "no-secrets", "reason": "x", "review": "2099-01-01", "approved_by": "G"}]}}
    problems, warnings = registry.lint(GATES, charters)
    assert any("which no charter in its chain pins" in p for p in problems)
    assert any("no charter pins it" in w for w in warnings)


def test_design_gates_need_a_probe():
    bad = dict(GATES["feature-ideas"])
    del bad["probe"]
    problems, _ = registry.lint({"feature-ideas": bad}, {})
    assert any("probe" in p for p in problems)
