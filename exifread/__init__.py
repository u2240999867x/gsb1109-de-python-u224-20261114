"""
Read Exif metadata from image files
Supported formats: TIFF, JPEG, PNG, Webp, HEIC
"""

import os
from typing import Any, BinaryIO, Dict, Hashable, Optional, Set, Union

from exifread.core.exceptions import ExifNotFound, InvalidExif
from exifread.core.exif_header import ExifHeader
from exifread.core.extraction import (
    CoverageDecision,
    ExtractionCache,
    ExtractionOptions,
    MAKERNOTE_IFD,
    ScanCoverage,
)
from exifread.core.find_exif import determine_type, get_endian_str
from exifread.core.xmp import find_xmp_data
from exifread.exif_log import get_logger
from exifread.serialize import convert_types
from exifread.tags import DEFAULT_STOP_TAG

__version__ = "3.5.1"

logger = get_logger()

# Reusable cache of collection scans, keyed by whatever image identity the
# caller provides (path, inode, ...). Source images sit on slow storage.
_collection_cache = ExtractionCache()


def _extract_xmp_data(hdr: ExifHeader, fh: BinaryIO):
    # Easy we already have them
    xmp_tag = hdr.tags.get("Image ApplicationNotes")
    if xmp_tag:
        logger.debug("XMP present in Exif")
        xmp_bytes = bytes(xmp_tag.values)
    # We need to look in the entire file for the XML
    else:
        xmp_bytes = find_xmp_data(fh)
    if xmp_bytes:
        hdr.parse_xmp(xmp_bytes)


def _run_extraction(
    fh: BinaryIO,
    options: ExtractionOptions,
    strict: bool = False,
    debug: bool = False,
    truncate_tags: bool = True,
    only_ifds: Optional[Set[str]] = None,
    fallback_tags: Optional[Dict[str, Any]] = None,
) -> ExifHeader:
    """
    Scan the file behind ``fh`` according to ``options``.

    ``fallback_tags`` supplies pointers (e.g. ``Image ExifOffset``) collected
    by an earlier scan when a restricted re-read stops before reaching them.
    """
    offset, endian_bytes, fake_exif = determine_type(fh)

    endian_str, endian_type = get_endian_str(endian_bytes)
    # deal with the EXIF info we found
    logger.debug("Endian format is %s (%s)", endian_str, endian_type)

    hdr = ExifHeader(
        fh,
        endian_str,
        offset,
        fake_exif,
        strict,
        debug,
        options.details,
        truncate_tags,
    )
    thumb_ifd = 0
    ctr = 0
    for ifd in hdr.list_ifd():
        if ctr == 0:
            ifd_name = "Image"
        elif ctr == 1:
            ifd_name = "Thumbnail"
            thumb_ifd = ifd
        else:
            ifd_name = "IFD %d" % ctr
        if only_ifds is None or ifd_name in only_ifds:
            logger.debug("IFD %d (%s) at offset %s:", ctr, ifd_name, ifd)
            hdr.dump_ifd(ifd=ifd, ifd_name=ifd_name, stop_tag=options.stop_tag)
        ctr += 1
    # EXIF IFD
    exif_off = hdr.tags.get("Image ExifOffset")
    if exif_off is None and fallback_tags:
        exif_off = fallback_tags.get("Image ExifOffset")
    if exif_off and (only_ifds is None or "EXIF" in only_ifds):
        logger.debug("Exif SubIFD at offset %s:", exif_off.values[0])
        hdr.dump_ifd(ifd=exif_off.values[0], ifd_name="EXIF", stop_tag=options.stop_tag)

    # EXIF SubIFD
    sub_ifds = hdr.tags.get("Image SubIFDs")
    if sub_ifds is None and fallback_tags:
        sub_ifds = fallback_tags.get("Image SubIFDs")
    if options.details and sub_ifds:
        for subifd_id, subifd_offset in enumerate(sub_ifds.values):
            ifd_name = f"EXIF SubIFD{subifd_id}"
            if only_ifds is None or ifd_name in only_ifds:
                logger.debug("Exif SubIFD%d at offset %d:", subifd_id, subifd_offset)
                hdr.dump_ifd(
                    ifd=subifd_offset,
                    ifd_name=ifd_name,
                    stop_tag=options.stop_tag,
                )

    # deal with MakerNote contained in EXIF IFD
    # (Some apps use MakerNote tags but do not use a format for which we
    # have a description, do not process these).
    makernote_in_scope = options.details and (
        only_ifds is None or MAKERNOTE_IFD in only_ifds
    )
    if makernote_in_scope and "EXIF MakerNote" in hdr.tags and "Image Make" in hdr.tags:
        try:
            hdr.decode_maker_note()
        except ValueError as err:
            if not strict:
                logger.debug("Failed to decode EXIF MakerNote: %s", str(err))
            else:
                raise err

    if makernote_in_scope and MAKERNOTE_IFD not in (
        hdr.complete_ifds | hdr.truncated_ifds
    ):
        # The private directory was inspected and has no readable entries
        # (no MakerNote blob, unknown vendor): proven absence, not truncation.
        hdr.complete_ifds.add(MAKERNOTE_IFD)

    # extract thumbnails
    if thumb_ifd and options.thumbnail:
        hdr.extract_tiff_thumbnail(thumb_ifd)
        hdr.extract_jpeg_thumbnail()

    # parse XMP tags (experimental)
    if options.xmp and options.details:
        _extract_xmp_data(hdr=hdr, fh=fh)

    return hdr


