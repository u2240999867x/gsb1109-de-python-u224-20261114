"""Tests for extraction options and the collection coverage cache."""

from pathlib import Path

import pytest

import exifread
from exifread import ExtractionCache, ExtractionOptions, process_cached, process_file
from exifread.core.extraction import MAKERNOTE_IFD, ScanCoverage
from exifread.tags import DEFAULT_STOP_TAG

RESOURCES_ROOT = Path(__file__).parent / "resources"
CANON = RESOURCES_ROOT / "jpg/Canon_40D.jpg"
IXUS = RESOURCES_ROOT / "jpg/Canon_DIGITAL_IXUS_400.jpg"
XMP_JPEG = RESOURCES_ROOT / "jpg/xmp/BlueSquare.jpg"
NO_EXIF = RESOURCES_ROOT / "jpg/xmp/no_exif.jpg"


class CountingFile:
    """File wrapper counting ``read`` calls (seeks do not count)."""

    def __init__(self, path: Path):
        self._fh = open(path, "rb")
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.read_count += 1
        return self._fh.read(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._fh, name)


def full_tags(path: Path) -> dict:
    with open(path, "rb") as fh:
        return process_file(fh, options=ExtractionOptions())


def test_legacy_options_mapping():
    options = ExtractionOptions.legacy(
        details=False, extract_thumbnail=False, stop_tag="Orientation"
    )
    assert options.details is False
    assert options.thumbnail is False
    assert options.stop_tag == "Orientation"
    assert options.xmp is False
    assert options.tag_filter is None


def test_with_filter_freezes_keys():
    options = ExtractionOptions()
    filtered = options.with_filter({"Image Make"})
    assert filtered.tag_filter == frozenset({"Image Make"})
    assert options.tag_filter is None
    assert options.with_filter(None).tag_filter is None


def test_complete_ifd_is_evidence_of_absence():
    coverage = ScanCoverage(
        options=ExtractionOptions(thumbnail=False),
        tags={"Image Make": object()},
        complete_ifds=frozenset({"Image"}),
        thumbnail_done=True,
    )
    decision = coverage.covers(
        ExtractionOptions(thumbnail=False, tag_filter=frozenset({"Image Artist"}))
    )
    assert decision.reread_ifds == set()
    assert decision.fully_reusable is True


def test_truncated_ifd_is_distinct_from_complete():
    coverage = ScanCoverage(
        options=ExtractionOptions(stop_tag="ColorSpace", thumbnail=False),
        tags={"EXIF ColorSpace": object()},
        complete_ifds=frozenset(),
        truncated_ifds=frozenset({"EXIF"}),
        thumbnail_done=True,
    )

    reached = coverage.covers(
        ExtractionOptions(thumbnail=False, tag_filter=frozenset({"EXIF ColorSpace"}))
    )
    assert reached.fully_reusable is True
    assert reached.reread_ifds == set()

    missing = coverage.covers(
        ExtractionOptions(thumbnail=False, tag_filter=frozenset({"EXIF WhiteBalance"}))
    )
    assert missing.reread_ifds == {"EXIF"}
    assert missing.fully_reusable is False


def test_unfiltered_request_rereads_every_truncated_ifd():
    coverage = ScanCoverage(
        options=ExtractionOptions(stop_tag="ColorSpace"),
        tags={},
        complete_ifds=frozenset({"Image"}),
        truncated_ifds=frozenset({"EXIF"}),
    )
    decision = coverage.covers(ExtractionOptions())
    assert decision.reread_ifds == {"EXIF"}
    assert decision.projection is None


def test_quick_scan_then_details_needs_private_directory():
    coverage = ScanCoverage(
        options=ExtractionOptions(details=False),
        tags={},
        complete_ifds=frozenset({"Image", "EXIF"}),
    )
    decision = coverage.covers(ExtractionOptions(details=True))
    assert MAKERNOTE_IFD in decision.reread_ifds
    assert "EXIF" in decision.reread_ifds


def test_thumbnail_and_xmp_coverage_flags():
    coverage = ScanCoverage(
        options=ExtractionOptions(thumbnail=False, xmp=False),
        tags={},
        complete_ifds=frozenset({"Image"}),
    )
    decision = coverage.covers(ExtractionOptions(thumbnail=True, xmp=True))
    assert decision.reread_thumbnail is True
    assert decision.reread_xmp is True
    assert decision.fully_reusable is False

    satisfied = coverage.covers(ExtractionOptions(thumbnail=False, xmp=False))
    assert satisfied.fully_reusable is True


def test_cache_update_promotes_completed_ifd():
    cache = ExtractionCache()
    identity = "img"
    cache.store(
        identity,
        ScanCoverage(
            options=ExtractionOptions(stop_tag="ColorSpace"),
            tags={"EXIF ColorSpace": object()},
            complete_ifds=frozenset(),
            truncated_ifds=frozenset({"EXIF"}),
        ),
    )
    merged = cache.update(
        identity,
        options=ExtractionOptions(),
        extra_tags={"EXIF WhiteBalance": object()},
        complete_ifds={"EXIF"},
        truncated_ifds=set(),
        thumbnail_done=True,
        xmp_done=False,
    )
    assert "EXIF" in merged.complete_ifds
    assert "EXIF" not in merged.truncated_ifds
    assert merged.thumbnail_done is True
    assert len(cache) == 1


