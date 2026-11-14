import datetime
import json
import unittest

from pyais import decode
from pyais.eta import ETAArchive, ETAPrecision, resolve_eta_candidates
from pyais.messages import MessageType5
from pyais.util import checksum

UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


class TestETAPrecision(unittest.TestCase):

    def test_minute_precision(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 5, 16, 12, 0),
            month=5, day=16, hour=14, minute=30,
            window_before=datetime.timedelta(days=1),
            window_after=datetime.timedelta(days=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.MINUTE)
        self.assertEqual(len(archive.candidates), 1)
        candidate = archive.candidates[0]
        self.assertEqual(candidate.start, utc(2024, 5, 16, 14, 30))
        self.assertEqual(candidate.end, utc(2024, 5, 16, 14, 31))
        self.assertEqual(candidate.minute, 30)

    def test_unreported_minute_is_not_midnight(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 5, 16, 12, 0),
            month=5, day=16, hour=14, minute=60,
            window_before=datetime.timedelta(days=1),
            window_after=datetime.timedelta(days=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.HOUR)
        candidate = archive.candidates[0]
        self.assertEqual(candidate.hour, 14)
        self.assertIsNone(candidate.minute)
        self.assertEqual(candidate.start, utc(2024, 5, 16, 14, 0))
        self.assertEqual(candidate.end, utc(2024, 5, 16, 15, 0))
        self.assertEqual(candidate.duration, datetime.timedelta(hours=1))

    def test_unreported_hour_is_not_zero(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 5, 16, 12, 0),
            month=5, day=16, hour=24, minute=60,
            window_before=datetime.timedelta(days=1),
            window_after=datetime.timedelta(days=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.DAY)
        candidate = archive.candidates[0]
        self.assertIsNone(candidate.hour)
        self.assertIsNone(candidate.minute)
        self.assertEqual(candidate.duration, datetime.timedelta(days=1))

    def test_unreported_day_keeps_month_precision(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 5, 15, 12, 0),
            month=5, day=0, hour=24, minute=60,
            window_before=datetime.timedelta(days=20),
            window_after=datetime.timedelta(days=20),
        )
        self.assertEqual(archive.precision, ETAPrecision.MONTH)
        candidate = archive.candidates[0]
        self.assertEqual(candidate.start, utc(2024, 5, 1))
        self.assertEqual(candidate.end, utc(2024, 6, 1))
        self.assertEqual(len(archive.candidates), 31)
        self.assertTrue(all(item.precision is ETAPrecision.MONTH for item in archive.candidates))
        self.assertEqual(archive.civil_dates[0], datetime.date(2024, 5, 1))
        self.assertEqual(archive.civil_dates[-1], datetime.date(2024, 5, 31))
        self.assertTrue(archive.is_ambiguous)

    def test_unreported_month_is_unknown(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 5, 15, 12, 0),
            month=0, day=0, hour=24, minute=60,
            window_before=datetime.timedelta(days=1),
            window_after=datetime.timedelta(days=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.UNKNOWN)
        self.assertEqual(archive.candidates, ())
        self.assertFalse(archive.is_resolvable)


