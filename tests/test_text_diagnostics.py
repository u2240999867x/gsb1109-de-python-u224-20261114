"""Tests for text-tag encoding diagnostics and their synthetic fixtures."""

import io
from pathlib import Path

import pytest

import exifread
from exifread.text_diagnostics import (
    CHARSET_PREFIX_LENGTH,
    diagnose_file,
    diagnose_text_tags,
    format_report,
)
from tests.text_encoding_fixtures import (
    AUTHOR_NAME,
    COMMENT_TEXT,
    TAG_USER_COMMENT,
    TAG_XP_AUTHOR,
    USER_COMMENT_PAYLOAD,
    USER_COMMENT_TRUNCATED_PAYLOAD,
    build_tiff,
    pair_payloads,
    write_fixtures,
)

RESOURCES_ROOT = Path(__file__).parent / "resources" / "tiff" / "text_encoding"
LE_FIXTURE = RESOURCES_ROOT / "xp_author_comment_le.tiff"
BE_FIXTURE = RESOURCES_ROOT / "xp_author_comment_be.tiff"
TRUNCATED_FIXTURE = RESOURCES_ROOT / "user_comment_truncated_le.tiff"

# Last byte dropped from the final three-byte character (七 -> e4 b8 __).
TRUNCATED_TAIL = USER_COMMENT_TRUNCATED_PAYLOAD[-2:]
TRUNCATED_FILE_OFFSET = 46


def test_fixtures_on_disk_match_generator():
    """The committed fixtures are exactly what the generator writes."""
    written = {path.name: path for path in write_fixtures()}
    assert LE_FIXTURE.read_bytes() == written[LE_FIXTURE.name].read_bytes()
    assert BE_FIXTURE.read_bytes() == written[BE_FIXTURE.name].read_bytes()
    assert (
        TRUNCATED_FIXTURE.read_bytes() == written[TRUNCATED_FIXTURE.name].read_bytes()
    )


def test_byte_order_pair_differs_only_in_endian():
    """The pair holds identical tags; only the TIFF byte order changes."""
    little = build_tiff("II", pair_payloads())
    big = build_tiff("MM", pair_payloads())
    assert little[:2] == b"II"
    assert big[:2] == b"MM"
    assert little != big

    le_tags = exifread.process_file(io.BytesIO(little))
    be_tags = exifread.process_file(io.BytesIO(big))
    assert set(le_tags) == set(be_tags) == {"Image XPAuthor", "Image UserComment"}
    for key in le_tags:
        assert le_tags[key].values == be_tags[key].values


@pytest.mark.parametrize("fixture, endian", ((LE_FIXTURE, "I"), (BE_FIXTURE, "M")))
def test_xp_author_decodes_in_both_byte_orders(fixture, endian):
    """The full author name survives regardless of TIFF byte order."""
    with open(fixture, "rb") as fh:
        diagnoses = diagnose_file(fh)

    author = next(item for item in diagnoses if item.tag_id == TAG_XP_AUTHOR)
    assert author.kind == "xp_field"
    assert author.endian == endian
    assert author.charset_declared is None
    assert author.charset_prefix is None
    assert author.decoding.codec == "utf-16-le"
    assert author.decoding.text == AUTHOR_NAME
    assert author.decoding.undecodable == []
    # The legacy pipeline shows why the name looked like question marks.
    assert author.library_printable != AUTHOR_NAME


@pytest.mark.parametrize("fixture", (LE_FIXTURE, BE_FIXTURE))
def test_prefixed_user_comment_decodes(fixture):
    """A UserComment with an 8-byte ASCII prefix is fully decodable."""
    with open(fixture, "rb") as fh:
        diagnoses = diagnose_file(fh)

    comment = next(item for item in diagnoses if item.tag_id == TAG_USER_COMMENT)
    assert comment.kind == "user_comment"
    assert comment.charset_declared == "ASCII"
    assert comment.charset_prefix == USER_COMMENT_PAYLOAD[:CHARSET_PREFIX_LENGTH]
    assert comment.decoding.codec == "utf-8"
    assert comment.decoding.text == COMMENT_TEXT
    assert comment.decoding.undecodable == []
    assert comment.raw_bytes == USER_COMMENT_PAYLOAD


def test_truncated_comment_marks_the_undecodable_bytes():
    """The half-character tail is located byte-by-byte, not hidden."""
    with open(TRUNCATED_FIXTURE, "rb") as fh:
        diagnoses = diagnose_file(fh)

    comment = next(item for item in diagnoses if item.tag_id == TAG_USER_COMMENT)
    assert comment.raw_bytes == USER_COMMENT_TRUNCATED_PAYLOAD
    assert len(comment.decoding.undecodable) == 1
    span = comment.decoding.undecodable[0]
    assert span.data == TRUNCATED_TAIL == b"\xe4\xb8"
    assert span.file_offset == TRUNCATED_FILE_OFFSET
    assert (
        comment.field_offset + CHARSET_PREFIX_LENGTH + span.content_offset
        == TRUNCATED_FILE_OFFSET
    )
    # Decodable prefix of the text remains intact, one replacement marker.
    assert comment.decoding.text == COMMENT_TEXT[:-1] + "�"
    # The legacy output simply drops the tail without any indication.
    assert comment.library_printable.endswith("\xb8")
    assert "�" not in comment.library_printable


