import unittest

from pyais import AISTracker, decode
from pyais.encode import encode_dict
from pyais.history import UNAVAILABLE, TrackHistory, TrackHistoryTable
from pyais.messages import AISSentence, NMEASentenceFactory
from pyais.util import checksum


def encode(message_type: int, mmsi: int = 111111111, **fields: object) -> bytes:
    data = {'type': message_type, 'mmsi': mmsi}
    data.update(fields)
    return encode_dict(data)[0]


def decode_dict(message_type: int, mmsi: int = 111111111, **fields: object):
    return decode(encode(message_type, mmsi, **fields))


class TestFieldHistory(unittest.TestCase):

    def test_older_message_fills_in_new_fields_without_moving_newer_position(self):
        tracker = AISTracker(ttl_in_seconds=None, backfill=True)

        # The first message reports only a position (type 18 has no status field).
        tracker.update(decode_dict(18, lat=40.0, lon=-70.0), 1000.0, source='newer')
        self.assertEqual(tracker.get_track(111111111).lat, 40.0)
        self.assertIsNone(tracker.get_track(111111111).status)  # type: ignore

        # The older message adds a navigation status that was never seen before.
        tracker.update(decode_dict(1, lat=41.0, lon=-71.0, status=1), 900.0, source='older')

        track = tracker.get_track(111111111)
        self.assertEqual(track.lat, 40.0)
        self.assertEqual(track.lon, -70.0)
        self.assertEqual(track.last_updated, 1000.0)

        # At the older observation time both values are visible.
        state = tracker.state_at(111111111, 900.0)
        self.assertEqual(state['lat'], 41.0)
        self.assertEqual(state['lon'], -71.0)
        self.assertEqual(state['status'], 1)

        # At the newer time the newer position still wins, but the status
        # that was only reported earlier is still known.
        state = tracker.state_at(111111111, 1000.0)
        self.assertEqual(state['lat'], 40.0)
        self.assertEqual(state['lon'], -70.0)
        self.assertEqual(state['status'], 1)

    def test_state_at_only_returns_fields_known_at_that_time(self):
        history = TrackHistory(111111111)
        history.record('lat', 1.0, observed_at=100.0, source='a')
        history.record('lon', 2.0, observed_at=200.0, source='a')
        history.record('status', 5, observed_at=300.0, source='a')

        state = history.state_at(150.0)
        self.assertEqual(state.asdict(), {'lat': 1.0})
        self.assertEqual(state.unavailable, frozenset())

        state = history.state_at(250.0)
        self.assertEqual(state.asdict(), {'lat': 1.0, 'lon': 2.0})

        state = history.values_at(250.0)
        self.assertEqual(state, {'lat': 1.0, 'lon': 2.0})

    def test_late_duplicate_does_not_duplicate_history(self):
        history = TrackHistory(111111111)
        history.record('lat', 1.0, observed_at=100.0, source='a')
        history.record('lat', 1.0, observed_at=100.0, source='a')
        self.assertEqual(len(history.history('lat')), 1)

    def test_conflicting_values_at_the_same_time_keep_both_sources(self):
        history = TrackHistory(111111111)
        history.record('lat', 1.0, observed_at=100.0, source='station-a')
        history.record('lat', 1.5, observed_at=100.0, source='station-b')

        state = history.state_at(100.0)
        self.assertEqual(state.entries('lat'), ((1.0, 'station-a'), (1.5, 'station-b')))
        self.assertEqual(state.sources('lat'), ('station-a', 'station-b'))
        self.assertIn('lat', state.conflicts)
        self.assertEqual(len(state.observations['lat']), 2)

    def test_equal_values_from_two_sources_are_not_a_conflict(self):
        history = TrackHistory(111111111)
        history.record('lat', 1.0, observed_at=100.0, source='station-a')
        history.record('lat', 1.0, observed_at=100.0, source='station-b')

        state = history.state_at(100.0)
        self.assertEqual(state.sources('lat'), ('station-a', 'station-b'))
        self.assertEqual(state.conflicts, {})

    def test_explicitly_unavailable_differs_from_absent(self):
        history = TrackHistory(111111111)
        history.record('lat', 1.0, observed_at=100.0)
        history.record('status', 15, observed_at=100.0)

        state = history.state_at(100.0)
        self.assertTrue(state.has('status'))
        self.assertTrue(state.is_unavailable('status'))
        self.assertIsNone(state['status'])
        self.assertEqual(state.raw['status'], 15)

        self.assertFalse(state.has('shipname'))
        self.assertNotIn('shipname', state)
        self.assertIn('shipname', state.absent)

    def test_message_without_field_does_not_override_previous_value(self):
        history = TrackHistory(111111111)
        history.record('status', 5, observed_at=100.0)
        # Message type 18 simply has no status field.
        history.merge_message(decode_dict(18, lat=1.0, lon=2.0), observed_at=200.0)
        self.assertEqual(history.state_at(200.0)['status'], 5)

    def test_field_that_is_explicitly_unavailable_overrides_older_value(self):
        history = TrackHistory(111111111)
        history.record('heading', 70, observed_at=100.0)
        history.merge_message(decode_dict(1, lat=1.0, lon=2.0, heading=511), observed_at=200.0)

        state = history.state_at(200.0)
        self.assertTrue(state.is_unavailable('heading'))
        self.assertIsNone(state['heading'])
        self.assertEqual(state.raw['heading'], 511)

    def test_sample_rows_distinguish_missing_and_unavailable(self):
        table = TrackHistoryTable()
        table.add_sample({
            'mmsi': 111111111,
            'observed_at': 100.0,
            'source': 'station-a',
            'fields': {'lat': 1.0, 'status': None},
        })
        state = table.state_at(111111111, 100.0)
        self.assertIsNone(state['status'])
        self.assertTrue(state.is_unavailable('status'))
        self.assertNotIn('shipname', state)

    def test_unavailable_sentinel(self):
        history = TrackHistory(111111111)
        history.add({'field': 'shipname', 'value': UNAVAILABLE, 'observed_at': 100.0})
        state = history.state_at(100.0)
        self.assertIsNone(state['shipname'])
        self.assertTrue(state.is_unavailable('shipname'))


