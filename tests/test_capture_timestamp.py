"""Tests for the synthesized sortable capture timestamp."""

import io
import struct
from datetime import datetime

import pytest

from exifread import TIMESTAMP_TAG, process_file
from exifread.core.ifd_tag import IfdTag
from exifread.datetime_utils import build_capture_timestamp
from exifread.tags.fields import FieldType


def _ascii_tag(value: str) -> IfdTag:
    return IfdTag(
        printable=value,
        tag=0,
        field_type=FieldType.ASCII,
        values=value,
        field_offset=0,
        field_length=len(value) + 1,
        prefer_printable=False,
    )


def _tags(mapping) -> dict:
    return {key: _ascii_tag(value) for key, value in mapping.items()}


def _build_minimal_tiff(exif_tags: dict) -> bytes:
    """Wrap EXIF IFD ASCII tags in a minimal big-endian TIFF byte stream."""
    ifd0_offset = 8
    ifd0_length = 2 + 12 + 4  # one ExifOffset entry + next-IFD pointer
    exif_ifd_offset = ifd0_offset + ifd0_length
    entries = sorted(exif_tags.items())
    exif_ifd_length = 2 + 12 * len(entries) + 4
    data_offset = exif_ifd_offset + exif_ifd_length

    blob = bytearray()
    blob += b"MM" + struct.pack(">HI", 42, ifd0_offset)

    data_blob = bytearray()
    value_fields = {}
    for tag_id, value in entries:
        raw = value.encode("ascii") + b"\x00"
        if len(raw) <= 4:
            # ASCII values fitting in 4 bytes are stored inline.
            value_fields[tag_id] = raw.ljust(4, b"\x00")
        else:
            value_fields[tag_id] = struct.pack(">I", data_offset + len(data_blob))
            data_blob += raw

    # IFD0 pointing at the EXIF SubIFD
    blob += struct.pack(">H", 1)
    blob += struct.pack(">HHII", 0x8769, 4, 1, exif_ifd_offset)
    blob += struct.pack(">I", 0)

    # EXIF SubIFD
    blob += struct.pack(">H", len(entries))
    for tag_id, value in entries:
        count = len(value.encode("ascii")) + 1
        blob += struct.pack(">HHI", tag_id, 2, count) + value_fields[tag_id]
    blob += struct.pack(">I", 0)

    blob += data_blob
    return bytes(blob)


def _process(tiff: bytes, **kwargs) -> dict:
    return process_file(io.BytesIO(tiff), details=False, **kwargs)


