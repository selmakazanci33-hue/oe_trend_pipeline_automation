import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from plotly.offline import get_plotlyjs

from history.store import WRITE_SAVED, SnapshotWriteResult
from metrics.catalog import CURRENTLY_ENROLLED, PENDING_MESSAGE
from reporting.html_report import chart_figure, render_html, write_report
from reporting.model import DATA_LIVE, DATA_NO_HISTORY, DATA_STALE, PIPELINE_ERROR, PIPELINE_PASS, RunOutcome, build_report_model
from tests.reporting_fixtures import T0, result, series

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTING_PACKAGES = ("metrics", "history", "analytics", "reporting")
PLOTLY_JS = get_plotlyjs()


def live_model(snaps, value=806_000):
    stored = snaps[-1] if snaps else None
    write = SnapshotWriteResult(WRITE_SAVED, stored) if stored else None
    outcome = RunOutcome("run", result(value, at=stored.retrieved_at_utc if stored else T0), snapshot_write=write)
    return build_report_model(CURRENTLY_ENROLLED, snaps, outcome, generated_at=T0)


def failed_model(snaps):
    return build_report_model(CURRENTLY_ENROLLED, snaps, RunOutcome("run", None, "AuthenticationError"), T0)


def visible_html(model):
    """Report HTML without the embedded Plotly library."""
    return render_html(model).replace(PLOTLY_JS, "")


class StatusTests(unittest.TestCase):
    def test_live_only_for_successful_run(self):
        m = live_model(series((1, 800_000), (0, 806_000)))
        self.assertEqual((m.data_status, m.pipeline_status), (DATA_LIVE, PIPELINE_PASS))

    def test_stale_when_run_failed_with_history(self):
        m = failed_model(series((1, 800_000), (0, 806_000)))
        self.assertEqual((m.data_status, m.pipeline_status), (DATA_STALE, PIPELINE_ERROR))
        self.assertEqual(m.kpi.value, 806_000)

    def test_no_history(self):
        m = failed_model([])
        self.assertEqual(m.data_status, DATA_NO_HISTORY)
        self.assertIsNone(m.kpi)

    def test_outcome_invariant(self):
        with self.assertRaises(ValueError):
            RunOutcome("r", None, None)
        with self.assertRaises(ValueError):
            RunOutcome("r", result(), "AuthenticationError")


class ChartTests(unittest.TestCase):
    def test_chart_points_match_snapshots_exactly(self):
        snaps = series((50, 790_000), (26, 799_000), (0, 806_000))
        fig = chart_figure(live_model(snaps))
        main_trace = fig["data"][0]
        self.assertEqual(main_trace["y"], [790_000, 799_000, 806_000])
        self.assertEqual(len(main_trace["x"]), 3)
        self.assertIn("rangeslider", fig["layout"]["xaxis"])
        self.assertEqual(fig["layout"]["yaxis"]["tickformat"], ",.0f")

    def test_single_snapshot_renders_without_trend(self):
        fig = chart_figure(live_model(series((0, 806_000))))
        self.assertEqual(fig["data"][0]["y"], [806_000])
        self.assertNotIn("rangeslider", fig["layout"]["xaxis"])
        html = visible_html(live_model(series((0, 806_000))))
        self.assertIn("Insufficient historical data", html)

    def test_no_history_has_no_traces(self):
        fig = chart_figure(failed_model([]))
        self.assertEqual(fig["data"], [])
        self.assertIn("No snapshot history yet", visible_html(failed_model([])))


class HtmlTests(unittest.TestCase):
    def test_header_kpi_sections_and_navigation(self):
        html = visible_html(live_model(series((26, 799_000), (1, 805_000), (0, 806_000))))
        for text in (
            "Georgia Access", "OE Executive Intelligence", "Plan Year 2027", "Last Successful Refresh",
            "Data Source", "Pipeline Status", "CURRENTLY ENROLLED PEOPLE", "806,000",
            "Currently Enrolled — Snapshot Trend",
            "Currently Enrolled — Recent Change", "Previous Snapshot Change", "Daily Change", "7-Day Change",
            "30-Day Change",
            "Data Freshness &amp; Quality", "Historical Snapshot Count", "PY 2027 - OE Executive Dashboard",
            "Executive Overview", "Applications", "Issuers", "EDE", "Financial Assistance", "Operations",
            "DMI &amp; Notices", "Contact Center", "Agent / Broker", PENDING_MESSAGE,
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn("class=\"badge badge-live\"", html)
        self.assertIn("+1,000", html)
        self.assertIn("Insufficient history", html)

    def test_stale_report_labels_historical_and_never_live(self):
        html = visible_html(failed_model(series((0, 806_000))))
        self.assertNotIn("class=\"badge badge-live\"", html)
        self.assertIn("class=\"badge badge-stale\"", html)
        self.assertIn("Historical value", html)
        self.assertIn("AuthenticationError", html)

    def test_pending_modules_show_no_values(self):
        html = visible_html(live_model(series((0, 806_000))))
        for module_id in ("issuers", "ede", "financial_assistance", "operations",
                          "dmi_notices", "contact_center", "agent_broker"):
            section = re.search(rf'id="view-{module_id}".*?</div>', html, re.S).group(0)
            self.assertIn(PENDING_MESSAGE, section)
            self.assertIsNone(re.search(r"\d{1,3}(,\d{3})+", section), f"{module_id} shows a number")

    def test_no_authentication_material_in_html(self):
        html = visible_html(live_model(series((1, 800_000), (0, 806_000)))).lower()
        for word in ("xsrf", "csrf", "cookie", "server.session", "x-tenant", "x-sisense", "authorization",
                     "password", "bearer", "jaql", "datasource"):
            self.assertNotIn(word, html)

    def test_escapes_dynamic_text(self):
        html = visible_html(failed_model([]))
        self.assertNotIn("<script>alert", html)

    def test_write_report_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(live_model(series((0, 806_000))), Path(tmp))
            self.assertEqual(path, Path(tmp) / "latest" / "index.html")
            self.assertTrue(path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))
            self.assertEqual([p.name for p in path.parent.iterdir()], ["index.html"])


class NoHardcodedValuesTests(unittest.TestCase):
    def test_baseline_never_in_reporting_code(self):
        for package in REPORTING_PACKAGES:
            for path in (PROJECT_ROOT / package).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                with self.subTest(path=str(path.relative_to(PROJECT_ROOT))):
                    self.assertNotIn("791826", text)
                    self.assertNotIn("791,826", text)
                    self.assertNotIn("MANUAL_BASELINE", text)
                    self.assertNotIn("manual_baseline", text)


if __name__ == "__main__":
    unittest.main()
