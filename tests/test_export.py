import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from glevels import export, registry


@pytest.fixture
def project_registry():
    gates = {
        "correct.test.daily": {"version": 2, "sensor": {"type": "review"}},
        "correct.test.milestone": {"trigger": "milestone", "sensor": {"type": "command"}},
        "correct.test.demand": {"trigger": "on-demand", "sensor": {"type": "review"}},
        "correct.test.hook": {"sensor": {"type": "hook"}},
        "correct.test.waived": {"sensor": {"type": "hook"}},
    }
    charters = {
        "system": {"id": "system", "title": "System", "repos": ["*"],
                   "gates": [{"id": gid, "version": 1, "blocking": gid != "correct.test.waived"} for gid in gates],
                   "exceptions": [{"gate": "correct.test.waived", "repo": "sample", "reason": "private waiver",
                                   "approved_by": "private approver", "review": "2099-01-01"}]},
        "project": {"id": "project", "title": "Project", "extends": ["system"], "repos": ["sample", "sample-old"],
                    "gates": [{"id": "correct.test.daily", "version": 1, "blocking": True, "paths": ["src/**"]}]},
        "peer": {"id": "peer", "extends": ["system"], "repos": ["peer"]},
    }
    return gates, charters


def test_project_states_without_rows(project_registry):
    gates, charters = project_registry
    view = export.project_view(["sample"], gates, charters, log=[])
    assert set(view) == {"repos", "charter", "chain", "gates", "counts", "blocking"}
    assert view["repos"] == ["sample"]
    assert view["charter"] == "project"
    assert view["chain"] == [{"id": "system", "title": "System"}, {"id": "project", "title": "Project"}]
    assert [(g["gate"], g["state"]) for g in view["gates"]] == [
        ("correct.test.daily", "silent"), ("correct.test.milestone", "not-run"),
        ("correct.test.demand", "not-run"), ("correct.test.hook", "hook"), ("correct.test.waived", "waived"),
    ]
    assert view["gates"][0] == {
        "gate": "correct.test.daily", "charter": "project", "version": 1, "trigger": "every-run",
        "sensor": "review", "blocking": True, "waived": False, "state": "silent", "last": None, "result": None,
    }
    assert all(set(g) == set(view["gates"][0]) for g in view["gates"])
    assert all(g["last"] is None and g["result"] is None for g in view["gates"])
    assert view["gates"][1]["charter"] == "system"
    assert view["gates"][-1]["waived"] is True
    assert view["counts"] == {"met": 0, "attempted": 0, "waived": 1, "impossible": 0, "caught": 0,
                              "silent": 1, "not-run": 2, "hook": 1}
    assert view["blocking"] == {"total": 4, "silent": 1, "failing": 0}


def test_latest_row_across_repo_aliases_ignores_grades_and_other_repos(project_registry):
    gates, charters = project_registry
    log = [
        {"gate": "correct.test.daily", "repo": "sample-old", "path": "other/file.py",
         "ts": "2026-10-20T12:00:00", "result": "attempted"},
        {"gate": "correct.test.daily", "repo": "sample", "path": "src/file.py",
         "ts": "2026-10-19T12:00:00", "result": "met"},
        {"gate": "correct.test.daily", "repo": "sample", "ts": "2026-10-21T12:00:00", "result": "grade"},
        {"gate": "correct.test.milestone", "repo": "sample", "ts": "2026-10-21T12:00:00", "result": "grade"},
        {"gate": "correct.test.daily", "repo": "another", "ts": "2026-10-22T12:00:00", "result": "caught"},
    ]
    view = export.project_view(["sample", "sample-old"], gates, charters, log)
    daily, milestone = view["gates"][:2]
    assert (daily["state"], daily["result"], daily["last"]) == ("attempted", "attempted", "2026-10-20T12:00:00")
    assert (milestone["state"], milestone["result"], milestone["last"]) == ("not-run", None, None)
    assert view["counts"]["attempted"] == 1
    assert view["blocking"] == {"total": 4, "silent": 0, "failing": 0}


@pytest.mark.parametrize("result", registry.RESULTS + ("caught",))
def test_logged_result_overrides_waiver_and_hook(project_registry, result):
    gates, charters = project_registry
    log = [{"gate": gid, "repo": "sample", "ts": "2026-10-20T12:00:00", "result": result}
           for gid in ("correct.test.hook", "correct.test.waived")]
    view = export.project_view(["sample"], gates, charters, log)
    assert [(g["state"], g["result"]) for g in view["gates"][-2:]] == [(result, result), (result, result)]
    assert view["gates"][-1]["waived"] is True


def test_blocking_counts_only_blocking_silent_and_failing_gates(project_registry):
    gates, charters = project_registry
    log = [{"gate": gid, "repo": "sample", "ts": "2026-10-20T12:00:00", "result": result}
           for gid, result in (("correct.test.milestone", "caught"), ("correct.test.demand", "impossible"),
                               ("correct.test.waived", "caught"))]
    view = export.project_view(["sample"], gates, charters, log)
    assert view["blocking"] == {"total": 4, "silent": 1, "failing": 2}
    assert view["counts"] == {"met": 0, "attempted": 0, "waived": 0, "impossible": 1, "caught": 2,
                              "silent": 1, "not-run": 0, "hook": 1}


