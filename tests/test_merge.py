"""
Tests for the bounded open-file merge.
"""

import json
from pathlib import Path

import pytest

import xopen.merge as merge_module
from xopen import MergeError, MergeResult, merge_sorted_files, xopen


def write(path: Path, rows: list[str], mode: str = "w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with xopen(path, mode) as stream:
        for row in rows:
            stream.write(row.encode() if "b" in mode else row)
    return path


def read_rows(path: Path, binary: bool = False) -> list[str]:
    with xopen(path, "rb" if binary else "rt") as stream:
        return list(stream)


def make_sources(tmp_path: Path, names: list[list[str]]) -> list[Path]:
    return [
        write(tmp_path / f"input-{index:03d}.txt", rows)
        for index, rows in enumerate(names)
    ]


def test_merges_by_key_and_source_then_row(tmp_path):
    inputs = make_sources(
        tmp_path,
        [
            ["b 0\n", "d 0\n", "f 0\n"],
            ["a 1\n", "d 1\n", "g 1\n"],
            ["d 2\n", "e 2\n"],
        ],
    )
    output = tmp_path / "merged.txt"
    manifest = tmp_path / "merged.manifest.json"

    result = merge_sorted_files(
        inputs,
        output,
        key=lambda line: line.split(" ")[0],
        manifest=manifest,
    )

    assert read_rows(output) == [
        "a 1\n",
        "b 0\n",
        "d 0\n",
        "d 1\n",
        "d 2\n",
        "e 2\n",
        "f 0\n",
        "g 1\n",
    ]
    assert isinstance(result, MergeResult)
    assert result.total_rows == 8
    assert result.row_counts == (3, 3, 2)
    assert result.duplicate_key_rows == 2
    payload = json.loads(manifest.read_text())
    assert payload["status"] == "complete"
    assert payload["completed"] is True
    assert payload["duplicate_key_rows"] == 2


@pytest.mark.parametrize("extension", [".txt", ".gz"])
@pytest.mark.parametrize("max_open_files", [1, 2, 3, 4, 5, 8])
def test_reducing_open_files_does_not_change_result(
    tmp_path, extension, max_open_files
):
    rows = [
        [f"{number:03d} source-{source}\n" for number in range(20)]
        for source in range(9)
    ]
    for source in range(9):
        rows[source].extend(
            f"010 payload-{source}-{copy}\n" for copy in range(3)
        )
        rows[source].sort()
    inputs = [
        write(tmp_path / f"input-{index:03d}{extension}", source_rows)
        for index, source_rows in enumerate(rows)
    ]
    output = tmp_path / f"merged{extension}"
    manifest = tmp_path / "merged.manifest.json"

    result = merge_sorted_files(
        inputs,
        output,
        key=lambda line: line.split(" ")[0],
        max_open_files=max_open_files,
        manifest=manifest,
    )

    expected = sorted(
        (line, source, row)
        for source, source_rows in enumerate(rows)
        for row, line in enumerate(source_rows, start=1)
    )
    expected_lines = [
        line
        for line, _source, _row in sorted(
            expected, key=lambda item: (item[0].split(" ")[0], item[1], item[2])
        )
    ]
    assert read_rows(output) == expected_lines
    expected_duplicates = len(expected_lines) - len(
        {line.split(" ")[0] for line in expected_lines}
    )
    assert result.row_counts == tuple(len(source_rows) for source_rows in rows)
    assert result.duplicate_key_rows == expected_duplicates
    assert result.total_rows == len(expected_lines)
    payload = json.loads(manifest.read_text())
    assert payload["status"] == "complete"
    assert payload["duplicate_key_rows"] == expected_duplicates
    assert payload["total_rows"] == len(expected_lines)


def test_identical_keys_are_preserved_not_collapsed(tmp_path):
    inputs = make_sources(
        tmp_path,
        [
            ["same-0\n", "same-1\n"],
            ["same-2\n", "same-3\n"],
        ],
    )
    output = tmp_path / "merged.txt"

    result = merge_sorted_files(inputs, output, key=lambda line: line[:4])

    assert read_rows(output) == ["same-0\n", "same-1\n", "same-2\n", "same-3\n"]
    assert result.total_rows == 4
    assert result.distinct_keys == 1
    assert result.duplicate_key_rows == 3


def test_complete_manifest_is_written_last():
    assert merge_module._write_json_atomic  # manifest is installed atomically


@pytest.mark.parametrize("max_open_files", [1, 2, 3, 4, 6])
def test_unsorted_input_reports_original_source_and_line(tmp_path, max_open_files):
    inputs = make_sources(
        tmp_path,
        [
            ["a\n", "c\n"],
            ["b\n", "z\n", "d\n"],
            ["e\n"],
        ],
    )
    output = tmp_path / "merged.txt"
    manifest = tmp_path / "merged.manifest.json"

    with pytest.raises(MergeError) as excinfo:
        merge_sorted_files(
            inputs,
            output,
            max_open_files=max_open_files,
            manifest=manifest,
        )

    error = excinfo.value
    assert error.reason == "not_sorted"
    assert error.source == str(inputs[1])
    assert error.line_number == 3
    assert error.source_index == 1
    assert error.path == str(inputs[1])
    assert "input-001.txt:3" in str(error)
    assert not output.exists()
    payload = json.loads(manifest.read_text())
    assert payload["status"] == "failed"
    assert payload["completed"] is False
    assert payload["error"]["source"] == str(inputs[1])
    assert payload["error"]["line_number"] == 3


@pytest.mark.parametrize("max_open_files", [1, 2, 3, 4, 6])
def test_truncated_input_that_fails_at_close_blocks_manifest(tmp_path, max_open_files):
    complete = tmp_path / "complete.gz"
    write(complete, [f"{number:05d}\n" for number in range(500)], mode="wb")
    payload = complete.read_bytes()

    corrupt = tmp_path / "corrupt.gz"
    corrupt.write_bytes(payload[:-1])
    inputs = [complete, corrupt, complete]
    output = tmp_path / "merged.txt.gz"
    manifest = tmp_path / "merged.manifest.json"

    with pytest.raises(MergeError) as excinfo:
        merge_sorted_files(
            inputs,
            output,
            max_open_files=max_open_files,
            manifest=manifest,
        )

    error = excinfo.value
    assert error.reason in ("close_error", "read_error")
    assert error.source == str(corrupt)
    assert error.line_number == 500
    assert not output.exists()
    payload = json.loads(manifest.read_text())
    assert payload["status"] == "failed"
    assert payload["completed"] is False
    assert payload["error"]["source"] == str(corrupt)
    assert payload["error"]["line_number"] == 500


@pytest.mark.parametrize("max_open_files", [1, 2, 3, 4, 6])
def test_existing_output_survives_failure(tmp_path, max_open_files):
    inputs = make_sources(tmp_path, [["a\n", "c\n", "b\n"]])
    output = tmp_path / "merged.txt"
    output.write_text("previous\n")
    manifest = tmp_path / "merged.manifest.json"

    with pytest.raises(MergeError):
        merge_sorted_files(
            inputs,
            output,
            max_open_files=max_open_files,
            manifest=manifest,
        )

    assert output.read_text() == "previous\n"
    assert json.loads(manifest.read_text())["completed"] is False


def test_open_stream_ceiling_is_respected(tmp_path, monkeypatch):
    inputs = make_sources(
        tmp_path,
        [[f"{source}-{number:03d}\n" for number in range(10)] for source in range(12)],
    )
    output = tmp_path / "merged.txt"

    active = 0
    peak = 0
    real_xopen = merge_module.xopen
    real_temp_sink = merge_module._TempSink

    class Tracked:
        def __init__(self, stream):
            self._stream = stream
            self._closed = False
            counters["active"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._stream)

        def write(self, data):
            return self._stream.write(data)

        def close(self):
            if not self._closed:
                self._closed = True
                counters["active"] -= 1
                self._stream.close()

    counters = {"active": active, "peak": peak}

    def tracking_xopen(path, mode="r", *args, **kwargs):
        return Tracked(real_xopen(path, mode, *args, **kwargs))

    class TrackingTempSink(real_temp_sink):
        def __init__(self, path):
            super().__init__(path)
            counters["active"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])

        def close(self):
            counters["active"] -= 1
            super().close()

    monkeypatch.setattr(merge_module, "xopen", tracking_xopen)
    monkeypatch.setattr(merge_module, "_TempSink", TrackingTempSink)

    for limit in (1, 2, 3, 4, 6):
        counters["active"] = 0
        counters["peak"] = 0
        merge_sorted_files(inputs, output, max_open_files=limit)
        assert counters["peak"] <= limit, (limit, counters)


