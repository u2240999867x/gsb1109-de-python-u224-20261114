"""Basic tests."""

import logging
import struct
from io import BytesIO
from pathlib import Path

import pytest

import exifread
from exifread import DEFAULT_STOP_TAG
from exifread.tags.fields import FieldType

RESOURCES_ROOT = Path(__file__).parent / "resources"

TYPE_SIZES = {FieldType.SHORT: 2, FieldType.LONG: 4}
TYPE_FMTS = {FieldType.SHORT: "H", FieldType.LONG: "I"}


def _ifd_entry(tag: int, field_type: int, count: int, value: bytes) -> bytes:
    assert len(value) == 4
    return struct.pack("<HHI", tag, field_type, count) + value


def _build_tiff_with_thumbnail(
    strip_offsets_type: FieldType,
    strips: list,
    disk_order=None,
    byte_counts=None,
    pixels_first=False,
):
    """
    Build a minimal little-endian TIFF holding a 640x480 main image and an
    uncompressed 8-bits grayscale thumbnail (8 pixels wide).

    ``strips`` holds the thumbnail pixel strips in logical (image) order;
    ``disk_order`` is a permutation of the strip indexes controlling the
    order in which the strips are laid out on disk. With ``pixels_first``
    the strips are stored right after the TIFF header, before the IFDs.
    """
    n_strips = len(strips)
    if disk_order is None:
        disk_order = list(range(n_strips))
    if byte_counts is None:
        byte_counts = [len(strip) for strip in strips]

    thumb_width = 8
    rows_per_strip = len(strips[0]) // thumb_width
    thumb_height = rows_per_strip * n_strips

    if pixels_first:
        ifd0_offset = 8 + sum(len(strip) for strip in strips)
    else:
        ifd0_offset = 8
    make_offset = ifd0_offset + 2 + 3 * 12 + 4
    ifd1_offset = make_offset + 6
    data_ptr = ifd1_offset + 2 + 9 * 12 + 4

    type_size = TYPE_SIZES[strip_offsets_type]
    offsets_ptr = counts_ptr = 0
    if n_strips * type_size > 4:
        offsets_ptr = data_ptr
        data_ptr += n_strips * type_size
    if n_strips * 4 > 4:
        counts_ptr = data_ptr
        data_ptr += n_strips * 4

    pixel_base = 8 if pixels_first else data_ptr
    strip_positions = {}
    pos = pixel_base
    for idx in disk_order:
        strip_positions[idx] = pos
        pos += len(strips[idx])
    strip_offsets = [strip_positions[idx] for idx in range(n_strips)]

    if n_strips * type_size > 4:
        offsets_value = struct.pack("<I", offsets_ptr)
    else:
        offsets_value = struct.pack(
            "<" + TYPE_FMTS[strip_offsets_type] * n_strips, *strip_offsets
        ).ljust(4, b"\x00")
    if n_strips * 4 > 4:
        counts_value = struct.pack("<I", counts_ptr)
    else:
        counts_value = struct.pack("<I", byte_counts[0])

    ifd0 = struct.pack("<H", 3)
    ifd0 += _ifd_entry(0x0100, FieldType.LONG, 1, struct.pack("<I", 640))
    ifd0 += _ifd_entry(0x0101, FieldType.LONG, 1, struct.pack("<I", 480))
    ifd0 += _ifd_entry(0x010F, FieldType.ASCII, 5, struct.pack("<I", make_offset))
    ifd0 += struct.pack("<I", ifd1_offset)

    ifd1 = struct.pack("<H", 9)
    ifd1 += _ifd_entry(0x0100, FieldType.LONG, 1, struct.pack("<I", thumb_width))
    ifd1 += _ifd_entry(0x0101, FieldType.LONG, 1, struct.pack("<I", thumb_height))
    ifd1 += _ifd_entry(0x0102, FieldType.SHORT, 1, struct.pack("<I", 8))
    ifd1 += _ifd_entry(0x0103, FieldType.SHORT, 1, struct.pack("<I", 1))
    ifd1 += _ifd_entry(0x0106, FieldType.SHORT, 1, struct.pack("<I", 1))
    ifd1 += _ifd_entry(0x0111, strip_offsets_type, n_strips, offsets_value)
    ifd1 += _ifd_entry(0x0115, FieldType.SHORT, 1, struct.pack("<I", 1))
    ifd1 += _ifd_entry(0x0116, FieldType.LONG, 1, struct.pack("<I", rows_per_strip))
    ifd1 += _ifd_entry(0x0117, FieldType.LONG, n_strips, counts_value)
    ifd1 += struct.pack("<I", 0)

    data = b"II*\x00" + struct.pack("<I", ifd0_offset)
    if pixels_first:
        for idx in disk_order:
            data += strips[idx]
    data += ifd0
    data += b"ACME\x00\x00"
    data += ifd1
    if offsets_ptr:
        data += struct.pack(
            "<" + TYPE_FMTS[strip_offsets_type] * n_strips, *strip_offsets
        )
    if counts_ptr:
        data += struct.pack("<%dI" % n_strips, *byte_counts)
    if not pixels_first:
        for idx in disk_order:
            data += strips[idx]
    return data, strip_offsets, byte_counts


