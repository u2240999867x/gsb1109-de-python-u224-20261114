"""Tests for physical size (millimetres) computation from Exif tags."""

import io
import struct
from pathlib import Path
from typing import Any, List, Tuple

import pytest

import exifread
from exifread import get_physical_dimensions
from exifread.dimensions import (
    MM_PER_CENTIMETRE,
    MM_PER_INCH,
    PhysicalDimensions,
)

RESOURCES_ROOT = Path(__file__).parent / "resources"

# TIFF tag numbers
IMAGE_WIDTH = 0x0100
IMAGE_LENGTH = 0x0101
ORIENTATION = 0x0112
X_RESOLUTION = 0x011A
Y_RESOLUTION = 0x011B
RESOLUTION_UNIT = 0x0128

SHORT = 3
LONG = 4
RATIO = 5
TYPE_LENGTH = {SHORT: 2, LONG: 4, RATIO: 8}

# ResolutionUnit values
UNIT_INCH = 2
UNIT_CENTIMETRE = 3
UNIT_NOT_ABSOLUTE = 1

Entry = Tuple[int, int, List[Any]]


def _build_tiff(ifds: List[List[Entry]], big_endian: bool = False) -> bytes:
    """Build a minimal, valid multi-IFD TIFF from raw tag entries."""
    endian = ">" if big_endian else "<"

    cursor = 8
    ifd_offsets = []
    for entries in ifds:
        ifd_offsets.append(cursor)
        cursor += 2 + len(entries) * 12 + 4

    external = bytearray()
    origin = cursor
    encoded_ifds = []
    for entries in ifds:
        fields = []
        for tag, field_type, values in sorted(entries):
            raw = bytearray()
            for value in values:
                if field_type == RATIO:
                    raw += struct.pack(endian + "II", value[0], value[1])
                elif field_type == SHORT:
                    raw += struct.pack(endian + "H", value)
                else:
                    raw += struct.pack(endian + "I", value)
            total = len(values) * TYPE_LENGTH[field_type]
            if total <= 4:
                value_field = bytes(raw) + b"\x00" * (4 - total)
            else:
                value_field = struct.pack(endian + "I", origin + len(external))
                external += raw
            fields.append((tag, field_type, len(values), value_field))
        encoded_ifds.append(fields)

    header = b"MM\x00\x2a" if big_endian else b"II\x2a\x00"
    out = bytearray(header)
    out += struct.pack(endian + "I", ifd_offsets[0])
    for index, fields in enumerate(encoded_ifds):
        out += struct.pack(endian + "H", len(fields))
        for tag, field_type, count, value_field in fields:
            out += struct.pack(endian + "HHI", tag, field_type, count)
            out += value_field
        next_ifd = ifd_offsets[index + 1] if index + 1 < len(ifds) else 0
        out += struct.pack(endian + "I", next_ifd)
    out += external
    return bytes(out)


def _process(data: bytes, **kwargs: Any):
    return exifread.process_file(io.BytesIO(data), **kwargs)


