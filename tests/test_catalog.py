import unittest

from metrics.catalog import (
    CURRENTLY_ENROLLED,
    METRICS,
    MODULES,
    ROADMAP,
    STATUS_ACTIVE,
    STATUS_PENDING,
    MetricDefinition,
    active_metrics,
    get_metric,
)
from sisense.payloads.currently_enrolled import build_currently_enrolled_query


class CatalogTests(unittest.TestCase):
    def test_only_currently_enrolled_is_active(self):
        self.assertEqual([m.metric_id for m in active_metrics()], ["currently_enrolled_people"])
        self.assertEqual(list(METRICS), ["currently_enrolled_people"])

    def test_metadata(self):
        m = get_metric("currently_enrolled_people")
        self.assertEqual(m.display_name, "Currently Enrolled People")
        self.assertEqual((m.category, m.unit, m.value_format), ("Enrollment", "people", "integer"))
        self.assertEqual((m.source, m.plan_year), ("Sisense MAIN", 2027))
        self.assertTrue(m.history_enabled)
        self.assertEqual(m.status, STATUS_ACTIVE)

    def test_references_verified_builder_not_payload(self):
        self.assertIs(CURRENTLY_ENROLLED.query_builder, build_currently_enrolled_query)
        for value in vars(CURRENTLY_ENROLLED).values():
            self.assertNotIsInstance(value, dict)
        self.assertNotIn("datasource", repr(CURRENTLY_ENROLLED))

    def test_roadmap_is_inactive(self):
        self.assertTrue(ROADMAP)
        for item in ROADMAP:
            self.assertEqual(item.status, STATUS_PENDING)
            self.assertNotIn(item.name, [m.display_name for m in METRICS.values()])

    def test_modules(self):
        titles = [m.title for m in MODULES]
        self.assertEqual(titles, [
            "Executive Overview", "Enrollment", "Applications", "Issuers", "EDE", "Financial Assistance",
            "Operations", "DMI & Notices", "Contact Center", "Agent / Broker",
        ])
        active = {m.module_id for m in MODULES if m.status == STATUS_ACTIVE}
        self.assertEqual(active, {"overview", "enrollment"})
        module_ids = {m.module_id for m in MODULES}
        self.assertTrue(all(item.module_id in module_ids for item in ROADMAP))

    def test_active_requires_builder_and_pending_forbids_it(self):
        base = dict(vars(CURRENTLY_ENROLLED))
        with self.assertRaises(ValueError):
            MetricDefinition(**{**base, "query_builder": None})
        with self.assertRaises(ValueError):
            MetricDefinition(**{**base, "status": STATUS_PENDING})

    def test_unknown_metric(self):
        with self.assertRaises(KeyError):
            get_metric("applications_started")


if __name__ == "__main__":
    unittest.main()
