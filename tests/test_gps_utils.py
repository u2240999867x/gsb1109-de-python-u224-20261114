"""Tests for GPS coordinate extraction, serialization interplay and batch analysis."""

import copy
import json
from pathlib import Path

import pytest

import exifread
from exifread.core.ifd_tag import IfdTag
from exifread.serialize import convert_types
from exifread.tags.fields import FieldType
from exifread.utils import (
    GPS_LATITUDE,
    GPS_LATITUDE_REF,
    GPS_LONGITUDE,
    GPS_LONGITUDE_REF,
    GPS_STATUS_CORRUPT,
    GPS_STATUS_MISSING,
    GPS_STATUS_VALID,
    Ratio,
    analyze_gps_batch,
    get_gps_coords,
)

RESOURCES_ROOT = Path(__file__).parent / "resources"

# Images with interesting GPS data
GPS_IMAGE = RESOURCES_ROOT / "jpg/gps/DSCN0012.jpg"
# Latitude/longitude seconds are the corrupt 0/0 ratio; degrees include a genuine 0
CORRUPT_GPS_IMAGE = RESOURCES_ROOT / "jpg/tests/46_UnicodeEncodeError.jpg"
# Genuine zero components (0/1), serialized as integer 0
TRUE_ZERO_GPS_IMAGE = RESOURCES_ROOT / "jpg/Kodak_CX7530.jpg"


def _ratio_tag(values):
    return IfdTag(str(values), 2, FieldType.RATIO, values, 0, 24, False)


def _ref_tag(value):
    return IfdTag(value, 1, FieldType.ASCII, value, 0, 2, True)


def _raw_observation(lat, lat_ref, lng, lng_ref):
    tags = {}
    if lat is not None:
        tags[GPS_LATITUDE] = _ratio_tag(lat)
    if lat_ref is not None:
        tags[GPS_LATITUDE_REF] = _ref_tag(lat_ref)
    if lng is not None:
        tags[GPS_LONGITUDE] = _ratio_tag(lng)
    if lng_ref is not None:
        tags[GPS_LONGITUDE_REF] = _ref_tag(lng_ref)
    return tags


def _load(path):
    with open(path, "rb") as fh:
        return exifread.process_file(fh)


# ---------------------------------------------------------------------------
# Ratio
# ---------------------------------------------------------------------------


def test_ratio_decimal_handles_zero_denominator():
    assert Ratio(5, 0).decimal() is None
    assert Ratio(0, 0).decimal() is None


def test_ratio_decimal_distinguishes_true_zero():
    assert Ratio(0, 1).decimal() == 0.0
    assert Ratio(0, 1).den == 1


def test_ratio_decimal_normal_value():
    assert Ratio(1, 4).decimal() == 0.25


# ---------------------------------------------------------------------------
# get_gps_coords: non-destructive, both representations
# ---------------------------------------------------------------------------


def test_get_gps_coords_raw_tags():
    tags = _raw_observation(
        [Ratio(43, 1), Ratio(28, 1), Ratio(0, 1)],
        "N",
        [Ratio(11, 1), Ratio(53, 1), Ratio(0, 1)],
        "E",
    )
    assert get_gps_coords(tags) == pytest.approx(
        (43.46666666666667, 11.883333333333333)
    )


