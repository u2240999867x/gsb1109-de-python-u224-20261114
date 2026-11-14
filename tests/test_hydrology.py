import datetime
import unittest

from pyais.hydrology import (
    HydrologyLongTable,
    SubReportType,
    TailSegment,
    TemperatureRow,
    build_long_table,
)

SLOT_LENGTH = 40
TS = "20260126080000"
TS_LATER = "20260126090000"


def make_slot(text: str, length: int = SLOT_LENGTH) -> bytes:
    if len(text) > length:
        raise ValueError("slot text exceeds fixed length")
    return text.encode("ascii").ljust(length, b" ")


def make_temperature(station: str, timestamp: str, layer: int, value: float) -> bytes:
    return make_slot(f"T|{station}|{timestamp}|{layer}|{value}")


def make_description(station: str, timestamp: str, name: str) -> bytes:
    return make_slot(f"D|{station}|{timestamp}|{name}")


class HydrologyLongTableTestCase(unittest.TestCase):

    def test_mixed_sensor_reports_in_one_payload(self):
        # HAVING one payload with a description and two temperature reports
        payload = (
            make_description("S01", TS, "North Pier")
            + make_temperature("S01", TS, 1, 12.5)
            + make_temperature("S01", TS, 2, 9.8)
        )

        # WHEN feeding the payload
        table = HydrologyLongTable()
        new_rows = table.feed_payload(payload, SLOT_LENGTH)

        # THEN both temperatures become rows, the description does not
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(len(new_rows), 2)
        self.assertTrue(all(row.report_type == SubReportType.TEMPERATURE for row in table.rows))

    def test_two_layers_at_same_station_never_overwrite(self):
        # HAVING two temperature layers of the same station at the same time
        payload = (
            make_temperature("S01", TS, 1, 12.5)
            + make_temperature("S01", TS, 2, 9.8)
            + make_temperature("S01", TS, 1, 99.9)
        )

        # WHEN building the long table
        table = build_long_table([payload], SLOT_LENGTH)

        # THEN both layers coexist and the repeated layer keeps its first value
        rows = table.rows
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.layer for row in rows}, {1, 2})
        layer_one = next(row for row in rows if row.layer == 1)
        self.assertEqual(layer_one.value, 12.5)

    def test_description_never_becomes_a_temperature_row(self):
        payload = make_description("S01", TS, "North Pier")
        table = build_long_table([payload], SLOT_LENGTH)

        self.assertEqual(table.rows, ())
        self.assertEqual(table.station_name("S01"), "North Pier")

    def test_long_table_keyed_by_type_station_time_and_layer(self):
        observed_at = datetime.datetime.strptime(TS, "%Y%m%d%H%M%S")
        payload = (
            make_temperature("S01", TS, 1, 12.5)
            + make_temperature("S02", TS, 1, 13.5)
            + make_temperature("S01", TS_LATER, 1, 11.0)
        )

        table = build_long_table([payload], SLOT_LENGTH)

        self.assertEqual(len(table.rows), 3)
        first, second, third = table.rows
        self.assertEqual(
            (first.report_type, first.station_id, first.observed_at, first.layer),
            (SubReportType.TEMPERATURE, "S01", observed_at, 1),
        )
        self.assertEqual(second.station_id, "S02")
        self.assertEqual(
            third.observed_at, datetime.datetime.strptime(TS_LATER, "%Y%m%d%H%M%S")
        )

    def test_rows_keep_arrival_order(self):
        payload = (
            make_temperature("S02", TS, 1, 1.0)
            + make_temperature("S01", TS, 1, 2.0)
        )
        table = build_long_table([payload], SLOT_LENGTH)

        self.assertEqual([row.station_id for row in table.rows], ["S02", "S01"])

    def test_name_comes_from_valid_description(self):
        payload = (
            make_description("S01", TS, "North Pier")
            + make_temperature("S01", TS, 1, 12.5)
        )
        table = build_long_table([payload], SLOT_LENGTH)

        self.assertEqual(table.rows[0].station_name, "North Pier")

    def test_temperature_before_description_has_no_name_and_is_not_rewritten(self):
        # HAVING a temperature before any description exists
        payload = make_temperature("S01", TS, 1, 12.5)
        table = HydrologyLongTable()
        table.feed_payload(payload, SLOT_LENGTH)
        self.assertIsNone(table.rows[0].station_name)

        # WHEN a description arrives in a later payload
        table.feed_payload(
            make_description("S01", TS_LATER, "North Pier")
            + make_temperature("S01", TS_LATER, 1, 11.0),
            SLOT_LENGTH,
        )

        # THEN the old sample is not rewritten while the new one gets the name
        old_row, new_row = table.rows
        self.assertIsNone(old_row.station_name)
        self.assertEqual(new_row.station_name, "North Pier")

    def test_later_rename_never_overwrites_old_samples(self):
        first_payload = (
            make_description("S01", TS, "Old Name")
            + make_temperature("S01", TS, 1, 12.5)
        )
        second_payload = (
            make_description("S01", TS_LATER, "New Name")
            + make_temperature("S01", TS_LATER, 1, 11.0)
        )

        table = HydrologyLongTable()
        table.feed_payload(first_payload, SLOT_LENGTH)
        table.feed_payload(second_payload, SLOT_LENGTH)

        self.assertEqual(table.station_name("S01"), "Old Name")
        self.assertTrue(all(row.station_name == "Old Name" for row in table.rows))

    def test_invalid_empty_description_is_ignored(self):
        payload = (
            make_slot("D|S01|" + TS + "|   ")
            + make_temperature("S01", TS, 1, 12.5)
        )
        table = build_long_table([payload], SLOT_LENGTH)

        self.assertIsNone(table.station_name("S01"))
        self.assertIsNone(table.rows[0].station_name)

        # A later valid description still applies.
        table.feed_payload(make_description("S01", TS_LATER, "North Pier"), SLOT_LENGTH)
        self.assertEqual(table.station_name("S01"), "North Pier")

    def test_nul_padded_slots_are_decoded(self):
        slot = b"T|S01|" + TS.encode() + b"|1|12.5"
        slot = slot.ljust(SLOT_LENGTH, b"\x00")
        table = build_long_table([slot], SLOT_LENGTH)

        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0].value, 12.5)

    def test_truncated_final_sub_report_leaves_tail_in_place(self):
        complete_one = make_temperature("S01", TS, 1, 12.5)
        complete_two = make_temperature("S01", TS, 2, 9.8)
        partial = b"T|S01|" + TS.encode() + b"|3|"

        # HAVING a payload whose last sub-report is shorter than a slot
        payload = complete_one + complete_two + partial

        # WHEN feeding it
        table = HydrologyLongTable()
        table.feed_payload(payload, SLOT_LENGTH)

        # THEN the complete layers stay queryable ...
        self.assertEqual(len(table.rows), 2)
        self.assertEqual({row.layer for row in table.rows}, {1, 2})

        # ... and the tail keeps its original in-place byte range
        self.assertEqual(
            table.tails,
            (TailSegment(payload_index=0, start=2 * SLOT_LENGTH,
                         end=2 * SLOT_LENGTH + len(partial), raw=partial),),
        )

    def test_tails_span_multiple_payloads_with_ranges(self):
        first_payload = make_temperature("S01", TS, 1, 12.5) + b"T|S01|"
        second_payload = make_description("S02", TS, "South Buoy") + b"D|S02|"

        table = HydrologyLongTable()
        table.feed_payload(first_payload, SLOT_LENGTH)
        table.feed_payload(second_payload, SLOT_LENGTH)

        self.assertEqual(len(table.rows), 1)
        self.assertEqual(len(table.tails), 2)
        first_tail, second_tail = table.tails
        self.assertEqual(first_tail.payload_index, 0)
        self.assertEqual((first_tail.start, first_tail.end), (SLOT_LENGTH, len(first_payload)))
        self.assertEqual(second_tail.payload_index, 1)
        self.assertEqual((second_tail.start, second_tail.end), (SLOT_LENGTH, len(second_payload)))
        self.assertEqual(first_tail.raw, b"T|S01|")
        self.assertEqual(second_tail.raw, b"D|S02|")

    def test_feed_payload_returns_only_new_rows(self):
        table = HydrologyLongTable()
        first = table.feed_payload(make_temperature("S01", TS, 1, 12.5), SLOT_LENGTH)
        second = table.feed_payload(make_description("S01", TS, "North Pier"), SLOT_LENGTH)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())
        self.assertIsInstance(first[0], TemperatureRow)

    def test_add_sub_report_accepts_decoded_mappings(self):
        table = HydrologyLongTable()
        table.add_sub_report({
            "type": SubReportType.STATION_DESCRIPTION,
            "station_id": "S01",
            "observed_at": datetime.datetime.strptime(TS, "%Y%m%d%H%M%S"),
            "name": "North Pier",
        })
        row = table.add_sub_report({
            "type": "temperature",
            "station_id": "S01",
            "observed_at": TS,
            "layer": 1,
            "value": 12.5,
        })

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.station_name, "North Pier")

    def test_malformed_slot_raises(self):
        table = HydrologyLongTable()
        with self.assertRaises(ValueError):
            table.feed_payload(make_slot("X|S01|garbage"), SLOT_LENGTH)

    def test_invalid_slot_length_raises(self):
        table = HydrologyLongTable()
        with self.assertRaises(ValueError):
            table.feed_payload(make_temperature("S01", TS, 1, 12.5), 0)


if __name__ == "__main__":
    unittest.main()