def test_project_output_has_no_private_details(project_registry):
    gates, charters = project_registry
    log = [{"gate": "correct.test.daily", "repo": "sample", "ts": "2026-10-20T12:00:00", "result": "met",
            "note": "private incident", "by": "private reviewer", "path": "private/file.py"}]
    for view in (export.project_view(["sample"], gates, charters, log),
                 export.projects_view({"sample": ["sample"]}, gates, charters, log)):
        encoded = json.dumps(view)
        for key in ("note", "reason", "by", "approved_by", "path", "paths"):
            assert f'"{key}"' not in encoded
        assert "private" not in encoded


def test_unknown_repo_uses_wildcard_charter():
    view = export.project_view(["unknown-repo"], log=[])
    assert view["charter"] == "g-system"
    assert [c["id"] for c in view["chain"]] == ["g-system"]
    assert view["gates"]


def test_wing_uses_narrow_charter_and_inherits_pins():
    view = export.project_view(["wing"], log=[])
    assert view["charter"] == "wing"
    assert [c["id"] for c in view["chain"]] == ["g-system", "delivery", "wing"]
    pins = {g["gate"]: g["charter"] for g in view["gates"]}
    assert pins["correct.secrets.argv"] == "g-system"
    assert pins["correct.delivery.verify-before-claiming-done"] == "delivery"
    assert pins["correct.product.server-enforced-rules"] == "wing"


@pytest.mark.parametrize("names, expected", [
    (["unknown", "sample"], "project"), (["sample", "unknown"], "project"),
    (["sample", "peer"], "project"), (["peer", "sample"], "peer"),
])
def test_longest_chain_wins_with_earlier_name_breaking_ties(project_registry, names, expected):
    gates, charters = project_registry
    assert export.project_view(names, gates, charters, log=[])["charter"] == expected


def test_winning_alias_supplies_waivers(project_registry):
    gates, charters = project_registry
    view = export.project_view(["sample-old", "sample"], gates, charters, log=[])
    assert view["gates"][-1]["waived"] is False
    assert view["gates"][-1]["state"] == "hook"


def test_empty_inputs_do_not_load_defaults(monkeypatch):
    for name in ("load_gates", "load_charters"):
        monkeypatch.setattr(registry, name, Mock(side_effect=AssertionError("unexpected registry read")))
    monkeypatch.setattr(export, "rows", Mock(side_effect=AssertionError("unexpected log read")))
    view = export.project_view(["unknown"], gates={}, charters={}, log=[])
    assert view == {"repos": ["unknown"], "charter": None, "chain": [], "gates": [],
                    "counts": {"met": 0, "attempted": 0, "waived": 0, "impossible": 0, "caught": 0,
                               "silent": 0, "not-run": 0, "hook": 0},
                    "blocking": {"total": 0, "silent": 0, "failing": 0}}
    assert export.projects_view({}, gates={}, charters={}, log=[]) == {}


def test_projects_view_loads_each_source_once(project_registry, monkeypatch):
    gates, charters = project_registry
    log = [{"gate": "correct.test.daily", "repo": "sample-old", "ts": "2026-10-20T12:00:00", "result": "met"}]
    monkeypatch.setattr(registry, "load_gates", Mock(return_value=gates))
    monkeypatch.setattr(registry, "load_charters", Mock(return_value=charters))
    monkeypatch.setattr(export, "rows", Mock(return_value=log))
    view = export.projects_view({"current": ["sample", "sample-old"], "other": ["peer"]})
    registry.load_gates.assert_called_once_with()
    registry.load_charters.assert_called_once_with()
    export.rows.assert_called_once_with()
    assert view == {
        "current": {"charter": "project", "counts": {"met": 1, "attempted": 0, "waived": 1, "impossible": 0,
                                                     "caught": 0, "silent": 0, "not-run": 2, "hook": 1},
                    "blocking": {"total": 4, "silent": 0, "failing": 0}, "total": 5},
        "other": {"charter": "peer", "counts": {"met": 0, "attempted": 0, "waived": 0, "impossible": 0,
                                                "caught": 0, "silent": 1, "not-run": 2, "hook": 2},
                  "blocking": {"total": 4, "silent": 1, "failing": 0}, "total": 5},
    }


@pytest.mark.parametrize("names", [["g-command-web"], ["unknown-repo", "wing"]])
def test_project_cli(tmp_path, names):
    log = [{"gate": "correct.secrets.argv", "repo": names[-1], "ts": "2026-10-20T12:00:00", "result": "met"}]
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in log) + "\n")
    env = dict(os.environ, GLEVELS_LOG=str(path))
    p = subprocess.run([sys.executable, "-m", "glevels.export", "project", *names], cwd=registry.ROOT, env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == export.project_view(names, log=log)
