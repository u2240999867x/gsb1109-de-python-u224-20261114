"""
Extraction options and scan-coverage bookkeeping.

Source images live on slow storage, so re-reading one is expensive. A scan
remembers how far into the image it actually got:

* an IFD that was read entry after entry up to its end is *complete*
  evidence, so a tag missing from it is genuinely absent;
* an IFD where the scan stopped early (stop tag reached, unreadable slice)
  is *truncated* evidence: anything past the stopping point is unknown and
  has to go back to the slow disk.

Later requests reuse everything covered by complete evidence, project the
requested keys out of the cached tags when the request is narrower, and only
re-read the scopes whose evidence is insufficient.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Hashable, Optional, Set, Tuple

from exifread.tags import DEFAULT_STOP_TAG

# Pseudo-IFD name for the camera-private MakerNote directory.
MAKERNOTE_IFD = "MakerNote"


@dataclass(frozen=True)
class ExtractionOptions:
    """
    What a caller wants out of an image.

    :param details: process the camera-private MakerNote directory.
    :param thumbnail: extract the embedded JPEG/TIFF thumbnail bytes.
    :param xmp: look for XMP metadata stored outside of the EXIF tags.
    :param stop_tag: stop scanning an IFD once this tag is retrieved.
    :param tag_filter: when set, the answer is projected to these
        ``"IFD_NAME TAG_NAME"`` keys; ``None`` returns every tag.
    """

    details: bool = True
    thumbnail: bool = True
    xmp: bool = False
    stop_tag: str = DEFAULT_STOP_TAG
    tag_filter: Optional[FrozenSet[str]] = None

    @classmethod
    def legacy(
        cls,
        details: bool = True,
        extract_thumbnail: bool = True,
        stop_tag: str = DEFAULT_STOP_TAG,
    ) -> "ExtractionOptions":
        """Build options from the legacy ``process_file`` keyword arguments."""
        return cls(details=details, thumbnail=extract_thumbnail, stop_tag=stop_tag)

    def with_filter(self, tag_filter: Optional[Set[str]]) -> "ExtractionOptions":
        """Return a copy restricted to the given projection keys."""
        frozen = frozenset(tag_filter) if tag_filter is not None else None
        return ExtractionOptions(
            details=self.details,
            thumbnail=self.thumbnail,
            xmp=self.xmp,
            stop_tag=self.stop_tag,
            tag_filter=frozen,
        )


@dataclass
class CoverageDecision:
    """
    Result of comparing a request against a cached :class:`ScanCoverage`.

    :param projection: keys to keep when projecting the cached tags,
        ``None`` when the request has no filter.
    :param reread_ifds: IFD names with insufficient evidence that must be
        re-read on the slow disk.
    :param reread_thumbnail: the thumbnail bytes were never fully read.
    :param reread_xmp: the out-of-band XMP data was never read.
    """

    projection: Optional[Set[str]]
    reread_ifds: Set[str] = field(default_factory=set)
    reread_thumbnail: bool = False
    reread_xmp: bool = False

    @property
    def fully_reusable(self) -> bool:
        """True when the cache answers the request without any disk access."""
        return (
            not self.reread_ifds and not self.reread_thumbnail and not self.reread_xmp
        )


@dataclass
class ScanCoverage:
    """
    Cache entry for one image: the extracted tags together with a record of
    how far the scan actually reached.

    :param options: extraction options used to produce this entry.
    :param tags: every tag the scan actually collected (unfiltered).
    :param complete_ifds: IFD names scanned from first entry to last.
    :param truncated_ifds: IFD names where the scan stopped early.
    :param thumbnail_done: the thumbnail phase ran to completion.
    :param xmp_done: the out-of-band XMP lookup ran to completion.
    """

    options: ExtractionOptions
    tags: Dict[str, Any]
    complete_ifds: FrozenSet[str]
    truncated_ifds: FrozenSet[str] = frozenset()
    thumbnail_done: bool = False
    xmp_done: bool = False

    def _ifd_of_key(self, key: str) -> Optional[str]:
        """Return the scanned IFD name a ``"IFD TAG"`` key belongs to."""
        known = self.complete_ifds | self.truncated_ifds
        matches = [ifd for ifd in known if key == ifd or key.startswith(ifd + " ")]
        if not matches:
            return None
        return max(matches, key=len)

    def covers(self, requested: ExtractionOptions) -> CoverageDecision:
        """Decide which parts of ``requested`` the cache can answer."""
        reread_ifds: Set[str] = set()

        if requested.tag_filter is None:
            # An unfiltered request needs every tag, so every directory the
            # previous scan did not finish has to be read again.
            reread_ifds.update(self.truncated_ifds)
        else:
            # A narrowed request only needs evidence for its missing keys:
            # a truncated directory speaks only for the tags it reached.
            for key in requested.tag_filter - set(self.tags):
                owner = self._ifd_of_key(key)
                if owner is None:
                    if requested.details:
                        reread_ifds.add(MAKERNOTE_IFD)
                elif owner not in self.complete_ifds:
                    reread_ifds.add(owner)
                # owner is complete: positive evidence the tag is absent.

        # Asking for the private directory after a quick scan means entering
        # a directory the previous scan never opened; the MakerNote blob is
        # itself an EXIF tag quick scans skip, so its IFD must be re-read too.
        if requested.details and not self.options.details:
            reread_ifds.add(MAKERNOTE_IFD)
            if "EXIF MakerNote" not in self.tags:
                reread_ifds.add("EXIF")

        return CoverageDecision(
            projection=(
                set(requested.tag_filter) if requested.tag_filter is not None else None
            ),
            reread_ifds=reread_ifds,
            reread_thumbnail=requested.thumbnail and not self.thumbnail_done,
            reread_xmp=requested.xmp and not self.xmp_done,
        )


class ExtractionCache:
    """
    Collection cache keyed by an image identity (e.g. path plus size and
    modification time, chosen by the caller).
    """

    def __init__(self) -> None:
        self._entries: Dict[Hashable, ScanCoverage] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(
        self, identity: Hashable, options: ExtractionOptions
    ) -> Optional[Tuple[ScanCoverage, CoverageDecision]]:
        """Return the cached entry and its coverage decision, if any."""
        coverage = self._entries.get(identity)
        if coverage is None:
            return None
        return coverage, coverage.covers(options)

    def store(self, identity: Hashable, coverage: ScanCoverage) -> None:
        """Cache a fresh full scan."""
        self._entries[identity] = coverage

    def update(
        self,
        identity: Hashable,
        options: ExtractionOptions,
        extra_tags: Dict[str, Any],
        complete_ifds: Set[str],
        truncated_ifds: Set[str],
        thumbnail_done: bool,
        xmp_done: bool,
    ) -> ScanCoverage:
        """Merge a partial re-read into the cached entry."""
        previous = self._entries[identity]
        tags = dict(previous.tags)
        tags.update(extra_tags)
        new_complete = previous.complete_ifds | frozenset(complete_ifds)
        # An IFD re-read to its end no longer counts as truncated evidence.
        new_truncated = (
            previous.truncated_ifds - frozenset(complete_ifds)
        ) | frozenset(truncated_ifds)
        coverage = ScanCoverage(
            options=options,
            tags=tags,
            complete_ifds=new_complete,
            truncated_ifds=new_truncated,
            thumbnail_done=previous.thumbnail_done or thumbnail_done,
            xmp_done=previous.xmp_done or xmp_done,
        )
        self._entries[identity] = coverage
        return coverage

    def clear(self) -> None:
        """Drop every cached entry."""
        self._entries.clear()
