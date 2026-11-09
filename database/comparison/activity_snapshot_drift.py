"""Why do PY2026 daily Cancelled/Terminated counts drift between snapshots?

Read-only. For each pair of consecutive dated snapshots (A earlier, B later) and
each status family, the policies A places on complete days before A's date are
traced into B at policy (enrollment_id) level inside the database. Only
aggregate counts leave the database. No identifier is returned, logged or
written. Snapshots are compared, never unioned or summed. PY2027 is not
queried here; it comes from Sisense.

The evidence decides between two readings of a snapshot table:
  EVENT HISTORY      a policy cancelled on day D stays a day-D cancellation later
  MUTABLE STATE      the policy's last-update date and/or status move later

    python -m database.comparison.activity_snapshot_drift
"""

from __future__ import annotations

import html
import json
from datetime import date, timedelta
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.lifecycle import PREVIOUS_PLAN_YEAR, QUERY_TIMEOUT_SECONDS
from database.comparison.activity_semantic_bridge import (
    CANCELLED_STATUS,
    HISTORICAL_CANDIDATE,
    STABILITY_LOOKBACK_DAYS,
    TERM_PREFIX,
    _as_int,
    _columns,
    _list_snapshots,
    _row_dict,
    _run,
)
from database.comparison.event_date_validation import snapshot_date_context
from database.comparison.field_mapping import is_blocked_event_date_name, is_datetime_sql_type
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_activity_snapshot_drift.json"
HTML_NAME = "py2026_activity_snapshot_drift.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.activity_snapshot_drift"

FAMILIES = ("Cancelled", "Terminated")

IMMUTABLE_EVENT_LIKE = "IMMUTABLE_EVENT_LIKE"
MUTABLE_LATEST_STATE = "MUTABLE_LATEST_STATE"
MIXED = "MIXED"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

COMPARABLE_CANDIDATE = "COMPARABLE_CANDIDATE"
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
NOT_RECONSTRUCTABLE = "NOT_RECONSTRUCTABLE_FROM_SNAPSHOTS"

MIN_POLICIES = 100
IMMUTABLE_UNCHANGED_SHARE = 0.95
IMMUTABLE_MAX_MOVED_IN_SHARE = 0.05
MAX_AMBIGUOUS_SHARE = 0.05
MUTABLE_UNCHANGED_SHARE = 0.5

REQUIRED_COLUMNS = ("enrollment_id", "enrollment_status_description", HISTORICAL_CANDIDATE)
TRANSITION_FIELDS = (
    "a_policies", "b_policies", "intersection_policies", "b_only_policies", "b_only_new_policies",
    "disappeared", "same_date_status", "moved_date", "changed_status", "both_changed",
    "moved_later", "moved_earlier", "left_family", "null_in_b", "ambiguous_policies",
)


def _family_predicate(family: str, expression: str) -> tuple[str, tuple]:
    if family == "Cancelled":
        return f"{expression} = ?", (CANCELLED_STATUS,)
    return f"{expression} LIKE ?", (TERM_PREFIX,)


def _per_policy_cte(name: str, qualified: str, columns: dict, family: str) -> tuple[str, tuple]:
    """One row per enrollment_id: its date and status in this snapshot, plus family membership."""
    key = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(columns[HISTORICAL_CANDIDATE]["name"])
    day = f"CAST({event} AS date)"
    member, member_params = _family_predicate(family, f"MAX({status})")
    year_sql, year_params = "", ()
    if "coverage_year" in columns:
        year_sql = f" AND {quote_identifier(columns['coverage_year']['name'])} = ?"
        year_params = (PREVIOUS_PLAN_YEAR,)
    sql = (
        f"{name} AS (\n"
        f"    SELECT {key} AS k, MAX({day}) AS d, MAX({status}) AS s,\n"
        f"           CASE WHEN MIN({day}) = MAX({day}) AND MIN({status}) = MAX({status}) "
        "THEN 0 ELSE 1 END AS ambiguous,\n"
        f"           CASE WHEN {member} THEN 1 ELSE 0 END AS in_family\n"
        f"    FROM {qualified}\n"
        f"    WHERE {key} IS NOT NULL{year_sql}\n"
        f"    GROUP BY {key}\n"
        ")"
    )
    return sql, member_params + year_params