class TestYearResolution(unittest.TestCase):

    def test_new_year_rolls_into_next_year(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 23, 30),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 1, 1),))
        self.assertFalse(archive.is_ambiguous)

    def test_new_year_at_minute_precision(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 23, 30),
            month=1, day=1, hour=0, minute=15,
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(archive.candidates[0].start, utc(2025, 1, 1, 0, 15))
        self.assertEqual(archive.precision, ETAPrecision.MINUTE)

    def test_window_can_contain_two_new_year_days(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2025, 6, 1, 12, 0),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(days=365),
            window_after=datetime.timedelta(days=365),
        )
        self.assertEqual(
            archive.civil_dates,
            (datetime.date(2025, 1, 1), datetime.date(2026, 1, 1)),
        )
        self.assertTrue(archive.is_ambiguous)

    def test_two_leap_days_are_both_reported(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2026, 3, 1, 12, 0),
            month=2, day=29, hour=24, minute=60,
            window_before=datetime.timedelta(days=365 * 4),
            window_after=datetime.timedelta(days=365 * 4),
        )
        self.assertEqual(
            archive.civil_dates,
            (datetime.date(2024, 2, 29), datetime.date(2028, 2, 29)),
        )
        self.assertTrue(archive.is_ambiguous)
        self.assertEqual(len(archive.candidates), 2)
        for candidate in archive.candidates:
            self.assertEqual(candidate.day, 29)
            self.assertEqual(candidate.month, 2)

    def test_month_precision_resolves_year_boundary(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 23, 30),
            month=1, day=0, hour=24, minute=60,
            window_before=datetime.timedelta(hours=1),
            window_after=datetime.timedelta(hours=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.MONTH)
        self.assertEqual(archive.years, (2025,))
        # Only 1 January overlaps the two-hour window around midnight.
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 1, 1),))

    def test_month_precision_can_cover_two_years(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2025, 6, 1, 12, 0),
            month=1, day=0, hour=24, minute=60,
            window_before=datetime.timedelta(days=366),
            window_after=datetime.timedelta(days=366),
        )
        self.assertEqual(archive.years, (2025, 2026))
        self.assertEqual(archive.civil_dates[0], datetime.date(2025, 1, 1))
        self.assertEqual(archive.civil_dates[-1], datetime.date(2026, 1, 31))
        self.assertTrue(archive.is_ambiguous)

    def test_leap_day_is_skipped_in_non_leap_year(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2025, 2, 28, 12, 0),
            month=2, day=29, hour=24, minute=60,
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(archive.candidates, ())
        self.assertFalse(archive.is_resolvable)

    def test_candidate_outside_window_is_dropped(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 12, 0),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(hours=1),
            window_after=datetime.timedelta(hours=1),
        )
        # The window ends at 13:00 on 31 December, before 1 January starts.
        self.assertEqual(archive.candidates, ())

    def test_window_is_inclusive_at_both_ends(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 23, 0),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(hours=1),
            window_after=datetime.timedelta(hours=1),
        )
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 1, 1),))


class TestArchiveRepresentation(unittest.TestCase):

    def make_archive(self):
        return ETAArchive.from_ais(
            received_at=utc(2024, 12, 31, 23, 30),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )

    def test_raw_fields_are_preserved(self):
        archive = self.make_archive()
        self.assertEqual(
            archive.reported,
            {'month': 1, 'day': 1, 'hour': 24, 'minute': 60},
        )
        self.assertEqual(archive.month, 1)
        self.assertEqual(archive.day, 1)
        self.assertIsNone(archive.hour)
        self.assertIsNone(archive.minute)

    def test_json_round_trip(self):
        archive = self.make_archive()
        text = archive.to_json()
        self.assertEqual(json.loads(text)['precision'], 'day')
        restored = ETAArchive.fromdict(json.loads(text))
        self.assertEqual(restored, archive)

    def test_fromdict_accepts_zulu_timestamps(self):
        data = self.make_archive().asdict()
        data['received_at'] = data['received_at'].replace('+00:00', 'Z')
        restored = ETAArchive.fromdict(data)
        self.assertEqual(restored, self.make_archive())

    def test_asdict_carries_candidates(self):
        data = self.make_archive().asdict()
        self.assertEqual(data['precision'], 'day')
        self.assertEqual(data['window']['before_seconds'], 2 * 24 * 3600)
        self.assertEqual(len(data['candidates']), 1)
        self.assertEqual(data['candidates'][0]['year'], 2025)

    def test_field_precision_keeps_reported_fields(self):
        archive = self.make_archive()
        self.assertEqual(
            archive.field_precision,
            {
                'month': ETAPrecision.MONTH,
                'day': ETAPrecision.DAY,
                'hour': None,
                'minute': None,
            },
        )
        data = json.loads(archive.to_json())
        self.assertEqual(
            data['field_precision'],
            {'month': 'month', 'day': 'day', 'hour': None, 'minute': None},
        )

    def test_field_precision_clears_orphaned_minute(self):
        archive = ETAArchive.from_ais(
            received_at=utc(2025, 5, 15, 12, 0),
            month=5, day=15, hour=24, minute=30,
            window_before=datetime.timedelta(days=1),
            window_after=datetime.timedelta(days=1),
        )
        self.assertEqual(archive.precision, ETAPrecision.DAY)
        self.assertIsNone(archive.field_precision['hour'])
        self.assertIsNone(archive.field_precision['minute'])

    def test_helper_matches_archive(self):
        candidates = resolve_eta_candidates(
            received_at=utc(2024, 12, 31, 23, 30),
            month=1, day=1, hour=24, minute=60,
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(candidates, self.make_archive().candidates)


class TestMessageType5(unittest.TestCase):

    def test_eta_archive_from_type_5(self):
        msg = decode(
            "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C",
            "!AIVDM,2,2,1,A,88888888880,2*25",
        )
        self.assertIsInstance(msg, MessageType5)
        archive = msg.eta_archive(
            received_at=utc(2025, 5, 15, 12, 0),
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertIsInstance(archive, ETAArchive)
        # The sample reports 15 May 14:00.  Minute 0 is a real minute, not
        # the "not available" sentinel.
        self.assertEqual(archive.precision, ETAPrecision.MINUTE)
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 5, 15),))
        self.assertEqual(archive.candidates[0].start, utc(2025, 5, 15, 14, 0))

    def test_eta_candidates_shorthand(self):
        msg = MessageType5.create(mmsi="123456", month=1, day=1, hour=24, minute=60)
        candidates = msg.eta_candidates(
            received_at=utc(2024, 12, 31, 23, 30),
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].date, datetime.date(2025, 1, 1))

    def test_from_message(self):
        msg = MessageType5.create(mmsi="123456", month=12, day=31, hour=23, minute=45)
        archive = ETAArchive.from_message(
            msg,
            received_at=utc(2024, 12, 1, 0, 0),
            window_before=datetime.timedelta(days=31),
            window_after=datetime.timedelta(days=31),
        )
        self.assertEqual(archive.civil_dates, (datetime.date(2024, 12, 31),))

    def test_from_message_without_eta_fields(self):
        with self.assertRaises(ValueError):
            ETAArchive.from_message(
                object(),
                received_at=utc(2024, 12, 1),
                window_before=datetime.timedelta(days=1),
                window_after=datetime.timedelta(days=1),
            )


