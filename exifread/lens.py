"""
Match a photograph against a catalogue of lenses.

A photograph carries an EXIF ``LensSpecification`` tag made of four rational
numbers:

    [min focal length, max focal length, min f-number, max f-number]

together with the actual ``FocalLength`` and ``FNumber`` used for the shot.
Lens names are frequently missing, so matching must rely on the numbers.

The matcher classifies each catalogue entry as one of :data:`EXCLUDED`,
:data:`COMPATIBLE` or :data:`INSUFFICIENT`, and reports which end of every
zoom range supplied the evidence.

Design rules, deliberately conservative:

* Only the two endpoints of a zoom range are known. No aperture curve is given
  for intermediate focal lengths, so a value strictly between the endpoints can
  never be used to exclude a candidate.
* The catalogue stores *maximum* apertures (the widest aperture the lens can
  reach). A photograph taken stopped down (a larger f-number) is normal and must
  never be read as the lens being unable to open wide enough.
* A single field with a missing denominator (the EXIF ``0/0`` placeholder) only
  invalidates that field; every other usable piece of evidence is kept.
"""

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Any, List, NamedTuple, Optional, Sequence, Union

from exifread.core.ifd_tag import IfdTag
from exifread.utils import Ratio

#: Tag names as produced by ``exifread.process_file``.
LENS_SPECIFICATION_TAG = "EXIF LensSpecification"
FOCAL_LENGTH_TAG = "EXIF FocalLength"
FNUMBER_TAG = "EXIF FNumber"
LENS_MODEL_TAG = "EXIF LensModel"


class MatchResult(str, Enum):
    """Classification of a catalogue lens against a photograph."""

    #: The lens provably cannot have taken the photograph.
    EXCLUDED = "excluded"
    #: No evidence contradicts the lens.
    COMPATIBLE = "compatible"
    #: Not enough valid evidence to decide either way.
    INSUFFICIENT = "insufficient"


#: Human-readable identifiers for the four LensSpecification fields, in order.
SPEC_FIELD_NAMES = (
    "minimum focal length",
    "maximum focal length",
    "minimum f-number",
    "maximum f-number",
)

#: Numeric representations accepted for catalogue and photograph values.
Number = Union[Fraction, int, float]


class Constraint(NamedTuple):
    """
    A single piece of evidence used in a decision.

    :param field: name of the LensSpecification field involved.
    :param end: ``"wide"`` or ``"tele"`` end of the zoom/aperture range.
    :param photo: value recorded by the photograph, or ``None`` when missing.
    :param lens: value advertised by the catalogue lens, or ``None`` when missing.
    :param detail: explanation of how the value was used.
    """

    field: str
    end: str
    photo: Optional[Fraction]
    lens: Optional[Fraction]
    detail: str


@dataclass
class LensMatch:
    """
    Outcome of matching one catalogue lens against one photograph.

    :param name: catalogue lens name, may be ``None``.
    :param result: the :class:`MatchResult` classification.
    :param constraints: every constraint that was evaluated, including the
        endpoint it came from and how it was used.
    :param reasons: human-readable summary of the decisive evidence.
    """

    name: Optional[str]
    result: MatchResult
    constraints: List[Constraint] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    @property
    def used_ends(self) -> List[str]:
        """Sorted, de-duplicated ends that contributed usable evidence."""
        return sorted({constraint.end for constraint in self.constraints})


@dataclass
class CatalogueLens:
    """
    One entry of the lens catalogue.

    The catalogue only documents the endpoints of each range:

    :param name: free-form lens name, may be empty or ``None``.
    :param min_focal_length: shortest focal length in millimetres.
    :param max_focal_length: longest focal length in millimetres.
    :param wide_max_aperture: widest available f-number at the wide end.
    :param tele_max_aperture: widest available f-number at the tele end.
    """

    min_focal_length: Number
    max_focal_length: Number
    wide_max_aperture: Number
    tele_max_aperture: Number
    name: Optional[str] = None


