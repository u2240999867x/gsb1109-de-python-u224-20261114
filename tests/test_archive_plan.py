"""
Tests for the archive write-scheme planner.
"""

import json
from fractions import Fraction

import pytest

from xopen.archive_plan import (
    PlanInputError,
    format_plan_json,
    format_plan_markdown,
    format_plan_text,
    main,
    plan_archive,
    plan_from_json,
)


def group(group_id, *schemes):
    return {"id": group_id, "schemes": list(schemes)}


def scheme(name, size, time, readers, fmt=None, artifact=None):
    entry = {"id": name, "size": size, "time": time, "readers": readers}
    if fmt is not None:
        entry["format"] = fmt
    if artifact is not None:
        entry["artifact"] = artifact
    return entry


def test_prefers_fewer_bytes_when_one_scheme_covers_all():
    result = plan_archive(
        {
            "time_limit": 100,
            "receivers": ["laptop", "server"],
            "groups": [
                group(
                    "logs",
                    scheme("logs-zstd", 1000, 20, ["laptop", "server"]),
                    scheme("logs-xz", 900, 80, ["laptop", "server"]),
                )
            ],
        }
    )

    assert result.status == "ok"
    assert result.total_size == 900
    assert result.total_time == 80
    assert result.budget_margin == 20
    assert [copy.scheme for copy in result.groups[0].copies] == ["logs-xz"]


