"""Phase 7B: one command, live end to end.

    LIVE DB REFRESH        database.comparison.py2026_population_diagnostics.retrieve_live
          ↓                (approved ActiveDirectoryInteractive layer; read-only aggregates)
    LIVE SISENSE REFRESH   main.run_pipeline (approved browser/auth flow; verified natural capture)
          ↓
    VALIDATE / SAVE        existing ReportingPipeline inside run_pipeline
          ↓
    BUILD MODEL            values retrieved in THIS invocation; history only for trend/fallback
          ↓
    CHARTS + HTML/JSON     comparison.executive_py2026_py2027_report

A source that fails is never presented as current: its values come from the
last validated artifact as STALE_FALLBACK, or are UNAVAILABLE.

    .\\.venv\\Scripts\\python.exe -m comparison.live_executive_runner
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

from comparison import executive_py2026_py2027_report as executive

logger = logging.getLogger("oe_trend_pipeline.executive")

SERIES_METRIC_IDS = (executive.CANCELLED_DAILY, executive.TERMINATED_DAILY)
SUMMARY_ROWS = (
    (executive.CE, "Currently Enrolled", "count"),
    (executive.PE, "% Effectuated", "ratio"),
    (executive.NEW, "New", "count"),
    (executive.ACTIVE, "Active", "count"),
    (executive.PASSIVE, "Passive", "count"),
    (executive.STARTED, "Applications Started", "count"),
    (executive.SUBMITTED, "Submitted Applications", "count"),
    (executive.APPLICANTS, "Total Applicants", "count"),
)


def refresh_py2026() -> dict:
    """Live PY2026 aggregates; on failure the Phase 6B artifact is shown as STALE_FALLBACK."""
    from database.comparison import py2026_population_diagnostics as diagnostics

    started = executive.now_utc()
    try:
        report, connected = diagnostics.retrieve_live()
    except Exception as exc:
        logger.error("PY2026 DB refresh failed: %s", type(exc).__name__)
        report, connected = None, False
    if connected and report is not None:
        try:
            diagnostics.write_report(report)
        except Exception as exc:
            logger.error("Could not refresh the Phase 6B artifact: %s", type(exc).__name__)
        return executive.py2026_live(report, started)
    reason = "database connection or metadata read failed"
    return executive.py2026_fallback(executive.load_json(executive.PHASE_6B_REPORT),
                                     executive.artifact_time(executive.PHASE_6B_REPORT), reason)


def sisense_settings():
    """Approved settings, with natural runtime observation on so Total Applicants and series can be captured."""
    from config.settings import load_settings

    settings = load_settings()
    if not settings.runtime_validation:
        settings = dataclasses.replace(settings, runtime_validation=True)
    return settings


def live_py2027_values(run, completed_at: str) -> tuple[dict, dict]:
    """(metric values, daily series) taken only from this invocation's PipelineRun."""
    values, series = {}, {}
    if run is None:
        return values, series
    if run.result is not None:
        values[executive.CE] = {"value": run.result.value, "retrieved_at": run.result.retrieved_at}
    for item in run.passive_values:
        if item.result is not None:
            values[item.metric.metric_id] = {"value": item.result.value, "retrieved_at": item.result.retrieved_at}
    for row in run.runtime_rows:
        if row.metric_id == executive.APPLICANTS and row.status == "VALUE_EXTRACTED" and row.value is not None:
            values[executive.APPLICANTS] = {"value": row.value, "retrieved_at": completed_at}
        if row.metric_id in SERIES_METRIC_IDS and row.status == "SERIES_EXTRACTED" and row.series_points:
            series[row.metric_id] = {"points": [{"date": d, "value": v} for d, v in row.series_points],
                                     "retrieved_at": completed_at}
    return values, series


def _duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def print_timing(timing: dict) -> None:
    print()
    print("Runtime timing")
    print(f"{'PY2026 database':<24}{timing['py2026_database']}")
    print(f"{'PY2027 Sisense':<24}{timing['py2027_sisense']}")
    print(f"{'History preparation':<24}{timing['history_preparation']}")
    print(f"{'Comparison model':<24}{timing['comparison_model']}")
    print(f"{'Report rendering':<24}{timing['report_rendering']}")
    print(f"{'Total':<24}{timing['total']}")