def test_inch_main_and_preview_are_separate_sets():
    """Each IFD is evaluated with its own pixel/resolution pair."""
    main = [
        (IMAGE_WIDTH, LONG, [1000]),
        (IMAGE_LENGTH, LONG, [2000]),
        # 200/2 dpi is a rational that reduces to 100 dpi.
        (X_RESOLUTION, RATIO, [(200, 2)]),
        (Y_RESOLUTION, RATIO, [(100, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [1]),
    ]
    preview = [
        (IMAGE_WIDTH, LONG, [500]),
        (IMAGE_LENGTH, LONG, [250]),
        (X_RESOLUTION, RATIO, [(50, 1)]),
        (Y_RESOLUTION, RATIO, [(25, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [1]),
    ]
    tags = _process(_build_tiff([main, preview]))

    dimensions = get_physical_dimensions(tags)

    assert set(dimensions) == {"Image", "Thumbnail"}
    assert dimensions["Image"].width_mm == pytest.approx(1000 / 100 * 25.4)
    assert dimensions["Image"].height_mm == pytest.approx(2000 / 100 * 25.4)
    # The preview is smaller and must not reuse the main image's resolution.
    assert dimensions["Thumbnail"].width_mm == pytest.approx(500 / 50 * 25.4)
    assert dimensions["Thumbnail"].height_mm == pytest.approx(250 / 25 * 25.4)
    assert dimensions["Image"].is_complete
    assert dimensions["Thumbnail"].is_complete


def test_centimetre_resolution_unit():
    ifd = [
        (IMAGE_WIDTH, LONG, [500]),
        (IMAGE_LENGTH, LONG, [250]),
        (X_RESOLUTION, RATIO, [(50, 1)]),
        (Y_RESOLUTION, RATIO, [(25, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_CENTIMETRE]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.resolution_unit == UNIT_CENTIMETRE
    assert dimensions.width_mm == pytest.approx(500 / 50 * MM_PER_CENTIMETRE)
    assert dimensions.height_mm == pytest.approx(250 / 25 * MM_PER_CENTIMETRE)
    assert dimensions.width_mm == pytest.approx(100.0)
    assert dimensions.height_mm == pytest.approx(100.0)


def test_missing_resolution_unit_defaults_to_inch():
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [150]),
        (X_RESOLUTION, RATIO, [(300, 1)]),
        (Y_RESOLUTION, RATIO, [(300, 1)]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.resolution_unit is None
    assert dimensions.width_mm == pytest.approx(float(MM_PER_INCH))
    assert dimensions.height_mm == pytest.approx(float(MM_PER_INCH) / 2)


def test_not_absolute_unit_yields_no_dimensions():
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [150]),
        (X_RESOLUTION, RATIO, [(300, 1)]),
        (Y_RESOLUTION, RATIO, [(300, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_NOT_ABSOLUTE]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.width_mm is None
    assert dimensions.height_mm is None
    assert not dimensions.is_complete


def test_missing_y_resolution_keeps_only_computable_axis():
    """A missing Y must never be filled from another IFD's YResolution."""
    main = [
        (IMAGE_WIDTH, LONG, [600]),
        (IMAGE_LENGTH, LONG, [300]),
        (X_RESOLUTION, RATIO, [(200, 1)]),
        # No YResolution on purpose.
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    preview = [
        (IMAGE_WIDTH, LONG, [100]),
        (IMAGE_LENGTH, LONG, [100]),
        (X_RESOLUTION, RATIO, [(100, 1)]),
        (Y_RESOLUTION, RATIO, [(100, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([main, preview])))

    main_dims = dimensions["Image"]
    assert main_dims.width_mm == pytest.approx(600 / 200 * 25.4)
    assert main_dims.height_mm is None
    assert not main_dims.is_complete
    # The preview still computes both axes on its own.
    assert dimensions["Thumbnail"].height_mm == pytest.approx(25.4)


def test_missing_x_resolution_keeps_only_height():
    ifd = [
        (IMAGE_WIDTH, LONG, [600]),
        (IMAGE_LENGTH, LONG, [300]),
        (Y_RESOLUTION, RATIO, [(200, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.width_mm is None
    assert dimensions.height_mm == pytest.approx(300 / 200 * 25.4)


def test_no_resolution_at_all():
    ifd = [
        (IMAGE_WIDTH, LONG, [600]),
        (IMAGE_LENGTH, LONG, [300]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.width_mm is None
    assert dimensions.height_mm is None


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_swap_orientation_delivers_in_display_order(orientation):
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [100]),
        (X_RESOLUTION, RATIO, [(100, 1)]),
        (Y_RESOLUTION, RATIO, [(50, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [orientation]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    stored_width = 300 / 100 * 25.4
    stored_height = 100 / 50 * 25.4
    assert dimensions.orientation == orientation
    assert dimensions.width_mm == pytest.approx(stored_height)
    assert dimensions.height_mm == pytest.approx(stored_width)


@pytest.mark.parametrize("orientation", [1, 2, 3, 4])
def test_non_swap_orientation_keeps_axis_order(orientation):
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [100]),
        (X_RESOLUTION, RATIO, [(100, 1)]),
        (Y_RESOLUTION, RATIO, [(50, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [orientation]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.width_mm == pytest.approx(300 / 100 * 25.4)
    assert dimensions.height_mm == pytest.approx(100 / 50 * 25.4)


def test_swap_is_skipped_when_one_axis_is_missing():
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [100]),
        (X_RESOLUTION, RATIO, [(100, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [6]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]

    assert dimensions.width_mm == pytest.approx(300 / 100 * 25.4)
    assert dimensions.height_mm is None


def test_rational_conversion_stays_within_tolerance():
    # 100 px at 300 dpi -> 25.4/3 mm, a non-terminating decimal.
    ifd = [
        (IMAGE_WIDTH, LONG, [100]),
        (IMAGE_LENGTH, LONG, [100]),
        (X_RESOLUTION, RATIO, [(300, 1)]),
        (Y_RESOLUTION, RATIO, [(300, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]
    expected = 100 / 300 * 25.4

    assert dimensions.width_mm == pytest.approx(expected)
    assert abs(dimensions.width_mm - expected) < 1e-9


def test_builtin_types_and_ifd_tag_inputs_agree():
    ifd = [
        (IMAGE_WIDTH, LONG, [1000]),
        (IMAGE_LENGTH, LONG, [2000]),
        (X_RESOLUTION, RATIO, [(200, 2)]),
        (Y_RESOLUTION, RATIO, [(100, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
        (ORIENTATION, SHORT, [1]),
    ]
    data = _build_tiff([ifd])
    from_tags = get_physical_dimensions(_process(data))["Image"]
    from_builtin = get_physical_dimensions(
        _process(data, builtin_types=True)
    )["Image"]

    assert from_tags == from_builtin
    assert from_builtin.resolution_unit == UNIT_INCH
    assert from_builtin.orientation == 1


def test_big_endian_tiff():
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (IMAGE_LENGTH, LONG, [150]),
        (X_RESOLUTION, RATIO, [(300, 1)]),
        (Y_RESOLUTION, RATIO, [(300, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    dimensions = get_physical_dimensions(
        _process(_build_tiff([ifd], big_endian=True))
    )["Image"]

    assert dimensions.width_mm == pytest.approx(25.4)
    assert dimensions.height_mm == pytest.approx(12.7)


def test_real_tiff_resource():
    file_path = RESOURCES_ROOT / "tiff/Crémieux11.tiff"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh)
    dimensions = get_physical_dimensions(tags)["Image"]

    assert dimensions.width_mm == pytest.approx(199 / 72 * 25.4)
    assert dimensions.height_mm == pytest.approx(47 / 72 * 25.4)
    assert dimensions.orientation == 1


def test_as_dict_is_serializable():
    ifd = [
        (IMAGE_WIDTH, LONG, [300]),
        (X_RESOLUTION, RATIO, [(300, 1)]),
        (RESOLUTION_UNIT, SHORT, [UNIT_INCH]),
    ]
    dimensions = get_physical_dimensions(_process(_build_tiff([ifd])))["Image"]
    payload = dimensions.as_dict()

    assert payload == {
        "ifd_name": "Image",
        "width_mm": pytest.approx(25.4),
        "height_mm": None,
        "orientation": None,
        "resolution_unit": UNIT_INCH,
    }
    assert isinstance(dimensions, PhysicalDimensions)
