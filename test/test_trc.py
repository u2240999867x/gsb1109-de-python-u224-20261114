"""Tests for declaration-driven TRC reading and offline record extraction."""

from __future__ import annotations

from io import StringIO

import pytest

import can
from can.io.trc import TRCRecord, TRCSegmentError

HEADER_2_1 = """\
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS={columns}
"""


def read(text: str) -> tuple[can.TRCReader, list[TRCRecord]]:
    reader = can.TRCReader(StringIO(text))
    records = list(reader.iter_records())
    return reader, records


def v2_file(columns: str, *rows: str) -> str:
    return HEADER_2_1.format(columns=columns) + "\n".join(rows) + "\n"


CANONICAL_ROWS = (
    "      1     17535.400 DT 1  00000100 Tx -  8    00 00 00 00 00 00 00 00",
    "      2     17540.300 DT 1  00000100 Rx -  4    00 00 00 08",
    "      3     48937.600 RR 1      0704 Rx -  1",
)

DIRECTION_LAST_ROWS = (
    "      1     17535.400 DT 1  00000100 -  8    00 00 00 00 00 00 00 00 Tx",
    "      2     17540.300 DT 1  00000100 -  4    00 00 00 08 Rx",
    "      3     48937.600 RR 1      0704 -  1 Rx",
)


class TestDeclarationOrder:
    def test_direction_column_at_end_matches_canonical(self):
        canonical, expected = read(v2_file("N,O,T,B,I,d,R,L,D", *CANONICAL_ROWS))
        moved, actual = read(v2_file("N,O,T,B,I,R,L,D,d", *DIRECTION_LAST_ROWS))

        assert not canonical.segment_errors
        assert not moved.segment_errors
        assert len(expected) == len(actual) == 3

        for canonical_record, moved_record in zip(expected, actual):
            assert canonical_record.message.equals(
                moved_record.message, check_channel=True
            )
            assert canonical_record.direction == moved_record.direction
            assert canonical_record.declared_length == moved_record.declared_length
            assert canonical_record.data_bytes == moved_record.data_bytes
            assert canonical_record.line_number == moved_record.line_number

    def test_fd_declared_length_and_actual_bytes(self):
        rows = (
            "      1     100.000 FD 1  00000100 Tx - 13    "
            + " ".join(f"{byte:02X}" for byte in range(13)),
        )
        reader, records = read(v2_file("N,O,T,B,I,d,R,l,D", *rows))

        assert not reader.segment_errors
        assert len(records) == 1
        record = records[0]
        assert record.direction == "Tx"
        assert record.declared_length == 13
        assert record.data_bytes == bytes(range(13))
        assert record.message.dlc == can.util.len2dlc(13)
        assert record.message.is_fd

    def test_direction_column_first(self):
        row = "Tx 1     100.000 DT 1  00000100 -  8    00 00 00 00 00 00 00 00"
        reader, records = read(v2_file("d,N,O,T,B,I,R,L,D", row))

        assert not reader.segment_errors
        assert len(records) == 1
        assert records[0].direction == "Tx"
        assert not records[0].message.is_rx
        assert records[0].declared_length == 8
        assert records[0].data_bytes == bytes(8)

    def test_direction_value_physically_moved_without_declaration_change(self):
        rows = (
            "      1     100.000 DT 1  00000100 -  2    AA BB Tx",
            "      2     101.000 DT 1  00000101 -  2    CC DD Rx",
        )
        reader, records = read(v2_file("N,O,T,B,I,d,R,L,D", *rows))

        assert not reader.segment_errors
        assert [record.direction for record in records] == ["Tx", "Rx"]
        assert [record.data_bytes for record in records] == [b"\xaa\xbb", b"\xcc\xdd"]
        assert [record.message.is_rx for record in records] == [False, True]

    def test_data_column_may_not_be_last_width_by_offset(self):
        # Without a variable-width data field the direction token would be
        # swallowed as a data byte; it must remain a separate declared field.
        row = "      1     100.000 DT 1  00000100 Tx -  2    AA BB"
        reader, records = read(v2_file("N,O,T,B,I,d,R,L,D", row))

        assert not reader.segment_errors
        assert records[0].data_bytes == b"\xaa\xbb"
        assert records[0].declared_length == 2

    def test_remote_frame_with_direction_after_data(self):
        rows = (
            "      1     100.000 RR 1      0704 -  1 Rx",
            "      2     101.000 RR 1      0705 -  0 Tx",
        )
        reader, records = read(v2_file("N,O,T,B,I,R,L,D,d", *rows))

        assert not reader.segment_errors
        assert [record.direction for record in records] == ["Rx", "Tx"]
        assert [record.data_bytes for record in records] == [b"", b""]
        assert [record.message.is_remote_frame for record in records] == [True, True]

    def test_short_data_run_keeps_declared_and_actual_lengths(self):
        reader, records = read(
            v2_file(
                "N,O,T,I,d,L,D",
                "      1     100.000 DT 00000100 Tx 3  AA BB",
                "      2     101.000 DT 00000101 Rx 1  CC",
            )
        )

        assert not reader.segment_errors
        assert [record.declared_length for record in records] == [3, 1]
        assert [record.data_bytes for record in records] == [b"\xaa\xbb", b"\xcc"]
        assert [record.data_length for record in records] == [2, 1]

    @pytest.mark.parametrize(
        "version,columns",
        [("2.0", "N,O,T,I,d,L,D"), ("2.1", "N,O,T,B,I,d,l,D")],
    )
    def test_reordered_versions(self, version, columns):
        header = f";$FILEVERSION={version}\n;$STARTTIME=44548.6028595139\n"
        rows = "      1     100.000 DT 00000100 Tx 8  00 00 00 00 00 00 00 00"
        if version == "2.1":
            rows = "      1     100.000 DT 1  00000100 -  8    00 00 00 00 00 00 00 00 Tx"
            columns = "N,O,T,B,I,R,l,D,d"
        text = header + f";$COLUMNS={columns}\n" + rows + "\n"
        reader, records = read(text)

        assert not reader.segment_errors
        assert records[0].direction == "Tx"
        assert records[0].declared_length == 8
        assert records[0].data_bytes == bytes(8)


