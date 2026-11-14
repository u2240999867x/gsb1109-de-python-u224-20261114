"""
Build a sortable capture timestamp from the EXIF date tags.

Three independent date tags may be present, each with its own matching
sub-second and offset companions:

- ``EXIF DateTimeOriginal`` with ``EXIF SubSecTimeOriginal`` and
  ``EXIF OffsetTimeOriginal`` (when the picture was taken)
- ``EXIF DateTimeDigitized`` with ``EXIF SubSecTimeDigitized`` and
  ``EXIF OffsetTimeDigitized`` (when a negative was scanned/digitized)
- ``Image DateTime`` with ``EXIF SubSecTime`` and ``EXIF OffsetTime``
  (generic file modification date)

The original capture time is preferred: a re-scan of an old negative can be
decades newer than the shot itself. Companions are only taken from the same
family, so an offset carried solely by the digitizing fields is never applied
to the original time. Without a matching offset the result is a naive local
time; UTC exports can then tell from the missing suffix why it is absent.
"""

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, Optional, Tuple

from exifread.core.exif_header import IfdTag
from exifread.exif_log import get_logger
from exifread.tags.fields import FieldType

logger = get_logger()

#: Key of the synthesized tag added to the results of ``process_file``.
TIMESTAMP_TAG = "EXIF CaptureTimestamp"

_DATETIME_RE = re.compile(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$")
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_SUBSEC_MAX_LEN = 6

#: (datetime tag, sub-second tag, offset tag) families, most relevant first.
_TIMESTAMP_FAMILIES: Tuple[Tuple[str, Optional[str], Optional[str]], ...] = (
    (
        "EXIF DateTimeOriginal",
        "EXIF SubSecTimeOriginal",
        "EXIF OffsetTimeOriginal",
    ),
    (
        "EXIF DateTimeDigitized",
        "EXIF SubSecTimeDigitized",
        "EXIF OffsetTimeDigitized",
    ),
    ("Image DateTime", "EXIF SubSecTime", "EXIF OffsetTime"),
)


def _tag_text(tags: Dict[str, Any], key: str) -> Optional[str]:
    """Return the printable text of an ASCII tag, if present and usable."""
    tag = tags.get(key)
    if tag is None or isinstance(tag, (bytes, bytearray)):
        return None
    text = tag.printable.strip()
    return text or None


def _parse_datetime(text: str) -> Optional[datetime]:
    if not _DATETIME_RE.match(text):
        return None
    try:
        return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _normalize_subsec(text: Optional[str]) -> str:
    """Turn an EXIF SubSecTime string into microsecond digits."""
    if not text:
        return ""
    if not text.isdigit():
        logger.warning("Ignoring invalid sub-second time: %r", text)
        return ""
    return (text + "000000")[:_SUBSEC_MAX_LEN]


def _parse_offset(text: Optional[str]) -> Optional[timezone]:
    if not text:
        return None
    match = _OFFSET_RE.match(text)
    if not match:
        logger.warning("Ignoring invalid UTC offset: %r", text)
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if sign == "-":
        delta = -delta
    try:
        return timezone(delta)
    except ValueError:
        logger.warning("Ignoring out-of-range UTC offset: %r", text)
        return None


def _build_timestamp(
    tags: Dict[str, Any],
    datetime_key: str,
    subsec_key: Optional[str],
    offset_key: Optional[str],
) -> Optional[IfdTag]:
    datetime_text = _tag_text(tags, datetime_key)
    if datetime_text is None:
        return None

    parsed = _parse_datetime(datetime_text)
    if parsed is None:
        logger.warning(
            "Ignoring unparseable date tag %s: %r", datetime_key, datetime_text
        )
        return None

    subsec = _normalize_subsec(_tag_text(tags, subsec_key) if subsec_key else None)
    if subsec:
        parsed = parsed.replace(microsecond=int(subsec))

    tzinfo = _parse_offset(_tag_text(tags, offset_key) if offset_key else None)
    if tzinfo is None:
        # Keep the wall-clock time local: do not borrow another family's
        # offset, e.g. one carried only by the digitizing fields.
        logger.debug(
            "%s has no matching %s; timestamp kept as naive local time",
            datetime_key,
            offset_key,
        )
    else:
        parsed = parsed.replace(tzinfo=tzinfo)

    return IfdTag(
        printable=parsed.isoformat(),
        tag=0,
        field_type=FieldType.ASCII,
        values=parsed.isoformat(),
        field_offset=0,
        field_length=0,
        prefer_printable=False,
    )


def build_capture_timestamp(tags: Dict[str, Any]) -> Optional[IfdTag]:
    """
    Synthesize a sortable ISO 8601 capture timestamp from the EXIF date tags.

    The first family (Original, then Digitized, then generic DateTime) whose
    date tag parses wins; its own sub-second and offset companions are used.
    A matching offset produces an aware timestamp (``...+08:00``), otherwise
    the result is a naive local timestamp without any offset suffix.

    Returns ``None`` when no usable date tag is available.
    """
    for datetime_key, subsec_key, offset_key in _TIMESTAMP_FAMILIES:
        timestamp = _build_timestamp(tags, datetime_key, subsec_key, offset_key)
        if timestamp is not None:
            return timestamp
    return None