def build_transition_sql(
    a_qualified: str, a_columns: dict, b_qualified: str, b_columns: dict, *,
    family: str, window_start: date, window_end: date,
) -> tuple[str, tuple]:
    """Aggregates only. Window is [window_start, window_end) on the snapshot's own date column."""
    for columns in (a_columns, b_columns):
        event = columns[HISTORICAL_CANDIDATE]
        if is_blocked_event_date_name(event["name"]) or not is_datetime_sql_type(event.get("data_type")):
            raise ValueError("event-date SQL refused a non-datetime or identifier column")
    a_cte, a_params = _per_policy_cte("a", a_qualified, a_columns, family)
    b_cte, b_params = _per_policy_cte("b", b_qualified, b_columns, family)
    window = (window_start.isoformat(), window_end.isoformat())
    sql = (
        f"WITH {a_cte},\n{b_cte},\n"
        "a_fam AS (SELECT k, d, s, ambiguous FROM a WHERE in_family = 1 AND d >= ? AND d < ?),\n"
        "b_fam AS (SELECT k, d, s, ambiguous FROM b WHERE in_family = 1 AND d >= ? AND d < ?),\n"
        "t AS (\n"
        "    SELECT COUNT_BIG(CASE WHEN b.k IS NULL THEN 1 END) AS disappeared,\n"
        "           COUNT_BIG(CASE WHEN b.d = a_fam.d AND b.s = a_fam.s THEN 1 END) AS same_date_status,\n"
        "           COUNT_BIG(CASE WHEN b.d <> a_fam.d AND b.s = a_fam.s THEN 1 END) AS moved_date,\n"
        "           COUNT_BIG(CASE WHEN b.d = a_fam.d AND b.s <> a_fam.s THEN 1 END) AS changed_status,\n"
        "           COUNT_BIG(CASE WHEN b.d <> a_fam.d AND b.s <> a_fam.s THEN 1 END) AS both_changed,\n"
        "           COUNT_BIG(CASE WHEN b.d > a_fam.d THEN 1 END) AS moved_later,\n"
        "           COUNT_BIG(CASE WHEN b.d < a_fam.d THEN 1 END) AS moved_earlier,\n"
        "           COUNT_BIG(CASE WHEN b.k IS NOT NULL AND b.in_family = 0 THEN 1 END) AS left_family,\n"
        "           COUNT_BIG(CASE WHEN b.k IS NOT NULL AND (b.d IS NULL OR b.s IS NULL) THEN 1 END) AS null_in_b,\n"
        "           COUNT_BIG(CASE WHEN a_fam.ambiguous = 1 OR b.ambiguous = 1 THEN 1 END) AS ambiguous_policies\n"
        "    FROM a_fam LEFT JOIN b ON b.k = a_fam.k\n"
        ")\n"
        "SELECT (SELECT COUNT_BIG(*) FROM a_fam) AS a_policies,\n"
        "       (SELECT COUNT_BIG(*) FROM b_fam) AS b_policies,\n"
        "       (SELECT COUNT_BIG(*) FROM a_fam INNER JOIN b_fam ON b_fam.k = a_fam.k) AS intersection_policies,\n"
        "       (SELECT COUNT_BIG(*) FROM b_fam WHERE NOT EXISTS "
        "(SELECT 1 FROM a_fam WHERE a_fam.k = b_fam.k)) AS b_only_policies,\n"
        "       (SELECT COUNT_BIG(*) FROM b_fam WHERE NOT EXISTS "
        "(SELECT 1 FROM a WHERE a.k = b_fam.k)) AS b_only_new_policies,\n"
        "       t.disappeared, t.same_date_status, t.moved_date, t.changed_status, t.both_changed,\n"
        "       t.moved_later, t.moved_earlier, t.left_family, t.null_in_b, t.ambiguous_policies\n"
        "FROM t"
    )
    assert_read_only(sql)
    return sql, a_params + b_params + window + window


def _share(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole, 6)


