import sys
import types
from types import SimpleNamespace

import pytest

from comparison import executive_py2026_py2027_report as ex
from comparison import live_executive_runner as runner

NOW = "2026-11-09T12:00:00+00:00"


def fake_run(exit_code=0, ce=950000, series=((("2026-11-01", 2534),))):
    passive = [SimpleNamespace(metric=SimpleNamespace(metric_id=mid), result=SimpleNamespace(value=v, retrieved_at=NOW))
               for mid, v in (("percent_effectuated", 0.62), ("new_enrolled", 4400), ("active_enrolled", 58800),
                              ("passive_enrolled", 734500), ("applications_started", 400000),
                              ("submitted_applications", 300000))]
    passive.append(SimpleNamespace(metric=SimpleNamespace(metric_id="other"), result=None))
    rows = [SimpleNamespace(metric_id="total_applicants", status="VALUE_EXTRACTED", value=500000, series_points=()),
            SimpleNamespace(metric_id="cancelled_policies_daily", status="SERIES_EXTRACTED", value=None,
                            series_points=series[0] if series else ()),
            SimpleNamespace(metric_id="terminated_policies_daily", status="VALIDATION_FAILED", value=None,
                            series_points=())]
    result = None if ce is None else SimpleNamespace(value=ce, retrieved_at=NOW)
    return SimpleNamespace(exit_code=exit_code, result=result, passive_values=tuple(passive), runtime_rows=tuple(rows),
                           snapshot_status="SAVED", failure=None if exit_code == 0 else "SisenseError")


def test_live_values_come_only_from_this_run():
    values, series = runner.live_py2027_values(fake_run(), NOW)
    assert values["currently_enrolled_people"] == {"value": 950000, "retrieved_at": NOW}
    assert values["total_applicants"]["value"] == 500000
    assert "other" not in values
    assert series == {"cancelled_policies_daily": {"points": [{"date": "2026-11-01", "value": 2534}],
                                                   "retrieved_at": NOW}}
    assert runner.live_py2027_values(None, NOW) == ({}, {})


def _install_fake_main(monkeypatch, run):
    fake = types.ModuleType("main")
    fake.EXIT_OK = 0
    fake.run_pipeline = lambda settings: run
    fake.configure_logging = lambda: None
    monkeypatch.setitem(sys.modules, "main", fake)
    monkeypatch.setattr(runner, "sisense_settings", lambda: SimpleNamespace(history_db_path=None))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "PHASE_6B_REPORT", tmp_path / "6b.json")
    monkeypatch.setattr(ex, "PHASE_6D_REPORT", tmp_path / "6d.json")
    monkeypatch.setattr(ex, "VALIDATION_REPORT", tmp_path / "v.json")
    monkeypatch.setattr(ex, "EXECUTIVE_DIR", tmp_path / "executive")
    monkeypatch.setattr("comparison.lifecycle.resolve_history_path", lambda: tmp_path / "none.sqlite3")
    return tmp_path


def test_sisense_success_is_live(isolated, monkeypatch):
    _install_fake_main(monkeypatch, fake_run())
    model, _, _ = runner.refresh_py2027()
    assert model["freshness"] == ex.LIVE
    assert model["metrics"]["currently_enrolled_people"]["freshness"] == ex.LIVE


def test_sisense_failure_never_live(isolated, monkeypatch):
    _install_fake_main(monkeypatch, fake_run(exit_code=1, ce=None))
    model, _, _ = runner.refresh_py2027()
    assert model["freshness"] == ex.UNAVAILABLE
    assert all(item["freshness"] != ex.LIVE for item in model["metrics"].values())


def test_db_failure_uses_stale_fallback(isolated, monkeypatch):
    (isolated / "6b.json").write_text('{"currently_enrolled_candidates": []}', encoding="utf-8")
    fake = types.ModuleType("database.comparison.py2026_population_diagnostics")
    fake.retrieve_live = lambda: ({"failures": []}, False)
    fake.write_report = lambda report: pytest.fail("fallback must not overwrite the Phase 6B artifact")
    monkeypatch.setitem(sys.modules, "database.comparison.py2026_population_diagnostics", fake)
    import database.comparison as package
    monkeypatch.setattr(package, "py2026_population_diagnostics", fake, raising=False)
    py2026 = runner.refresh_py2026()
    assert py2026["freshness"] == ex.STALE and "PY2026 DB unavailable" in py2026["note"]


def test_db_success_is_live_and_refreshes_artifact(isolated, monkeypatch):
    written = []
    fake = types.ModuleType("database.comparison.py2026_population_diagnostics")
    fake.retrieve_live = lambda: ({"currently_enrolled_candidates": []}, True)
    fake.write_report = written.append
    monkeypatch.setitem(sys.modules, "database.comparison.py2026_population_diagnostics", fake)
    import database.comparison as package
    monkeypatch.setattr(package, "py2026_population_diagnostics", fake, raising=False)
    py2026 = runner.refresh_py2026()
    assert py2026["freshness"] == ex.LIVE and written


def test_summary_is_concise_and_safe(isolated, monkeypatch, capsys):
    _install_fake_main(monkeypatch, fake_run())
    monkeypatch.setattr(runner, "refresh_py2026", lambda: ex.py2026_live({"currently_enrolled_candidates": []}, NOW))
    assert runner.main() == 0
    out = capsys.readouterr().out
    lines = {line.split()[0] + " " + line.split()[1]: line for line in out.splitlines() if len(line.split()) >= 3}
    assert lines["PY2026 DB"].split()[-1] == "LIVE_THIS_RUN"
    assert lines["PY2027 Sisense"].split()[-1] == "LIVE_THIS_RUN"
    assert "950,000" in out and "Executive report" in out
    assert "{" not in out and "cookie" not in out.lower() and "token" not in out.lower()
    assert (isolated / "executive" / ex.HTML_NAME).is_file()
