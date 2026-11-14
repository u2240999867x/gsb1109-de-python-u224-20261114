import datetime as dt
import io
import unittest

from pyais.coverage import (
    BucketReport,
    MessageRecord,
    Outage,
    StationWindow,
    bucket_report,
    bucket_reports,
    daily_reports,
    intersect_intervals,
    is_valid_message,
    merge_intervals,
    subtract_intervals,
    write_daily_csv,
)

UTC = dt.timezone.utc
DAY = dt.datetime(2026, 1, 1, tzinfo=UTC)
HOUR = dt.timedelta(hours=1)
MIN = dt.timedelta(minutes=1)

VALID_NMEA = '!AIVDM,1,1,,B,33P;?P0000PD;88MD5MTDwwT0000,0*5C'
GARBAGE = 'this is not an AIS message'


def at(hour=0, minute=0):
    return DAY + hour * HOUR + minute * MIN


def window(station, h_start, h_end, status='open'):
    return StationWindow(station, at(h_start), at(h_end), status)


class IntervalMathTestCase(unittest.TestCase):
    def test_merge_adjacent_intervals(self):
        merged = merge_intervals([(at(10), at(11)), (at(8), at(10))])
        self.assertEqual(merged, [(at(8), at(11))])

    def test_merge_overlapping_intervals(self):
        merged = merge_intervals([(at(8), at(10)), (at(9), at(12)), (at(14), at(15))])
        self.assertEqual(merged, [(at(8), at(12)), (at(14), at(15))])

    def test_subtract_intervals(self):
        rest = subtract_intervals([(at(8), at(12))], [(at(9), at(10)), (at(11), at(13))])
        self.assertEqual(rest, [(at(8), at(9)), (at(10), at(11))])

    def test_intersect_intervals(self):
        both = intersect_intervals([(at(8), at(12))], [(at(9), at(10)), (at(20), at(21))])
        self.assertEqual(both, [(at(9), at(10))])


class BucketReportTestCase(unittest.TestCase):
    def test_effective_coverage_per_bucket(self):
        buckets = bucket_reports(
            [window('A', 8, 12)], [], [], at(0), at(0) + 24 * HOUR,
        )
        covered = {b.start.hour: b.covered_seconds for b in buckets}
        self.assertEqual(covered[8], 3600.0)
        self.assertEqual(covered[11], 3600.0)
        self.assertEqual(covered[7], 0.0)
        self.assertEqual(covered[12], 0.0)

    def test_overlapping_stations_counted_as_union(self):
        windows = [window('A', 8, 11), window('B', 9, 12)]
        report = bucket_report(windows, [], [], (at(8), at(12)), 5 * MIN)
        self.assertEqual(report.covered_seconds, 4 * 3600.0)

    def test_outage_is_subtracted(self):
        report = bucket_report(
            [window('A', 8, 12)],
            [Outage('A', at(9), at(10))],
            [],
            (at(8), at(12)),
            5 * MIN,
        )
        self.assertEqual(report.covered_seconds, 3 * 3600.0)
        self.assertEqual(report.outage_seconds, 3600.0)

    def test_fully_offline_bucket_leaves_ratio_blank(self):
        (report,) = bucket_reports(
            [window('A', 8, 9)],
            [Outage('A', at(8), at(9))],
            [MessageRecord(at(8, 30), VALID_NMEA)],
            at(8), at(9),
        )
        self.assertEqual(report.covered_seconds, 0.0)
        self.assertIsNone(report.message_ratio)

    def test_unknown_status_listed_separately_not_online(self):
        report = bucket_report(
            [window('A', 8, 10), window('B', 9, 12, status='unknown')],
            [],
            [],
            (at(8), at(12)),
            5 * MIN,
        )
        self.assertEqual(report.covered_seconds, 2 * 3600.0)
        # the 09-10 overlap is covered by A, only 10-12 stays unknown
        self.assertEqual(report.unknown_seconds, 2 * 3600.0)
        self.assertEqual(report.active_subperiods, 24)
        self.assertEqual(report.message_ratio, 0.0)

    def test_message_ratio_counts_valid_messages_only(self):
        messages = [
            MessageRecord(at(8, 3), VALID_NMEA),
            MessageRecord(at(8, 4), VALID_NMEA),  # same sub-period as above
            MessageRecord(at(8, 33), GARBAGE),    # invalid, must not count
            MessageRecord(at(8, 47), VALID_NMEA),
        ]
        (report,) = bucket_reports(
            [window('A', 8, 9)], [], messages, at(8), at(9),
        )
        self.assertEqual(report.active_subperiods, 12)
        self.assertEqual(report.observed_subperiods, 2)
        self.assertAlmostEqual(report.message_ratio, 2 / 12)

    def test_default_validator(self):
        self.assertTrue(is_valid_message(VALID_NMEA))
        self.assertFalse(is_valid_message(GARBAGE))


class DailyReportTestCase(unittest.TestCase):
    def _daily(self, windows):
        buckets = bucket_reports(
            windows, [], [MessageRecord(at(8, 3), VALID_NMEA)],
            at(0), at(0) + 24 * HOUR,
        )
        return daily_reports(buckets)

    def test_splitting_open_hours_does_not_change_daily_report(self):
        unsplit = self._daily([window('A', 8, 12)])
        split = self._daily([window('A', 8, 10), window('A', 10, 12)])
        self.assertEqual(len(unsplit), len(split))
        for left, right in zip(unsplit, split):
            self.assertEqual(left.day, right.day)
            self.assertEqual(left.covered_seconds, right.covered_seconds)
            self.assertEqual(left.outage_seconds, right.outage_seconds)
            self.assertEqual(left.unknown_seconds, right.unknown_seconds)
            self.assertEqual(left.active_subperiods, right.active_subperiods)
            self.assertEqual(left.observed_subperiods, right.observed_subperiods)
            self.assertEqual(left.message_ratio, right.message_ratio)

    def test_daily_report_aggregates_buckets(self):
        (report,) = self._daily([window('A', 8, 12)])
        self.assertEqual(report.day, dt.date(2026, 1, 1))
        self.assertEqual(report.covered_seconds, 4 * 3600.0)
        self.assertEqual(report.active_subperiods, 48)
        self.assertEqual(report.observed_subperiods, 1)

    def test_daily_ratio_blank_when_fully_offline(self):
        buckets = bucket_reports(
            [window('A', 8, 12)],
            [Outage('A', at(0), at(0) + 24 * HOUR)],
            [],
            at(0), at(0) + 24 * HOUR,
        )
        (report,) = daily_reports(buckets)
        self.assertEqual(report.covered_seconds, 0.0)
        self.assertIsNone(report.message_ratio)

    def test_daily_csv_renders_blank_ratio(self):
        out = io.StringIO()
        write_daily_csv(daily_reports(bucket_reports(
            [window('A', 8, 12)],
            [Outage('A', at(0), at(0) + 24 * HOUR)],
            [],
            at(0), at(0) + 24 * HOUR,
        )), out)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(
            lines[0],
            'date,covered_seconds,outage_seconds,unknown_seconds,message_ratio',
        )
        self.assertTrue(lines[1].endswith(','), lines[1])


if __name__ == '__main__':
    unittest.main()
