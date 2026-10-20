import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from glevels import claims

FIXTURES = Path(__file__).parent / "fixtures-claims"
SCRIPT = Path(claims.__file__)


def run(tmp_path, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path / "out"),
                           "--now", "2026-10-20", *map(str, args)], capture_output=True, text=True)


def checks(html):
    return claims.inspect_page("fixture", claims.html_units(html), date(2026, 10, 20))


def test_original_faults_and_report(tmp_path):
    p = run(tmp_path, FIXTURES / "broken.html")
    assert p.returncode == 1, p.stdout + p.stderr
    for word in ("blocking (", "for the reviewer (", "NaN", "140%", "2099-01-01", "negative count", "measurement window", "#1"):
        assert word in p.stdout
    saved = json.loads((tmp_path / "out/findings.json").read_text())
    assert {f["check"] for f in saved["findings"] if f["severity"] == "block"} >= {
        "broken-value", "percentage-over-100", "future-date", "negative-count", "empty-value"}
    assert not p.stderr


def test_clean_page(tmp_path):
    p = run(tmp_path, "--json", FIXTURES / "clean.html")
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(p.stdout)
    assert data["findings"] == []
    assert data == json.loads((tmp_path / "out/findings.json").read_text())


def test_related_conflicts_series_and_geography(tmp_path):
    p = run(tmp_path, "--json", FIXTURES / "related.html")
    assert p.returncode == 1, p.stdout + p.stderr
    fs = json.loads(p.stdout)["findings"]
    assert {f["check"] for f in fs} >= {"conflicting-metric", "repeated-series", "round-series", "outlier-country", "outlier-currency"}
    assert all(f["severity"] == "review" for f in fs if f["check"] != "conflicting-metric")


@pytest.mark.parametrize("value", ["NaN", "undefined", "null", "None", "Infinity", "[object Object]", "Invalid Date", "$NaN", "{{value}}", "{value}", "-", "--", "0 of 0"])
def test_broken_values_in_rows(value):
    fs = checks(f"<table><tr><th>Visitors</th><td>{value}</td></tr></table>")
    assert any(f["severity"] == "block" for f in fs)


@pytest.mark.parametrize("text", ["Share of total 140%", "140% share of voice", "Conversion rate: 101%", "Users -4", "-3 visits", "Views: −1", "Checked 21 October 2026", "Last run: October 21, 2026"])
def test_related_clear_faults(text):
    assert any(f["severity"] == "block" for f in checks(f"<p>{text}</p>"))


@pytest.mark.parametrize("text", ["Revenue growth 140% vs previous month", "Visits up 12% vs previous week", "None of these are customers", "Checked 20 October 2026", "Visitors 12 last 7 days"])
def test_ordinary_prose_and_valid_values(text):
    assert not any(f["severity"] == "block" for f in checks(f"<p>{text}</p>"))


def test_conflicting_values_allow_different_windows_and_formatting():
    html = '<div class="card">Visitors 1,204 last 30 days</div><div class="card">Visitors 980 last 7 days</div>'
    assert not any(f["check"] == "conflicting-metric" for f in checks(html))
    assert not any(f["check"] == "conflicting-metric" for f in checks(html.replace("980 last 7", "1204 last 30")))
    assert any(f["check"] == "conflicting-metric" for f in checks(html.replace("last 7", "last 30")))


def test_equivalent_decimals_and_negative_percent_are_not_clear_faults():
    html = '<div class="card">Visitors 1204</div><div class="card">Visitors 1204.0</div><p>Share −140%</p>'
    assert not any(f["severity"] == "block" for f in checks(html))


def test_inline_value_elements_and_signed_deltas():
    fs = checks('<div><span>Visits</span><strong>-3</strong></div><p>Visitors +12</p>')
    assert any(f["check"] == "negative-count" for f in fs)
    assert any(f["check"] == "missing-comparison" for f in fs)


def test_percentage_label_does_not_leak_into_a_growth_delta():
    fs = checks('<div class="card">Share of voice 24% Growth +140% vs previous month</div>')
    assert not any(f["check"] == "percentage-over-100" for f in fs)


def test_no_window_and_unsourced_claim_only_lead(tmp_path):
    page = tmp_path / "leads.html"
    page.write_text('<div class="card">Views 123</div><p>#1 local audit</p>')
    p = run(tmp_path, "--json", page)
    assert p.returncode == 0
    assert {f["check"] for f in json.loads(p.stdout)["findings"]} == {"missing-window", "unsourced-claim"}