def _thumbnail_pixels(thumb: bytes, thumb_tags: dict) -> bytes:
    offsets = thumb_tags["Image StripOffsets"].values
    counts = thumb_tags["Image StripByteCounts"].values
    return b"".join(thumb[off : off + count] for off, count in zip(offsets, counts))


def test_corrupted_exception():
    file_path = RESOURCES_ROOT / "jpg/corrupted.jpg"
    with open(file_path, "rb") as fh:
        with pytest.raises(ValueError) as err:
            exifread.process_file(fh=fh, strict=True)
        assert "tag 0x089C" in str(err.value)


def test_corrupted_pass():
    file_path = RESOURCES_ROOT / "jpg/corrupted.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, strict=False)
    assert "EXIF Contrast" in tags
    assert len(tags) == 69


@pytest.mark.parametrize("builtin_types", (True, False))
@pytest.mark.parametrize(
    "stop_tag, tag_count", (("ColorSpace", 39), (DEFAULT_STOP_TAG, 51))
)
def test_stop_at_tag(builtin_types, stop_tag, tag_count):
    file_path = RESOURCES_ROOT / "jpg/Canon_40D.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(
            fh=fh, stop_tag=stop_tag, builtin_types=builtin_types
        )
    assert len(tags) == tag_count


@pytest.mark.parametrize("extract_thumbnail", (True, False))
def test_makernote_extract(extract_thumbnail):
    file_path = RESOURCES_ROOT / "jpg/Canon_DIGITAL_IXUS_400.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(
            fh=fh, extract_thumbnail=extract_thumbnail, details=True
        )
    assert "MakerNote AESetting" in tags


@pytest.mark.parametrize("extract_thumbnail", (True, False))
def test_no_makernote_extract(extract_thumbnail):
    file_path = RESOURCES_ROOT / "jpg/Canon_DIGITAL_IXUS_400.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(
            fh=fh, extract_thumbnail=extract_thumbnail, details=False
        )
    assert "MakerNote AESetting" not in tags


@pytest.mark.parametrize("details", (True, False))
def test_thumbnail_extract(details):
    file_path = RESOURCES_ROOT / "jpg/Canon_40D.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, extract_thumbnail=True, details=details)
    assert len(tags["JPEGThumbnail"]) == 1378


@pytest.mark.parametrize("details", (True, False))
def test_no_thumbnail_extract(details):
    file_path = RESOURCES_ROOT / "jpg/Canon_40D.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, extract_thumbnail=False, details=details)
    assert "JPEGThumbnail" not in tags


@pytest.mark.parametrize("strict", (True, False))
def test_no_exif(strict):
    file_path = RESOURCES_ROOT / "jpg/xmp/no_exif.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=True, strict=strict)
    assert not tags


@pytest.mark.parametrize("strict", (True, False))
def test_invalid_exif(strict):
    file_path = RESOURCES_ROOT / "jpg/invalid/image00971.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=True, strict=strict)
    assert not tags


@pytest.mark.parametrize(
    "file_path, message",
    (
        ("jpg/tests/30-type_error.jpg", "corrupted IFD: EXIF"),
        ("jpg/tests/35-empty.jpg", "corrupted field RecordingMode"),
        ("jpg/tests/45-gps_ifd.jpg", "No values found for GPS SubIFD"),
    ),
)
def test_warning_messages(caplog, file_path, message):
    """
    We already capture this in the dump file.
    Need to make sure it's the logger capturing this rather than a print() statement or equivalent.
    """
    caplog.set_level(logging.WARNING)
    with open(RESOURCES_ROOT / file_path, "rb") as fh:
        exifread.process_file(fh=fh, details=True)
    assert message in caplog.text


def test_stop_tag_with_thumbnail_extract():
    """
    Stop at `Orientation` tag and extract thumbnail.
    Fails since the `Orientation` tag comes before the `JPEGInterchangeFormatLength` tag.
    Should not raise an Exception.
    """
    file_path = RESOURCES_ROOT / "jpg/tests/Xiaomi_Mi_9T_KeyError.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=False, stop_tag="Orientation")
    assert tags


@pytest.mark.parametrize("details", (True, False))
@pytest.mark.parametrize("truncate_tags", (True, False))
@pytest.mark.parametrize("stop_tag", ("WhiteBalance", DEFAULT_STOP_TAG))
def test_builtin_types(stop_tag, details, truncate_tags):
    """
    When ``builtin_types=True``, always convert to Python types.
    Test with various other options to make sure they don't interfere.
    The "WhiteBalance" tag is after al the tags tested so must not have an impact.
    """
    file_path = RESOURCES_ROOT / "jpg/Canon_DIGITAL_IXUS_400.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(
            fh=fh,
            builtin_types=True,
            stop_tag=stop_tag,
            details=details,
            truncate_tags=truncate_tags,
        )
    # Short mapped to string value
    assert tags["EXIF ColorSpace"] == "sRGB"
    # Short
    assert isinstance(tags["EXIF ExifImageLength"], int)
    assert tags["EXIF ExifImageLength"] == 75
    # Ratio
    assert isinstance(tags["EXIF ExposureTime"], float)
    assert tags["EXIF ExposureTime"] == 0.005
    # ASCII
    assert tags["Image Make"] == "Canon"
    # Unknown / Undefined
    assert tags["EXIF FlashPixVersion"] == "0100"