class TestShardIsolation:
    def test_reordered_then_plain_shard(self):
        text = (
            v2_file(
                "N,O,T,B,I,R,L,D,d",
                "      1     100.000 DT 1  00000100 -  2    AA BB Tx",
            )
            + """
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,B,I,d,R,L,D
      1     200.000 DT 2  00000101 Tx -  2    CC DD
"""
        )
        reader, records = read(text)

        assert not reader.segment_errors
        assert [record.direction for record in records] == ["Tx", "Tx"]
        assert [record.line_number for record in records] == [4, 9]
        assert records[0].data_bytes == b"\xaa\xbb"
        assert records[1].data_bytes == b"\xcc\xdd"
        assert records[0].message.channel == 1
        assert records[1].message.channel == 2

    def test_shard_without_columns_does_not_borrow_previous(self):
        text = (
            v2_file("N,O,T,B,I,R,L,D,d", "      1     100.0 DT 1  1 -  2  AA BB Tx")
            + """
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
      1     200.0 DT 1  1 Tx -  2  CC DD
"""
        )
        reader, records = read(text)

        assert len(records) == 1
        assert records[0].line_number == 4
        assert len(reader.segment_errors) == 1
        error = reader.segment_errors[0]
        assert error.reason == "missing_column"
        assert error.first_line >= 5
        assert error.last_line >= error.first_line
        assert 6 in error

    def test_version_is_not_borrowed(self):
        text = """\
;$FILEVERSION=1.1
;$STARTTIME=44548.6028595139
     1)     100.0  Tx     00000100  2  AA BB
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,I,d,L,D
      1     200.0 DT 00000101 Rx 2  CC DD
"""
        reader, records = read(text)

        assert not reader.segment_errors
        assert records[0].file_version == can.TRCFileVersion.V1_1
        assert records[1].file_version == can.TRCFileVersion.V2_1
        assert records[1].message.timestamp - records[0].message.timestamp == (
            pytest.approx(0.1, abs=1e-6)
        )
        assert records[0].message.channel == 1
        assert records[0].direction == "Tx"
        assert records[1].direction == "Rx"

    def test_columns_directive_identifies_v2_without_fileversion(self):
        text = """\
;$COLUMNS=N,O,T,I,d,L,D
      1     100.0 DT 00000100 Tx 2  AA BB
"""
        reader, records = read(text)

        assert not reader.segment_errors
        assert len(records) == 1
        assert records[0].file_version == can.TRCFileVersion.V2_0
        assert records[0].direction == "Tx"
        assert records[0].data_bytes == b"\xaa\xbb"

    def test_new_columns_directive_before_records_restarts_header(self):
        text = """\
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,B,I,d,R,L,D
;$COLUMNS=N,O,T,I,d,L,D
      1     100.0 DT 00000100 Tx 2  AA BB
"""
        reader, records = read(text)

        assert not reader.segment_errors
        assert len(records) == 1
        assert records[0].declared_columns == ("N", "O", "T", "I", "d", "L", "D")