class TestTrackerIntegration(unittest.TestCase):

    def test_exact_duplicates_are_still_rejected_by_default(self):
        tracker = AISTracker(ttl_in_seconds=None)
        message = decode_dict(1, lat=1.0, lon=2.0, speed=3.0)
        tracker.update(message, 100.0)

        with self.assertRaises(ValueError):
            # Nothing new is learned from replaying the same message.
            tracker.update(message, 90.0)

        # A different older position adds history instead of being rejected.
        tracker.update(decode_dict(1, lat=1.1, lon=2.1, speed=3.1), 90.0)
        self.assertEqual(tracker.state_at(111111111, 90.0)['lat'], 1.1)
        self.assertEqual(tracker.get_track(111111111).lat, 1.0)

    def test_merge_accepts_older_messages_even_in_ordered_streams(self):
        tracker = AISTracker(ttl_in_seconds=None, stream_is_ordered=True)
        tracker.update(decode_dict(18, lat=1.0, lon=2.0), 100.0)
        tracker.merge(decode_dict(1, lat=0.5, lon=1.5, status=1), 90.0)

        self.assertEqual(tracker.get_track(111111111).lat, 1.0)
        self.assertEqual(tracker.state_at(111111111, 100.0)['status'], 1)
        self.assertEqual([track.mmsi for track in tracker.n_latest_tracks(1)], [111111111])

    def test_observations_at_is_inclusive(self):
        tracker = AISTracker(ttl_in_seconds=None)
        tracker.update(decode_dict(1, lat=1.0, lon=2.0), 100.0)
        tracker.update(decode_dict(1, lat=1.5, lon=2.5), 200.0)

        self.assertEqual(tracker.values_at(111111111, 99.0), {})
        self.assertEqual(tracker.values_at(111111111, 100.0)['lat'], 1.0)
        self.assertEqual(tracker.values_at(111111111, 150.0)['lat'], 1.0)
        self.assertEqual(tracker.values_at(111111111, 200.0)['lat'], 1.5)

    def test_tag_block_source_is_used(self):
        payload = b's:STATION-A,c:1671533231'
        raw = b'\\' + payload + b'*' + f'{checksum(payload):02X}'.encode() + b'\\' + \
            b'!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23'
        sentence = NMEASentenceFactory.produce(raw)
        self.assertIsInstance(sentence, AISSentence)

        tracker = AISTracker(ttl_in_seconds=None)
        tracker.update(sentence, 1000.0)
        observation = tracker.history(227006760).history('lat')[-1]  # type: ignore
        self.assertEqual(observation.source, 'STATION-A')

    def test_pop_track_removes_history(self):
        tracker = AISTracker(ttl_in_seconds=None)
        tracker.update(decode_dict(1, lat=1.0, lon=2.0), 100.0)
        self.assertIsNotNone(tracker.history(111111111))
        tracker.pop_track(111111111)
        self.assertIsNone(tracker.history(111111111))

    def test_keep_history_can_be_disabled(self):
        tracker = AISTracker(ttl_in_seconds=None, keep_history=False)
        tracker.update(decode_dict(1, lat=1.0, lon=2.0), 100.0)
        self.assertIsNone(tracker.history(111111111))

    def test_backfill_false_rejects_every_older_message(self):
        tracker = AISTracker(ttl_in_seconds=None, backfill=False)
        tracker.update(decode_dict(1, lat=1.0, lon=2.0), 100.0)
        with self.assertRaises(ValueError):
            tracker.update(decode_dict(1, lat=0.5, lon=1.5, status=1), 90.0)
        self.assertEqual(tracker.get_track(111111111).lat, 1.0)

    def test_backfill_true_accepts_older_messages(self):
        tracker = AISTracker(ttl_in_seconds=None, backfill=True)
        tracker.update(decode_dict(1, lat=1.0, lon=2.0), 100.0)
        tracker.update(decode_dict(1, lat=0.5, lon=1.5), 90.0)
        self.assertEqual(tracker.get_track(111111111).lat, 1.0)
        self.assertEqual(tracker.state_at(111111111, 90.0)['lat'], 0.5)


if __name__ == '__main__':
    unittest.main()