def test_xmp_no_tag():
    """Read XMP data not in an Exif tag."""

    file_path = RESOURCES_ROOT / "tiff/Arbitro.tiff"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(
            fh=fh,
            builtin_types=True,
        )
    assert len(tags["Image ApplicationNotes"]) == 323


def test_tiff_thumbnail_extract_out_of_order_strips():
    """
    Extract an uncompressed TIFF thumbnail whose strips are stored out of
    order on disk, with the strip offsets array outside the IFD entries.
    """
    strip0 = bytes(range(16))
    strip1 = bytes(range(16, 32))
    data, strip_offsets, _ = _build_tiff_with_thumbnail(
        strip_offsets_type=FieldType.LONG,
        strips=[strip0, strip1],
        disk_order=[1, 0],
    )
    assert strip_offsets[0] > strip_offsets[1]  # disk order != logical order

    tags = exifread.process_file(fh=BytesIO(data))

    # byte range of each strip in the original file, in logical order
    assert tags["TIFFThumbnailStripRanges"].values == [
        (strip_offsets[0], strip_offsets[0] + 16),
        (strip_offsets[1], strip_offsets[1] + 16),
    ]

    # the extracted preview is re-readable as a standalone TIFF
    thumb = tags["TIFFThumbnail"]
    thumb_tags = exifread.process_file(fh=BytesIO(thumb))
    assert thumb_tags["Image ImageWidth"].values[0] == 8
    assert thumb_tags["Image ImageLength"].values[0] == 4
    # pixels are in logical order, not disk order
    assert _thumbnail_pixels(thumb, thumb_tags) == strip0 + strip1


def test_tiff_thumbnail_extract_inline_strip_offsets():
    """Strip offsets array inlined in the directory entry (SHORT, count 1)."""
    strip = bytes(range(32))
    data, strip_offsets, _ = _build_tiff_with_thumbnail(
        strip_offsets_type=FieldType.SHORT,
        strips=[strip],
    )

    tags = exifread.process_file(fh=BytesIO(data))

    assert tags["TIFFThumbnailStripRanges"].values == [
        (strip_offsets[0], strip_offsets[0] + 32)
    ]
    thumb = tags["TIFFThumbnail"]
    thumb_tags = exifread.process_file(fh=BytesIO(thumb))
    assert thumb_tags["Image ImageWidth"].values[0] == 8
    assert thumb_tags["Image ImageLength"].values[0] == 4
    assert _thumbnail_pixels(thumb, thumb_tags) == strip


def test_tiff_thumbnail_extract_short_offsets_overflow():
    """
    New strip offsets no longer fit in the original SHORT field type:
    the offsets array must be relocated and stored as LONGs.
    """
    # All source offsets fit in a SHORT, but the rebuilt TIFF lays the
    # strips out after its IFD, pushing the second offset past 0xFFFF.
    strip0 = b"\x01" * 65500
    strip1 = b"\x02" * 27
    data, _, _ = _build_tiff_with_thumbnail(
        strip_offsets_type=FieldType.SHORT,
        strips=[strip0, strip1],
        pixels_first=True,
    )

    tags = exifread.process_file(fh=BytesIO(data))
    thumb = tags["TIFFThumbnail"]

    thumb_tags = exifread.process_file(fh=BytesIO(thumb))
    offsets = thumb_tags["Image StripOffsets"].values
    counts = thumb_tags["Image StripByteCounts"].values
    assert offsets[1] > 0xFFFF  # cannot be stored in a SHORT
    assert thumb[offsets[0] : offsets[0] + counts[0]] == strip0
    assert thumb[offsets[1] : offsets[1] + counts[1]] == strip1


def test_tiff_thumbnail_strip_beyond_eof(caplog):
    """
    Last strip extends past the end of the file: reject only the preview,
    keep the main metadata and report the unverifiable byte range.
    """
    strip0 = bytes(range(16))
    strip1 = bytes(range(16, 32))
    data, strip_offsets, _ = _build_tiff_with_thumbnail(
        strip_offsets_type=FieldType.LONG,
        strips=[strip0, strip1],
        byte_counts=[16, 100],
    )
    file_size = len(data)

    caplog.set_level(logging.WARNING)
    tags = exifread.process_file(fh=BytesIO(data))

    assert "TIFFThumbnail" not in tags
    assert "TIFFThumbnailStripRanges" not in tags
    # main image metadata is still ingested
    assert tags["Image Make"].printable == "ACME"
    # the byte range that cannot be proven is reported
    unprovable = (max(strip_offsets[1], file_size), strip_offsets[1] + 100)
    assert f"[{unprovable[0]}, {unprovable[1]})" in caplog.text
