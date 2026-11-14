"""Tests for installer.choices"""

import pytest

from installer.choices import (
    WheelChoice,
    expand_wheel_tags,
    select_wheel,
    select_wheels,
)


class TestExpandWheelTags:
    def test_simple_tag(self):
        assert expand_wheel_tags("cp310-cp310-manylinux_2_17_x86_64") == frozenset(
            ["cp310-cp310-manylinux_2_17_x86_64"]
        )

    def test_compressed_python_tag(self):
        assert expand_wheel_tags("py2.py3-none-any") == frozenset(
            ["py2-none-any", "py3-none-any"]
        )

    def test_cross_product_of_all_parts(self):
        assert expand_wheel_tags("py3.cp310-abi3.cp310-any.linux_x86_64") == frozenset(
            [
                "py3-abi3-any",
                "py3-abi3-linux_x86_64",
                "py3-cp310-any",
                "py3-cp310-linux_x86_64",
                "cp310-abi3-any",
                "cp310-abi3-linux_x86_64",
                "cp310-cp310-any",
                "cp310-cp310-linux_x86_64",
            ]
        )

    @pytest.mark.parametrize("tag", ["py3-none", "py3-none-any-extra", "py3"])
    def test_invalid_tag(self, tag):
        with pytest.raises(ValueError):
            expand_wheel_tags(tag)


class TestSelectWheelTagPreference:
    def test_prefers_higher_priority_tag(self):
        candidates = [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl",
        ]
        supported = [
            "cp310-cp310-manylinux_2_17_x86_64",
            "py3-none-any",
        ]

        result = select_wheel(candidates, supported)

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl"
        assert result.matched_tag == "cp310-cp310-manylinux_2_17_x86_64"
        assert result.tag_rank == 0

    def test_compressed_tag_matches_best_supported_tag(self):
        candidates = ["pkg-1.0-py2.py3-none-any.whl"]
        supported = ["py3-none-any", "py2-none-any"]

        result = select_wheel(candidates, supported)

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-py2.py3-none-any.whl"
        assert result.matched_tag == "py3-none-any"
        assert result.tag_rank == 0

    def test_second_choice_when_first_is_absent(self):
        candidates = ["pkg-1.0-py3-none-any.whl"]
        supported = ["cp310-cp310-manylinux_2_17_x86_64", "py3-none-any"]

        result = select_wheel(candidates, supported)

        assert isinstance(result, WheelChoice)
        assert result.matched_tag == "py3-none-any"
        assert result.tag_rank == 1

    def test_returns_none_when_nothing_matches(self):
        candidates = ["pkg-1.0-cp310-cp310-win_amd64.whl"]
        supported = ["cp310-cp310-manylinux_2_17_x86_64"]

        assert select_wheel(candidates, supported) is None

    def test_incompatible_wheels_are_ignored(self):
        candidates = [
            "pkg-1.0-cp310-cp310-win_amd64.whl",
            "pkg-1.0-py3-none-any.whl",
        ]
        supported = ["py3-none-any"]

        result = select_wheel(candidates, supported)

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-py3-none-any.whl"