class TestSegmentErrors:
    def test_duplicate_column_marks_segment_but_keeps_prior_records(self):
        text = (
            """\
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,I,d,L,D
      1     100.0 DT 00000100 Tx 2  AA BB
"""
            + """\
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,I,d,L,D,d
      1     200.0 DT 00000101 Rx 2  CC DD
"""
        )
        reader, records = read(text)

        assert len(records) == 1
        assert records[0].line_number == 4
        assert len(reader.segment_errors) == 1
        error = reader.segment_errors[0]
        assert isinstance(error, TRCSegmentError)
        assert error.reason == "duplicate_column"
        assert "d" in error.detail
        assert error.first_line <= 5
        assert error.last_line >= 7

    @pytest.mark.parametrize(
        ("columns", "expected"),
        [
            ("N,O,T,B,I,d,R,L", "D"),
            ("N,O,T,B,I,R,L,D", "d"),
            ("N,O,T,I,d,L,D", None),
            ("N,O,I,d,L,D", "T"),
            ("N,O,T,d,L,D", "I"),
            ("N,O,T,I,d,D", "l/L"),
        ],
    )
    def test_missing_key_column(self, columns, expected):
        row = "      1     100.0 DT 00000100 Tx 8  00 00 00 00 00 00 00 00"
        if "B" in columns:
            row = "      1     100.0 DT 1  00000100 Tx 8  00 00 00 00 00 00 00 00"
        text = v2_file(columns, row)
        reader, records = read(text)

        if expected is None:
            assert not reader.segment_errors
            assert records
            return
        assert not records
        assert len(reader.segment_errors) == 1
        error = reader.segment_errors[0]
        assert error.reason == "missing_column"
        assert expected in error.detail

    def test_malformed_runs_are_grouped_without_losing_prior_records(self):
        text = v2_file(
            "N,O,T,I,d,L,D",
            "      1     100.0 DT 00000100 Tx 2  AA BB",
            "      2     101.0 BROKEN",
            "      3     102.0 BROKEN",
            "      4     103.0 DT 00000101 Rx 2  CC DD",
            "      5     104.0 BROKEN",
            "      6     105.0 DT 00000110 Tx 2  EE FF",
        )
        reader, records = read(text)

        assert [record.line_number for record in records] == [4, 7, 9]
        assert [error.lines for error in reader.segment_errors] == [(5, 6), (8, 8)]
        assert all(error.reason == "invalid_record" for error in reader.segment_errors)
        assert records[0].data_bytes == b"\xaa\xbb"
        assert records[2].data_bytes == b"\xee\xff"

    def test_records_before_duplicate_header_are_still_delivered(self):
        text = """\
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,I,d,L,D
      1     100.0 DT 00000100 Tx 2  AA BB
      2     101.0 DT 00000101 Rx 2  CC DD
;$FILEVERSION=2.1
;$STARTTIME=44548.6028595139
;$COLUMNS=N,O,T,I,d,L,D,T
      1     200.0 DT 00000110 Tx 2  EE FF
"""
        reader, records = read(text)

        assert len(records) == 2
        assert reader.segment_errors[0].reason == "duplicate_column"
        assert reader.segment_errors[0].first_line == 6