def test_original_family_preferred_over_digitized():
    tags = _tags(
        {
            "EXIF DateTimeOriginal": "1985:03:07 10:20:30",
            "EXIF SubSecTimeOriginal": "123",
            "EXIF OffsetTimeOriginal": "+09:00",
            "EXIF DateTimeDigitized": "2024:06:01 09:00:00",
            "EXIF SubSecTimeDigitized": "9",
            "EXIF OffsetTimeDigitized": "+08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "1985-03-07T10:20:30.123000+09:00"


def test_digitized_family_used_when_original_missing():
    tags = _tags(
        {
            "EXIF DateTimeDigitized": "2024:06:01 09:00:00",
            "EXIF SubSecTimeDigitized": "09",
            "EXIF OffsetTimeDigitized": "+08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "2024-06-01T09:00:00.090000+08:00"


def test_generic_datetime_uses_its_own_companions():
    tags = _tags(
        {
            "Image DateTime": "2024:06:01 09:00:00",
            "EXIF SubSecTime": "5",
            "EXIF OffsetTime": "-05:30",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "2024-06-01T09:00:00.500000-05:30"


def test_companions_never_cross_families():
    # Sub-second and offset from the other families must not leak into Original.
    tags = _tags(
        {
            "EXIF DateTimeOriginal": "1985:03:07 10:20:30",
            "EXIF SubSecTime": "500",
            "EXIF OffsetTimeDigitized": "+08:00",
            "EXIF OffsetTime": "+08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "1985-03-07T10:20:30"
    parsed = datetime.fromisoformat(timestamp.printable)
    assert parsed.tzinfo is None
    assert parsed.microsecond == 0


def test_original_without_offset_stays_naive_even_if_digitized_has_one():
    # The scenario from the film re-scan: the negative says 23:59:59.09 and
    # only the digitizing fields carry +08:00. The shot must stay on the
    # local timeline instead of inheriting the scan's offset.
    tags = _tags(
        {
            "EXIF DateTimeOriginal": "1985:03:07 23:59:59",
            "EXIF SubSecTimeOriginal": "09",
            "EXIF DateTimeDigitized": "2024:06:01 09:00:00",
            "EXIF SubSecTimeDigitized": "00",
            "EXIF OffsetTimeDigitized": "+08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "1985-03-07T23:59:59.090000"

    parsed = datetime.fromisoformat(timestamp.printable)
    assert parsed.tzinfo is None

    # A UTC export only accepts offset-bearing timestamps: this one is
    # skipped, and the reason is visible in the untouched source tags.
    assert "EXIF OffsetTimeOriginal" not in tags
    assert tags["EXIF OffsetTimeDigitized"].printable == "+08:00"


def test_invalid_date_falls_back_to_next_family():
    tags = _tags(
        {
            "EXIF DateTimeOriginal": "0000:00:00 00:00:00",
            "EXIF DateTimeDigitized": "2024:06:01 09:00:00",
            "EXIF OffsetTimeDigitized": "+08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "2024-06-01T09:00:00+08:00"


def test_invalid_companions_are_ignored():
    tags = _tags(
        {
            "EXIF DateTimeOriginal": "2024:06:01 09:00:00",
            "EXIF SubSecTimeOriginal": "12.5",
            "EXIF OffsetTimeOriginal": "08:00",
        }
    )
    timestamp = build_capture_timestamp(tags)
    assert timestamp.printable == "2024-06-01T09:00:00"


def test_no_date_tag_returns_none():
    assert build_capture_timestamp({}) is None


def test_timestamp_strings_are_sortable():
    earlier = build_capture_timestamp(
        _tags({"EXIF DateTimeOriginal": "1985:03:07 23:59:59"})
    ).printable
    later = build_capture_timestamp(
        _tags(
            {
                "EXIF DateTimeOriginal": "1985:03:08 00:00:00",
                "EXIF OffsetTimeOriginal": "+08:00",
            }
        )
    ).printable
    assert earlier < later


@pytest.mark.parametrize("builtin_types", (True, False))
def test_process_file_adds_timestamp_and_keeps_source_tags(builtin_types):
    tiff = _build_minimal_tiff(
        {
            0x9003: "1985:03:07 23:59:59",
            0x9291: "09",
            0x9004: "2024:06:01 09:00:00",
            0x9012: "+08:00",
        }
    )
    tags = _process(tiff, builtin_types=builtin_types)

    # The original labels are preserved alongside the synthesized column.
    assert tags["EXIF DateTimeOriginal"]
    assert tags["EXIF SubSecTimeOriginal"]
    assert tags["EXIF DateTimeDigitized"]
    assert tags["EXIF OffsetTimeDigitized"]

    timestamp = tags[TIMESTAMP_TAG]
    if builtin_types:
        assert isinstance(timestamp, str)
    else:
        timestamp = timestamp.printable
    assert timestamp == "1985-03-07T23:59:59.090000"
    assert datetime.fromisoformat(timestamp).tzinfo is None


def test_process_file_aware_timestamp_end_to_end():
    tiff = _build_minimal_tiff(
        {
            0x9003: "1985:03:07 23:59:59",
            0x9291: "09",
            0x9011: "+08:00",
        }
    )
    tags = _process(tiff)
    assert tags[TIMESTAMP_TAG].printable == "1985-03-07T23:59:59.090000+08:00"


def test_process_file_without_dates_has_no_timestamp():
    tiff = _build_minimal_tiff({0x010F: "Acme"})
    tags = _process(tiff)
    assert TIMESTAMP_TAG not in tags
