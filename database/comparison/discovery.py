"""Inspect repository evidence required before PY2026 comparison SQL.

This module does not invent columns, does not write SQL for production
comparisons, and does not query PY2027 enrollment rows.

    python -m database.comparison.discovery
"""

from __future__ import annotations

import json
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.models import FOUNDATION_METRICS, UNRESOLVED_DATABASE_QUESTIONS, default_identifier_requirement

JSON_NAME = "py2026_semantic_requirements.json"

# Names already present in repository evidence (enrollment source discovery,
# latest-state ordering, cancellation catalog). Not a claim that they exist
# on the authoritative table, and not a PY2027 query list.
EVIDENCED_COLUMN_NAMES = (
    "coverage_year",
    "enrollment_id",
    "enrollee_id",
    "hios_issuer_id",
    "enrollment_status_description",
    "enrollee_status_description",
    "application_type",
    "enrollment_confirmation_date",
    "enrollment_create_date",
    "enrollment_last_update_date",
    "enrollment_update_date",
    "last_update_timestamp",
    "benefit_effective_date",
    "benefit_end_date",
    "person_type_description",
    "insurance_type_description",
)

REQUIRED_BY_FAMILY = {
    "currently_enrolled_people": (
        "coverage_year",
        "enrollment_status_description",
        "enrollee_status_description",
        "benefit_effective_date",
        "person_type_description",
        "insurance_type_description",
    ),
    "cancelled_policies_daily": (
        "enrollment_id",
        "enrollment_status_description",
        "last_update_timestamp",
        "coverage_year",
    ),
    "terminated_policies_daily": (
        "enrollment_id",
        "enrollment_status_description",
        "coverage_year",
    ),
}


def evaluate_schema(columns: set[str] | None, metric_id: str) -> dict:
    """Compare an injected schema to evidenced names. Missing names are not invented."""
    required = REQUIRED_BY_FAMILY.get(metric_id, ())
    if columns is None:
        return {
            "metric_id": metric_id,
            "schema_read": False,
            "required_evidenced_names": list(required),
            "present": [],
            "missing": list(required),
            "status": "SCHEMA_NOT_READ",
        }
    present = [name for name in required if name in columns]
    missing = [name for name in required if name not in columns]
    return {
        "metric_id": metric_id,
        "schema_read": True,
        "required_evidenced_names": list(required),
        "present": present,
        "missing": missing,
        "status": "COLUMNS_PRESENT" if not missing else "COLUMNS_INCOMPLETE",
    }


def requirements_document(columns: set[str] | None = None) -> dict:
    identifier = default_identifier_requirement()
    return {
        "report_kind": "PY2026_SEMANTIC_REQUIREMENTS",
        "queries_py2027_enrollment": False,
        "production_sql_written": False,
        "identifier_equivalence": {
            "py2027_field": identifier.py2027_field,
            "py2026_candidate": identifier.py2026_candidate,
            "status": identifier.status,
            "reason": identifier.reason,
        },
        "evidenced_column_names": list(EVIDENCED_COLUMN_NAMES),
        "metrics": [
            {
                "metric_id": spec.metric_id,
                "metric_name": spec.metric_name,
                "metric_family": spec.metric_family,
                "grain": spec.grain,
                "py2026_sql_status": "NOT_WRITTEN",
                "schema": evaluate_schema(columns, spec.metric_id),
            }
            for spec in FOUNDATION_METRICS
        ],
        "unresolved_database_questions": list(UNRESOLVED_DATABASE_QUESTIONS),
        "gates_before_production_sql": [
            "inspect existing repository evidence",
            "verify table and column existence",
            "verify grain",
            "verify date field",
            "verify status values",
            "verify identifier mapping",
        ],
    }


def write_document(document: dict, output_dir: Path = OUTPUT_DIR) -> Path:
    guard_no_pii(document)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / JSON_NAME
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    document = requirements_document()
    path = write_document(document)
    print("PY2026 semantic requirements — no database connection, no PY2027 query")
    print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
