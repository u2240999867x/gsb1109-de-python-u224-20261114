"""Tests for lens catalogue matching."""

from fractions import Fraction
from pathlib import Path

import pytest

import exifread
from exifread.core.ifd_tag import IfdTag
from exifread.lens import (
    FNUMBER_TAG,
    FOCAL_LENGTH_TAG,
    LENS_MODEL_TAG,
    LENS_SPECIFICATION_TAG,
    CatalogueLens,
    LensMatcher,
    MatchResult,
)
from exifread.tags.fields import FieldType
from exifread.utils import Ratio

RESOURCES_ROOT = Path(__file__).parent / "resources"


def make_ifd_tag(values, tag):
    return IfdTag(
        printable=str(values),
        tag=tag,
        field_type=FieldType.RATIO,
        values=values,
        field_offset=0,
        field_length=8 * len(values),
    )


def matcher(spec=None, focal=None, fnumber=None, name=None):
    return LensMatcher(
        lens_specification=spec,
        focal_length=focal,
        fnumber=fnumber,
        lens_name=name,
    )


# A 24-70 mm f/2.8 zoom and a 50 mm f/1.8 prime used across the tests.
ZOOM = CatalogueLens(24, 70, Fraction(28, 10), Fraction(28, 10), "24-70 f/2.8")
PRIME = CatalogueLens(50, 50, Fraction(18, 10), Fraction(18, 10), "50 f/1.8")


def test_focal_length_below_wide_end_is_excluded():
    match = matcher(focal=Ratio(20, 1)).match_lens(ZOOM)
    assert match.result is MatchResult.EXCLUDED
    assert match.used_ends == ["wide"]
    constraint = match.constraints[0]
    assert constraint.end == "wide"
    assert constraint.photo == Fraction(20)
    assert constraint.lens == Fraction(24)


def test_focal_length_above_tele_end_is_excluded():
    match = matcher(focal=Ratio(85, 1)).match_lens(ZOOM)
    assert match.result is MatchResult.EXCLUDED
    assert match.constraints[0].end == "tele"
    assert match.constraints[0].lens == Fraction(70)


def test_shot_inside_zoom_range_is_compatible():
    match = matcher(focal=Ratio(35, 1)).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    assert match.constraints[0].detail == "within the lens zoom range"


def test_spec_endpoint_outside_lens_range_is_excluded():
    # Photograph claims a 105 mm tele end, lens stops at 70 mm.
    match = matcher(
        spec=[Ratio(24), Ratio(105), Ratio(28, 10), Ratio(28, 10)]
    ).match_lens(ZOOM)
    assert match.result is MatchResult.EXCLUDED
    assert match.constraints[-1].end == "tele"


def test_wider_aperture_than_lens_provides_is_excluded():
    # 50 mm shot at f/1.4 while both the shot and the prime only reach f/1.8.
    match = matcher(
        spec=[Ratio(50), Ratio(50), Ratio(18, 10), Ratio(18, 10)],
        focal=Ratio(50),
        fnumber=Ratio(14, 10),
    ).match_lens(PRIME)
    assert match.result is MatchResult.EXCLUDED
    fnumber_constraint = [c for c in match.constraints if c.field == "shot f-number"][0]
    assert fnumber_constraint.end == "wide"
    assert fnumber_constraint.photo == Fraction(14, 10)
    assert fnumber_constraint.lens == Fraction(18, 10)


def test_spec_aperture_faster_than_catalogue_is_excluded():
    match = matcher(
        spec=[Ratio(50), Ratio(50), Ratio(14, 10), Ratio(14, 10)]
    ).match_lens(PRIME)
    assert match.result is MatchResult.EXCLUDED
    aperture_constraint = [
        c for c in match.constraints if c.field == "minimum f-number"
    ][0]
    assert aperture_constraint.end == "wide"
    assert aperture_constraint.photo == Fraction(14, 10)


def test_stopped_down_shot_is_not_excluded():
    # f/8 is well beyond the f/1.8 maximum aperture but is a valid choice.
    match = matcher(
        spec=[Ratio(50), Ratio(50), Ratio(18, 10), Ratio(18, 10)],
        focal=Ratio(50),
        fnumber=Ratio(8),
    ).match_lens(PRIME)
    assert match.result is MatchResult.COMPATIBLE
    fnumber_constraint = [c for c in match.constraints if c.field == "shot f-number"][0]
    assert "stopping down" in fnumber_constraint.detail


def test_intermediate_focal_does_not_interpolate_aperture():
    # f/1.4 at 50 mm would fail a naive linear check against f/2.8, but no
    # aperture curve is documented for the middle of the zoom.
    match = matcher(
        spec=[Ratio(24), Ratio(70), Ratio(28, 10), Ratio(28, 10)],
        focal=Ratio(50),
        fnumber=Ratio(14, 10),
    ).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    assert all(c.field != "shot f-number" for c in match.constraints)


def test_tele_end_aperture_uses_tele_constraint():
    # Variable-aperture zoom: f/4 at the tele end.
    variable = CatalogueLens(24, 70, Fraction(28, 10), Fraction(4), "24-70 f/2.8-4")
    match = matcher(
        spec=[Ratio(24), Ratio(70), Ratio(28, 10), Ratio(4)],
        focal=Ratio(70),
        fnumber=Ratio(28, 10),  # f/2.8 demanded at tele, lens only reaches f/4
    ).match_lens(variable)
    assert match.result is MatchResult.EXCLUDED
    fnumber_constraint = [c for c in match.constraints if c.field == "shot f-number"][0]
    assert fnumber_constraint.end == "tele"
    assert fnumber_constraint.lens == Fraction(4)


