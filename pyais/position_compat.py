"""
Compatibility checks between position reports of different encoding precision.

Archive samples may mix high-resolution position reports (message types 1-3,
18 and 19; coordinates in 1/10000 of a minute) with low-resolution long-range
reports (message type 27; coordinates in 1/10 of a minute). Every decoded
coordinate is the *center* of a quantization cell; the true position may lie
anywhere inside that cell. Two reports of the same (anonymized) sample within
the same observation window are *compatible* iff their quantization cells
intersect on every axis where both reports carry a value.

Consequences:

* Longitude cells are intersected on the +-180 degree circle, so cells on
  opposite sides of the antimeridian overlap correctly.
* If a coordinate is not available (the 181/91 degree sentinel), that axis
  cannot be judged; with only longitude available the check is local to
  longitude.
* Different cell centers do **not** imply a conflict: as long as the cells
  overlap, the reports may describe the same true position. A pair is only a
  definite conflict when the cells are disjoint on at least one axis, and the
  verdict names that axis.
"""
import dataclasses
import enum
import typing
from typing import Dict, Hashable, List, Optional, Tuple

# Coordinate scales (cells per degree) of the position-report message types.
FINE_SCALE = 600000.0   # 1/10000 of a minute: message types 1, 2, 3, 18, 19
COARSE_SCALE = 600.0    # 1/10 of a minute: message type 27

MSG_TYPE_SCALES: Dict[int, float] = {
    1: FINE_SCALE,
    2: FINE_SCALE,
    3: FINE_SCALE,
    18: FINE_SCALE,
    19: FINE_SCALE,
    27: COARSE_SCALE,
}

# Sentinels used on the wire for "not available". They decode to values
# outside the valid range, which is how they are recognized here.
LON_NOT_AVAILABLE = 181.0
LAT_NOT_AVAILABLE = 91.0

LON_MIN, LON_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0

# Tolerance for cell intersection, in degrees. Far smaller than the finest
# half cell (1/1200000 deg), it only absorbs floating-point noise so that
# exactly touching cell boundaries count as overlapping. This biases the
# comparison towards "compatible", never towards a false conflict.
_EPS = 1e-9


class Axis(enum.Enum):
    """A coordinate axis on which two regions can be disjoint."""
    LON = 'lon'
    LAT = 'lat'


@dataclasses.dataclass(frozen=True)
class LonCell:
    """A longitude quantization cell on the +-180 degree circle.

    Stored as one or two closed ``(lo, hi)`` segments inside [-180, 180].
    A cell that crosses the antimeridian is split into two segments.
    """
    segments: Tuple[Tuple[float, float], ...]

    def intersects(self, other: 'LonCell') -> bool:
        return any(
            lo_a <= hi_b + _EPS and lo_b <= hi_a + _EPS
            for lo_a, hi_a in self.segments
            for lo_b, hi_b in other.segments
        )


@dataclasses.dataclass(frozen=True)
class LatCell:
    """A latitude quantization cell: a closed interval inside [-90, 90]."""
    lo: float
    hi: float

    def intersects(self, other: 'LatCell') -> bool:
        return self.lo <= other.hi + _EPS and other.lo <= self.hi + _EPS


@dataclasses.dataclass(frozen=True)
class PositionRegion:
    """The possible area of a reported position: one cell per axis.

    An axis is ``None`` when the report carries no usable value for it
    (not-available sentinel or out-of-range value).
    """
    lon: Optional[LonCell]
    lat: Optional[LatCell]


def _lon_cell(center: float, scale: float) -> LonCell:
    half = 0.5 / scale
    lo, hi = center - half, center + half
    if lo < LON_MIN:
        return LonCell(((lo + 360.0, LON_MAX), (LON_MIN, hi)))
    if hi > LON_MAX:
        return LonCell(((lo, LON_MAX), (LON_MIN, hi - 360.0)))
    return LonCell(((lo, hi),))


