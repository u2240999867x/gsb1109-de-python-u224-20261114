"""
Diagnostic decoding for text-bearing EXIF tags.

The regular tag processing in :mod:`exifread.tags.str_utils` maps every raw
byte to a single Latin-range character, which leaves multi-byte text (Chinese,
Japanese, ...) looking like mojibake or question marks.  This module follows
the parsed :class:`~exifread.core.ifd_tag.IfdTag` back to the actual bytes on
disk and decodes them according to the tag convention:

* Windows XP fields (``XPTitle`` ... ``XPSubject``) are UTF-16-LE regardless of
  the surrounding TIFF byte order and carry no charset declaration.
* ``UserComment`` values may start with the 8-byte EXIF charset prefix
  (``ASCII``, ``JIS``, ``UNICODE`` or eight null bytes for "undefined").
* ``UserComment`` values without any prefix are private content: the encoding
  can only be guessed, so every assumption is reported explicitly.

Bytes that no codec can consume are reported with their payload-relative and
absolute file offsets instead of being silently replaced.
"""

from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional, Tuple

from exifread import process_file
from exifread.core.exceptions import ExifNotFound, InvalidExif
from exifread.core.find_exif import determine_type, get_endian_str
from exifread.core.ifd_tag import IfdTag
from exifread.tags.fields import FIELD_DEFINITIONS

#: Windows Explorer "XP" tags, always stored as UTF-16-LE.
XP_TAGS = {
    0x9C9B: "XPTitle",
    0x9C9C: "XPComment",
    0x9C9D: "XPAuthor",
    0x9C9E: "XPKeywords",
    0x9C9F: "XPSubject",
}

#: EXIF UserComment tag identifier.
USER_COMMENT_TAG = 0x9286

#: The EXIF UserComment charset prefix is always eight bytes long.
CHARSET_PREFIX_LENGTH = 8

#: Recognised UserComment charset prefixes mapped to their declared label.
CHARSET_PREFIXES = {
    b"ASCII\x00\x00\x00": "ASCII",
    b"JIS\x00\x00\x00\x00\x00": "JIS",
    b"UNICODE\x00": "UNICODE",
}

#: The spec's "undefined" marker: eight null bytes, i.e. no declaration.
UNDEFINED_PREFIX = b"\x00" * CHARSET_PREFIX_LENGTH

ENDIAN_NAMES = {"I": "Intel (II, little-endian)", "M": "Motorola (MM, big-endian)"}


@dataclass
class UndecodableSpan:
    """A byte range that the selected codec cannot decode."""

    #: Offset of the first bad byte relative to the tag's content bytes.
    content_offset: int
    #: Offset of the first bad byte relative to the beginning of the file.
    file_offset: int
    #: The raw bytes that could not be decoded.
    data: bytes


@dataclass
class TextDecoding:
    """Result of decoding one tag's text content."""

    #: Codec used for decoding, or `None` when no encoding can be assumed.
    codec: Optional[str]
    #: Decoded text; undecodable spans are replaced with U+FFFD.
    text: str
    #: Byte ranges that could not be decoded.
    undecodable: List[UndecodableSpan] = field(default_factory=list)
    #: Human-readable observations about the decoding process.
    notes: List[str] = field(default_factory=list)


@dataclass
class TextTagDiagnosis:
    """Full, re-checkable diagnosis of one text tag."""

    #: Tag key as returned by `process_file`, e.g. `"Image XPAuthor"`.
    key: str
    #: Numeric tag identifier.
    tag_id: int
    #: `"xp_field"`, `"user_comment"` or `"private_user_comment"`.
    kind: str
    #: Name of the EXIF field type, e.g. `"Byte"`, `"Undefined"`.
    field_type_name: str
    #: TIFF byte order marker (`"I"` or `"M"`).
    endian: str
    #: Absolute file offset of the tag value.
    field_offset: int
    #: Length of the tag value in bytes.
    field_length: int
    #: Full raw tag bytes as stored in the file.
    raw_bytes: bytes
    #: Charset declared by the UserComment prefix, if any.
    charset_declared: Optional[str]
    #: The raw 8-byte charset prefix, when present.
    charset_prefix: Optional[bytes]
    #: Content bytes after stripping the charset prefix / null terminators.
    content_bytes: bytes
    #: Decoding outcome.
    decoding: TextDecoding
    #: What the regular library pipeline exposes as `printable`.
    library_printable: str