def _coverage_from_header(hdr: ExifHeader, options: ExtractionOptions) -> ScanCoverage:
    return ScanCoverage(
        options=options,
        tags=hdr.tags,
        complete_ifds=frozenset(hdr.complete_ifds),
        truncated_ifds=frozenset(hdr.truncated_ifds),
        thumbnail_done=options.thumbnail,
        xmp_done=options.xmp,
    )


def _apply_projection(
    tags: Dict[str, Any], options: ExtractionOptions
) -> Dict[str, Any]:
    if options.tag_filter is None:
        return tags
    return {key: tags[key] for key in options.tag_filter if key in tags}


def process_file(
    fh: BinaryIO,
    stop_tag: str = DEFAULT_STOP_TAG,
    details=True,
    strict=False,
    debug=False,
    truncate_tags=True,
    auto_seek=True,
    extract_thumbnail=True,
    builtin_types=False,
    options: Optional[ExtractionOptions] = None,
) -> Dict[str, Any]:
    """
    Process an image file to extract EXIF metadata.

    This is the function that has to deal with all the arbitrary nasty bits
    of the EXIF standard.

    :param fh: the file to process, must be opened in binary mode.
    :param stop_tag: Stop processing when the given tag is retrieved.
    :param details: If `True`, process MakerNotes.
    :param strict: If `True`, raise exceptions on errors.
    :param debug: Output debug information.
    :param truncate_tags: If `True`, truncate the `printable` tag output.
        There is no effect on tag `values`.
    :param auto_seek: If `True`, automatically `seek` to the start of the file.
    :param extract_thumbnail: If `True`, extract the JPEG thumbnail.
        The thumbnail is not always present in the EXIF metadata.
    :param builtin_types: If `True`, convert tags to standard Python types.
    :param options: extraction options. When provided, ``details``,
        ``extract_thumbnail`` and ``stop_tag`` are taken from it.

    :returns: A `dict` containing the EXIF metadata.
        The keys are a string in the format `"IFD_NAME TAG_NAME"`.
        If `builtin_types` is `False`, the value will be a `IfdTag` class, or bytes.
        IF `builtin_types` is `True`, the value will be a standard Python type.
    """

    if auto_seek:
        fh.seek(0)

    if options is None:
        options = ExtractionOptions.legacy(
            details=details,
            extract_thumbnail=extract_thumbnail,
            stop_tag=stop_tag,
        )

    try:
        hdr = _run_extraction(
            fh,
            options,
            strict=strict,
            debug=debug,
            truncate_tags=truncate_tags,
        )
    except ExifNotFound as err:
        logger.warning(err)
        return {}
    except InvalidExif as err:
        logger.debug(err)
        return {}

    tags = _apply_projection(hdr.tags, options)

    if builtin_types:
        return convert_types(tags)

    return tags


def _reread_scopes(decision: CoverageDecision) -> Set[str]:
    """IFDs that must be re-opened, including their dependencies."""
    scopes = set(decision.reread_ifds)

    # Sub-IFDs are only reached through pointer tags in a parent IFD.
    while True:
        added = set()
        for ifd_name in scopes:
            if ifd_name == "EXIF" or ifd_name.startswith("EXIF SubIFD"):
                added.add("Image")
            if ifd_name in ("GPS", "Interoperability", MAKERNOTE_IFD):
                added.add("EXIF")
        if MAKERNOTE_IFD in scopes:
            # MakerNote decoding needs the blob and the camera make.
            added.add("Image")
        if decision.reread_thumbnail:
            # Thumbnail bytes hang off the Thumbnail IFD.
            added.add("Thumbnail")
        if added <= scopes:
            return scopes
        scopes |= added


