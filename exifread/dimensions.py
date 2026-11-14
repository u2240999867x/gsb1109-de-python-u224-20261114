"""
Compute physical image dimensions in millimetres from Exif tags.

Pixel counts (``ImageWidth`` / ``ImageLength``) and resolution
(``XResolution`` / ``YResolution``) live in independent IFDs. A TIFF usually
holds one IFD for the main image and another one for a reduced-resolution
preview, each with its own values. The tags of a single physical size are
therefore always taken as a set from the same IFD: a value missing from one
IFD is never borrowed from another one.

Resolutions are stored as rationals, so the arithmetic keeps the exact
:class:`fractions.Fraction` until the final millimetre value is produced.
Only the axes that can actually be computed are returned; an IFD that lacks
e.g. ``YResolution`` yields ``height_mm = None`` rather than a guessed value.
"""

from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple, Union

from exifread.core.ifd_tag import IfdTag
from exifread.utils import Ratio

# Exact conversion factors kept as fractions to preserve rational accuracy.
MM_PER_INCH: Fraction = Fraction(127, 5)  # 25.4 mm
MM_PER_CENTIMETRE: Fraction = Fraction(10, 1)

# ResolutionUnit tag (0x0128) values.
UNIT_NOT_ABSOLUTE = 1
UNIT_INCH = 2
UNIT_CENTIMETRE = 3

# Orientation tag (0x0112) values that swap the displayed width and height.
SWAP_ORIENTATIONS = frozenset((5, 6, 7, 8))

# Printable values of the ResolutionUnit enum when tags are serialized.
_UNIT_LABELS = {
    UNIT_INCH: "Pixels/Inch",
    UNIT_CENTIMETRE: "Pixels/Centimeter",
}

# Printable values of the Orientation enum when tags are serialized.
_ORIENTATION_LABELS = {
    1: "Horizontal (normal)",
    2: "Mirrored horizontal",
    3: "Rotated 180",
    4: "Mirrored vertical",
    5: "Mirrored horizontal then rotated 90 CCW",
    6: "Rotated 90 CW",
    7: "Mirrored horizontal then rotated 90 CW",
    8: "Rotated 90 CCW",
}

# Tags that define a physical size, without the IFD name prefix.
_SIZE_TAGS = (
    "ImageWidth",
    "ImageLength",
    "XResolution",
    "YResolution",
    "ResolutionUnit",
    "Orientation",
)

Scalar = Union[int, float, str, Ratio, None]


class PhysicalDimensions:
    """
    Physical size of one image IFD, expressed in millimetres.

    ``width_mm`` and ``height_mm`` follow the actual display order: when the
    orientation swaps the axes, the stored width/height are exchanged. An axis
    is ``None`` when it cannot be computed from the tags of this IFD alone.
    """

    __slots__ = ("ifd_name", "width_mm", "height_mm", "orientation", "resolution_unit")

    def __init__(
        self,
        ifd_name: str,
        width_mm: Optional[float],
        height_mm: Optional[float],
        orientation: Optional[int],
        resolution_unit: Optional[int],
    ) -> None:
        self.ifd_name = ifd_name
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.orientation = orientation
        self.resolution_unit = resolution_unit

    @property
    def is_complete(self) -> bool:
        """``True`` when both axes could be computed."""
        return self.width_mm is not None and self.height_mm is not None

    def as_dict(self) -> Dict[str, Any]:
        """Return the dimensions as a plain, serializable dictionary."""
        return {
            "ifd_name": self.ifd_name,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "orientation": self.orientation,
            "resolution_unit": self.resolution_unit,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PhysicalDimensions):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return (
            "PhysicalDimensions(ifd_name=%r, width_mm=%r, height_mm=%r, "
            "orientation=%r, resolution_unit=%r)"
            % (
                self.ifd_name,
                self.width_mm,
                self.height_mm,
                self.orientation,
                self.resolution_unit,
            )
        )


def _first_value(tag: Any) -> Scalar:
    """Return the first scalar of an IfdTag, or a native serialized value."""
    if tag is None:
        return None
    if isinstance(tag, IfdTag):
        values = tag.values
        return values[0] if values else None
    if isinstance(tag, (list, tuple)):
        return tag[0] if tag else None
    return tag


