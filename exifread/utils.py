"""
Misc utilities.
"""

from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from exifread.core.ifd_tag import IfdTag

# GPS tag names as stored in the tags dictionary
GPS_LATITUDE = "GPS GPSLatitude"
GPS_LATITUDE_REF = "GPS GPSLatitudeRef"
GPS_LONGITUDE = "GPS GPSLongitude"
GPS_LONGITUDE_REF = "GPS GPSLongitudeRef"

_GPS_TAG_NAMES: Tuple[str, ...] = (
    GPS_LATITUDE,
    GPS_LATITUDE_REF,
    GPS_LONGITUDE,
    GPS_LONGITUDE_REF,
)

# Per-axis and per-component statuses used by the batch analysis
GPS_STATUS_VALID = "valid"
GPS_STATUS_MISSING = "missing"
GPS_STATUS_CORRUPT = "corrupt"

# Default business valid ranges, expressed in decimal degrees
DEFAULT_LATITUDE_RANGE: Tuple[float, float] = (-90.0, 90.0)
DEFAULT_LONGITUDE_RANGE: Tuple[float, float] = (-180.0, 180.0)

_COMPONENT_NAMES = ("degrees", "minutes", "seconds")
_HEMISPHERES = ("N", "S", "E", "W")


def _degrees_to_decimal(degrees: float, minutes: float, seconds: float) -> float:
    """
    Converts coordinates from a degrees minutes seconds format to a decimal degrees format.
    Reference: https://en.wikipedia.org/wiki/Geographic_coordinate_conversion
    """
    return degrees + minutes / 60 + seconds / 3600


def _unwrap_value(value: Any) -> Any:
    """Return the plain value behind an IfdTag, leaving built-in types untouched."""
    if isinstance(value, IfdTag):
        return value.values
    return value


def _coordinate_components(value: Any) -> Optional[List[Any]]:
    """
    Return the three degrees/minutes/seconds components of a coordinate tag.

    Works with both a raw IfdTag and an already serialized list of numbers.
    Returns None when the tag is absent or does not hold three components.
    """
    values = _unwrap_value(value)
    if not isinstance(values, list) or len(values) != 3:
        return None
    return list(values)


def _component_status(component: Any) -> str:
    """
    Classify a single DMS component of a coordinate tag that is present.

    A coordinate tag always carries three components. A zero denominator
    cannot represent a number (a raw Ratio with denominator 0, or the None
    produced when such a ratio is serialized), so it is corrupt. A genuine
    zero such as 0/1 or the integer 0 stays valid. Whole-tag absence is
    handled by the caller and reported as missing rather than corrupt.
    """
    if component is None:
        return GPS_STATUS_CORRUPT
    if isinstance(component, Ratio) and component.den == 0:
        return GPS_STATUS_CORRUPT
    return GPS_STATUS_VALID


def get_gps_coords(tags: dict) -> Optional[Tuple[float, float]]:
    """
    Extract tuple of latitude and longitude values in decimal degrees format from EXIF tags.
    Return None if no GPS coordinates are found, or they cannot be decoded
    (missing component, zero denominator, unknown direction reference).

    Handles regular and serialized Exif tags. The input dictionary is never
    modified, so it can safely be shared with other consumers such as
    ``convert_types`` in any call order.
    """
    # Verify if required keys are a subset of provided tags
    if not set(_GPS_TAG_NAMES) <= tags.keys():
        return None

    lat_components = _coordinate_components(tags[GPS_LATITUDE])
    lng_components = _coordinate_components(tags[GPS_LONGITUDE])
    if lat_components is None or lng_components is None:
        return None

    components = lat_components + lng_components
    if any(
        _component_status(component) != GPS_STATUS_VALID for component in components
    ):
        return None

    lat_ref = _unwrap_value(tags[GPS_LATITUDE_REF])
    lng_ref = _unwrap_value(tags[GPS_LONGITUDE_REF])
    if lat_ref not in ("N", "S") or lng_ref not in ("E", "W"):
        return None

    lat = _degrees_to_decimal(*[float(component) for component in lat_components])
    if lat_ref == "S":
        lat *= -1

    lng = _degrees_to_decimal(*[float(component) for component in lng_components])
    if lng_ref == "W":
        lng *= -1

    return lat, lng


def _analyze_ref(
    tags: Dict[str, Any], ref_name: str, diagnostics: List[str]
) -> Tuple[Optional[str], str]:
    """Classify a direction reference tag."""
    ref_value = _unwrap_value(tags[ref_name]) if ref_name in tags else None

    if ref_value is None or ref_value == "":
        diagnostics.append("%s tag is missing" % ref_name)
        return None, GPS_STATUS_MISSING

    if ref_value in _HEMISPHERES:
        return ref_value, GPS_STATUS_VALID

    diagnostics.append(
        "%s has an unexpected direction reference: %r" % (ref_name, ref_value)
    )
    return ref_value, GPS_STATUS_CORRUPT


