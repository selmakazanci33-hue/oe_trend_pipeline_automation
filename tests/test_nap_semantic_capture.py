"""Phase 6D NAP semantic capture on synthetic natural requests (no browser, no network)."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from sisense import nap_semantic_capture as module
from sisense.nap_semantic_capture import (
    APPLICATION_ACTIVITY,
    CONFIRM_BRIDGE,
    CREATE_BRIDGE,
    CURRENT_POPULATION,
    DASHBOARD_SCOPE,
    ENROLLMENT_ACTIVITY,
    INDEPENDENT,
    INSUFFICIENT,
    MEASURE_CONTEXT,
    MUTUALLY_EXCLUSIVE,
    NAP_WIDGET_ID,
    NO_BRIDGE,
    OBSERVATIONAL_CONTEXT,
    OVERLAPPING,
    PERSON,
    POLICY,
    REDACTED,
    TARGET_NOT_OBSERVED,
    TARGET_OBSERVED,
    UNRESOLVED,
    WIDGET_SCOPE,
    CaptureOutputError,
    build_report,
    capture_from_observer,
    guard_output,
    lifecycle_relationship,
    render_html,
    sanitize_payload,
    write_nap_capture,
    write_report,
)

SECRETS = ("COOKIE-SENTINEL", "TOKEN-SENTINEL", "CSRF-SENTINEL", "GUID-SENTINEL", "PERSON-551234567",
           "LITERAL-SENTINEL", "SECRETFN", "DATE-SENTINEL")
NEW_WINDOW = ("2026-11-01", "2027-01-15")
ACTIVE_WINDOW = ("2025-11-01", "2026-10-31")
PASSIVE_WINDOW = ("2024-01-01", "2025-10-31")


def field(table, column, datatype="text", level=None, flt=None):
    jaql = {"table": table, "column": column, "dim": f"[{table}.{column}]", "datatype": datatype,
            "title": column}
    if level:
        jaql["level"] = level
    if flt is not None:
        jaql["filter"] = flt
    return jaql


def window(bounds):
    return {"from": bounds[0], "to": bounds[1]}


def measure(title, counted, date=None, status=None, extra=None):
    context = {"[AAAAA-001]": counted}
    formula = "(COUNT([AAAAA-001])"
    for key, value in (("[BBBBB-002]", date), ("[CCCCC-003]", status), ("[DDDDD-004]", extra)):
        if value is not None:
            context[key] = value
            formula += f",{key}"
    return {"jaql": {"type": "measure", "title": title, "formula": formula + ")", "context": context}}


def policy():
    return field("enrollment", "id")


def person():
    return field("enrollee", "exchg_indiv_identifier")


def created(bounds=NEW_WINDOW):
    return field("enrollment", "enrollment_create_date", "datetime", "days", window(bounds))


def confirmed(bounds=NEW_WINDOW):
    return field("enrollment", "enrollment_confirmation_date", "datetime", "days", window(bounds))


def app_created(bounds=NEW_WINDOW):
    return field("ssap_applications", "creation_timestamp", "datetime", "days", window(bounds))


def enrolled_status(members=("Enrolled",)):
    return field("enrollment", "enrollment_status_description", flt={"members": list(members)})


def payload(new, active=None, passive=None, scope=(), widget=NAP_WIDGET_ID):
    active = active or measure("ACTIVE", policy(), created(ACTIVE_WINDOW))
    passive = passive or measure("PASSIVE", policy(), created(PASSIVE_WINDOW))
    return {
        "widget": f"{widget};New/Active/Passive - Enrolled People",
        "dashboard": "d0001;PY 2027 - OE Executive Dashboard",
        "datasource": {"id": "localhost_aMAIN", "title": "MAIN"},
        "widgetType": "indicator",
        "queryGuid": "GUID-SENTINEL",
        "metadata": [new, active, passive, *({"panel": "scope", "jaql": s} for s in scope)],
    }


class FakeResponse:
    status = 200


class FakeRequest:
    def __init__(self, body):
        self.post_data = json.dumps(body)
        self.headers = {"cookie": "COOKIE-SENTINEL", "authorization": "TOKEN-SENTINEL",
                        "x-xsrf-token": "CSRF-SENTINEL"}

    def response(self):
        return FakeResponse()


class FakeDefinition:
    def __init__(self, body, sequence=1):
        self.fingerprint = f"sha256:{sequence:064d}"
        self.observations = 1
        self.sequence = sequence
        self.latest_request = FakeRequest(body)


class FakeEntry:
    def __init__(self, *bodies):
        self.definitions = {f"d{i}": FakeDefinition(body, i + 1) for i, body in enumerate(bodies)}


class FakeObserver:
    def __init__(self, entries):
        self.entries_by_id = entries
        self.target_requests_seen = 7

    def entry(self, widget_id):
        return self.entries_by_id.get(widget_id)


def report_for(body):
    return build_report(capture_from_observer(FakeObserver({NAP_WIDGET_ID: FakeEntry(body)})))


def walk(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, key)


class IsolationTests(unittest.TestCase):
    def test_only_target_widget_reported(self):
        other = payload(measure("NEW", policy(), app_created()), widget="6a98048ccef60720fe7f9b44")
        observer = FakeObserver({NAP_WIDGET_ID: FakeEntry(payload(measure("NEW", policy(), created()))),
                                 "6a98048ccef60720fe7f9b44": FakeEntry(other)})
        capture = capture_from_observer(observer)
        self.assertEqual({d["definition"]["widget_oid"] for d in capture["definitions"]}, {NAP_WIDGET_ID})
        report = build_report(capture)
        self.assertEqual(report["target_widget"]["status"], TARGET_OBSERVED)
        self.assertNotIn("9b44", json.dumps(report))

    def test_foreign_request_inside_target_entry_skipped(self):
        foreign = payload(measure("NEW", policy(), created()), widget="other")
        capture = capture_from_observer(FakeObserver({NAP_WIDGET_ID: FakeEntry(foreign)}))
        self.assertEqual(capture["definitions"], [])

    def test_target_not_observed(self):
        report = build_report(capture_from_observer(FakeObserver({})))
        self.assertEqual(report["target_widget"]["status"], TARGET_NOT_OBSERVED)
        self.assertEqual(report["phase_6d_decision"]["new_classification"], INSUFFICIENT)
        self.assertEqual(report["phase_6d_decision"]["bridge_decision"], INSUFFICIENT)

    def test_module_sends_no_request(self):
        source = inspect.getsource(module)
        for forbidden in ("fetch(", ".post(", "evaluate(", "route(", "execute(", "SisenseJaqlClient"):
            self.assertNotIn(forbidden, source)


class NewClassificationTests(unittest.TestCase):
    def test_enrollment_created_activity(self):
        report = report_for(payload(measure("NEW", policy(), created())))
        new = report["new_definition"]
        self.assertEqual(new["classification"]["classification"], ENROLLMENT_ACTIVITY)
        self.assertEqual(new["grain"], POLICY)
        self.assertEqual(new["aggregation"], ["count"])
        self.assertEqual(new["formula_structure"], "(COUNT({0}),{1})")
        self.assertEqual(new["counted_fields"], [{"table": "enrollment", "column": "id"}])

    def test_application_created_activity(self):
        report = report_for(payload(measure("NEW", policy(), app_created())))
        self.assertEqual(report["new_definition"]["classification"]["classification"], APPLICATION_ACTIVITY)

    def test_current_population_lifecycle_class(self):
        report = report_for(payload(measure("NEW", person(), app_created(), enrolled_status())))
        cls = report["new_definition"]["classification"]
        self.assertEqual(cls["classification"], CURRENT_POPULATION)
        self.assertTrue(cls["status_restricted"])
        self.assertEqual(report["py2026_bridge_assessment"]["decision"], NO_BRIDGE)

    def test_scope_status_filter_makes_current_population(self):
        report = report_for(payload(measure("NEW", policy(), created()), scope=[enrolled_status()]))
        self.assertEqual(report["new_definition"]["classification"]["classification"], CURRENT_POPULATION)

    def test_redacted_status_is_insufficient(self):
        status = field("enrollment", "enrollment_status_description", flt={"members": ["PERSON-551234567!"]})
        report = report_for(payload(measure("NEW", policy(), created(), status)))
        self.assertEqual(report["new_definition"]["classification"]["classification"], INSUFFICIENT)

    def test_title_alone_never_classifies(self):
        bare = {"jaql": {"type": "measure", "title": "NEW", "formula": "SECRETFN()", "context": {}}}
        report = report_for(payload(bare))
        self.assertEqual(report["new_definition"]["classification"]["classification"], INSUFFICIENT)


class DateAndContextTests(unittest.TestCase):
    def test_date_field_and_window_extraction(self):
        dates = report_for(payload(measure("NEW", policy(), created())))["new_definition"]["date_filters"]
        self.assertEqual(len(dates), 1)
        self.assertEqual(dates[0]["field"], "enrollment.enrollment_create_date")
        self.assertEqual((dates[0]["from"], dates[0]["to"]), NEW_WINDOW)
        self.assertEqual(dates[0]["date_grain"], "days")
        self.assertEqual(dates[0]["location"], MEASURE_CONTEXT)
        self.assertEqual(dates[0]["event_basis"], "CAPTURED_FIELD_IDENTITY")

    def test_relative_window_counts_only(self):
        relative = field("enrollment", "enrollment_create_date", "datetime", "days",
                         {"last": {"count": 7, "offset": 0, "anchor": "DATE-SENTINEL"}})
        dates = report_for(payload(measure("NEW", policy(), relative)))["new_definition"]["date_filters"]
        self.assertEqual(dates[0]["relative"], {"last": {"count": 7, "offset": 0, "anchor": REDACTED}})

    def test_measure_context_versus_scope_filter(self):
        scope_date = dict(created(), isDashboardFilter=True)
        report = report_for(payload(measure("NEW", policy()), scope=[scope_date, enrolled_status(("Enrolled", "Pending"))]))
        dates = report["new_definition"]["date_filters"]
        self.assertEqual(dates[0]["location"], DASHBOARD_SCOPE)
        locations = {s["location"] for s in report["shared_context"]["scope_filters"]}
        self.assertEqual(locations, {DASHBOARD_SCOPE, WIDGET_SCOPE})
        report = report_for(payload(measure("NEW", policy(), app_created()), scope=[created()]))
        self.assertEqual(report["new_definition"]["classification"]["date_event_family"], "application created")

    def test_disabled_scope_filter_not_applied(self):
        body = payload(measure("NEW", policy(), created()), scope=[enrolled_status()])
        body["metadata"][-1]["disabled"] = True
        report = report_for(body)
        self.assertEqual(report["new_definition"]["classification"]["classification"], ENROLLMENT_ACTIVITY)

    def test_active_passive_preserved_separately(self):
        report = report_for(payload(measure("NEW", policy(), created())))
        active, passive = report["active_definition"], report["passive_definition"]
        self.assertEqual(active["title"], "ACTIVE")
        self.assertEqual(passive["title"], "PASSIVE")
        self.assertEqual((active["date_filters"][0]["from"], active["date_filters"][0]["to"]), ACTIVE_WINDOW)
        self.assertEqual((passive["date_filters"][0]["from"], passive["date_filters"][0]["to"]), PASSIVE_WINDOW)


class LifecycleTests(unittest.TestCase):
    def test_disjoint_windows_mutually_exclusive_with_caveat(self):
        rel = report_for(payload(measure("NEW", policy(), created())))["lifecycle_relationship"]
        self.assertEqual(rel["relationship"], MUTUALLY_EXCLUSIVE)
        self.assertIn("person-level", rel["caveat"])

    def test_overlapping_windows(self):
        body = payload(measure("NEW", policy(), created(("2026-01-01", "2027-01-15"))))
        self.assertEqual(report_for(body)["lifecycle_relationship"]["relationship"], OVERLAPPING)

    def test_different_counted_fields_independent(self):
        body = payload(measure("NEW", person(), created()))
        self.assertEqual(report_for(body)["lifecycle_relationship"]["relationship"], INDEPENDENT)

    def test_numeric_partition_does_not_prove_lifecycle(self):
        body = payload(measure("NEW", policy(), created()),
                       active=measure("ACTIVE", policy(), created(), field("enrollment", "application_type")),
                       passive=measure("PASSIVE", policy()))
        rel = report_for(body)["lifecycle_relationship"]
        self.assertEqual(rel["relationship"], UNRESOLVED)
        self.assertEqual(rel["observational_context"], OBSERVATIONAL_CONTEXT)
        self.assertEqual(list(inspect.signature(lifecycle_relationship).parameters), ["described"])

    def test_missing_measure_unresolved(self):
        body = payload(measure("NEW", policy(), created()))
        body["metadata"] = body["metadata"][:1]
        self.assertEqual(report_for(body)["lifecycle_relationship"]["relationship"], UNRESOLVED)


class BridgeTests(unittest.TestCase):
    def test_application_created_cannot_bridge(self):
        for body in (payload(measure("NEW", policy(), app_created())),
                     payload(measure("NEW", person(), app_created(), enrolled_status()))):
            bridge = report_for(body)["py2026_bridge_assessment"]
            self.assertEqual(bridge["decision"], NO_BRIDGE)
            self.assertIsNone(bridge["py2026_candidate"])

    def test_enrollment_created_only_potential_bridge(self):
        bridge = report_for(payload(measure("NEW", policy(), created())))["py2026_bridge_assessment"]
        self.assertEqual(bridge["decision"], CREATE_BRIDGE)
        self.assertEqual(bridge["py2026_candidate"], "enrollment_create_date")
        self.assertFalse(bridge["production_comparable"])
        self.assertTrue(any("OE_START_ANCHOR_UNPROVEN" in b for b in bridge["blockers"]))

    def test_confirmation_only_when_captured(self):
        self.assertEqual(report_for(payload(measure("NEW", policy(), confirmed())))["py2026_bridge_assessment"]
                         ["decision"], CONFIRM_BRIDGE)
        self.assertNotEqual(report_for(payload(measure("NEW", policy(), created())))["py2026_bridge_assessment"]
                            ["decision"], CONFIRM_BRIDGE)

    def test_application_grain_with_enrollment_date_no_bridge(self):
        body = payload(measure("NEW", field("ssap_applications", "id"), created()))
        self.assertEqual(report_for(body)["py2026_bridge_assessment"]["decision"], NO_BRIDGE)

    def test_identifier_mismatch_remains_blocker(self):
        report = report_for(payload(measure("NEW", person(), created())))
        self.assertEqual(report["new_definition"]["grain"], PERSON)
        blockers = report["py2026_bridge_assessment"]["blockers"]
        self.assertTrue(blockers[0].startswith("IDENTIFIER_EQUIVALENCE_UNPROVEN"))
        self.assertIn("enrollee_id", blockers[0])
        self.assertEqual(report["remaining_blockers"], blockers)


class SafetyTests(unittest.TestCase):
    def leaky_body(self):
        identifier_filter = field("enrollee", "exchg_indiv_identifier", flt={"members": ["PERSON-551234567"]})
        new = measure("NEW", policy(), created(("DATE-SENTINEL", "2027-01-15")), extra=identifier_filter)
        new["jaql"]["formula"] = new["jaql"]["formula"].replace("(COUNT", "(SECRETFN('LITERAL-SENTINEL')+COUNT")
        return payload(new)

    def test_no_auth_header_cookie_token_or_pii_leakage(self):
        report = report_for(self.leaky_body())
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            text = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        new = report["new_definition"]
        self.assertIn("fn(<literal>)", new["formula_structure"])
        identifier = next(f for f in new["other_filters"] if f["field"] == "enrollee.exchg_indiv_identifier")
        self.assertEqual(identifier["filter"]["members"], REDACTED)
        self.assertEqual(new["date_filters"][0]["from"], REDACTED)

    def test_guard_refuses_sensitive_keys(self):
        for key in ("cookie", "authorization", "headers", "body", "enrollee_id"):
            with self.assertRaises(CaptureOutputError):
                guard_output({"x": [{key: "v"}]})

    def test_sanitized_payload_has_no_raw_keys(self):
        sanitized = sanitize_payload(self.leaky_body())
        self.assertNotIn("queryGuid", json.dumps(sanitized))
        self.assertNotIn("metadata", sanitized)

    def test_production_comparable_false(self):
        for body in (payload(measure("NEW", policy(), created())), payload(measure("NEW", person(), app_created()))):
            report = report_for(body)
            flags = list(walk(report, "production_comparable"))
            self.assertGreaterEqual(len(flags), 3)
            self.assertTrue(all(flag is False for flag in flags))

    def test_report_sections(self):
        page = render_html(report_for(payload(measure("NEW", policy(), created()))))
        for section in ("Target Widget", "Natural Request Evidence", "NEW Definition", "ACTIVE Definition",
                        "PASSIVE Definition", "Shared Context", "Lifecycle Relationship",
                        "PY2026 Bridge Assessment", "Remaining Blockers", "Phase 6D Decision"):
            self.assertIn(f"<h2>{section}</h2>", page)

    def test_write_hook_never_raises(self):
        class Broken:
            def entry(self, _):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(write_nap_capture(Broken(), Path(tmp)))
            path = write_nap_capture(FakeObserver({NAP_WIDGET_ID: FakeEntry(payload(measure("NEW", policy(), created())))}),
                                     Path(tmp))
            self.assertEqual(path, Path(tmp) / "comparison" / "nap_semantic_capture.json")
        self.assertIsNone(write_nap_capture(FakeObserver({}), None))


if __name__ == "__main__":
    unittest.main()