def summarize_transition(counts: dict | None) -> dict:
    """Shares and a semantic classification for one (A, B, family). Missing evidence stays None."""
    counts = counts or {}
    values = {field: _as_int(counts.get(field)) for field in TRANSITION_FIELDS}
    a_total = values["a_policies"]
    b_total = values["b_policies"]
    intersection = values["intersection_policies"]
    b_only = values["b_only_policies"]
    b_only_new = values["b_only_new_policies"]
    disappeared = values["disappeared"]
    a_only = None if a_total is None or intersection is None else a_total - intersection
    present = None if a_total is None or disappeared is None else a_total - disappeared
    moved_in = None if b_only is None or b_only_new is None else b_only - b_only_new
    shares = {
        "retention": _share(intersection, a_total),
        "unchanged_share": _share(values["same_date_status"], a_total),
        "disappeared_share": _share(disappeared, a_total),
        "moved_date_share": _share(values["moved_date"], a_total),
        "changed_status_share": _share(values["changed_status"], a_total),
        "both_changed_share": _share(values["both_changed"], a_total),
        "ambiguous_share": _share(values["ambiguous_policies"], a_total),
        "b_moved_in_share": _share(moved_in, b_total),
        "b_new_share": _share(b_only_new, b_total),
    }
    mechanisms = {
        "disappeared": disappeared,
        "moved_date": values["moved_date"],
        "changed_status": values["changed_status"],
        "both_changed": values["both_changed"],
    }
    known = {name: value for name, value in mechanisms.items() if value}
    dominant = max(known, key=known.get) if known else None
    if a_total is None or shares["unchanged_share"] is None and a_total >= MIN_POLICIES:
        classification, reason = INSUFFICIENT_EVIDENCE, "Query did not run; counts are N/A, not zero."
    elif a_total < MIN_POLICIES:
        classification, reason = INSUFFICIENT_EVIDENCE, f"Fewer than {MIN_POLICIES} policies in the earlier snapshot window."
    elif (shares["unchanged_share"] >= IMMUTABLE_UNCHANGED_SHARE
          and (shares["b_moved_in_share"] or 0) <= IMMUTABLE_MAX_MOVED_IN_SHARE
          and (shares["ambiguous_share"] or 0) <= MAX_AMBIGUOUS_SHARE):
        classification, reason = IMMUTABLE_EVENT_LIKE, (
            "Policies keep the same date and status in the later snapshot; earlier days are preserved.")
    elif shares["unchanged_share"] <= MUTABLE_UNCHANGED_SHARE:
        classification, reason = MUTABLE_LATEST_STATE, (
            "Most policies leave the snapshot, move to another last-update date, or change status. "
            "The table holds latest state, not event history.")
    else:
        classification, reason = MIXED, "Part of the earlier day placement is preserved and part is not."
    return {
        "counts": {**values, "a_only_policies": a_only, "present_in_b": present, "b_moved_in_policies": moved_in},
        "shares": shares,
        "dominant_change": dominant,
        "classification": classification,
        "reason": reason,
    }