def test_get_gps_coords_applies_hemisphere_sign():
    tags = _raw_observation(
        [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
        "S",
        [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
        "W",
    )
    lat, lng = get_gps_coords(tags)
    assert lat == pytest.approx(-0.5)
    assert lng == pytest.approx(-0.5)


def test_get_gps_coords_does_not_mutate_tags():
    tags = _load(GPS_IMAGE)
    snapshot = copy.deepcopy(tags)
    get_gps_coords(tags)
    get_gps_coords(tags)
    assert set(tags) == set(snapshot)
    for key in snapshot:
        assert type(tags[key]) is type(snapshot[key])
        if isinstance(tags[key], IfdTag):
            assert tags[key].values == snapshot[key].values
            assert tags[key].printable == snapshot[key].printable
        else:
            assert tags[key] == snapshot[key]
    assert isinstance(tags[GPS_LATITUDE], IfdTag)
    assert isinstance(tags[GPS_LATITUDE_REF], IfdTag)


def test_get_gps_coords_on_serialized_tags():
    tags = _load(GPS_IMAGE)
    serialized = convert_types(tags)
    assert get_gps_coords(serialized) == get_gps_coords(tags)
    # Repeated calls stay stable
    assert get_gps_coords(serialized) == get_gps_coords(serialized)


def test_get_gps_coords_on_mixed_table():
    raw = _raw_observation(
        [Ratio(51, 1), Ratio(30, 1), Ratio(0, 1)],
        "N",
        [Ratio(0, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    mixed = {
        GPS_LATITUDE: raw[GPS_LATITUDE],  # still an IfdTag
        GPS_LATITUDE_REF: "N",  # already serialized
        GPS_LONGITUDE: [0, 0, 0],  # already serialized
        GPS_LONGITUDE_REF: raw[GPS_LONGITUDE_REF],  # still an IfdTag
    }
    assert get_gps_coords(mixed) == pytest.approx((51.5, 0.0))


def test_get_gps_coords_missing_tags_returns_none():
    assert get_gps_coords({}) is None
    tags = _raw_observation(
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        None,
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    assert get_gps_coords(tags) is None


def test_get_gps_coords_zero_denominator_returns_none():
    tags = _raw_observation(
        [Ratio(51, 1), Ratio(25, 1), Ratio(0, 0)],
        "N",
        [Ratio(0, 1), Ratio(40, 1), Ratio(5, 0)],
        "W",
    )
    assert get_gps_coords(tags) is None
    # Same conclusion once serialized, where zero denominators become None
    assert get_gps_coords(convert_types(tags)) is None


def test_get_gps_coords_true_zero_is_a_valid_coordinate():
    tags = _raw_observation(
        [Ratio(0, 1), Ratio(0, 1), Ratio(0, 1)],
        "N",
        [Ratio(0, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    assert get_gps_coords(tags) == (0.0, 0.0)


def test_get_gps_coords_unknown_reference_returns_none():
    tags = _raw_observation(
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        "X",
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    assert get_gps_coords(tags) is None


# ---------------------------------------------------------------------------
# Consumer order independence
# ---------------------------------------------------------------------------


def test_consumer_order_produces_identical_results():
    # Order A: GPS consumer first, archiving second
    tags_a = _load(GPS_IMAGE)
    coords_a_first = get_gps_coords(tags_a)
    archived_a = convert_types(tags_a)
    coords_a_second = get_gps_coords(archived_a)

    # Order B: archiving first, GPS consumer second
    tags_b = _load(GPS_IMAGE)
    archived_b = convert_types(tags_b)
    coords_b_first = get_gps_coords(tags_b)
    coords_b_second = get_gps_coords(archived_b)

    assert archived_a == archived_b
    assert coords_a_first == coords_b_first
    assert coords_a_second == coords_b_second == coords_a_first


def test_archiving_after_gps_consumer_does_not_raise():
    # Regression: get_gps_coords used to overwrite IfdTags with plain lists,
    # which then made convert_types crash with an AttributeError.
    tags = _load(GPS_IMAGE)
    get_gps_coords(tags)
    archived = convert_types(tags)
    assert archived[GPS_LATITUDE] == [43, 28, 1.76399999]
    assert archived[GPS_LATITUDE_REF] == "N"


def test_convert_types_passes_serialized_values_through():
    tags = _load(GPS_IMAGE)
    tags["Image Make"] = "Canon"  # plain value mixed among IfdTags
    archived = convert_types(tags)
    assert archived["Image Make"] == "Canon"
    assert isinstance(archived[GPS_LATITUDE], list)


def test_serialized_zero_denominator_is_none_not_zero():
    tags = _raw_observation(
        [Ratio(0, 1), Ratio(0, 1), Ratio(0, 0)],
        "N",
        [Ratio(5, 0), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    archived = convert_types(tags)
    # Genuine zero stays 0, zero denominator becomes None
    assert archived[GPS_LATITUDE] == [0, 0, None]
    assert archived[GPS_LONGITUDE] == [None, 0, 0]
    json.dumps(archived)  # remains JSON serializable


# ---------------------------------------------------------------------------
# analyze_gps_batch
# ---------------------------------------------------------------------------


def _diagnostic_fields(axis):
    return {
        key: axis[key]
        for key in (
            "value",
            "status",
            "component_status",
            "ref",
            "ref_status",
            "diagnostics",
        )
    }


def test_batch_marks_corrupt_components_and_keeps_raw_components():
    tags = _load(CORRUPT_GPS_IMAGE)
    result = analyze_gps_batch([tags])[0]

    for axis_name in ("latitude", "longitude"):
        axis = result[axis_name]
        assert axis["status"] == GPS_STATUS_CORRUPT
        assert axis["component_status"] == [
            GPS_STATUS_VALID,
            GPS_STATUS_VALID,
            GPS_STATUS_CORRUPT,
        ]
        # The original zero-denominator component is still exposed
        assert any(
            isinstance(component, Ratio) and component.den == 0
            for component in axis["raw_components"]
        )
        assert axis["value"] is None
        assert any("zero denominator" in message for message in axis["diagnostics"])


def test_batch_corrupt_diagnostics_identical_for_raw_and_serialized():
    tags = _load(CORRUPT_GPS_IMAGE)
    raw_result = analyze_gps_batch([tags])[0]
    serialized_result = analyze_gps_batch([convert_types(tags)])[0]

    for axis_name in ("latitude", "longitude"):
        assert _diagnostic_fields(raw_result[axis_name]) == _diagnostic_fields(
            serialized_result[axis_name]
        )


def test_batch_true_zero_components_are_valid():
    tags = _load(TRUE_ZERO_GPS_IMAGE)
    result = analyze_gps_batch([tags])[0]

    assert result["latitude"]["status"] == GPS_STATUS_VALID
    assert result["longitude"]["status"] == GPS_STATUS_VALID
    assert result["latitude"]["component_status"] == [GPS_STATUS_VALID] * 3
    # Genuine zeros survive both representations
    serialized = analyze_gps_batch([convert_types(tags)])[0]
    assert serialized["latitude"]["status"] == GPS_STATUS_VALID
    assert get_gps_coords(tags) == (
        result["latitude"]["value"],
        result["longitude"]["value"],
    )


def test_batch_missing_direction_reference():
    tags = _raw_observation(
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        None,  # no latitude reference
        [Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    result = analyze_gps_batch([tags])[0]

    assert result["latitude"]["status"] == GPS_STATUS_MISSING
    assert result["latitude"]["ref_status"] == GPS_STATUS_MISSING
    # The other axis is unaffected
    assert result["longitude"]["status"] == GPS_STATUS_VALID


def test_batch_missing_coordinate_tag():
    tags = {
        GPS_LATITUDE_REF: "N",
        GPS_LONGITUDE: _ratio_tag([Ratio(1, 1), Ratio(0, 1), Ratio(0, 1)]),
        GPS_LONGITUDE_REF: "E",
    }
    result = analyze_gps_batch([tags])[0]
    assert result["latitude"]["status"] == GPS_STATUS_MISSING
    assert result["latitude"]["component_status"] == [GPS_STATUS_MISSING] * 3
    assert result["longitude"]["status"] == GPS_STATUS_VALID


def test_batch_enforces_business_valid_ranges():
    tags = _raw_observation(
        [Ratio(95, 1), Ratio(0, 1), Ratio(0, 1)],
        "N",
        [Ratio(200, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    result = analyze_gps_batch([tags])[0]
    assert result["latitude"]["status"] == GPS_STATUS_CORRUPT
    assert result["longitude"]["status"] == GPS_STATUS_CORRUPT
    assert any(
        "outside the valid range" in m for m in result["latitude"]["diagnostics"]
    )


def test_batch_custom_ranges():
    tags = _raw_observation(
        [Ratio(51, 1), Ratio(0, 1), Ratio(0, 1)],
        "N",
        [Ratio(10, 1), Ratio(0, 1), Ratio(0, 1)],
        "E",
    )
    result = analyze_gps_batch(
        [tags], latitude_range=(-45.0, 45.0), longitude_range=(-45.0, 45.0)
    )[0]
    assert result["latitude"]["status"] == GPS_STATUS_CORRUPT
    assert result["longitude"]["status"] == GPS_STATUS_VALID


def test_batch_observations_around_zero_are_valid():
    observations = [
        _raw_observation(
            [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
            "S",
            [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
            "W",
        ),
        _raw_observation(
            [Ratio(0, 1), Ratio(0, 1), Ratio(0, 1)],
            "N",
            [Ratio(0, 1), Ratio(0, 1), Ratio(0, 1)],
            "E",
        ),
        _raw_observation(
            [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
            "N",
            [Ratio(0, 1), Ratio(30, 1), Ratio(0, 1)],
            "E",
        ),
    ]
    results = analyze_gps_batch(observations)
    assert len(results) == 3
    for result in results:
        assert result["latitude"]["status"] == GPS_STATUS_VALID
        assert result["longitude"]["status"] == GPS_STATUS_VALID
    # Crosses zero: negative, exactly zero, positive
    assert results[0]["latitude"]["value"] < 0
    assert results[1]["latitude"]["value"] == 0
    assert results[2]["latitude"]["value"] > 0


def test_batch_repeatable_and_order_independent():
    tags = _load(CORRUPT_GPS_IMAGE)
    first = analyze_gps_batch([tags])
    second = analyze_gps_batch([tags])
    assert first == second

    serialized = convert_types(_load(CORRUPT_GPS_IMAGE))
    # Statuses do not depend on which consumer ran first
    raw_statuses = [
        (axis["status"], axis["component_status"]) for axis in first[0].values()
    ]
    serialized_statuses = [
        (axis["status"], axis["component_status"])
        for axis in analyze_gps_batch([serialized])[0].values()
    ]
    assert raw_statuses == serialized_statuses
