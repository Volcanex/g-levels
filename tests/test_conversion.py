import json

import pytest

from glevels import conversion


def cap(cid, **over):
    row = {"id": cid, "kind": "view", "what": "A score out of 100 for the site", "where": "content.html:10",
           "endpoint": None, "writes": False, "spends": None, "status": "missing", "evidence": ""}
    row.update(over)
    return row


def page(caps, **over):
    data = {"old": "/start", "source": "geo/pages/start/content.html", "title": "Start",
            "purpose": "Names a site and starts the first audits", "capabilities": caps}
    data.update(over)
    return data


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "convert/start/parity").mkdir(parents=True)
    (tmp_path / "views/start").mkdir(parents=True)
    return tmp_path


def run(repo, pages, source, capsys, *extra):
    for name, data in pages.items():
        (repo / "convert/start/parity" / f"{name}.json").write_text(json.dumps(data))
    if source is not None:
        (repo / "views/start/view.js").write_text(source)
    code = conversion.main([str(repo), *extra])
    return code, capsys.readouterr().out


MET = dict(status="met", evidence="views/start/view.js Score, seen on the stand-in at 390 and 1440")
SOURCE = "const CALLS = {status: '/api/geo/onboarding/status', one: `/api/geo/reports/${id}/full`}\n"


def test_an_inventory_nobody_has_built_is_all_catches(repo, capsys):
    caps = [cap("start.01", kind="state", what="Waiting, the four audits tick over one by one"),
            cap("start.02", endpoint="GET /api/geo/onboarding/status?save_id="),
            cap("start.03", kind="action", endpoint="POST /api/geo/onboarding/email", writes=True, spends="one email sent")]
    code, out = run(repo, {"start": page(caps)}, None, capsys)
    assert code == 1
    assert "0 of 3 met, 3 missing" in out and "blocking: 3" in out
    assert "missing start.01 state: Waiting" in out
    assert "writes or spends, try it on staging only, spends: one email sent" in out


def test_summary_prints_counts_only(repo, capsys):
    code, out = run(repo, {"start": page([cap("start.01")])}, None, capsys, "--summary")
    assert code == 1 and "blocking: 1" in out and "missing start.01" not in out


def test_clean_flow_passes(repo, capsys):
    caps = [cap("start.01", endpoint="GET /api/geo/onboarding/status?save_id=", **MET),
            cap("start.02", kind="drill", endpoint="GET /api/geo/reports/{id}/full; GET /api/geo/onboarding/status", **MET),
            cap("start.03", kind="state", what="Empty: no audits yet", **MET)]
    code, out = run(repo, {"start": page(caps, view=["views/start"])}, SOURCE, capsys)
    assert code == 0, out
    assert "3 of 3 met, 0 missing" in out and "blocking" not in out


def test_met_with_no_evidence_is_refused(repo, capsys):
    caps = [cap("start.01", status="met", evidence=" ")]
    code, out = run(repo, {"start": page(caps, view=["views/start"])}, SOURCE, capsys)
    assert code == 2 and "start.01: met with no evidence" in out


def test_met_with_no_view_to_check_is_refused(repo, capsys):
    code, out = run(repo, {"start": page([cap("start.01", **MET)])}, None, capsys)
    assert code == 2 and "marked met but no view source is named" in out


def test_a_builder_cannot_waive_alone(repo, capsys):
    code, out = run(repo, {"start": page([cap("start.01", status="waived")])}, None, capsys)
    assert code == 2 and "only the owner waives" in out


def test_an_owner_waiver_passes_and_is_always_shown(repo, capsys):
    caps = [cap("start.01", status="waived", waived_by="Gabriel", reason="The old tooltip repeated the heading", review="2027-01-15")]
    code, out = run(repo, {"start": page(caps)}, None, capsys)
    assert code == 0 and "1 waived" in out and "waived by Gabriel until 2027-01-15" in out