class TestRecordShape:
    def test_record_fields_are_source_accurate(self):
        row = "      1     100.0 DT 00000100 Tx 2  AA BB"
        reader, records = read(v2_file("N,O,T,I,d,L,D", row))

        record = records[0]
        assert record.line_number == 4
        assert record.line == row.strip()
        assert record.direction == "Tx"
        assert record.declared_length == 2
        assert record.data_bytes == b"\xaa\xbb"
        assert record.as_dict()["data_bytes"] == b"\xaa\xbb"
        assert record["direction"] == "Tx"
        assert record.get("missing", "fallback") == "fallback"
        assert dict(record)["line_number"] == 4
        assert "direction" in record
        assert len(record) == 8
        assert record.actual_bytes == b"\xaa\xbb"
        assert record.actual_length == 2
        assert record.raw_line == row.strip()

    def test_iteration_still_yields_messages(self):
        text = v2_file("N,O,T,I,d,L,D", "      1     100.0 DT 00000100 Tx 2  AA BB")
        reader = can.TRCReader(StringIO(text))
        messages = list(reader)

        assert len(messages) == 1
        assert messages[0].arbitration_id == 0x100
        assert reader.record_count == 1
        assert reader.records[0].direction == "Tx"

    def test_repeated_iteration_does_not_duplicate_records(self):
        text = v2_file("N,O,T,I,d,L,D", "      1     100.0 DT 00000100 Tx 2  AA BB")
        reader = can.TRCReader(StringIO(text))
        list(reader)
        list(reader)

        assert reader.record_count == 1


class TestLegacyDirection:
    @pytest.mark.parametrize(
        ("version", "row", "direction"),
        [
            ("1.1", "     1)     100.0  Tx     00000100  2  AA BB", "Tx"),
            ("1.1", "     1)     100.0  00000100  2  AA BB Rx", "Rx"),
            ("1.1", "     1)     100.0  Tx     00000100  2  AA BB Rx", "Rx"),
            ("1.3", "     1)     100.0 1  Tx    00000100 -  2  AA BB", "Tx"),
            ("1.3", "     1)     100.0 1  00000100 -  2  AA BB Rx", "Rx"),
            ("1.3", "     1)     100.0 1  Tx    00000100 -  2  AA BB Rx", "Rx"),
        ],
    )
    def test_direction_layouts(self, version, row, direction):
        text = f";$FILEVERSION={version}\n;$STARTTIME=1.0\n{row}\n"
        reader, records = read(text)

        assert not reader.segment_errors, reader.segment_errors
        assert records[0].direction == direction
        assert records[0].message.is_rx == (direction == "Rx")
        assert records[0].declared_length == 2
        assert records[0].data_bytes == b"\xaa\xbb"

    def test_1_0_has_no_direction(self):
        text = "     1)     17535  00000100  2  AA BB\n"
        reader, records = read(text)

        assert not reader.segment_errors
        assert records[0].direction is None
        assert records[0].message.is_rx


class TestExports:
    def test_record_types_are_exported(self):
        assert can.io.TRCRecord is TRCRecord
        assert can.io.TRCSegmentError is TRCSegmentError
        assert can.TRCRecord is TRCRecord
        assert can.TRCSegmentError is TRCSegmentError

    def test_message_only_iterator(self):
        text = v2_file("N,O,T,I,d,L,D", "      1     100.0 DT 00000100 Tx 2  AA BB")
        reader = can.TRCReader(StringIO(text))
        messages = list(reader.iter_messages())

        assert len(messages) == 1
        assert messages[0].arbitration_id == 0x100