def _decode_with_spans(data: bytes, codec: str) -> Tuple[str, List[Tuple[int, int]]]:
    """
    Decode `data` with `codec`, replacing undecodable bytes with U+FFFD.

    Returns the decoded text and a list of `(start, end)` offsets pointing at
    the byte ranges that strict decoding rejected.
    """
    parts: List[str] = []
    spans: List[Tuple[int, int]] = []
    cursor = 0
    while cursor < len(data):
        try:
            parts.append(data[cursor:].decode(codec))
            cursor = len(data)
        except UnicodeDecodeError as err:
            bad_start = cursor + err.start
            bad_end = cursor + err.end
            parts.append(data[cursor:bad_start].decode(codec, errors="replace"))
            parts.append("�")
            spans.append((bad_start, bad_end))
            cursor = bad_end
    return "".join(parts), spans


def _build_decoding(
    content: bytes,
    codec: Optional[str],
    base_offset: int,
    notes: Optional[List[str]] = None,
) -> TextDecoding:
    """Run the codec and attach absolute file offsets to every bad span."""
    result_notes = list(notes or [])
    spans: List[UndecodableSpan] = []
    if codec is None:
        text = ""
        result_notes.append("no usable encoding declaration; content not decoded")
    else:
        text, raw_spans = _decode_with_spans(content, codec)
        text = text.rstrip("\x00")
        for start, end in raw_spans:
            spans.append(
                UndecodableSpan(
                    content_offset=start,
                    file_offset=base_offset + start,
                    data=content[start:end],
                )
            )
        if spans:
            result_notes.append(
                "undecodable bytes replaced with U+FFFD in the decoded text"
            )
    return TextDecoding(codec=codec, text=text, undecodable=spans, notes=result_notes)


def _decode_xp_field(data: bytes, base_offset: int) -> TextDecoding:
    """Decode a Windows XP field, which is always UTF-16 little-endian."""
    notes = [
        "Windows XP field: UTF-16-LE by convention, independent of TIFF byte order"
    ]
    return _build_decoding(data, "utf-16-le", base_offset, notes)


def _split_user_comment(
    data: bytes,
) -> Tuple[Optional[str], Optional[bytes], bytes]:
    """
    Split a UserComment into `(charset label, prefix, content)`.

    A missing or unrecognised prefix yields `(None, None, data)`; the
    eight-null "undefined" prefix is stripped but also yields no label.
    """
    if len(data) >= CHARSET_PREFIX_LENGTH:
        prefix = data[:CHARSET_PREFIX_LENGTH]
        if prefix in CHARSET_PREFIXES:
            return CHARSET_PREFIXES[prefix], prefix, data[CHARSET_PREFIX_LENGTH:]
        if prefix == UNDEFINED_PREFIX:
            return None, prefix, data[CHARSET_PREFIX_LENGTH:]
    return None, None, data


