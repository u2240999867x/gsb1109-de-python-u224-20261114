"""
Synthetic TIFF fixtures for text-encoding diagnostics.

The files generated here are minimal, hand-laid-out TIFF containers carrying
only the tags needed to reproduce the scanner-batch scenario:

* ``xp_author_comment_*.tiff`` - a pair whose *only* difference is the TIFF
  byte order (Intel ``II`` vs. Motorola ``MM``).  Both hold a UTF-16-LE
  ``XPAuthor`` and an ASCII-prefixed UTF-8 ``UserComment``.
* ``user_comment_truncated_le.tiff`` - an ASCII-prefixed comment whose final
  multi-byte character is missing its last byte.

Run the module directly to (re)write the resources::

    python tests/text_encoding_fixtures.py
"""

import struct
from pathlib import Path
from typing import List, Tuple

RESOURCE_DIR = Path(__file__).parent / "resources" / "tiff" / "text_encoding"

TAG_XP_AUTHOR = 0x9C9D
TAG_USER_COMMENT = 0x9286

FIELD_TYPE_BYTE = 1
FIELD_TYPE_UNDEFINED = 7

AUTHOR_NAME = "司马迁"
COMMENT_TEXT = "扫描批次七"

#: UTF-16-LE author with the Windows double-null terminator.
XP_AUTHOR_PAYLOAD = AUTHOR_NAME.encode("utf-16-le") + b"\x00\x00"

#: Complete UserComment: 8-byte ASCII prefix + UTF-8 text + null terminator.
USER_COMMENT_PAYLOAD = b"ASCII\x00\x00\x00" + COMMENT_TEXT.encode("utf-8") + b"\x00"

#: Same comment, but the final 3-byte character loses its last byte.
USER_COMMENT_TRUNCATED_PAYLOAD = (
    b"ASCII\x00\x00\x00" + COMMENT_TEXT.encode("utf-8")[:-1]
)


def build_tiff(endian: str, entries: List[Tuple[int, int, bytes]]) -> bytes:
    """
    Build a minimal TIFF byte string for the given byte order.

    :param endian: ``"II"`` (little-endian) or ``"MM"`` (big-endian).
    :param entries: ``(tag_id, field_type, payload)`` tuples; tag order is
        normalised by tag identifier as required by the TIFF spec.
    """
    if endian not in ("II", "MM"):
        raise ValueError("endian must be 'II' or 'MM'")
    byte_order = "<" if endian == "II" else ">"

    ordered = sorted(entries, key=lambda entry: entry[0])
    entry_count = len(ordered)
    ifd_start = 8
    data_start = ifd_start + 2 + 12 * entry_count + 4

    cursor = data_start
    packed_entries: List[bytes] = []
    extra_data = bytearray()

    for tag_id, field_type, payload in ordered:
        count = len(payload)
        if count <= 4:
            value_field = payload + b"\x00" * (4 - count)
        else:
            value_field = struct.pack(byte_order + "I", cursor)
            extra_data += payload
            cursor += count
        packed_entries.append(
            struct.pack(byte_order + "HHI", tag_id, field_type, count) + value_field
        )

    tiff = bytearray()
    tiff += endian.encode("ascii")
    tiff += b"*\x00" if endian == "II" else b"\x00*"
    tiff += struct.pack(byte_order + "I", ifd_start)
    tiff += struct.pack(byte_order + "H", entry_count)
    tiff += b"".join(packed_entries)
    tiff += b"\x00\x00\x00\x00"  # no next IFD
    tiff += extra_data
    return bytes(tiff)


def pair_payloads() -> List[Tuple[int, int, bytes]]:
    """Entries shared by the byte-order pair."""
    return [
        (TAG_XP_AUTHOR, FIELD_TYPE_BYTE, XP_AUTHOR_PAYLOAD),
        (TAG_USER_COMMENT, FIELD_TYPE_UNDEFINED, USER_COMMENT_PAYLOAD),
    ]


def truncated_payload() -> List[Tuple[int, int, bytes]]:
    """Entries for the truncated-comment file."""
    return [(TAG_USER_COMMENT, FIELD_TYPE_UNDEFINED, USER_COMMENT_TRUNCATED_PAYLOAD)]


def write_fixtures() -> List[Path]:
    """Write the three fixture TIFFs and return their paths."""
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    paths = [
        RESOURCE_DIR / "xp_author_comment_le.tiff",
        RESOURCE_DIR / "xp_author_comment_be.tiff",
        RESOURCE_DIR / "user_comment_truncated_le.tiff",
    ]
    paths[0].write_bytes(build_tiff("II", pair_payloads()))
    paths[1].write_bytes(build_tiff("MM", pair_payloads()))
    paths[2].write_bytes(build_tiff("II", truncated_payload()))
    return paths


if __name__ == "__main__":
    for path in write_fixtures():
        print("wrote", path)