def test_data_missing_and_coverage(tmp_path):
    p = run(tmp_path, FIXTURES / "clean.html", "--data", FIXTURES / "missing.json")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "0 of 3 records sourced and dated" in p.stdout
    for name in ("record 0 (Unattributed fare)", "record 1 (Undated quote)", "record 2 (Source and date missing)"):
        assert name in p.stdout


def test_data_option_before_target(tmp_path):
    p = run(tmp_path, "--data", FIXTURES / "missing.json", FIXTURES / "sourced.json", FIXTURES / "clean.html")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "0 of 3 records sourced and dated" in p.stdout
    assert "3 of 3 records sourced and dated" in p.stdout


@pytest.mark.parametrize("value,check", [("1204", "missing-window"), ("-", "empty-value"), ("--", "empty-value"), ("-3", "negative-count")])
def test_walk_text_keeps_separate_label_and_value_together(value, check):
    fs = claims.inspect_page("walk", claims.text_units(f"Visitors\n{value}\n"), date(2026, 10, 20))
    assert any(f["check"] == check for f in fs)


def test_json_and_csv_data_and_require(tmp_path):
    p = run(tmp_path, FIXTURES / "clean.html", "--json", "--data", FIXTURES / "sourced.json")
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)["coverage"][0]["both"] == 3
    p = run(tmp_path, FIXTURES / "clean.html", "--json", "--data", FIXTURES / "records.csv")
    assert p.returncode == 1
    assert json.loads(p.stdout)["coverage"][0]["both"] == 1
    p = run(tmp_path, FIXTURES / "clean.html", "--json", "--data", FIXTURES / "missing.json", "--require", "source")
    fs = json.loads(p.stdout)["findings"]
    assert len(fs) == 2
    assert all(f["detail"] == "missing source" for f in fs)


def test_single_record_object_and_zero_price():
    fs, counts = claims.inspect_data(str(FIXTURES / "sourced.json"), {"source", "as_of"})
    assert not fs and counts["records"] == 3
    assert list(claims.records({"price": 0, "name": "Free"})) == [{"price": 0, "name": "Free"}]


def test_walk_reads_each_text_file(tmp_path):
    p = run(tmp_path, "--json", FIXTURES / "walk")
    assert p.returncode == 1
    data = json.loads(p.stdout)
    assert len(data["pages"]) == 2
    assert any(f["check"] == "negative-count" for f in data["findings"])


def test_url_fetch_without_network(monkeypatch):
    class Response:
        from email.message import Message
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return (FIXTURES / "clean.html").read_bytes()

    monkeypatch.setattr(claims.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    text, html = claims.read_target("https://example.test/report")
    assert html and not checks(text)


@pytest.mark.parametrize("args", [("absent.html",), ("--now", "nonsense", "clean.html"), ("--require", "unknown", "clean.html")])
def test_unreadable_input_is_impossible(tmp_path, args):
    p = run(tmp_path, *args)
    assert p.returncode == 2
    assert p.stdout.startswith("impossible:")
    assert "Traceback" not in p.stderr


def test_axis_ticks_with_two_values_are_leads_not_faults():
    html = "<p>Jun 19 mentions</p><p>Jun 141 mentions</p><p>Visitors: 120</p><p>Visitors: 98</p>"
    findings = claims.inspect_page("deck.html", claims.html_units(html), date(2026, 10, 20))
    conflicts = {(f["detail"].split("'")[1], f["severity"]) for f in findings if f["check"] == "conflicting-metric"}
    assert ("visitors", "block") in conflicts
    assert not any(sev == "block" for label, sev in conflicts if label != "visitors")


def test_provenance_fields_match_by_word(tmp_path):
    data = tmp_path / "venues.json"
    data.write_text(json.dumps([{"name": "A", "price_gbp": 90, "availability_evidence": "owner email", "checked_on": "2026-10-14"},
                                {"name": "B", "price_gbp": 80, "availability_evidence": "owner email"}]))
    fs, counts = claims.inspect_data(str(data), {"source", "as_of"})
    assert counts["both"] == 1 and len(fs) == 1 and "(B)" in fs[0]["selector"]