def _to_fraction(value: Any) -> Optional[Fraction]:
    """
    Normalize any supported numeric representation to an exact :class:`Fraction`.

    Returns ``None`` for missing values and for the EXIF ``0/0`` placeholder,
    which marks an unused field rather than a real measurement. Focal lengths
    and f-numbers are physically strictly positive, so serialized ``0`` values
    (the builtin-types form of the placeholder) are treated as missing too.
    """
    if value is None:
        return None
    if isinstance(value, Ratio):
        # Ratio swallows ZeroDivisionError and stores the raw parts, so a
        # 0/0 placeholder still reports a zero denominator here.
        if value.denominator == 0:
            return None
        fraction = Fraction(value.numerator, value.denominator)
    elif isinstance(value, Fraction):
        if value.denominator == 0:
            return None
        fraction = value
    elif isinstance(value, int):
        fraction = Fraction(value)
    elif isinstance(value, float):
        fraction = Fraction(value).limit_denominator()
    else:
        raise TypeError(f"Unsupported numeric value: {value!r}")
    return fraction if fraction > 0 else None


def _single_tag_value(values: Any) -> Any:
    """Unwrap the one-element lists exifread uses for scalar EXIF tags."""
    if isinstance(values, IfdTag):
        values = values.values
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


def _spec_values(spec_tag: Any) -> List[Optional[Fraction]]:
    """Extract the four LensSpecification rationals, tolerating gaps."""
    if isinstance(spec_tag, IfdTag):
        raw_values: Sequence[Any] = spec_tag.values
    elif isinstance(spec_tag, (list, tuple)):
        raw_values = spec_tag
    else:
        raise TypeError("LensSpecification must be an IfdTag or a sequence")
    values = [_to_fraction(item) for item in raw_values]
    return values[:4] if len(values) >= 4 else values + [None] * (4 - len(values))