def _decode_user_comment(
    data: bytes, base_offset: int
) -> Tuple[str, Optional[str], Optional[bytes], bytes, TextDecoding]:
    """Decode a UserComment, distinguishing declared and private encodings."""
    charset, prefix, content = _split_user_comment(data)

    if charset == "ASCII":
        # Pure ASCII is valid UTF-8; real-world files store UTF-8 in practice.
        codec = "utf-8"
        notes = ["charset prefix 'ASCII'; UTF-8 used (superset of ASCII)"]
        kind = "user_comment"
    elif charset == "UNICODE":
        codec, bom_notes = _unicode_codec(content)
        notes = bom_notes
        kind = "user_comment"
    elif charset == "JIS":
        codec = "shift_jis"
        notes = ["charset prefix 'JIS'; Shift-JIS codec used"]
        kind = "user_comment"
    elif prefix is not None:
        codec = "utf-8"
        notes = [
            "eight-null 'undefined' charset prefix; UTF-8 assumed without"
            " any declaration"
        ]
        kind = "private_user_comment"
    else:
        codec = "utf-8"
        notes = [
            "no 8-byte charset prefix found; private content, UTF-8 assumed"
            " without any declaration"
        ]
        kind = "private_user_comment"

    prefix_length = len(prefix) if prefix else 0
    decoding = _build_decoding(content, codec, base_offset + prefix_length, notes)
    if charset == "UNICODE" and decoding.text.startswith("\ufeff"):
        decoding.text = decoding.text[1:]
        decoding.notes.append("leading byte-order mark removed from decoded text")
    return kind, charset, prefix, content, decoding


def _unicode_codec(content: bytes) -> Tuple[str, List[str]]:
    """Pick a UTF-16 codec for a UNICODE comment, honouring a BOM if present."""
    if content.startswith(b"\xff\xfe"):
        return "utf-16-le", ["charset prefix 'UNICODE'; UTF-16-LE BOM detected"]
    if content.startswith(b"\xfe\xff"):
        return "utf-16-be", ["charset prefix 'UNICODE'; UTF-16-BE BOM detected"]
    return "utf-16-le", ["charset prefix 'UNICODE' without BOM; UTF-16-LE assumed"]


def _read_tag_bytes(
    tag: IfdTag, fh: BinaryIO, tiff_offset: int
) -> Tuple[bytes, List[str]]:
    """Recover the raw tag bytes, cross-checking the parser against the file."""
    notes: List[str] = []
    if isinstance(tag.values, str):
        # ASCII fields are already decoded by the parser; Latin-1 round-trips
        # byte-valued strings, but multi-byte text has to be re-read directly.
        try:
            parsed = tag.values.encode("latin-1")
        except UnicodeEncodeError:
            try:
                fh.seek(tiff_offset + tag.field_offset)
                parsed = fh.read(tag.field_length)
            except OSError as err:
                notes.append("raw bytes could not be re-read from file: %s" % err)
                return b"", notes
            notes.append(
                "parser decoded the field as text; raw bytes re-read from disk"
            )
            return parsed, notes
    else:
        parsed = bytes(tag.values)
    try:
        fh.seek(tiff_offset + tag.field_offset)
        on_disk = fh.read(tag.field_length)
    except OSError as err:
        notes.append("raw bytes could not be re-read from file: %s" % err)
        return parsed, notes
    if on_disk != parsed:
        notes.append(
            "parser bytes differ from disk bytes at offset %d"
            % (tiff_offset + tag.field_offset)
        )
    return parsed, notes


def _diagnose_one(
    key: str,
    tag: IfdTag,
    fh: BinaryIO,
    tiff_offset: int,
    endian: str,
) -> TextTagDiagnosis:
    """Build a full diagnosis for a single XP or UserComment tag."""
    raw_bytes, read_notes = _read_tag_bytes(tag, fh, tiff_offset)
    absolute_offset = tiff_offset + tag.field_offset

    if tag.tag in XP_TAGS:
        kind = "xp_field"
        charset_declared: Optional[str] = None
        charset_prefix: Optional[bytes] = None
        content_bytes = raw_bytes
        decoding = _decode_xp_field(raw_bytes, absolute_offset)
    else:
        kind, charset_declared, charset_prefix, content_bytes, decoding = (
            _decode_user_comment(raw_bytes, absolute_offset)
        )

    decoding.notes.extend(read_notes)
    return TextTagDiagnosis(
        key=key,
        tag_id=tag.tag,
        kind=kind,
        field_type_name=FIELD_DEFINITIONS[tag.field_type][1],
        endian=endian,
        field_offset=absolute_offset,
        field_length=tag.field_length,
        raw_bytes=raw_bytes,
        charset_declared=charset_declared,
        charset_prefix=charset_prefix,
        content_bytes=content_bytes,
        decoding=decoding,
        library_printable=tag.printable,
    )