def test_zero_denominator_field_keeps_other_evidence():
    # The wide-end aperture is a 0/0 placeholder; everything else stays usable.
    match = matcher(
        spec=[Ratio(24), Ratio(70), Ratio(0, 0), Ratio(28, 10)],
        focal=Ratio(50),
    ).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    fields = {constraint.field for constraint in match.constraints}
    assert "minimum f-number" not in fields
    assert "maximum f-number" in fields
    assert "shot focal length" in fields


def test_no_evidence_is_insufficient():
    match = matcher().match_lens(ZOOM)
    assert match.result is MatchResult.INSUFFICIENT
    assert match.constraints == []
    assert match.reasons == []


def test_serialized_zero_placeholder_is_treated_as_missing():
    # With builtin_types=True the 0/0 placeholder serializes to 0.
    match = matcher(spec=[24, 70, 0, 2.8], focal=35, fnumber=0).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    fields = {constraint.field for constraint in match.constraints}
    assert "minimum f-number" not in fields
    assert "shot f-number" not in fields
    assert "shot focal length" in fields


def test_broken_focal_endpoint_keeps_aperture_evidence():
    # The tele focal endpoint is unusable; aperture evidence still counts.
    match = matcher(
        spec=[Ratio(24), Ratio(0, 0), Ratio(28, 10), Ratio(28, 10)]
    ).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    fields = {constraint.field for constraint in match.constraints}
    assert "maximum focal length" not in fields
    assert "minimum focal length" in fields
    assert "minimum f-number" in fields


def test_all_broken_fields_are_insufficient():
    match = matcher(
        spec=[Ratio(0, 0)] * 4,
        focal=Ratio(0, 0),
        fnumber=Ratio(0, 0),
    ).match_lens(ZOOM)
    assert match.result is MatchResult.INSUFFICIENT


def test_name_alone_is_insufficient():
    match = matcher(name="").match_lens(PRIME)
    assert match.result is MatchResult.INSUFFICIENT


def test_integer_and_float_inputs_are_supported():
    variable = CatalogueLens(24, 70, 2.8, 4.0)
    match = matcher(spec=[24, 70, 2.8, 4.0], focal=70, fnumber=2.8).match_lens(variable)
    # f/2.8 at the tele end beats the f/4 tele maximum aperture.
    assert match.result is MatchResult.EXCLUDED
    constraint = [c for c in match.constraints if c.field == "shot f-number"][0]
    assert constraint.lens == Fraction(4)


def test_from_tags_reads_native_ifd_tags():
    tags = {
        LENS_SPECIFICATION_TAG: make_ifd_tag(
            [Ratio(24), Ratio(70), Ratio(28, 10), Ratio(28, 10)], 0xA432
        ),
        FOCAL_LENGTH_TAG: make_ifd_tag([Ratio(35)], 0x920A),
        FNUMBER_TAG: make_ifd_tag([Ratio(8)], 0x829D),
        LENS_MODEL_TAG: IfdTag(
            printable="",
            tag=0xA434,
            field_type=FieldType.ASCII,
            values="",
            field_offset=0,
            field_length=0,
        ),
    }
    matches = LensMatcher.from_tags(tags).match_catalogue([ZOOM, PRIME])
    assert [m.result for m in matches] == [
        MatchResult.COMPATIBLE,
        MatchResult.EXCLUDED,
    ]


def test_from_tags_reads_serialized_builtin_types():
    file_path = RESOURCES_ROOT / "heic/mobile/iphone_13_pro_max.heic"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh, details=False, builtin_types=True)
    lens_matcher = LensMatcher.from_tags(tags)
    assert lens_matcher.focal_length == Fraction(57, 10)
    assert lens_matcher.fnumber == Fraction(3, 2)

    covering = CatalogueLens(1, 9, Fraction(3, 2), Fraction(14, 5), "covering")
    too_short = CatalogueLens(1, 4, Fraction(3, 2), Fraction(14, 5), "too short")
    results = lens_matcher.match_catalogue([covering, too_short])
    assert results[0].result is MatchResult.COMPATIBLE
    assert results[1].result is MatchResult.EXCLUDED
    assert results[1].constraints[0].end == "tele"


def test_real_heic_native_tags():
    file_path = RESOURCES_ROOT / "heic/mobile/iphone_13_pro_max.heic"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh, details=False)
    lens_matcher = LensMatcher.from_tags(tags)
    assert lens_matcher.spec_max_aperture == Fraction(14, 5)
    assert lens_matcher.lens_name is not None
    match = lens_matcher.match_lens(
        CatalogueLens(1, 9, Fraction(3, 2), Fraction(14, 5))
    )
    assert match.result is MatchResult.COMPATIBLE
    assert "intermediate" in {c.end for c in match.constraints}


def test_constraints_report_which_end_was_used():
    match = matcher(
        spec=[Ratio(24), Ratio(70), Ratio(28, 10), Ratio(28, 10)],
        focal=Ratio(24),
        fnumber=Ratio(8),
    ).match_lens(ZOOM)
    assert match.result is MatchResult.COMPATIBLE
    ends = {constraint.end for constraint in match.constraints}
    assert "wide" in ends and "tele" in ends


@pytest.mark.parametrize(
    "fnumber,expected",
    [(Ratio(14, 10), MatchResult.EXCLUDED), (Ratio(8), MatchResult.COMPATIBLE)],
)
def test_prime_endpoint_aperture_boundary(fnumber, expected):
    match = matcher(
        spec=[Ratio(50), Ratio(50), Ratio(18, 10), Ratio(18, 10)],
        focal=Ratio(50),
        fnumber=fnumber,
    ).match_lens(PRIME)
    assert match.result is expected