def process_cached(
    identity: Hashable,
    fh: BinaryIO,
    options: ExtractionOptions,
    cache: Optional[ExtractionCache] = None,
    auto_seek: bool = True,
    builtin_types: bool = False,
) -> Dict[str, Any]:
    """
    Extract metadata using the collection cache.

    A coverage check decides per image and per IFD whether the previous scan
    reached far enough: complete IFDs are reused (a missing tag is a proven
    absence), truncated IFDs and never-opened private directories go back to
    the slow disk. Narrow requests are projected out of the cached tags
    without any re-read.

    :param identity: stable key identifying the image behind ``fh``.
    :param fh: the file to process, opened in binary mode.
    :param options: requested extraction options, optionally with a
        ``tag_filter`` projection.
    :param cache: cache to use; defaults to the shared collection cache.
    :param auto_seek: seek to the start of the file before scanning.
    :param builtin_types: convert the returned tags to standard Python types.
    """
    if cache is None:
        cache = _collection_cache
    if auto_seek:
        fh.seek(0)

    try:
        cached = cache.lookup(identity, options)

        if cached is None:
            hdr = _run_extraction(fh, options)
            coverage = _coverage_from_header(hdr, options)
            cache.store(identity, coverage)
            tags = dict(coverage.tags)
        else:
            coverage, decision = cached
            tags = _reuse_or_reread(identity, fh, options, coverage, decision, cache)
    except ExifNotFound as err:
        logger.warning(err)
        return {}
    except InvalidExif as err:
        logger.debug(err)
        return {}

    projected = _apply_projection(tags, options)
    if builtin_types:
        return convert_types(projected)
    return projected


def _reuse_or_reread(
    identity: Hashable,
    fh: BinaryIO,
    options: ExtractionOptions,
    coverage: ScanCoverage,
    decision: CoverageDecision,
    cache: ExtractionCache,
) -> Dict[str, Any]:
    """Project cached evidence and re-read only the insufficient scopes."""
    if decision.fully_reusable:
        return dict(coverage.tags)

    fh.seek(0)
    scopes = _reread_scopes(decision)

    # Only open the phases whose evidence is actually missing: a thumbnail
    # or XMP block already cached must not be read off the slow disk again.
    reread_options = ExtractionOptions(
        details=options.details,
        thumbnail=options.thumbnail and decision.reread_thumbnail,
        xmp=options.xmp and decision.reread_xmp,
        stop_tag=options.stop_tag,
    )

    if scopes:
        hdr = _run_extraction(
            fh,
            reread_options,
            only_ifds=scopes,
            fallback_tags=coverage.tags,
        )
        extra_tags = hdr.tags
        complete_ifds = hdr.complete_ifds
        truncated_ifds = hdr.truncated_ifds
    else:
        # Only out-of-band XMP is missing: no IFD has to be walked again.
        hdr = _header_for_xmp(fh, coverage, options.details)
        if reread_options.xmp:
            _extract_xmp_data(hdr=hdr, fh=fh)
        extra_tags = {
            key: value for key, value in hdr.tags.items() if key not in coverage.tags
        }
        complete_ifds = set()
        truncated_ifds = set()

    merged = cache.update(
        identity,
        options=options,
        extra_tags=extra_tags,
        complete_ifds=complete_ifds,
        truncated_ifds=truncated_ifds,
        thumbnail_done=options.thumbnail,
        xmp_done=options.xmp,
    )
    return dict(merged.tags)


def _header_for_xmp(fh: BinaryIO, coverage: ScanCoverage, details: bool) -> ExifHeader:
    """Build a header carrying the cached tags, ready for an XMP lookup."""
    offset, endian_bytes, fake_exif = determine_type(fh)
    endian_str, _ = get_endian_str(endian_bytes)
    hdr = ExifHeader(fh, endian_str, offset, fake_exif, False, False, details, True)
    hdr.tags = dict(coverage.tags)
    return hdr


def clear_cache(cache: Optional[ExtractionCache] = None) -> None:
    """Drop cached collection scans (the shared cache by default)."""
    target = _collection_cache if cache is None else cache
    target.clear()


def file_identity(path: Union[str, "os.PathLike[str]"]) -> Hashable:
    """Build a cache identity from a path, size and modification time."""
    stat = os.stat(path)
    return str(path), stat.st_size, stat.st_mtime_ns
