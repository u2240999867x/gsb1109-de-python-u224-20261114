"""
Tests for the two reconciliation identities (raw archive bytes vs fully
decompressed business bytes) and for reproducible gzip conditions.
"""

import gzip
import hashlib
import io
from dataclasses import FrozenInstanceError

import pytest

from xopen import (
    ArchiveIdentity,
    ReproducibleGzipConditions,
    _reproducible_gzip_backend_name,
    archive_identity,
    reproducible_gzip_conditions,
    xopen,
)

CONTENT = b"Testing, testing ...\nThe second line.\n"

# All formats that can be written and read fully in-process (threads=0),
# so the tests do not depend on external compression binaries.
IN_PROCESS_FORMATS = ["gz", "bz2", "xz", "zst"]
try:
    import lz4.frame  # noqa: F401

    IN_PROCESS_FORMATS.append("lz4")
except ImportError:
    pass

EXTENSION = {
    "gz": ".gz",
    "bz2": ".bz2",
    "xz": ".xz",
    "zst": ".zst",
    "lz4": ".lz4",
}


def _write(path, payload, *, fmt=None, compresslevel=None):
    with xopen(path, "wb", threads=0, format=fmt, compresslevel=compresslevel) as f:
        f.write(payload)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Two independently queryable identities
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", IN_PROCESS_FORMATS)
def test_content_digest_matches_business_bytes(tmp_path, fmt):
    path = tmp_path / f"record{EXTENSION[fmt]}"
    _write(path, CONTENT, fmt=fmt)
    identity = archive_identity(path)
    assert identity.content_digest == _sha256(CONTENT)
    assert identity.content_size == len(CONTENT)


@pytest.mark.parametrize("fmt", IN_PROCESS_FORMATS)
def test_archive_digest_matches_raw_bytes(tmp_path, fmt):
    path = tmp_path / f"record{EXTENSION[fmt]}"
    _write(path, CONTENT, fmt=fmt)
    raw = path.read_bytes()
    identity = archive_identity(path)
    assert identity.archive_digest == _sha256(raw)
    assert identity.archive_size == len(raw)


def test_two_gzip_files_same_content_different_archive(tmp_path):
    # The same business record archived through two different compression
    # settings yields two different gzip files: their raw-archive identity
    # differs but their business-content identity is shared.
    payload = CONTENT * 200
    one = tmp_path / "one.gz"
    two = tmp_path / "two.gz"
    _write(one, payload, compresslevel=1)
    _write(two, payload, compresslevel=9)

    assert one.read_bytes() != two.read_bytes()

    id_one = archive_identity(one)
    id_two = archive_identity(two)

    assert not id_one.same_archive(id_two)
    assert id_one.same_content(id_two)
    assert id_one.content_digest == id_two.content_digest == _sha256(payload)


@pytest.mark.parametrize("fmt", IN_PROCESS_FORMATS)
def test_identical_archives_share_both_identities(tmp_path, fmt):
    first = tmp_path / f"first{EXTENSION[fmt]}"
    second = tmp_path / f"second{EXTENSION[fmt]}"
    _write(first, CONTENT, fmt=fmt)
    _write(second, CONTENT, fmt=fmt)

    id_first = archive_identity(first)
    id_second = archive_identity(second)
    assert id_first.same_archive(id_second)
    assert id_first.same_content(id_second)


def test_different_content_never_matches(tmp_path):
    one = tmp_path / "one.gz"
    two = tmp_path / "two.gz"
    _write(one, CONTENT)
    _write(two, CONTENT + b"extra")

    id_one = archive_identity(one)
    id_two = archive_identity(two)
    assert not id_one.same_content(id_two)
    assert not id_one.same_archive(id_two)


def test_uncompressed_file_has_coinciding_identities(tmp_path):
    path = tmp_path / "record.txt"
    path.write_bytes(CONTENT)
    identity = archive_identity(path)
    assert identity.archive_digest == identity.content_digest == _sha256(CONTENT)
    assert identity.archive_size == identity.content_size == len(CONTENT)


def test_file_object_matches_path_identity(tmp_path):
    path = tmp_path / "record.gz"
    _write(path, CONTENT)
    path_identity = archive_identity(path)
    with open(path, "rb") as fh:
        object_identity = archive_identity(fh, format="gz")
    assert object_identity == path_identity


def test_format_override_for_extensionless_file(tmp_path):
    path = tmp_path / "record"
    _write(path, CONTENT, fmt="gz")
    identity = archive_identity(path, format="gz")
    assert identity.content_digest == _sha256(CONTENT)
    assert identity.archive_digest == _sha256(path.read_bytes())


def test_identity_is_frozen():
    record = ArchiveIdentity("a", 1, "b", 2)
    with pytest.raises(FrozenInstanceError):
        record.content_digest = "changed"


# --------------------------------------------------------------------------- #
# Reproducible gzip conditions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", [0, 1, 6, 9])
def test_conditions_pin_all_reproduction_parameters(level):
    conditions = reproducible_gzip_conditions(level)
    assert isinstance(conditions, ReproducibleGzipConditions)
    assert conditions.compresslevel == level
    assert conditions.mtime == 0
    assert conditions.filename == ""
    assert conditions.backend == _reproducible_gzip_backend_name(level)
    assert conditions.backend in ("gzip", "igzip", "gzip_ng")


