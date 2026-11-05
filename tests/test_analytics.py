import unittest
from datetime import timedelta

from analytics.derived import DERIVED, INSUFFICIENT_HISTORY, SOURCE, DataPoint, compute_analytics
from tests.reporting_fixtures import METRIC_ID, T0, series, snapshot


def analytics(snaps, current=None):
    return compute_analytics(METRIC_ID, snaps, current)


class EmptyAndSingleTests(unittest.TestCase):
    def test_no_history(self):
        a = analytics([])
        self.assertIsNone(a.latest)
        self.assertEqual(a.points, ())
        for c in a.comparisons:
            self.assertFalse(c.available)
            self.assertIsNone(c.change.value)
        self.assertFalse(a.latest_snapshot_time.available)

    def test_single_snapshot(self):
        a = analytics(series((0, 812_000)))
        self.assertEqual(a.latest.value, 812_000)
        self.assertEqual(a.latest.kind, SOURCE)
        for c in a.comparisons:
            self.assertIsNone(c.change.value, "never zero-filled")
            self.assertEqual(c.change.reason, INSUFFICIENT_HISTORY)
        self.assertFalse(a.historical_min.available)
        self.assertFalse(a.historical_max.available)
        self.assertFalse(a.average_daily_change.available)
        self.assertEqual(a.latest_snapshot_time.value, T0)


class ComparisonTests(unittest.TestCase):
    def test_latest_and_previous(self):
        a = analytics(series((2, 800_000), (1, 805_000), (0, 806_000)))
        self.assertEqual(a.latest.value, 806_000)
        self.assertEqual(a.previous.change.value, 1_000)
        self.assertAlmostEqual(a.previous.change_pct.value, 1_000 / 805_000 * 100)
        self.assertEqual(a.previous.change.reference.value, 805_000)
        self.assertEqual(a.previous.change.kind, DERIVED)

    def test_daily_uses_point_at_least_24h_earlier(self):
        a = analytics(series((30, 790_000), (25, 795_000), (20, 799_000), (0, 806_000)))
        daily = a.comparison("daily")
        self.assertEqual(daily.change.reference.value, 795_000)
        self.assertEqual(daily.change.value, 11_000)

    def test_daily_insufficient_when_only_recent_or_too_old(self):
        self.assertFalse(analytics(series((5, 800_000), (0, 806_000))).comparison("daily").available)
        self.assertFalse(analytics(series((40, 800_000), (0, 806_000))).comparison("daily").available)

    def test_seven_day(self):
        a = analytics(series((7 * 24 + 3, 760_000), (0, 806_000)))
        self.assertEqual(a.comparison("7d").change.value, 46_000)
        self.assertAlmostEqual(a.comparison("7d").change_pct.value, 46_000 / 760_000 * 100)
        self.assertFalse(analytics(series((9 * 24, 760_000), (0, 806_000))).comparison("7d").available)

    def test_thirty_day(self):
        a = analytics(series((30 * 24 + 10, 600_000), (0, 806_000)))
        self.assertEqual(a.comparison("30d").change.value, 206_000)
        self.assertFalse(analytics(series((20 * 24, 600_000), (0, 806_000))).comparison("30d").available)

    def test_zero_reference_percent_undefined(self):
        a = analytics(series((1, 0), (0, 10)))
        self.assertEqual(a.previous.change.value, 10)
        self.assertFalse(a.previous.change_pct.available)


class SummaryStatsTests(unittest.TestCase):
    def test_min_max(self):
        a = analytics(series((3, 805_000), (2, 799_000), (1, 811_000), (0, 806_000)))
        self.assertEqual(a.historical_min.value, 799_000)
        self.assertEqual(a.historical_max.value, 811_000)

    def test_average_daily_change(self):
        a = analytics(series((48, 800_000), (0, 806_000)))
        self.assertAlmostEqual(a.average_daily_change.value, 3_000)
        self.assertFalse(analytics(series((10, 800_000), (0, 806_000))).average_daily_change.available)


class CurrentPointTests(unittest.TestCase):
    def test_unstored_live_value_is_latest_but_not_counted_as_snapshot(self):
        snaps = series((26, 795_000), (0.2, 805_000))
        current = DataPoint(806_000, T0 + timedelta(minutes=5), stored=False)
        a = analytics(snaps, current)
        self.assertEqual(a.latest.value, 806_000)
        self.assertEqual(a.stored_snapshot_count, 2)
        self.assertEqual(len(a.points), 3)
        self.assertEqual(a.previous.change.reference.value, 805_000)

    def test_stored_current_not_double_counted(self):
        snaps = series((1, 800_000), (0, 806_000))
        current = DataPoint(806_000, T0, stored=True, snapshot_id=snaps[-1].id)
        a = analytics(snaps, current)
        self.assertEqual(len(a.points), 2)
        self.assertEqual(a.previous.change.reference.value, 800_000)

    def test_no_fabricated_points(self):
        snaps = [snapshot(800_000, T0)]
        self.assertEqual(len(analytics(snaps).points), 1)


if __name__ == "__main__":
    unittest.main()
