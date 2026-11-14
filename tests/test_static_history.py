import unittest

from pyais.messages import AISSentence
from pyais.static_history import StaticDataHistory, StaticHalfState

MMSI = 211512345
AUX_MMSI = 982261001

# Encoded with pyais.encode.encode_dict
PART_A_NMEA = b"!AIVDO,1,1,,A,H39ed6A@E=A<PU00000000000000,0*50"
PART_B_NMEA = b"!AIVDO,1,1,,A,H39ed6E60000000D5CD0001@D560,0*52"
PART_B_AUX_NMEA = b"!AIVDO,1,1,,A,H>`hM2DO00000001EH0000<VnhI0,0*37"


def part_a(shipname='TESTSHIP'):
    return AISSentence(PART_A_NMEA).decode() if shipname == 'TESTSHIP' else _encode_a(shipname)


def _encode_a(shipname):
    from pyais.encode import encode_dict
    return AISSentence(encode_dict(
        {'msg_type': 24, 'partno': 0, 'mmsi': MMSI, 'shipname': shipname},
        radio_channel='A', talker_id='AI',
    )[0].encode()).decode()


def part_b(**overrides):
    from pyais.encode import encode_dict
    payload = {
        'msg_type': 24, 'partno': 1, 'mmsi': MMSI, 'ship_type': 70, 'callsign': 'TEST',
        'to_bow': 10, 'to_stern': 20, 'to_port': 5, 'to_starboard': 6,
    }
    payload.update(overrides)
    return AISSentence(encode_dict(payload, radio_channel='A', talker_id='AI')[0].encode()).decode()


