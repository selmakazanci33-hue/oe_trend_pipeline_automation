"""Phase 3F-A: executive vs developer report consistency (fakes only, network blocked, invented values)."""

import re
from datetime import timedelta

from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_session_context import FakeResponse
from tests.test_pipeline_main import RUN_TIME
from tests.test_runtime_validation import natural_wave1

DEVELOPER_TERMS = ("VERIFIED", "Verified", "DISCOVERED", "VALUE_EXTRACTED", "NOT_AVAILABLE", "VALIDATION",
                   "fingerprint", "sha256", "NO HISTORY", "6a98048ccef60720fe7f9b", "w-start", "w-nap")
VALIDATION_ONLY = {"started": 400_101, "submitted": 400_202, "applicants": 400_303,
                   "new": 4_404, "active": 40_505, "passive": 400_606}


def visible(html):
    return re.sub(r"<script>.*?</script>|<style>.*?</style>", "", html, flags=re.S)


class ExecutiveReportConsistencyTests(PassiveMetricsHarness):
    def full_run(self, ce=700_100, pct=0.904434, **kw):
        wave = natural_wave1({"ce": ce, **VALIDATION_ONLY})
        wave[0] = self.ce_widget(ce=ce, pct=pct)
        return self.run_pipeline(ce, *wave, discovery=True, runtime_validation=True, **kw)

    def test_no_developer_terminology_in_executive_report(self):
        self.full_run()
        text = visible(self.report_html())
        for term in DEVELOPER_TERMS:
            self.assertNotIn(term, text)

    def test_percent_effectuated_only_live_never_pending(self):
        self.full_run()
        text = visible(self.report_html())
        self.assertEqual(text.count('data-metric="percent_effectuated"'), 2)  # Overview + Enrollment
        self.assertNotIn("% Effectuated", self.pending_chips())
        self.assertIn('class="badge badge-live"', self.card("percent_effectuated"))

    def test_validation_values_stay_in_developer_report(self):
        self.full_run()
        executive = visible(self.report_html())
        validation = (self.reports / "validation" / "wave1_runtime_validation.html").read_text(encoding="utf-8")
        for value in VALIDATION_ONLY.values():
            shown = f"{value:,}"
            self.assertIn(shown, validation)
            self.assertNotIn(shown, executive)
        self.assertIn("<h2>Wave 1 metrics</h2>", validation)
        self.assertNotIn("Discovered metrics", validation)

    def test_snapshot_state_is_per_metric(self):
        mutate = lambda b: b["datasource"].update(title="OTHER")
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, mutate=mutate))
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), at=RUN_TIME + timedelta(minutes=5))
        self.assertEqual(self.rows("currently_enrolled_people"), [700_100])
        self.assertEqual(self.rows("percent_effectuated"), [0.904434])
        html = self.report_html()
        card = self.card("percent_effectuated")
        self.assertIn('Snapshot this run: <span class="badge badge-pass">SAVED</span>', card)
        self.assertIn("(1 stored)", card)
        quality = html.split("Data Freshness &amp; Quality")[1]
        self.assertIn('<span class="section-sub">Currently Enrolled People</span>', quality)
        self.assertIn("DUPLICATE_SLOT", quality.split("</section>")[0])

    def test_histories_are_metric_specific_and_changes_in_points(self):
        self.run_pipeline(700_000, self.ce_widget(ce=700_000, pct=0.901234), at=RUN_TIME - timedelta(hours=4))
        rejected = lambda b: b["datasource"].update(title="OTHER")
        self.run_pipeline(700_050, self.ce_widget(ce=700_050, mutate=rejected), at=RUN_TIME - timedelta(hours=2))
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434))
        card = self.card("percent_effectuated")
        self.assertIn("90.44%", card)
        self.assertIn("+0.32 pp", card)
        self.assertIn(f"since previous snapshot ({(RUN_TIME - timedelta(hours=4)).strftime('%b %d, %Y %H:%M')} UTC)",
                      card)
        self.assertIn("(2 stored)", card)
        self.assertNotIn("+0.36%", card)  # relative change of the ratio is never shown
        self.assertNotIn("+32", card)
        text = visible(self.report_html())
        self.assertIn("+50", text)  # Currently Enrolled previous change uses its own history (700,050 -> 700,100)
        self.assertNotIn("0.904434", text)
        self.assertNotIn("0.901234", text)

    def test_one_metric_failing_does_not_change_the_other(self):
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), at=RUN_TIME - timedelta(hours=2))
        rejected = lambda b: b["datasource"].update(title="OTHER")
        self.run_pipeline(700_200, self.ce_widget(ce=700_200, mutate=rejected))
        html = self.report_html()
        self.assertIn('class="badge badge-stale"', self.card("percent_effectuated"))
        kpi = html.split('<section class="card kpi">')[1].split("</section>")[0]
        self.assertIn('class="badge badge-live"', kpi)
        self.assertIn("700,200", kpi)
        self.assertNotIn("banner-stale", visible(html))

    def test_ce_without_data_uses_not_available(self):
        self.run_pipeline(None, responses=[FakeResponse(401, "{}")])
        text = visible(self.report_html())
        self.assertNotIn("NO HISTORY", text)
        kpi = text.split('<section class="card kpi">')[1].split("</section>")[0]
        self.assertIn("NOT AVAILABLE", kpi)
        self.assertIn("NOT AVAILABLE", self.card("percent_effectuated"))

    def test_ce_only_sections_name_their_metric(self):
        self.full_run()
        html = self.report_html()
        text = visible(html)
        self.assertIn("Currently Enrolled People — derived from actual snapshot history only", text)
        self.assertIn("Currently Enrolled People — built only from stored validated snapshots", text)
        self.assertIn("Currently Enrolled — History &amp; Trends", html)
        self.assertNotIn("Enrollment Movement", html)
        self.assertNotIn("Additional Enrollment Metrics", html)
        self.assertNotIn("Source &amp; Derived Values", html)

    def test_pending_roadmap_is_catalog_driven_without_status_notes(self):
        self.full_run()
        chips = self.pending_chips()
        for name in ("Total Applicants", "Cancelled", "Terminated", "Metal Tier"):
            self.assertIn(name, chips)
        for name in ("Applications Started", "Submitted Applications", "New", "Active", "Passive"):
            self.assertNotIn(name, chips)
        self.assertNotIn("roadmap-note", self.report_html())