@pytest.mark.parametrize("level", range(10))
def test_conditions_backend_matches_writer_selection(monkeypatch, level):
    # The conditions record and the actual reproducible writer must resolve the
    # same backend in every environment, including ones without isal/zlib-ng.
    import xopen as xopen_module

    def assert_backend(igzip_available, gzip_ng_available, expected):
        monkeypatch.setattr(
            xopen_module, "igzip", object() if igzip_available else None
        )
        monkeypatch.setattr(
            xopen_module, "gzip_ng", object() if gzip_ng_available else None
        )
        assert _reproducible_gzip_backend_name(level) == expected
        assert reproducible_gzip_conditions(level).backend == expected

    assert_backend(False, False, "gzip")
    assert_backend(True, False, "igzip" if level in (1, 2) else "gzip")
    assert_backend(False, True, "gzip_ng")
    assert_backend(True, True, "igzip" if level in (1, 2) else "gzip_ng")


def test_conditions_are_frozen():
    conditions = reproducible_gzip_conditions(6)
    with pytest.raises(FrozenInstanceError):
        conditions.compresslevel = 1


@pytest.mark.parametrize("level", [-1, 10, 17])
def test_invalid_reproducible_level(level):
    with pytest.raises(ValueError):
        reproducible_gzip_conditions(level)


@pytest.mark.parametrize("level", [1, 6])
def test_same_conditions_reproduce_across_target_filenames(tmp_path, level):
    # Fixed reproduction conditions must survive a change of the target file
    # name: neither mtime nor the original file name is embedded.
    names = [
        tmp_path / "a.gz",
        tmp_path / "renamed.gz",
        tmp_path / "nested" / "deeply" / "b.gz",
    ]
    raws = []
    for name in names:
        name.parent.mkdir(parents=True, exist_ok=True)
        _write(name, CONTENT, compresslevel=level)
        raws.append(name.read_bytes())

    assert raws[0] == raws[1] == raws[2]

    identities = [archive_identity(name) for name in names]
    assert all(identities[0].same_archive(other) for other in identities)
    assert all(other.same_content(identities[0]) for other in identities)


def test_fixed_conditions_reproduce_same_archive_digest(tmp_path):
    conditions = reproducible_gzip_conditions(6)
    first = tmp_path / "first.gz"
    second = tmp_path / "second.gz"
    _write(first, CONTENT, compresslevel=conditions.compresslevel)
    _write(second, CONTENT, compresslevel=conditions.compresslevel)
    assert (
        archive_identity(first).archive_digest
        == archive_identity(second).archive_digest
    )


def test_different_levels_are_different_conditions():
    assert reproducible_gzip_conditions(1) != reproducible_gzip_conditions(6)


def test_reproducible_header_has_no_name_or_mtime(tmp_path):
    path = tmp_path / "out.gz"
    _write(path, CONTENT, compresslevel=6)
    data = path.read_bytes()
    assert (data[3] & gzip.FNAME) == 0
    assert data[4:8] == b"\0\0\0\0"


# --------------------------------------------------------------------------- #
# A truncated trailer must never be pronounced content-identical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", IN_PROCESS_FORMATS)
def test_trailer_truncation_raises(tmp_path, fmt):
    path = tmp_path / f"record{EXTENSION[fmt]}"
    _write(path, CONTENT, fmt=fmt)
    truncated = tmp_path / f"truncated{EXTENSION[fmt]}"
    truncated.write_bytes(path.read_bytes()[:-8])

    with pytest.raises((EOFError, OSError)):
        archive_identity(truncated)


def test_gzip_truncated_even_when_matching_prefix_was_read(tmp_path):
    # Strip only the 8-byte gzip trailer (CRC32 + ISIZE). The decompressed
    # bytes that can still be read are a genuine prefix of the intact business
    # content, yet a content-consistent conclusion must still be refused.
    path = tmp_path / "large.gz"
    payload = (b"reconciliation-detail-line\n") * 40000
    _write(path, payload, compresslevel=1)

    truncated = tmp_path / "large-truncated.gz"
    truncated.write_bytes(path.read_bytes()[:-8])

    # First demonstrate that the bytes already read out really are an
    # identical (non-empty) prefix of the intact content.
    delivered = bytearray()
    with pytest.raises(EOFError):
        with xopen(truncated, "rb", threads=0) as f:
            while True:
                chunk = f.read(128 * 1024)
                if not chunk:
                    break
                delivered.extend(chunk)
    assert delivered
    assert bytes(delivered) == payload[: len(delivered)]

    # Despite the identical prefix, no content identity may be concluded.
    with pytest.raises(EOFError):
        archive_identity(truncated)


def test_corrupt_gzip_trailer_raises(tmp_path):
    path = tmp_path / "record.gz"
    _write(path, CONTENT)
    raw = bytearray(path.read_bytes())
    raw[-8] ^= 0xFF  # corrupt the CRC32
    corrupted = tmp_path / "corrupt.gz"
    corrupted.write_bytes(bytes(raw))

    with pytest.raises(OSError):
        archive_identity(corrupted)


def test_truncated_copy_not_equal_to_intact_identity(tmp_path):
    intact = tmp_path / "intact.gz"
    _write(intact, CONTENT)
    truncated = tmp_path / "truncated.gz"
    truncated.write_bytes(intact.read_bytes()[:-8])

    intact_identity = archive_identity(intact)
    with pytest.raises(EOFError):
        archive_identity(truncated)

    # And it cannot be coerced into an identity by hand either.
    with pytest.raises(EOFError):
        with xopen(truncated, "rb", threads=0) as f:
            f.read()
    assert intact_identity.content_digest == _sha256(CONTENT)


def test_bytesio_truncation_raises():
    buffer = io.BytesIO()
    with xopen(buffer, "wb", threads=0, format="gz") as f:
        f.write(CONTENT * 100)
    raw = buffer.getvalue()
    truncated = io.BytesIO(raw[:-8])
    with pytest.raises((EOFError, OSError)):
        archive_identity(truncated, format="gz")