def test_binary_merge_and_repeated_source_path(tmp_path):
    inputs = make_sources(
        tmp_path,
        [["a\n", "c\n"], ["b\n", "c\n"]],
    )
    output = tmp_path / "merged.bin"

    result = merge_sorted_files(inputs + [inputs[0]], output, binary=True)

    assert read_rows(output, binary=True) == [
        b"a\n",
        b"a\n",
        b"b\n",
        b"c\n",
        b"c\n",
        b"c\n",
    ]
    assert result.source_count == 3
    assert result.row_counts == (2, 2, 2)


def test_lower_limit_without_manifest_still_works(tmp_path):
    inputs = make_sources(tmp_path, [["b\n"], ["a\n"], ["c\n"]])
    output = tmp_path / "merged.txt"

    result = merge_sorted_files(inputs, output, max_open_files=2)

    assert read_rows(output) == ["a\n", "b\n", "c\n"]
    assert result.manifest_path is None


def test_invalid_arguments(tmp_path):
    with pytest.raises(TypeError):
        merge_sorted_files("single.txt", tmp_path / "out.txt")
    with pytest.raises(ValueError):
        merge_sorted_files([], tmp_path / "out.txt", max_open_files=0)
    with pytest.raises(TypeError):
        merge_sorted_files([], tmp_path / "out.txt", max_open_files=2.5)
    with pytest.raises(ValueError):
        merge_sorted_files(
            [],
            tmp_path / "out.txt",
            manifest=tmp_path / "out.txt",
        )