def _as_int(tag: Any) -> Optional[int]:
    """Extract an integral pixel count, or ``None`` when unavailable."""
    value = _first_value(tag)
    if isinstance(value, Ratio):
        if value.denominator == 0:
            return None
        return int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_fraction(tag: Any) -> Optional[Fraction]:
    """Extract an exact positive rational resolution, or ``None``."""
    value = _first_value(tag)
    if isinstance(value, Ratio):
        if value.denominator == 0:
            return None
        return Fraction(value.numerator, value.denominator)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == 0:
        return None
    return Fraction(value)


def _unit_scale(
    tag: Any,
) -> Tuple[Optional[Fraction], Optional[int]]:
    """
    Return ``(millimetres per resolution unit, unit code)``.

    The TIFF default is pixels per inch when the tag is absent. A unit that
    carries no physical meaning (``Not Absolute`` or unknown) yields ``None``.
    """
    value = _first_value(tag)
    if value is None:
        return MM_PER_INCH, None
    if isinstance(value, Ratio):
        value = int(value)
    if isinstance(value, str):
        for code, label in _UNIT_LABELS.items():
            if value == label:
                value = code
                break
        else:
            return None, None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, None
    if value == UNIT_INCH:
        return MM_PER_INCH, UNIT_INCH
    if value == UNIT_CENTIMETRE:
        return MM_PER_CENTIMETRE, UNIT_CENTIMETRE
    return None, value


def _orientation(tag: Any) -> Optional[int]:
    """Extract the orientation code (1-8), or ``None``."""
    value = _first_value(tag)
    if isinstance(value, Ratio):
        value = int(value)
    if isinstance(value, str):
        for code, label in _ORIENTATION_LABELS.items():
            if value == label:
                value = code
                break
        else:
            return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 8 else None


def _axis_mm(
    pixels: Optional[int],
    resolution: Optional[Fraction],
    mm_per_unit: Optional[Fraction],
) -> Optional[float]:
    """Compute one axis in millimetres from same-IFD pixel count and resolution."""
    if pixels is None or resolution is None or mm_per_unit is None:
        return None
    if resolution <= 0:
        return None
    exact = Fraction(pixels) / resolution * mm_per_unit
    return float(exact)


def _ifd_names(tags: Dict[str, Any]) -> List[str]:
    """Collect IFD name prefixes of size-related tags, in parse order."""
    names: List[str] = []
    seen = set()
    for key in tags:
        for tag_name in _SIZE_TAGS:
            suffix = " " + tag_name
            if key.endswith(suffix):
                ifd_name = key[: -len(suffix)]
                if ifd_name and ifd_name not in seen:
                    seen.add(ifd_name)
                    names.append(ifd_name)
    return names


def get_physical_dimensions(
    tags: Dict[str, Any],
) -> Dict[str, PhysicalDimensions]:
    """
    Compute the physical size in millimetres of every image IFD.

    :param tags: the tag dictionary returned by
        :func:`exifread.process_file`, either as ``IfdTag`` objects or
        converted to native Python types with ``builtin_types=True``.
    :returns: a mapping of IFD name (e.g. ``"Image"``, ``"Thumbnail"``) to its
        :class:`PhysicalDimensions`. Each IFD is evaluated independently, so a
        missing resolution in one IFD never reuses another IFD's value.
    """
    result: Dict[str, PhysicalDimensions] = {}

    for ifd_name in _ifd_names(tags):
        prefix = ifd_name + " "

        width_px = _as_int(tags.get(prefix + "ImageWidth"))
        height_px = _as_int(tags.get(prefix + "ImageLength"))
        x_resolution = _as_fraction(tags.get(prefix + "XResolution"))
        y_resolution = _as_fraction(tags.get(prefix + "YResolution"))
        mm_per_unit, unit_code = _unit_scale(tags.get(prefix + "ResolutionUnit"))
        orientation = _orientation(tags.get(prefix + "Orientation"))

        width_mm = _axis_mm(width_px, x_resolution, mm_per_unit)
        height_mm = _axis_mm(height_px, y_resolution, mm_per_unit)

        # Deliver in actual display order when the orientation swaps the axes.
        if (
            orientation in SWAP_ORIENTATIONS
            and width_mm is not None
            and height_mm is not None
        ):
            width_mm, height_mm = height_mm, width_mm

        result[ifd_name] = PhysicalDimensions(
            ifd_name=ifd_name,
            width_mm=width_mm,
            height_mm=height_mm,
            orientation=orientation,
            resolution_unit=unit_code,
        )

    return result