def diagnose_text_tags(
    tags: dict,
    fh: BinaryIO,
    tiff_offset: int = 0,
    endian: str = "?",
) -> List[TextTagDiagnosis]:
    """
    Diagnose every XP / UserComment tag found in `tags`.

    :param tags: mapping returned by :func:`exifread.process_file`.
    :param fh: open binary handle of the same file.
    :param tiff_offset: offset of the TIFF header within the file (0 for TIFF).
    :param endian: `"I"` (Intel) or `"M"` (Motorola).
    """
    diagnoses: List[TextTagDiagnosis] = []
    for key in sorted(tags):
        tag = tags[key]
        if not isinstance(tag, IfdTag):
            continue
        if tag.tag in XP_TAGS or tag.tag == USER_COMMENT_TAG:
            diagnoses.append(_diagnose_one(key, tag, fh, tiff_offset, endian))
    return diagnoses


def diagnose_file(fh: BinaryIO) -> List[TextTagDiagnosis]:
    """
    Parse `fh` and diagnose its text tags in one pass.

    The handle must be opened in binary mode and positioned at the start of
    the file; it is read from again to verify the raw tag bytes. Files without
    any recognisable EXIF data yield an empty list.
    """
    try:
        tiff_offset, endian_bytes, _ = determine_type(fh)
    except (ExifNotFound, InvalidExif):
        return []
    endian, _ = get_endian_str(endian_bytes)
    tags = process_file(fh)
    return diagnose_text_tags(tags, fh, tiff_offset, endian)


def _hex(data: bytes) -> str:
    return data.hex(" ") or "(empty)"


def format_report(diagnoses: List[TextTagDiagnosis], source: str = "") -> str:
    """Render diagnoses as a plain-text report suitable for archival review."""
    lines: List[str] = []
    title = "Text encoding diagnostic"
    if source:
        title += ": %s" % source
    lines.append(title)
    lines.append("=" * len(title))

    if not diagnoses:
        lines.append("No XP or UserComment text tags found.")
        return "\n".join(lines)

    for diagnosis in diagnoses:
        decoding = diagnosis.decoding
        lines.append("")
        lines.append(
            "[%s] tag 0x%04X, %s, %s"
            % (
                diagnosis.key,
                diagnosis.tag_id,
                diagnosis.field_type_name,
                ENDIAN_NAMES.get(diagnosis.endian, diagnosis.endian),
            )
        )
        lines.append("  kind: %s" % diagnosis.kind)
        lines.append(
            "  field bytes @ file offset %d, %d bytes: %s"
            % (
                diagnosis.field_offset,
                diagnosis.field_length,
                _hex(diagnosis.raw_bytes),
            )
        )
        if diagnosis.charset_prefix is not None:
            lines.append(
                "  charset prefix: %s (%s)"
                % (
                    _hex(diagnosis.charset_prefix),
                    diagnosis.charset_declared or "undefined / no declaration",
                )
            )
        else:
            lines.append("  charset prefix: none (no encoding declaration)")
        lines.append("  content bytes: %s" % _hex(diagnosis.content_bytes))
        lines.append(
            "  decoded with %s: %s"
            % (decoding.codec or "no codec", repr(decoding.text))
        )
        if decoding.undecodable:
            lines.append("  undecodable bytes:")
            for span in decoding.undecodable:
                lines.append(
                    "    - content offset %d, file offset %d, %d byte(s): %s"
                    % (
                        span.content_offset,
                        span.file_offset,
                        len(span.data),
                        _hex(span.data),
                    )
                )
        else:
            lines.append("  undecodable bytes: none")
        for note in decoding.notes:
            lines.append("  note: %s" % note)
        lines.append(
            "  library printable output: %s" % repr(diagnosis.library_printable)
        )

    return "\n".join(lines)