def test_private_comment_without_charset_declaration():
    """Private UserComment content (no prefix) is labelled as an assumption."""
    private_payload = COMMENT_TEXT.encode("utf-8") + b"\x00"
    tiff = build_tiff("II", [(TAG_USER_COMMENT, 7, private_payload)])

    with io.BytesIO(tiff) as fh:
        diagnoses = diagnose_file(fh)

    comment = diagnoses[0]
    assert comment.kind == "private_user_comment"
    assert comment.charset_declared is None
    assert comment.charset_prefix is None
    assert comment.content_bytes == private_payload
    assert comment.decoding.codec == "utf-8"
    assert comment.decoding.text == COMMENT_TEXT
    assert any("no 8-byte charset prefix" in note for note in comment.decoding.notes)


def test_private_comment_with_invalid_bytes_is_flagged():
    """Undecodable private content reports the exact offending bytes."""
    private_payload = b"batch \xff\xfe end"
    tiff = build_tiff("II", [(TAG_USER_COMMENT, 7, private_payload)])

    with io.BytesIO(tiff) as fh:
        diagnoses = diagnose_file(fh)

    comment = diagnoses[0]
    assert comment.kind == "private_user_comment"
    # Each invalid UTF-8 start byte is reported with its own file offset.
    assert [span.data for span in comment.decoding.undecodable] == [
        b"\xff",
        b"\xfe",
    ]
    for span in comment.decoding.undecodable:
        assert span.file_offset == comment.field_offset + span.content_offset
    assert comment.decoding.text == "batch �� end"


def test_report_is_reviewable_and_mentions_bad_offsets():
    """The librarian-facing report carries hex bytes and absolute offsets."""
    with open(TRUNCATED_FIXTURE, "rb") as fh:
        diagnoses = diagnose_file(fh)
    report = format_report(diagnoses, TRUNCATED_FIXTURE.name)

    assert "tag 0x9286" in report
    assert "e4 b8" in report
    assert "file offset %d" % TRUNCATED_FILE_OFFSET in report
    assert "content offset 12" in report
    assert "charset prefix: 41 53 43 49 49 00 00 00 (ASCII)" in report
    assert "library printable output" in report


def test_diagnose_text_tags_skips_non_text_and_thumbnails():
    """Only XP and UserComment tags are diagnosed; thumbnails are ignored."""
    with open(LE_FIXTURE, "rb") as fh:
        tags = exifread.process_file(fh)
        tags["JPEGThumbnail"] = b"\xff\xd8fake"
        diagnoses = diagnose_text_tags(tags, fh, 0, "I")

    assert {item.tag_id for item in diagnoses} == {
        TAG_XP_AUTHOR,
        TAG_USER_COMMENT,
    }
    assert all(item.field_length > 0 for item in diagnoses)


def test_unicode_prefixed_comment_honours_bom():
    """A UNICODE prefix with a UTF-16-LE BOM decodes correctly."""
    payload = b"UNICODE\x00" + b"\xff\xfe" + COMMENT_TEXT.encode("utf-16-le")
    tiff = build_tiff("II", [(TAG_USER_COMMENT, 7, payload)])

    with io.BytesIO(tiff) as fh:
        diagnoses = diagnose_file(fh)

    comment = diagnoses[0]
    assert comment.kind == "user_comment"
    assert comment.charset_declared == "UNICODE"
    assert comment.decoding.codec == "utf-16-le"
    assert comment.decoding.text == COMMENT_TEXT
    assert comment.decoding.undecodable == []


def test_undefined_prefix_is_treated_as_private():
    """The eight-null 'undefined' marker is stripped but declared as such."""
    payload = b"\x00" * 8 + COMMENT_TEXT.encode("utf-8")
    tiff = build_tiff("II", [(TAG_USER_COMMENT, 7, payload)])

    with io.BytesIO(tiff) as fh:
        diagnoses = diagnose_file(fh)

    comment = diagnoses[0]
    assert comment.kind == "private_user_comment"
    assert comment.charset_declared is None
    assert comment.charset_prefix == b"\x00" * 8
    assert comment.decoding.text == COMMENT_TEXT
    assert any("undefined" in note for note in comment.decoding.notes)


def test_file_without_exif_returns_empty_diagnoses():
    """Files without EXIF data produce an empty result, not an exception."""
    with io.BytesIO(b"not an image at all") as fh:
        assert diagnose_file(fh) == []


def test_real_jpeg_uses_absolute_offsets_and_matches_disk():
    """Embedded-TIFF offsets are resolved against the JPEG wrapper."""
    jpeg_path = RESOURCES_ROOT.parent.parent / "jpg" / "Pentax_K10D.jpg"
    with open(jpeg_path, "rb") as fh:
        data = fh.read()
        diagnoses = diagnose_file(io.BytesIO(data))

    author = next(item for item in diagnoses if item.tag_id == TAG_XP_AUTHOR)
    assert author.decoding.text == "www.laitche.com"
    # The reported offset points exactly at the XPAuthor bytes in the file.
    assert data[author.field_offset : author.field_offset + author.field_length] == (
        author.raw_bytes
    )
    # Disk/parser cross-check found no mismatch.
    assert not any("differ" in note for note in author.decoding.notes)