def refresh_py2027(timings: dict | None = None) -> tuple[dict, object | None, object]:
    """(py2027 model, history path, run). Snapshots are saved by the existing pipeline before history is read."""
    import main as pipeline
    from comparison.lifecycle import resolve_history_path

    settings, run, reason = None, None, None
    sisense_started = time.monotonic()
    try:
        settings = sisense_settings()
        run = pipeline.run_pipeline(settings)
    except Exception as exc:
        reason = type(exc).__name__
        logger.error("PY2027 Sisense refresh failed: %s", reason)
    if timings is not None:
        timings["py2027_sisense_seconds"] = time.monotonic() - sisense_started
    completed = executive.now_utc()
    live_ok = run is not None and run.exit_code == pipeline.EXIT_OK and run.result is not None
    if run is not None and not live_ok:
        reason = run.failure or f"exit code {run.exit_code}"
    history_started = time.monotonic()
    history_path = settings.history_db_path if settings is not None and settings.history_db_path else \
        resolve_history_path()
    history = executive.read_history(history_path)
    values, series = live_py2027_values(run if live_ok else None, completed)
    model = executive.py2027_model(values, series, history, executive.load_json(executive.VALIDATION_REPORT),
                                   live_ok, reason)
    if timings is not None:
        timings["history_preparation_seconds"] = time.monotonic() - history_started
    return model, history, run


def print_summary(report: dict, html_path, run) -> None:
    summary = report["executive_summary"]
    metrics = {row["metric_id"]: row for row in report["py2027_current_snapshot"]}
    print()
    print("=" * 60)
    print("Georgia Access — PY2026 vs PY2027 Executive Report")
    print("=" * 60)
    print(f"{'Data refreshed':<24}{summary['data_refreshed']}")
    print(f"{'PY2026 DB':<24}{summary['py2026']['freshness']}")
    print(f"{'PY2027 Sisense':<24}{summary['py2027']['freshness']}")
    for side in ("py2026", "py2027"):
        if summary[side]["freshness"] != executive.LIVE:
            print(f"{'':<24}{summary[side]['note']}")
    for metric_id, label, kind in SUMMARY_ROWS:
        item = metrics[metric_id]
        print(f"{label:<24}{executive.fmt(item['value'], kind):<16}{item['freshness']}")
    if run is not None and run.snapshot_status:
        print(f"{'Snapshots':<24}{run.snapshot_status}")
    print(f"{'Executive report':<24}{html_path}")
    print("=" * 60)


def main() -> int:
    import main as pipeline

    pipeline.configure_logging()
    generated = executive.now_utc()
    timings: dict = {}
    started = time.monotonic()
    logger.info("Step 1/4: live PY2026 database refresh")
    db_started = time.monotonic()
    py2026 = refresh_py2026()
    timings["py2026_database_seconds"] = time.monotonic() - db_started
    logger.info("Step 2/4: live PY2027 Sisense refresh and snapshot save")
    py2027, history, run = refresh_py2027(timings)
    logger.info("Step 3/4: building the comparison model from this run")
    model_started = time.monotonic()
    model = executive.build_model(py2026, py2027, history, executive.load_json(executive.PHASE_6D_REPORT), generated)
    report = executive.build_report(model)
    timings["comparison_model_seconds"] = time.monotonic() - model_started
    logger.info("Step 4/4: writing the executive report")
    render_started = time.monotonic()
    json_path, html_path = executive.write_report(report)
    timings["report_rendering_seconds"] = time.monotonic() - render_started
    timings["total_seconds"] = time.monotonic() - started
    timing = {
        "py2026_database": _duration(timings["py2026_database_seconds"]),
        "py2027_sisense": _duration(timings.get("py2027_sisense_seconds", 0)),
        "history_preparation": _duration(timings.get("history_preparation_seconds", 0)),
        "comparison_model": _duration(timings["comparison_model_seconds"]),
        "report_rendering": _duration(timings["report_rendering_seconds"]),
        "total": _duration(timings["total_seconds"]),
        "seconds": {key: round(value, 3) for key, value in timings.items()},
    }
    report["runtime_timing"] = timing
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print_summary(report, html_path, run)
    print_timing(timing)
    live = py2026["freshness"] == executive.LIVE and py2027["freshness"] == executive.LIVE
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