@pytest.mark.parametrize("max_open_files", [2, 3, 5])
def test_cli_merges_and_reports_errors(tmp_path, max_open_files):
    inputs = make_sources(tmp_path, [["b\n"], ["a\n"]])
    output = tmp_path / "merged.txt.gz"

    status = merge_module.main(
        [
            "--max-open-files",
            str(max_open_files),
            str(output),
            *(str(path) for path in inputs),
        ]
    )

    assert status == 0
    assert read_rows(output) == ["a\n", "b\n"]
    assert json.loads((tmp_path / "merged.txt.gz.manifest.json").read_text())[
        "completed"
    ]

    unsorted = make_sources(tmp_path, [["z\n", "a\n"]])
    bad_output = tmp_path / "bad.txt"
    status = merge_module.main(
        [
            "--max-open-files",
            str(max_open_files),
            str(bad_output),
            *(str(path) for path in unsorted),
        ]
    )
    assert status == 2
    assert not bad_output.exists()
    payload = json.loads((tmp_path / "bad.txt.manifest.json").read_text())
    assert payload["completed"] is False


def test_duplicate_key_rows_and_duplicate_line_rows_are_separate(tmp_path):
    inputs = make_sources(
        tmp_path,
        [
            ["a first\n", "b second\n", "c third\n"],
            ["a first\n", "b different\n", "c third\n"],
        ],
    )
    output = tmp_path / "merged.json"

    result = merge_sorted_files(
        inputs, output, key=lambda line: line.split(" ")[0]
    )

    assert result.duplicate_key_rows == 3
    assert result.duplicate_line_rows == 2
    assert result.duplicate_rows == 3
    assert result.exact_duplicate_rows == 2
    assert result.total_rows == 6
    assert read_rows(output) == [
        "a first\n",
        "a first\n",
        "b second\n",
        "b different\n",
        "c third\n",
        "c third\n",
    ]