def test_narrow_projection_does_not_touch_the_disk():
    cache = ExtractionCache()
    identity = "canon"
    with open(CANON, "rb") as fh:
        process_cached(identity, fh, ExtractionOptions(), cache=cache)

    handle = CountingFile(CANON)
    requested = frozenset({"Image Make", "Image Model", "EXIF ColorSpace"})
    tags = process_cached(
        identity,
        handle,
        ExtractionOptions(tag_filter=requested),
        cache=cache,
    )
    handle.close()

    assert handle.read_count == 0
    assert set(tags) == requested


def test_absent_tag_in_complete_ifd_is_served_from_cache():
    cache = ExtractionCache()
    identity = "canon"
    with open(CANON, "rb") as fh:
        process_cached(identity, fh, ExtractionOptions(), cache=cache)

    handle = CountingFile(CANON)
    tags = process_cached(
        identity,
        handle,
        ExtractionOptions(tag_filter=frozenset({"Image Artist"})),
        cache=cache,
    )
    handle.close()

    assert handle.read_count == 0
    assert tags == {}


def test_widening_a_truncated_scan_matches_a_full_scan():
    cache = ExtractionCache()
    identity = "canon"
    with open(CANON, "rb") as fh:
        truncated = process_cached(
            identity,
            fh,
            ExtractionOptions(stop_tag="ColorSpace"),
            cache=cache,
        )
    assert len(truncated) < len(full_tags(CANON))
    assert "EXIF" in cache.lookup(identity, ExtractionOptions())[0].truncated_ifds

    with open(CANON, "rb") as fh:
        widened = process_cached(identity, fh, ExtractionOptions(), cache=cache)

    assert set(widened) == set(full_tags(CANON))
    assert "EXIF" in cache.lookup(identity, ExtractionOptions())[0].complete_ifds


def test_quick_scan_then_details_recovers_makernote():
    cache = ExtractionCache()
    identity = "ixus"
    with open(IXUS, "rb") as fh:
        quick = process_cached(
            identity,
            fh,
            ExtractionOptions(details=False, thumbnail=False),
            cache=cache,
        )
    assert "MakerNote AESetting" not in quick

    with open(IXUS, "rb") as fh:
        detailed = process_cached(
            identity,
            fh,
            ExtractionOptions(details=True, thumbnail=False),
            cache=cache,
        )
    assert "MakerNote AESetting" in detailed


def test_thumbnail_widening_only_happens_on_request():
    cache = ExtractionCache()
    identity = "canon"
    with open(CANON, "rb") as fh:
        without_thumb = process_cached(
            identity, fh, ExtractionOptions(thumbnail=False), cache=cache
        )
    assert "JPEGThumbnail" not in without_thumb

    with open(CANON, "rb") as fh:
        with_thumb = process_cached(
            identity, fh, ExtractionOptions(thumbnail=True), cache=cache
        )
    assert len(with_thumb["JPEGThumbnail"]) == 1378

    # The thumbnail is now evidence: a further request reads nothing.
    handle = CountingFile(CANON)
    again = process_cached(
        identity, handle, ExtractionOptions(thumbnail=True), cache=cache
    )
    handle.close()
    assert handle.read_count == 0
    assert "JPEGThumbnail" in again


def test_xmp_widening_through_cache():
    cache = ExtractionCache()
    identity = "xmp-jpeg"
    with open(XMP_JPEG, "rb") as fh:
        first = process_cached(identity, fh, ExtractionOptions(xmp=False), cache=cache)
    assert "Image ApplicationNotes" not in first

    with open(XMP_JPEG, "rb") as fh:
        second = process_cached(
            identity,
            fh,
            ExtractionOptions(xmp=True),
            cache=cache,
            builtin_types=True,
        )
    assert "Image ApplicationNotes" in second
    assert isinstance(second["Image ApplicationNotes"], str)

    # XMP is now evidence: the next widened-equivalent request is reused.
    handle = CountingFile(XMP_JPEG)
    third = process_cached(identity, handle, ExtractionOptions(xmp=True), cache=cache)
    handle.close()
    assert handle.read_count == 0
    assert "Image ApplicationNotes" in third


def test_cached_scan_without_exif_returns_empty():
    cache = ExtractionCache()
    with open(NO_EXIF, "rb") as fh:
        assert process_cached("no-exif", fh, ExtractionOptions(), cache=cache) == {}
    assert len(cache) == 0


def test_file_identity_and_cache_clearing():
    identity = exifread.file_identity(CANON)
    assert identity[0] == str(CANON)
    assert len(identity) == 3

    cache = ExtractionCache()
    with open(CANON, "rb") as fh:
        process_cached(identity, fh, ExtractionOptions(), cache=cache)
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


@pytest.mark.parametrize(
    "stop_tag, tag_count", (("ColorSpace", 39), (DEFAULT_STOP_TAG, 51))
)
def test_process_file_keeps_legacy_stop_tag_behavior(stop_tag, tag_count):
    with open(CANON, "rb") as fh:
        tags = process_file(fh, options=ExtractionOptions(stop_tag=stop_tag))
    assert len(tags) == tag_count