class TestSelectWheelBuildTag:
    def test_larger_build_number_wins_numerically_not_lexicographically(self):
        candidates = [
            "pkg-1.0-2-py3-none-any.whl",
            "pkg-1.0-10-py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-10-py3-none-any.whl"
        assert "10" in result.reason

    def test_build_number_ordering_independent_of_input_order(self):
        candidates = [
            "pkg-1.0-10-py3-none-any.whl",
            "pkg-1.0-2-py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-10-py3-none-any.whl"

    def test_suffix_breaks_build_number_tie(self):
        candidates = [
            "pkg-1.0-10a-py3-none-any.whl",
            "pkg-1.0-10b-py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-10a-py3-none-any.whl"

    def test_wheel_without_build_tag_ranks_below_build_tag(self):
        candidates = [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-1-py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-1-py3-none-any.whl"

    def test_tag_preference_beats_build_number(self):
        candidates = [
            "pkg-1.0-999-cp310-cp310-win_amd64.whl",
            "pkg-1.0-1-py3-none-any.whl",
        ]
        supported = ["py3-none-any", "cp310-cp310-win_amd64"]

        result = select_wheel(candidates, supported)

        assert isinstance(result, WheelChoice)
        assert result.filename == "pkg-1.0-1-py3-none-any.whl"
        assert result.matched_tag == "py3-none-any"


class TestSelectWheelTies:
    def test_fully_tied_wheels_are_all_kept(self):
        candidates = [
            "pkg-1.0-2-py3-none-any.whl",
            "pkg-1.0-2-py2.py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, tuple)
        assert len(result) == 2
        filenames = {choice.filename for choice in result}
        assert filenames == {
            "pkg-1.0-2-py3-none-any.whl",
            "pkg-1.0-2-py2.py3-none-any.whl",
        }
        for choice in result:
            assert choice.matched_tag == "py3-none-any"
            assert "tied" in choice.reason
            # The reason mentions every tied filename.
            assert choice.filename in choice.reason

    def test_tie_without_build_tags(self):
        candidates = [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-py2.py3-none-any.whl",
        ]

        result = select_wheel(candidates, ["py3-none-any"])

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert {choice.filename for choice in result} == set(candidates)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_result_does_not_depend_on_input_order(self, reverse):
        ordered = [
            "pkg-1.0-2-py3-none-any.whl",
            "pkg-1.0-10-py3-none-any.whl",
            "pkg-1.0-10-py2.py3-none-any.whl",
        ]
        candidates = list(reversed(ordered)) if reverse else ordered

        winners = select_wheel(candidates, ["py3-none-any"])
        tied = select_wheel(
            [
                "pkg-1.0-2-py3-none-any.whl",
                "pkg-1.0-2-py2.py3-none-any.whl",
            ],
            ["py3-none-any"],
        )

        # Both build-10 wheels tie on every criterion; build-2 loses.
        assert isinstance(winners, tuple)
        assert [choice.filename for choice in winners] == [
            "pkg-1.0-10-py2.py3-none-any.whl",
            "pkg-1.0-10-py3-none-any.whl",
        ]
        assert isinstance(tied, tuple)
        assert [choice.filename for choice in tied] == sorted(
            choice.filename for choice in tied
        )

    def test_duplicate_filename_is_rejected(self):
        candidates = [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-py3-none-any.whl",
        ]
        with pytest.raises(ValueError):
            select_wheel(candidates, ["py3-none-any"])


class TestSelectWheelEdgeCases:
    def test_empty_candidates(self):
        assert select_wheel([], ["py3-none-any"]) is None

    def test_empty_supported_tags_matches_nothing(self):
        assert select_wheel(["pkg-1.0-py3-none-any.whl"], []) is None

    def test_duplicate_supported_tag_keeps_first_position(self):
        result = select_wheel(
            ["pkg-1.0-py3-none-any.whl"],
            ["py3-none-any", "cp310-cp310-manylinux_2_17_x86_64", "py3-none-any"],
        )

        assert isinstance(result, WheelChoice)
        assert result.tag_rank == 0


class TestSelectWheels:
    def test_selects_independently_per_environment(self):
        candidates = [
            "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl",
            "pkg-1.0-cp310-cp310-win_amd64.whl",
            "pkg-1.0-py3-none-any.whl",
        ]
        environments = {
            "linux": [
                "cp310-cp310-manylinux_2_17_x86_64",
                "py3-none-any",
            ],
            "windows": [
                "cp310-cp310-win_amd64",
                "py3-none-any",
            ],
            "isolated": ["cp311-cp311-macosx_11_0_arm64"],
        }

        results = select_wheels(candidates, environments)

        assert isinstance(results["linux"], WheelChoice)
        assert results["linux"].filename == (
            "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(results["windows"], WheelChoice)
        assert results["windows"].filename == "pkg-1.0-cp310-cp310-win_amd64.whl"
        assert results["isolated"] is None

    def test_accepts_one_shot_iterator_of_candidates(self):
        candidates = iter(
            [
                "pkg-1.0-2-py3-none-any.whl",
                "pkg-1.0-10-py3-none-any.whl",
            ]
        )
        environments = {
            "first": ["py3-none-any"],
            "second": ["py3-none-any"],
        }

        results = select_wheels(candidates, environments)

        for result in results.values():
            assert isinstance(result, WheelChoice)
            assert result.filename == "pkg-1.0-10-py3-none-any.whl"

    def test_build_tie_is_reported_per_environment(self):
        candidates = [
            "pkg-1.0-7-py3-none-any.whl",
            "pkg-1.0-7-py2.py3-none-any.whl",
        ]

        results = select_wheels(candidates, {"env": ["py3-none-any"]})

        assert isinstance(results["env"], tuple)
        assert len(results["env"]) == 2