class StaticDataHistoryTestCase(unittest.TestCase):

    def test_combines_halves_from_different_batches_with_own_expiry(self):
        history = StaticDataHistory()
        # Batch 1: Part A observed at t=1000, valid for one hour
        history.update(part_a(), ts_epoch=1000.0, ttl_in_seconds=3600)
        # Batch 2: Part B observed at t=2000, valid for ten minutes
        history.update(part_b(), ts_epoch=2000.0, ttl_in_seconds=600)

        snapshot = history.query(MMSI, at_epoch=2100.0)
        self.assertEqual(snapshot.a_state, StaticHalfState.VALID)
        self.assertEqual(snapshot.b_state, StaticHalfState.VALID)
        self.assertEqual(snapshot.shipname, 'TESTSHIP')
        self.assertEqual(snapshot.to_bow, 10)
        self.assertEqual(snapshot.to_stern, 20)
        self.assertEqual(snapshot.to_port, 5)
        self.assertEqual(snapshot.to_starboard, 6)
        self.assertEqual(snapshot.ship_type, 70)
        self.assertEqual(snapshot.callsign, 'TEST')
        self.assertEqual(snapshot.a_observed_at, 1000.0)
        self.assertEqual(snapshot.b_observed_at, 2000.0)

    def test_late_imported_old_part_b_does_not_overwrite_newer_one(self):
        history = StaticDataHistory()
        self.assertTrue(history.update(part_b(to_bow=50), ts_epoch=2000.0))
        # A stale record from an earlier batch is imported afterwards
        self.assertFalse(history.update(part_b(to_bow=99), ts_epoch=1000.0))

        snapshot = history.query(MMSI, at_epoch=3000.0)
        self.assertEqual(snapshot.to_bow, 50)

        # But time travel to a moment before the newer record still works
        snapshot = history.query(MMSI, at_epoch=1500.0)
        self.assertEqual(snapshot.b_state, StaticHalfState.VALID)
        self.assertEqual(snapshot.to_bow, 99)

    def test_auxiliary_craft_reports_mothership_instead_of_dimensions(self):
        history = StaticDataHistory()
        history.update(AISSentence(PART_B_AUX_NMEA), ts_epoch=1000.0)

        snapshot = history.query(AUX_MMSI, at_epoch=1000.0)
        self.assertEqual(snapshot.b_state, StaticHalfState.VALID)
        self.assertTrue(snapshot.is_auxiliary)
        self.assertEqual(snapshot.mothership_mmsi, 211512345)
        self.assertIsNone(snapshot.to_bow)
        self.assertIsNone(snapshot.to_stern)
        self.assertIsNone(snapshot.to_port)
        self.assertIsNone(snapshot.to_starboard)

    def test_normal_vessel_reports_dimensions_instead_of_mothership(self):
        history = StaticDataHistory()
        history.update(part_b(), ts_epoch=1000.0)

        snapshot = history.query(MMSI, at_epoch=1000.0)
        self.assertEqual(snapshot.b_state, StaticHalfState.VALID)
        self.assertFalse(snapshot.is_auxiliary)
        self.assertIsNone(snapshot.mothership_mmsi)
        self.assertEqual(snapshot.to_bow, 10)

    def test_expired_part_b_yields_name_and_expiry_evidence_only(self):
        history = StaticDataHistory()
        history.update(part_a(), ts_epoch=1000.0, ttl_in_seconds=86400)
        # Part B received "last night", already expired at query time
        history.update(part_b(), ts_epoch=1000.0, ttl_in_seconds=3600)

        snapshot = history.query(MMSI, at_epoch=10000.0)
        self.assertEqual(snapshot.a_state, StaticHalfState.VALID)
        self.assertEqual(snapshot.shipname, 'TESTSHIP')

        self.assertEqual(snapshot.b_state, StaticHalfState.EXPIRED)
        self.assertEqual(snapshot.b_observed_at, 1000.0)
        self.assertEqual(snapshot.b_expires_at, 4600.0)
        # The expired dimensions must not leak into the snapshot
        self.assertIsNone(snapshot.to_bow)
        self.assertIsNone(snapshot.to_stern)
        self.assertIsNone(snapshot.to_port)
        self.assertIsNone(snapshot.to_starboard)
        self.assertIsNone(snapshot.ship_type)
        self.assertIsNone(snapshot.callsign)
        self.assertIsNone(snapshot.is_auxiliary)

    def test_unknown_mmsi_is_missing(self):
        history = StaticDataHistory()
        snapshot = history.query(999999999, at_epoch=1000.0)
        self.assertEqual(snapshot.a_state, StaticHalfState.MISSING)
        self.assertEqual(snapshot.b_state, StaticHalfState.MISSING)
        self.assertIsNone(snapshot.shipname)
        self.assertIsNone(snapshot.a_observed_at)
        self.assertIsNone(snapshot.b_expires_at)

    def test_query_before_first_observation_is_missing(self):
        history = StaticDataHistory()
        history.update(part_a(), ts_epoch=1000.0)
        history.update(part_b(), ts_epoch=1000.0)

        snapshot = history.query(MMSI, at_epoch=500.0)
        self.assertEqual(snapshot.a_state, StaticHalfState.MISSING)
        self.assertEqual(snapshot.b_state, StaticHalfState.MISSING)
        self.assertIsNone(snapshot.shipname)
        self.assertIsNone(snapshot.to_bow)

    def test_expired_part_a_hides_the_name(self):
        history = StaticDataHistory()
        history.update(part_a(), ts_epoch=1000.0, ttl_in_seconds=60)

        snapshot = history.query(MMSI, at_epoch=2000.0)
        self.assertEqual(snapshot.a_state, StaticHalfState.EXPIRED)
        self.assertIsNone(snapshot.shipname)
        self.assertEqual(snapshot.a_expires_at, 1060.0)

    def test_accepts_raw_sentences_and_decoded_messages(self):
        history = StaticDataHistory()
        history.update(AISSentence(PART_A_NMEA), ts_epoch=1000.0)
        history.update(AISSentence(PART_B_NMEA).decode(), ts_epoch=1000.0)

        snapshot = history.query(MMSI, at_epoch=1000.0)
        self.assertEqual(snapshot.shipname, 'TESTSHIP')
        self.assertEqual(snapshot.to_bow, 10)

    def test_rejects_other_message_types(self):
        history = StaticDataHistory()
        msg = AISSentence(b"!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23")
        with self.assertRaises(TypeError):
            history.update(msg, ts_epoch=1000.0)

    def test_records_may_never_expire(self):
        history = StaticDataHistory(ttl_in_seconds=None)
        history.update(part_a(), ts_epoch=1000.0)

        snapshot = history.query(MMSI, at_epoch=10 ** 12)
        self.assertEqual(snapshot.a_state, StaticHalfState.VALID)
        self.assertEqual(snapshot.shipname, 'TESTSHIP')

    def test_reimport_of_same_observation_replaces_record(self):
        history = StaticDataHistory()
        history.update(part_b(to_bow=10), ts_epoch=1000.0)
        history.update(part_b(to_bow=42), ts_epoch=1000.0)

        snapshot = history.query(MMSI, at_epoch=1000.0)
        self.assertEqual(snapshot.to_bow, 42)
        self.assertEqual(len(history._part_b[MMSI]), 1)


if __name__ == '__main__':
    unittest.main()