def test_met_but_the_view_never_calls_the_endpoint(repo, capsys):
    caps = [cap("start.01", endpoint="GET /api/geo/starter_snapshot?run_id=", **MET)]
    code, out = run(repo, {"start": page(caps, view=["views/start"])}, SOURCE, capsys)
    assert code == 1
    assert "marked met, but the view's source never names /api/geo/starter_snapshot" in out


def test_a_call_the_old_screen_never_made_is_a_catch(repo, capsys):
    caps = [cap("start.01", endpoint="GET /api/geo/onboarding/status", **MET),
            cap("start.02", endpoint="GET /api/geo/reports/{id}/full", **MET)]
    source = SOURCE + "fetch('/api/core/auth/me')\n"
    code, out = run(repo, {"start": page(caps, view=["views/start"])}, source, capsys)
    assert code == 1
    assert "view.js calls /api/core/auth/me, which the old screen never called" in out


def test_an_added_call_needs_a_reason_and_becomes_a_lead(repo, capsys):
    caps = [cap("start.01", endpoint="GET /api/geo/onboarding/status", **MET),
            cap("start.02", endpoint="GET /api/geo/reports/{id}/full", **MET)]
    source = SOURCE + "fetch('/api/core/auth/actor')\n"
    added = [{"endpoint": "GET /api/core/auth/actor", "why": "Reads who is signed in without creating an anonymous user"}]
    code, out = run(repo, {"start": page(caps, view=["views/start"], added=added)}, source, capsys)
    assert code == 0, out
    assert "added endpoint /api/core/auth/actor" in out
    code, out = run(repo, {"start": page(caps, view=["views/start"], added=[{"endpoint": "GET /api/core/auth/actor"}])}, source, capsys)
    assert code == 2 and "an added line needs an endpoint under /api/ and a why" in out


def test_a_path_built_from_a_prefix_is_a_lead_not_a_pass(repo, capsys):
    caps = [cap("start.01", endpoint="GET /api/geo/reports/{id}/full", **MET)]
    source = "const one = '/api/geo/reports/' + id + '/full'\n"
    code, out = run(repo, {"start": page(caps, view=["views/start"])}, source, capsys)
    assert code == 0, out
    assert "builds a path from /api/geo/reports, read the full path by hand" in out


def test_one_flow_shares_endpoints_across_its_pages(repo, capsys):
    first = page([cap("start.01", endpoint="GET /api/geo/onboarding/status", **MET)], view=["views/start"])
    second = page([cap("results.01", endpoint="GET /api/geo/reports/{id}/full", **MET)], old="/start/results")
    code, out = run(repo, {"start": first, "results": second}, SOURCE, capsys)
    assert code == 0, out
    assert "across 2 old pages" in out


def test_a_page_the_owner_retired_is_not_counted(repo, capsys):
    kept = page([cap("start.01", endpoint="GET /api/geo/onboarding/status", **MET),
                 cap("start.02", endpoint="GET /api/geo/reports/{id}/full", **MET)], view=["views/start"])
    gone = page([cap("trial.01")], old="/trial", decision="retire", decision_by="owner", decision_reason="Folded into the first screen")
    code, out = run(repo, {"start": kept, "trial": gone}, SOURCE, capsys)
    assert code == 0, out
    proposed = dict(gone, decision_by="proposed, the owner has not ruled")
    code, out = run(repo, {"start": kept, "trial": proposed}, SOURCE, capsys)
    assert code == 1 and "missing trial.01" in out


def test_broken_files_are_refused(repo, capsys):
    (repo / "convert/start/parity/bad.json").write_text("{not json")
    code, out = run(repo, {"start": page([cap("a.01"), cap("a.01", kind="graph")])}, None, capsys)
    assert code == 2
    assert "bad.json: does not read as JSON" in out and "a.01: id used twice" in out and "kind 'graph'" in out


def test_no_parity_files_cannot_run(tmp_path, capsys):
    assert conversion.main([str(tmp_path)]) == 2
    assert "impossible: no parity files found" in capsys.readouterr().out