def _analyze_axis(
    tags: Dict[str, Any],
    coord_name: str,
    ref_name: str,
    valid_range: Tuple[float, float],
) -> Dict[str, Any]:
    """
    Classify one coordinate axis (latitude or longitude) of a single observation.

    Components are individually marked valid, missing or corrupt, and the
    axis status additionally enforces the supplied business valid range.
    The original components are always returned so corrupt values can be
    inspected, including ratios with a zero denominator.
    """
    diagnostics: List[str] = []
    decimal_value: Optional[float] = None

    if coord_name not in tags:
        diagnostics.append("%s tag is missing" % coord_name)
        raw_components: List[Any] = [None, None, None]
        component_status = [GPS_STATUS_MISSING] * 3
    else:
        components = _coordinate_components(tags[coord_name])
        if components is None:
            diagnostics.append(
                "%s does not hold three degrees/minutes/seconds components" % coord_name
            )
            raw_components = []
            component_status = [GPS_STATUS_CORRUPT] * 3
        else:
            raw_components = components
            component_status = [
                _component_status(component) for component in components
            ]
            for index, status in enumerate(component_status):
                if status == GPS_STATUS_CORRUPT:
                    diagnostics.append(
                        "%s %s has a zero denominator and cannot be converted "
                        "to a number" % (coord_name, _COMPONENT_NAMES[index])
                    )

    ref_value, ref_status = _analyze_ref(tags, ref_name, diagnostics)

    if GPS_STATUS_MISSING in component_status or ref_status == GPS_STATUS_MISSING:
        axis_status = GPS_STATUS_MISSING
    elif GPS_STATUS_CORRUPT in component_status or ref_status == GPS_STATUS_CORRUPT:
        axis_status = GPS_STATUS_CORRUPT
    else:
        decimal_value = _degrees_to_decimal(
            *[float(component) for component in raw_components]
        )
        if ref_value in ("S", "W"):
            decimal_value *= -1

        range_min, range_max = valid_range
        if not range_min <= decimal_value <= range_max:
            diagnostics.append(
                "%s value %s is outside the valid range %s"
                % (coord_name, decimal_value, valid_range)
            )
            axis_status = GPS_STATUS_CORRUPT
        else:
            axis_status = GPS_STATUS_VALID

    return {
        "value": decimal_value,
        "status": axis_status,
        "component_status": component_status,
        "ref": ref_value,
        "ref_status": ref_status,
        "raw_components": raw_components,
        "diagnostics": diagnostics,
    }


def analyze_gps_batch(
    observations: Sequence[Dict[str, Any]],
    latitude_range: Tuple[float, float] = DEFAULT_LATITUDE_RANGE,
    longitude_range: Tuple[float, float] = DEFAULT_LONGITUDE_RANGE,
) -> List[Dict[str, Dict[str, Any]]]:
    """
    Analyze a batch of EXIF tag dictionaries and classify the GPS coordinates.

    Each observation is accepted either as raw IfdTag objects or as already
    serialized built-in values, and the two may be mixed. For every axis the
    components and the direction reference are individually marked as valid,
    missing or corrupt, genuine zero values being kept valid. The axis status
    also enforces the supplied business valid ranges, and corrupt axes retain
    their original components for inspection.

    Returns one dictionary per observation with ``latitude`` and ``longitude``
    entries, each exposing ``value``, ``status``, ``component_status``,
    ``ref``, ``ref_status``, ``raw_components`` and ``diagnostics``.
    """
    results: List[Dict[str, Dict[str, Any]]] = []
    for tags in observations:
        results.append(
            {
                "latitude": _analyze_axis(
                    tags, GPS_LATITUDE, GPS_LATITUDE_REF, latitude_range
                ),
                "longitude": _analyze_axis(
                    tags, GPS_LONGITUDE, GPS_LONGITUDE_REF, longitude_range
                ),
            }
        )
    return results


class Ratio(Fraction):
    """
    Ratio object that eventually will be able to reduce itself to lowest
    common denominator for printing.
    """

    _numerator: Optional[int]
    _denominator: Optional[int]

    # We're immutable, so use __new__ not __init__
    def __new__(cls, numerator: int = 0, denominator: Optional[int] = None):
        try:
            self = super(Ratio, cls).__new__(cls, numerator, denominator)
        except ZeroDivisionError:
            self = super(Ratio, cls).__new__(cls)
            self._numerator = numerator
            self._denominator = denominator
        return self

    def __repr__(self) -> str:
        return str(self)

    @property
    def num(self) -> int:
        return self.numerator

    @property
    def den(self) -> int:
        return self.denominator

    def decimal(self) -> Optional[float]:
        """Return the decimal value, or None when the denominator is zero."""
        if self.denominator == 0:
            return None
        return float(self)