def test_keeps_two_copies_and_counts_both():
    manifest = {
        "time_limit": 60,
        "receivers": ["laptop", "server"],
        "groups": [
            group(
                "logs",
                scheme("logs-zstd", 1000, 20, ["laptop"]),
                scheme("logs-gz", 1200, 10, ["server"]),
                scheme("logs-xz", 900, 90, ["laptop", "server"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert result.total_size == 2200
    assert result.total_time == 30
    assert [copy.scheme for copy in result.groups[0].copies] == ["logs-zstd", "logs-gz"]
    assert len(result.groups[0].copies) == 2


def test_two_copies_are_not_forced_when_one_fits():
    manifest = {
        "time_limit": 100,
        "receivers": ["laptop", "server"],
        "groups": [
            group(
                "logs",
                scheme("logs-zstd", 1000, 20, ["laptop"]),
                scheme("logs-gz", 1200, 10, ["server"]),
                scheme("logs-xz", 900, 90, ["laptop", "server"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert [copy.scheme for copy in result.groups[0].copies] == ["logs-xz"]


def test_minimizes_total_size_across_groups_with_time_limit():
    manifest = {
        "time_limit": 35,
        "receivers": ["a", "b"],
        "groups": [
            group(
                "g1",
                scheme("g1-fast", 100, 10, ["a", "b"]),
                scheme("g1-small", 60, 30, ["a", "b"]),
            ),
            group(
                "g2",
                scheme("g2-fast", 200, 10, ["a", "b"]),
                scheme("g2-small", 150, 20, ["a", "b"]),
            ),
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert result.total_time == 30
    assert result.total_size == 250
    assert [copy.scheme for copy in result.groups[0].copies] == ["g1-fast"]
    assert [copy.scheme for copy in result.groups[1].copies] == ["g2-small"]


def test_reports_minimum_extra_time_when_budget_is_too_small():
    manifest = {
        "time_limit": 25,
        "receivers": ["a", "b"],
        "groups": [
            group(
                "g1",
                scheme("g1-fast", 1000, 20, ["a", "b"]),
                scheme("g1-small", 900, 40, ["a", "b"]),
            ),
            group("g2", scheme("g2-fast", 100, 10, ["a", "b"])),
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "over_budget"
    assert result.minimum_cover_time == 30
    assert result.minimum_extra_time == 5
    assert result.budget_margin == -5
    assert result.total_time == 30


def test_reports_uncoverable_receiver():
    manifest = {
        "time_limit": 100,
        "receivers": ["a", "b", "c"],
        "groups": [
            group(
                "g1",
                scheme("g1-zstd", 100, 10, ["a", "b"]),
                scheme("g1-xz", 90, 20, ["a"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "uncoverable"
    assert result.missing_by_group == (("g1", ("c",)),)
    assert result.total_size == 0
    assert "c" in format_plan_text(result)


def test_receiver_read_paths_include_fallback_copies():
    manifest = {
        "time_limit": 60,
        "receivers": ["laptop", "server"],
        "groups": [
            group(
                "logs",
                scheme("logs-zstd", 1000, 20, ["laptop", "server"]),
                scheme("logs-gz", 1200, 10, ["server"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert [copy.scheme for copy in result.groups[0].copies] == ["logs-zstd"]
    by_receiver = {path.receiver: path for path in result.receivers}
    assert by_receiver["laptop"].primary == (("logs", "logs-zstd"),)
    assert by_receiver["server"].primary == (("logs", "logs-zstd"),)
    assert by_receiver["server"].alternatives("logs") == ("logs-zstd",)


def test_reader_formats_are_used_when_scheme_readers_are_absent():
    manifest = {
        "time_limit": 100,
        "receivers": {
            "laptop": ["zst", "gz"],
            "server": ["xz"],
        },
        "groups": [
            group(
                "logs",
                {"id": "logs-zstd", "size": 100, "time": 10, "format": "zstd"},
                {"id": "logs-xz", "size": 200, "time": 20, "format": "xz"},
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert [copy.scheme for copy in result.groups[0].copies] == ["logs-zstd", "logs-xz"]
    assert result.total_size == 300


def test_format_is_inferred_from_artifact_extension():
    manifest = {
        "time_limit": 100,
        "receivers": {"laptop": ["gz"], "server": ["xz"]},
        "groups": [
            group(
                "logs",
                {"id": "logs-gz", "size": 100, "time": 10, "artifact": "logs.txt.gz"},
                {"id": "logs-xz", "size": 200, "time": 20, "artifact": "logs.txt.xz"},
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.total_size == 300
    assert result.groups[0].copies[0].format == "gz"
    assert result.groups[0].copies[1].format == "xz"


def test_mapping_inputs_and_string_times():
    manifest = {
        "total_time_limit": "1.5",
        "receivers": {"a": None, "b": None},
        "groups": {
            "only": {
                "schemes": {
                    "s1": {"size": "10", "write_time": "0.5", "readers": "a b"},
                }
            }
        },
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert result.total_time == Fraction("0.5")
    assert result.budget_margin == Fraction("1")


def test_fractional_time_is_exact():
    manifest = {
        "time_limit": 0.3,
        "receivers": ["a"],
        "groups": [
            group(
                "g",
                scheme("one", 10, 0.1, ["a"]),
                scheme("two", 20, 0.2, ["a"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert result.total_time == Fraction("0.1")
    assert result.budget_margin == Fraction("0.2")


def test_duplicate_scheme_ids_are_rejected():
    with pytest.raises(PlanInputError, match="duplicate scheme id"):
        plan_archive(
            {
                "receivers": ["a"],
                "groups": [
                    group(
                        "g",
                        scheme("same", 1, 1, ["a"]),
                        scheme("same", 2, 2, ["a"]),
                    )
                ],
            }
        )


def test_unknown_reader_is_rejected():
    with pytest.raises(PlanInputError, match="unknown receivers"):
        plan_archive(
            {
                "receivers": ["a"],
                "groups": [group("g", scheme("s", 1, 1, ["a", "typo"]))],
            }
        )


def test_empty_manifest_is_rejected():
    with pytest.raises(PlanInputError):
        plan_archive({"receivers": ["a"], "groups": []})


def test_size_and_time_unit_suffixes():
    manifest = {
        "time_limit": "2 s",
        "receivers": ["a"],
        "groups": [
            group(
                "g",
                {"id": "s", "size": "1.5 KB", "time": "250 ms", "readers": ["a"]},
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.total_size == 1500
    assert result.total_time == Fraction("0.25")
    assert result.budget_margin == Fraction("1.75")


def test_flat_rows_are_grouped():
    rows = [
        {"group": "g1", "scheme": "a", "size": 10, "time": 1, "readers": "x y"},
        {"group": "g1", "scheme": "b", "size": 20, "time": 1, "readers": "x y"},
        {"group": "g2", "scheme": "c", "size": 5, "time": 1, "readers": "x y"},
    ]

    result = plan_from_json(json.dumps(rows))

    assert result.status == "ok"
    assert result.selection == (("g1", ("a",)), ("g2", ("c",)))
    assert result.total_size == 15


def test_csv_rows_are_supported(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_text(
        "group,scheme,format,size,time,readers,time_limit\n"
        "g1,a,zst,1 KB,20s,x y,100\n"
        "g1,b,xz,2 KB,90s,x y,100\n",
        encoding="utf-8",
    )

    result = plan_from_json(path)

    assert result.status == "ok"
    assert result.total_size == 1000
    assert result.total_time == 20


def test_two_copy_limit_reports_only_uncoverable_receiver():
    manifest = {
        "time_limit": 100,
        "receivers": ["a", "b", "c"],
        "groups": [
            group(
                "g",
                scheme("s-a", 10, 10, ["a"]),
                scheme("s-b", 20, 10, ["b"]),
                scheme("s-c", 30, 10, ["c"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "uncoverable"
    assert result.missing_by_group == (("g", ("c",)),)


def test_plan_result_convenience_fields():
    manifest = {
        "time_limit": 10,
        "receivers": ["a"],
        "groups": [group("g", scheme("s", 5, 5, ["a"]))],
    }

    result = plan_archive(manifest)

    assert result.feasible is True
    assert result.total_bytes == 5
    assert result.remaining_time == 5
    assert result.minimum_shortfall == 0
    assert result.selection == (("g", ("s",)),)


def test_artifact_extension_is_used_for_format_detection():
    manifest = {
        "time_limit": 10,
        "receivers": {"a": ["gz"], "b": ["xz"]},
        "groups": [
            group(
                "g",
                {"id": "gz", "size": 1, "time": 1, "artifact": "data.tar.gz"},
                {"id": "xz", "size": 1, "time": 1, "artifact": "data.tar.xz"},
            )
        ],
    }

    result = plan_archive(manifest)

    assert [copy.format for copy in result.groups[0].copies] == ["gz", "xz"]


def test_json_and_markdown_formats():
    manifest = {
        "time_limit": 10,
        "time_unit": "s",
        "receivers": ["a"],
        "groups": [group("g", scheme("s", 5, 5, ["a"], fmt="gz", artifact="g.gz"))],
    }
    result = plan_archive(manifest)

    payload = json.loads(format_plan_json(result))
    assert payload["status"] == "ok"
    assert payload["groups"][0]["copies"][0]["artifact"] == "g.gz"
    assert payload["receivers"][0]["primary"] == [{"group": "g", "scheme": "s"}]
    assert "g -> s" in format_plan_text(result)
    markdown = format_plan_markdown(result)
    assert "| g | s | gz | 5 |" in markdown
    assert "- **a**：g → s" in markdown


def test_two_copies_with_complementary_coverage_are_considered():
    manifest = {
        "time_limit": 100,
        "receivers": ["a", "b"],
        "groups": [
            group(
                "g1",
                scheme("g1-a", 100, 20, ["a"]),
                scheme("g1-b", 200, 20, ["b"]),
                scheme("g1-all", 500, 10, ["a", "b"]),
                scheme("g1-a2", 90, 30, ["a"]),
                scheme("g1-b2", 180, 30, ["b"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "ok"
    assert [copy.scheme for copy in result.groups[0].copies] == ["g1-a2", "g1-b2"]
    assert result.total_size == 270
    assert result.total_time == 60


def test_minimum_shortfall_uses_fastest_cover_not_fastest_single():
    manifest = {
        "time_limit": 1,
        "receivers": ["a", "b"],
        "groups": [
            group(
                "g1",
                scheme("g1-a", 100, 5, ["a"]),
                scheme("g1-b", 200, 5, ["b"]),
            )
        ],
    }

    result = plan_archive(manifest)

    assert result.status == "over_budget"
    assert result.minimum_cover_time == 10
    assert result.minimum_extra_time == 9
    assert result.total_time == 10
    assert [copy.scheme for copy in result.groups[0].copies] == ["g1-a", "g1-b"]


def test_cli_check_mode(tmp_path, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "time_limit": 10,
                "receivers": ["a"],
                "groups": [group("g", scheme("s", 5, 5, ["a"]))],
            }
        ),
        encoding="utf-8",
    )

    assert main([str(manifest_path), "--check"]) == 0
    captured = capsys.readouterr()
    assert "ok total_size=5 total_time=5" in captured.out


def test_cli_text_output(tmp_path, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "time_limit": 10,
                "receivers": ["a"],
                "groups": [group("g", scheme("s", 5, 5, ["a"]))],
            }
        ),
        encoding="utf-8",
    )

    assert main([str(manifest_path)]) == 0
    captured = capsys.readouterr()
    assert "选用方案总字节: 5 B" in captured.out
    assert "预算余量: 5" in captured.out


def test_cli_strict_returns_three_when_over_budget(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "time_limit": 1,
                "receivers": ["a"],
                "groups": [group("g", scheme("s", 5, 5, ["a"]))],
            }
        ),
        encoding="utf-8",
    )

    assert main([str(manifest_path), "--strict"]) == 3


def test_plan_from_json_string():
    result = plan_from_json(
        '{"time_limit": 5, "receivers": ["r"], '
        '"groups": {"g": [{"id": "a", "size": 1, "time": 2, "readers": ["r"]}]}}'
    )
    assert result.status == "ok"
    assert result.total_size == 1