def test_all_observables_stable_across_open_file_limits(tmp_path):
    inputs = make_sources(
        tmp_path,
        [
            ["a 0\n", "b 0\n", "c 0\n", "d 0\n"],
            ["a 0\n", "b 1\n", "e 1\n"],
            ["b 2\n", "c 0\n", "f 2\n"],
            ["a 0\n", "g 3\n"],
        ],
    )

    outputs = []
    summaries = []
    for limit in (1, 2, 3, 4, 8):
        output = tmp_path / f"merged-{limit}.txt"
        manifest = tmp_path / f"manifest-{limit}.json"
        result = merge_sorted_files(
            inputs,
            output,
            key=lambda line: line.split(" ")[0],
            max_open_files=limit,
            manifest=manifest,
        )
        outputs.append(read_rows(output))
        payload = json.loads(manifest.read_text())
        summaries.append(
            (
                result.total_rows,
                result.distinct_keys,
                result.duplicate_key_rows,
                result.duplicate_line_rows,
                result.row_counts,
                payload["status"],
                payload["total_rows"],
                payload["distinct_keys"],
                payload["duplicate_key_rows"],
                payload["duplicate_line_rows"],
                payload["row_counts"],
            )
        )

    assert all(rows == outputs[0] for rows in outputs)
    assert all(summary == summaries[0] for summary in summaries)
    assert summaries[0][:4] == (12, 7, 5, 3)


@pytest.mark.parametrize("max_open_files", [1, 2, 3, 4, 8])
def test_error_provenance_stable_across_open_file_limits(tmp_path, max_open_files):
    good = make_sources(tmp_path, [[f"{number:03d}\n" for number in range(30)]])
    bad = write(
        tmp_path / "bad.txt",
        [f"{number:03d}\n" for number in range(10)]
        + ["005\n"]
        + [f"{number:03d}\n" for number in range(11, 30)],
    )

    with pytest.raises(MergeError) as excinfo:
        merge_sorted_files(
            good + [bad] + good,
            tmp_path / "merged.txt",
            max_open_files=max_open_files,
        )

    assert excinfo.value.source == str(bad)
    assert excinfo.value.source_index == 1
    assert excinfo.value.line_number == 11
    assert excinfo.value.reason == "not_sorted"


def test_manifest_write_failure_is_reported_and_output_is_retained(
    tmp_path, monkeypatch
):
    inputs = make_sources(tmp_path, [["a\n"]])
    output = tmp_path / "merged.txt"
    manifest = tmp_path / "merged.manifest.json"

    real_stage = merge_module._stage_json

    def failing_stage(path, payload):
        if payload.get("status") == "complete":
            raise OSError("disk full")
        return real_stage(path, payload)

    monkeypatch.setattr(merge_module, "_stage_json", failing_stage)
    output.write_text("previous\n")

    with pytest.raises(MergeError) as excinfo:
        merge_sorted_files(inputs, output, manifest=manifest)

    assert excinfo.value.reason == "manifest_error"
    assert output.read_text() == "previous\n"
    payload = json.loads(manifest.read_text())
    assert payload["status"] == "failed"
    assert payload["completed"] is False


def test_module_entry_point_does_not_warn(tmp_path):
    import subprocess
    import sys

    script = (
        "import xopen, xopen.merge;"
        "assert xopen.merge_sorted_files is xopen.merge.merge_sorted_files"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