def consecutive_pairs(tables: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Dated snapshots in date order, paired with the next one. Undated tables are reported, not guessed."""
    dated, skipped = [], []
    for item in tables:
        context = snapshot_date_context(item["table"])
        if context.get("snapshot_date"):
            dated.append({**item, "snapshot_date": context["snapshot_date"]})
        else:
            skipped.append({"table": item["table"], "filename_status": context.get("filename_status"),
                            "reason": "No proven snapshot date; month-day tokens do not invent a year."})
    dated.sort(key=lambda item: item["snapshot_date"])
    return list(zip(dated, dated[1:])), skipped


def metric_decision(pairs: list[dict]) -> dict:
    labels = [item["classification"] for item in pairs if item["classification"] != INSUFFICIENT_EVIDENCE]
    if not labels:
        behavior, decision = INSUFFICIENT_EVIDENCE, DIAGNOSTIC_ONLY
        reason = "No snapshot pair had enough evidence."
    elif all(label == IMMUTABLE_EVENT_LIKE for label in labels):
        behavior, decision = IMMUTABLE_EVENT_LIKE, COMPARABLE_CANDIDATE
        reason = ("Every sufficient pair preserves earlier day placement. One late snapshot grouped by "
                  "enrollment_last_update_date can reconstruct daily activity; other checks still apply.")
    elif all(label == MUTABLE_LATEST_STATE for label in labels):
        behavior, decision = MUTABLE_LATEST_STATE, NOT_RECONSTRUCTABLE
        reason = ("Every sufficient pair shows latest-state behavior. A later snapshot cannot reproduce "
                  "the daily series as it stood at an earlier point.")
    else:
        behavior, decision = MIXED, DIAGNOSTIC_ONLY
        reason = "Pairs disagree; behavior is not uniform across the plan year."
    return {
        "historical_behavior": behavior,
        "decision": decision,
        "reason": reason,
        "pairs_sufficient": len(labels),
        "pairs_total": len(pairs),
        "production_comparable": False,
    }


def collect(client) -> dict:
    failures: list[dict] = []
    tables = _list_snapshots(client, failures)
    pairs, skipped = consecutive_pairs(tables)
    columns = {}
    rows = []
    for earlier, later in pairs:
        for item in (earlier, later):
            if item["table"] not in columns:
                columns[item["table"]] = _columns(client, failures, item)
        a_cols, b_cols = columns[earlier["table"]], columns[later["table"]]
        missing = [name for name in REQUIRED_COLUMNS if name not in a_cols or name not in b_cols]
        a_date = date.fromisoformat(earlier["snapshot_date"])
        window = (a_date - timedelta(days=STABILITY_LOOKBACK_DAYS), a_date)
        for family in FAMILIES:
            record = {
                "earlier": earlier["table"], "later": later["table"],
                "earlier_date": earlier["snapshot_date"], "later_date": later["snapshot_date"],
                "lag_days": (date.fromisoformat(later["snapshot_date"]) - a_date).days,
                "window_start": window[0].isoformat(), "window_end_exclusive": window[1].isoformat(),
                "status_family": family, "counts": None,
            }
            if missing:
                record["skip_reason"] = "Missing columns: " + ", ".join(missing)
                rows.append(record)
                continue
            try:
                sql, params = build_transition_sql(
                    qualified_name(earlier["schema_name"], earlier["table"]), a_cols,
                    qualified_name(later["schema_name"], later["table"]), b_cols,
                    family=family, window_start=window[0], window_end=window[1],
                )
            except ValueError as exc:
                record["skip_reason"] = str(exc)
                rows.append(record)
                continue
            result = _run(client, failures, f"transition {earlier['table']}->{later['table']} {family}", sql, params)
            if result is not None and result.rows:
                record["counts"] = _row_dict(result)
            rows.append(record)
    return {"rows": rows, "skipped_tables": skipped, "failures": failures}


def build_report(evidence: dict) -> dict:
    pairs = []
    for row in evidence.get("rows") or []:
        summary = summarize_transition(row.get("counts"))
        if row.get("skip_reason"):
            summary["reason"] = row["skip_reason"]
        pairs.append({key: value for key, value in row.items() if key != "counts"} | summary)
    metrics = {
        family: metric_decision([item for item in pairs if item["status_family"] == family])
        for family in FAMILIES
    }
    report = {
        "report_kind": "PY2026_ACTIVITY_SNAPSHOT_DRIFT",
        "question": ("Do PY2026 snapshot tables keep a policy's Cancelled/Terminated day placement "
                     "(event history) or move it as the record changes (latest state)?"),
        "operator": "COUNT(DISTINCT enrollment_id) per CAST(enrollment_last_update_date AS date)",
        "status_semantics": {"Cancelled": "= 'Cancelled'", "Terminated": "LIKE 'Term%'"},
        "coverage_year": PREVIOUS_PLAN_YEAR,
        "window_rule": f"Complete days in the {STABILITY_LOOKBACK_DAYS} days before the earlier snapshot date.",
        "thresholds": {
            "min_policies": MIN_POLICIES,
            "immutable_unchanged_share": IMMUTABLE_UNCHANGED_SHARE,
            "immutable_max_moved_in_share": IMMUTABLE_MAX_MOVED_IN_SHARE,
            "max_ambiguous_share": MAX_AMBIGUOUS_SHARE,
            "mutable_unchanged_share": MUTABLE_UNCHANGED_SHARE,
        },
        "definitions": {
            "intersection_policies": "In the family and in the window in both snapshots.",
            "same_date_status": "Earlier-window policy found later with the same date and the same status.",
            "moved_date": "Same status, different last-update date.",
            "changed_status": "Same last-update date, different status.",
            "both_changed": "Different date and different status.",
            "disappeared": "Earlier-window policy absent from the later snapshot.",
            "b_only_new_policies": "Later-window policy absent from the earlier snapshot.",
            "b_moved_in_policies": "Later-window policy present earlier with another date or status.",
            "null": "N/A: evidence did not run. Never zero.",
        },
        "pairs": pairs,
        "skipped_tables": evidence.get("skipped_tables") or [],
        "metrics": metrics,
        "failures": evidence.get("failures") or [],
        "notes": [
            "Identifier-level tracing runs inside the database; only aggregates are returned.",
            "Snapshots are compared pairwise and never unioned or summed.",
            "The Sisense widget is computed from current rows. Latest-state history can only be compared "
            "as-of aligned, which is a separate decision; nothing here is production-comparable.",
        ],
        "command": COMMAND,
    }
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def _cell(value) -> str:
    return "N/A" if value is None else html.escape(str(value))


def render_html(report: dict) -> str:
    head = ("<tr><th>Family</th><th>A → B</th><th>Lag</th><th>Window</th><th>A</th><th>B</th>"
            "<th>Both</th><th>A only</th><th>B only (new)</th><th>Retention</th><th>Same</th>"
            "<th>Moved date</th><th>Changed status</th><th>Both changed</th><th>Disappeared</th>"
            "<th>Classification</th></tr>")
    body = []
    for item in report["pairs"]:
        c = item["counts"]
        body.append(
            f"<tr><td>{_cell(item['status_family'])}</td><td>{_cell(item['earlier'])} → {_cell(item['later'])}</td>"
            f"<td>{_cell(item['lag_days'])}</td><td>{_cell(item['window_start'])} – {_cell(item['window_end_exclusive'])}</td>"
            f"<td>{_cell(c['a_policies'])}</td><td>{_cell(c['b_policies'])}</td><td>{_cell(c['intersection_policies'])}</td>"
            f"<td>{_cell(c['a_only_policies'])}</td><td>{_cell(c['b_only_policies'])} ({_cell(c['b_only_new_policies'])})</td>"
            f"<td>{_cell(item['shares']['retention'])}</td><td>{_cell(c['same_date_status'])}</td>"
            f"<td>{_cell(c['moved_date'])}</td><td>{_cell(c['changed_status'])}</td><td>{_cell(c['both_changed'])}</td>"
            f"<td>{_cell(c['disappeared'])}</td><td>{_cell(item['classification'])}</td></tr>"
        )
    decisions = "".join(
        f"<li><b>{_cell(family)}</b>: {_cell(block['decision'])} ({_cell(block['historical_behavior'])}). "
        f"{_cell(block['reason'])}</li>"
        for family, block in report["metrics"].items()
    )
    skipped = "".join(f"<li>{_cell(item['table'])}: {_cell(item['reason'])}</li>" for item in report["skipped_tables"])
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PY2026 activity snapshot drift</title></head>"
        "<body><h1>PY2026 activity snapshot drift</h1>"
        f"<p>{_cell(report['question'])}</p><p>Operator: {_cell(report['operator'])}</p>"
        f"<h2>Decisions</h2><ul>{decisions}</ul>"
        f"<h2>Snapshot pairs</h2><table border='1' cellpadding='4'>{head}{''.join(body)}</table>"
        f"<h2>Skipped tables</h2><ul>{skipped or '<li>None</li>'}</ul>"
        "<p>Aggregates only. N/A means the evidence did not run; it is never zero.</p></body></html>"
    )


def print_summary(report: dict) -> None:
    print("PY2026 ACTIVITY SNAPSHOT DRIFT (read-only, aggregates only)")
    for item in report["pairs"]:
        c, s = item["counts"], item["shares"]
        print(
            f"{item['status_family']}: {item['earlier']} -> {item['later']} lag={item['lag_days']}d "
            f"window={item['window_start']}..{item['window_end_exclusive']} "
            f"A={_text(c['a_policies'])} B={_text(c['b_policies'])} both={_text(c['intersection_policies'])} "
            f"A_only={_text(c['a_only_policies'])} B_only={_text(c['b_only_policies'])} "
            f"(new={_text(c['b_only_new_policies'])}) retention={_text(s['retention'])} "
            f"same={_text(c['same_date_status'])} moved_date={_text(c['moved_date'])} "
            f"changed_status={_text(c['changed_status'])} both_changed={_text(c['both_changed'])} "
            f"disappeared={_text(c['disappeared'])} -> {item['classification']}"
        )
    for family, block in report["metrics"].items():
        print(f"{family}: {block['decision']} ({block['historical_behavior']}; "
              f"{block['pairs_sufficient']}/{block['pairs_total']} pairs sufficient)")
    for item in report["skipped_tables"]:
        print(f"Skipped {item['table']}: {item['reason']}")
    for failure in report["failures"]:
        print(f"Failure: {failure['step']}: {failure['error']}")


def _text(value) -> str:
    return "N/A" if value is None else str(value)


def main() -> int:
    evidence = {"rows": [], "skipped_tables": [], "failures": []}
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect(DatabaseClient(connection))
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