def _lat_cell(center: float, scale: float) -> LatCell:
    half = 0.5 / scale
    return LatCell(max(center - half, LAT_MIN), min(center + half, LAT_MAX))


def position_region(lon: Optional[float], lat: Optional[float], scale: float) -> PositionRegion:
    """Build the quantization region of a single position report.

    :param lon: decoded longitude in degrees (``None`` or a sentinel such as
        181.0 if not available)
    :param lat: decoded latitude in degrees (``None`` or a sentinel such as
        91.0 if not available)
    :param scale: cells per degree of the encoding (``FINE_SCALE`` or
        ``COARSE_SCALE``)
    """
    lon_cell = None
    if lon is not None and LON_MIN <= lon <= LON_MAX:
        lon_cell = _lon_cell(lon, scale)
    lat_cell = None
    if lat is not None and LAT_MIN <= lat <= LAT_MAX:
        lat_cell = _lat_cell(lat, scale)
    return PositionRegion(lon=lon_cell, lat=lat_cell)


def region_for_message(msg_type: int, lon: Optional[float], lat: Optional[float]) -> PositionRegion:
    """Build the quantization region for a decoded position-report message."""
    try:
        scale = MSG_TYPE_SCALES[msg_type]
    except KeyError:
        raise ValueError(f'message type {msg_type} carries no position report')
    return position_region(lon, lat, scale)


def disjoint_axis(first: PositionRegion, second: PositionRegion) -> Optional[Axis]:
    """Return the axis on which two regions provably do not overlap.

    Returns ``None`` when the regions are compatible, i.e. when they
    intersect on every axis where both sides carry a value. An axis that is
    unavailable on either side can never produce a conflict.
    """
    if first.lon is not None and second.lon is not None and not first.lon.intersects(second.lon):
        return Axis.LON
    if first.lat is not None and second.lat is not None and not first.lat.intersects(second.lat):
        return Axis.LAT
    return None


@dataclasses.dataclass(frozen=True)
class PositionSample:
    """One position record of an anonymized archive sample."""
    sample_id: Hashable
    timestamp: float
    msg_type: int
    lon: Optional[float]
    lat: Optional[float]

    def region(self) -> PositionRegion:
        return region_for_message(self.msg_type, self.lon, self.lat)


@dataclasses.dataclass(frozen=True)
class Conflict:
    """A definite conflict between two samples of the same observation window.

    ``axis`` names the axis on which the two quantization regions are
    disjoint; this is the evidence that makes the conflict certain.
    """
    first: PositionSample
    second: PositionSample
    axis: Axis


def compare_samples(first: PositionSample, second: PositionSample,
                    window_seconds: float) -> Optional[Axis]:
    """Compare two position records for compatibility.

    Returns the disjoint :class:`Axis` if the records are a definite
    conflict, or ``None`` when they are compatible - including the cases
    where they belong to different samples, fall outside the observation
    window, or share no commonly available axis.
    """
    if first.sample_id != second.sample_id:
        return None
    if abs(first.timestamp - second.timestamp) > window_seconds:
        return None
    return disjoint_axis(first.region(), second.region())


def find_conflicts(samples: typing.Iterable[PositionSample],
                   window_seconds: float) -> List[Conflict]:
    """Find all definite conflicts within an archive of position samples.

    Only samples of the same anonymized id whose timestamps lie within
    ``window_seconds`` of each other are compared. A pair is reported only
    when its quantization regions are disjoint on an axis available to both
    records; overlapping cells with different centers are not conflicts.
    """
    by_sample: Dict[Hashable, List[PositionSample]] = {}
    for sample in samples:
        by_sample.setdefault(sample.sample_id, []).append(sample)

    conflicts: List[Conflict] = []
    for group in by_sample.values():
        group.sort(key=lambda s: s.timestamp)
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if second.timestamp - first.timestamp > window_seconds:
                    break
                axis = disjoint_axis(first.region(), second.region())
                if axis is not None:
                    conflicts.append(Conflict(first=first, second=second, axis=axis))
    return conflicts