class LensMatcher:
    """
    Match the lens evidence of one photograph against catalogue lenses.

    Construct it from the raw ``exifread`` tag dictionary, the serialized
    dictionary (``builtin_types=True``), or directly from the individual
    values::

        matcher = LensMatcher.from_tags(exifread.process_file(fh))
        matches = matcher.match_catalogue(catalogue)
    """

    def __init__(
        self,
        lens_specification: Any = None,
        focal_length: Any = None,
        fnumber: Any = None,
        lens_name: Optional[str] = None,
    ) -> None:
        spec = (
            _spec_values(lens_specification)
            if lens_specification is not None
            else [
                None,
                None,
                None,
                None,
            ]
        )
        self.spec_min_focal: Optional[Fraction] = spec[0]
        self.spec_max_focal: Optional[Fraction] = spec[1]
        self.spec_min_aperture: Optional[Fraction] = spec[2]
        self.spec_max_aperture: Optional[Fraction] = spec[3]
        self.focal_length: Optional[Fraction] = _to_fraction(
            _single_tag_value(focal_length)
        )
        self.fnumber: Optional[Fraction] = _to_fraction(_single_tag_value(fnumber))
        self.lens_name = lens_name

    @classmethod
    def from_tags(cls, tags: dict) -> "LensMatcher":
        """
        Build a matcher from the dictionary returned by ``process_file``.

        Works with both the default :class:`~exifread.core.ifd_tag.IfdTag`
        objects and the plain Python types produced with ``builtin_types=True``.
        """
        spec_tag = tags.get(LENS_SPECIFICATION_TAG)
        focal_tag = tags.get(FOCAL_LENGTH_TAG)
        fnumber_tag = tags.get(FNUMBER_TAG)
        name_tag = tags.get(LENS_MODEL_TAG)

        if isinstance(name_tag, IfdTag):
            lens_name = name_tag.values or None
        elif isinstance(name_tag, str):
            lens_name = name_tag.strip() or None
        else:
            lens_name = name_tag

        return cls(
            lens_specification=spec_tag,
            focal_length=focal_tag,
            fnumber=fnumber_tag,
            lens_name=lens_name,
        )

    def match_catalogue(self, catalogue: Sequence[CatalogueLens]) -> List[LensMatch]:
        """Classify every lens in ``catalogue`` against the photograph."""
        return [self.match_lens(lens) for lens in catalogue]

    def match_lens(self, lens: CatalogueLens) -> LensMatch:
        """
        Classify a single catalogue lens.

        Focal-length evidence comes first: a focal length outside the lens's
        zoom range is a hard exclusion. Aperture evidence can only exclude when
        the photograph actually demands more light than the lens can provide at
        a documented endpoint. Everything else is compatible, and a candidate
        with no usable evidence at all is reported as insufficient.
        """
        match = LensMatch(name=lens.name, result=MatchResult.INSUFFICIENT)

        excluded = self._check_focal_range(lens, match)
        if not excluded:
            excluded = self._check_aperture(lens, match)

        if excluded:
            match.result = MatchResult.EXCLUDED
        elif match.constraints:
            match.result = MatchResult.COMPATIBLE
        return match

    def _check_focal_range(self, lens: CatalogueLens, match: LensMatch) -> bool:
        """Return ``True`` (and record evidence) when focal length excludes."""
        shot_focal = self.focal_length
        lens_wide = _to_fraction(lens.min_focal_length)
        lens_tele = _to_fraction(lens.max_focal_length)
        photo_wide = self.spec_min_focal
        photo_tele = self.spec_max_focal

        if shot_focal is not None and lens_wide is not None and lens_tele is not None:
            if shot_focal < lens_wide:
                match.reasons.append(
                    f"shot focal length {_fmt(shot_focal)} mm is wider than the "
                    f"lens minimum {_fmt(lens_wide)} mm"
                )
                match.constraints.append(
                    Constraint(
                        SPEC_FIELD_NAMES[0],
                        "wide",
                        shot_focal,
                        lens_wide,
                        "shot focal length below the wide-end limit",
                    )
                )
                return True
            if shot_focal > lens_tele:
                match.reasons.append(
                    f"shot focal length {_fmt(shot_focal)} mm is longer than the "
                    f"lens maximum {_fmt(lens_tele)} mm"
                )
                match.constraints.append(
                    Constraint(
                        SPEC_FIELD_NAMES[1],
                        "tele",
                        shot_focal,
                        lens_tele,
                        "shot focal length above the tele-end limit",
                    )
                )
                return True
            # The shot sits inside (or on) the documented zoom range. Only the
            # endpoints were checked; intermediate values need no interpolation.
            match.constraints.append(
                Constraint(
                    "shot focal length",
                    _focal_end(shot_focal, lens_wide, lens_tele),
                    shot_focal,
                    None,
                    "within the lens zoom range",
                )
            )

        # Cross-check the photograph's own LensSpecification endpoints against
        # the catalogue range. These are independent corroborating evidence.
        if self._check_endpoint_against_range(
            photo_wide,
            lens_wide,
            lens_tele,
            SPEC_FIELD_NAMES[0],
            "wide",
            match,
        ):
            return True
        return self._check_endpoint_against_range(
            photo_tele,
            lens_wide,
            lens_tele,
            SPEC_FIELD_NAMES[1],
            "tele",
            match,
        )

    def _check_endpoint_against_range(
        self,
        endpoint: Optional[Fraction],
        lens_wide: Optional[Fraction],
        lens_tele: Optional[Fraction],
        field_name: str,
        end: str,
        match: LensMatch,
    ) -> bool:
        """Return ``True`` when a LensSpecification endpoint excludes."""
        if endpoint is None or lens_wide is None or lens_tele is None:
            return False
        if endpoint < lens_wide or endpoint > lens_tele:
            match.reasons.append(
                f"photograph {field_name} {_fmt(endpoint)} mm falls outside the "
                f"lens range {_fmt(lens_wide)}-{_fmt(lens_tele)} mm"
            )
            match.constraints.append(
                Constraint(
                    field_name,
                    end,
                    endpoint,
                    lens_wide if end == "wide" else lens_tele,
                    "LensSpecification endpoint outside the lens range",
                )
            )
            return True
        match.constraints.append(
            Constraint(
                field_name,
                end,
                endpoint,
                lens_wide if end == "wide" else lens_tele,
                "LensSpecification endpoint inside the lens range",
            )
        )
        return False

    def _check_aperture(self, lens: CatalogueLens, match: LensMatch) -> bool:
        """Return ``True`` (and record evidence) when aperture excludes."""
        lens_wide_aperture = _to_fraction(lens.wide_max_aperture)
        lens_tele_aperture = _to_fraction(lens.tele_max_aperture)
        photo_wide_aperture = self.spec_min_aperture
        photo_tele_aperture = self.spec_max_aperture
        shot_fnumber = self.fnumber

        # Compare the photograph's own maximum-aperture endpoints with the
        # catalogue. A smaller f-number means a faster (wider) lens.
        if self._check_aperture_endpoint(
            photo_wide_aperture,
            lens_wide_aperture,
            SPEC_FIELD_NAMES[2],
            "wide",
            match,
        ):
            return True
        if self._check_aperture_endpoint(
            photo_tele_aperture,
            lens_tele_aperture,
            SPEC_FIELD_NAMES[3],
            "tele",
            match,
        ):
            return True

        # The shot f-number only excludes at a documented endpoint, because no
        # aperture curve exists for intermediate focal lengths. Stopping down
        # (a larger f-number than the maximum aperture) is always fine.
        if shot_fnumber is None:
            return False
        focal_end = self._shot_focal_end(lens)
        if focal_end is None:
            # Strictly inside the zoom range (or missing focal data): the
            # catalogue gives no aperture curve here, so the shot f-number must
            # not be interpolated and cannot exclude the candidate.
            return False
        lens_end_aperture = (
            lens_wide_aperture if focal_end == "wide" else lens_tele_aperture
        )
        if lens_end_aperture is None:
            return False
        if shot_fnumber < lens_end_aperture:
            match.reasons.append(
                f"shot f/{_fmt(shot_fnumber)} at the {focal_end} end is wider than "
                f"the lens maximum aperture f/{_fmt(lens_end_aperture)}"
            )
            match.constraints.append(
                Constraint(
                    "shot f-number",
                    focal_end,
                    shot_fnumber,
                    lens_end_aperture,
                    "shot demands a wider aperture than this end provides",
                )
            )
            return True
        match.constraints.append(
            Constraint(
                "shot f-number",
                focal_end,
                shot_fnumber,
                lens_end_aperture,
                "within the maximum aperture, including stopping down",
            )
        )
        return False

    def _check_aperture_endpoint(
        self,
        photo_endpoint: Optional[Fraction],
        lens_endpoint: Optional[Fraction],
        field_name: str,
        end: str,
        match: LensMatch,
    ) -> bool:
        """Return ``True`` when a LensSpecification aperture excludes."""
        if photo_endpoint is None or lens_endpoint is None:
            return False
        if photo_endpoint < lens_endpoint:
            match.reasons.append(
                f"photograph {field_name} f/{_fmt(photo_endpoint)} is faster than "
                f"the catalogue f/{_fmt(lens_endpoint)} at the {end} end"
            )
            match.constraints.append(
                Constraint(
                    field_name,
                    end,
                    photo_endpoint,
                    lens_endpoint,
                    "LensSpecification aperture wider than the lens can open",
                )
            )
            return True
        match.constraints.append(
            Constraint(
                field_name,
                end,
                photo_endpoint,
                lens_endpoint,
                "LensSpecification aperture no wider than the lens maximum",
            )
        )
        return False

    def _shot_focal_end(self, lens: CatalogueLens) -> Optional[str]:
        """
        Return which documented endpoint the shot was taken at.

        ``None`` means the focal length is missing, strictly inside the zoom
        range, or the catalogue endpoints are unknown: in those cases no
        aperture comparison is legitimate.
        """
        shot_focal = self.focal_length
        lens_wide = _to_fraction(lens.min_focal_length)
        lens_tele = _to_fraction(lens.max_focal_length)
        if shot_focal is None or lens_wide is None or lens_tele is None:
            return None
        if shot_focal == lens_wide:
            return "wide"
        if shot_focal == lens_tele:
            return "tele"
        return None


def _focal_end(shot_focal: Fraction, lens_wide: Fraction, lens_tele: Fraction) -> str:
    if shot_focal == lens_wide:
        return "wide"
    if shot_focal == lens_tele:
        return "tele"
    return "intermediate"


def _fmt(value: Fraction) -> str:
    """Render a fraction compactly, e.g. ``14/5`` or ``25``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"