class TestSentenceInput(unittest.TestCase):

    PARTS = (
        "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C",
        "!AIVDM,2,2,1,A,88888888880,2*25",
    )

    def assembled(self, first=None):
        from pyais.decode import decode_nmea_and_ais
        parts = self.PARTS
        if first is not None:
            parts = (first,) + parts[1:]
        sentence, _ = decode_nmea_and_ais(*parts)
        return sentence

    def test_received_at_must_be_available(self):
        with self.assertRaises(ValueError):
            ETAArchive.from_sentence(
                self.assembled(),
                window_before=datetime.timedelta(days=1),
                window_after=datetime.timedelta(days=1),
            )

    def test_tag_block_receiver_timestamp(self):
        # c:1747310400 is 2025-05-15T12:00:00Z.
        payload = b"s:2573535,c:1747310400"
        tag_block = payload + b"*" + hex(checksum(payload))[2:].upper().encode()
        first = b"\\" + tag_block + b"\\" + self.PARTS[0].encode()
        archive = ETAArchive.from_sentence(
            self.assembled(first),
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(archive.received_at, utc(2025, 5, 15, 12, 0))
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 5, 15),))

    def test_explicit_received_at_wins(self):
        archive = ETAArchive.from_sentence(
            self.assembled(),
            received_at=utc(2025, 5, 15, 12, 0),
            window_before=datetime.timedelta(days=2),
            window_after=datetime.timedelta(days=2),
        )
        self.assertEqual(archive.civil_dates, (datetime.date(2025, 5, 15),))


class TestValidation(unittest.TestCase):

    def test_negative_window_is_rejected(self):
        with self.assertRaises(ValueError):
            ETAArchive.from_ais(
                received_at=utc(2024, 1, 1),
                month=1, day=2, hour=24, minute=60,
                window_before=datetime.timedelta(days=-1),
                window_after=datetime.timedelta(days=1),
            )

    def test_non_timedelta_window_is_rejected(self):
        with self.assertRaises(TypeError):
            ETAArchive.from_ais(
                received_at=utc(2024, 1, 1),
                month=1, day=2, hour=24, minute=60,
                window_before=1,  # type: ignore[arg-type]
                window_after=datetime.timedelta(days=1),
            )

    def test_non_datetime_received_at_is_rejected(self):
        with self.assertRaises(TypeError):
            ETAArchive.from_ais(
                received_at='2024-01-01',
                month=1, day=2, hour=24, minute=60,
                window_before=datetime.timedelta(days=1),
                window_after=datetime.timedelta(days=1),
            )


if __name__ == '__main__':
    unittest.main()
