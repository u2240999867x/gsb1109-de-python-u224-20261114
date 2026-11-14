from io import BytesIO
from pathlib import Path

import pytest

from installer.records import (
    ConflictingRecordEntry,
    Hash,
    InvalidRecordEntry,
    RecordEntry,
    RecordStatus,
    group_record_rows,
    parse_record_file,
    parse_record_rows,
)


#
# pytest fixture witchcraft
#
@pytest.fixture()
def record_simple_list():
    return [
        "file.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144",
        "distribution-1.0.dist-info/RECORD,,",
    ]


@pytest.fixture()
def record_simple_iter(record_simple_list):
    return iter(record_simple_list)


@pytest.fixture()
def record_simple_file(tmpdir, record_simple_list):
    p = tmpdir.join("RECORD")
    p.write("\n".join(record_simple_list))
    with Path(p).open() as f:
        yield f


@pytest.fixture()
def record_input(request):
    return request.getfixturevalue(request.param)


SAMPLE_RECORDS = [
    (
        "purelib",
        ("test1.py", "sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4", 6),
        b"test1\n",
        True,
    ),
    (
        "purelib",
        ("test2.py", "sha256=fW_Xd08Nh2JNptzxbQ09EEwxkedx--LznIau1LK_Gg8", 6),
        b"test2\n",
        True,
    ),
    (
        "purelib",
        ("test3.py", "sha256=qwPDTx7OCCEf4qgDn9ZCQZmz9de1X_E7ETSzZHdsRcU", 6),
        b"test3\n",
        True,
    ),
    (
        "purelib",
        (
            "test4.py",
            "sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4",
            None,
        ),
        b"test1\n",
        True,
    ),
    ("purelib", ("test5.py", None, None), b"test1\n", True),
    ("purelib", ("test6.py", None, 6), b"test1\n", True),
    (
        "purelib",
        ("test7.py", "sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4", 7),
        b"test1\n",
        False,
    ),
    (
        "purelib",
        ("test7.py", "sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4", None),
        b"not-test1\n",
        False,
    ),
    ("purelib", ("test8.py", None, 10), b"test1\n", False),
]


#
# Actual Tests
#
class TestRecordEntry:
    @pytest.mark.parametrize(
        "path, hash_, size, caused_by",
        [
            ("", "", "", ["path"]),
            ("", "", "non-int", ["path", "size"]),
            ("a.py", "", "non-int", ["size"]),
            # Notice that we're explicitly allowing non-compliant hash values
            ("a.py", "some-random-value", "non-int", ["size"]),
        ],
    )
    def test_invalid_elements(self, path, hash_, size, caused_by):
        with pytest.raises(InvalidRecordEntry) as exc_info:
            RecordEntry.from_elements(path, hash_, size)

        assert exc_info.value.elements == (path, hash_, size)
        for word in caused_by:
            assert word in str(exc_info.value)

    @pytest.mark.parametrize(
        "path, hash_, size",
        [
            ("a.py", "", ""),
            ("a.py", "", "3144"),
            ("a.py", "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI", ""),
            ("a.py", "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI", "3144"),
        ],
    )
    def test_valid_elements(self, path, hash_, size):
        RecordEntry.from_elements(path, hash_, size)

    @pytest.mark.parametrize(
        ("scheme", "elements", "data", "passes_validation"), SAMPLE_RECORDS
    )
    def test_populates_attributes_correctly(
        self, scheme, elements, data, passes_validation
    ):
        path, hash_string, size = elements

        record = RecordEntry.from_elements(path, hash_string, size)

        assert record.path == path
        assert record.size == size

        if record.hash_ is not None:
            assert isinstance(record.hash_, Hash)
            assert record.hash_.name == "sha256"
            assert record.hash_.value == hash_string[len("sha256=") :]

    @pytest.mark.parametrize(
        ("scheme", "elements", "data", "passes_validation"), SAMPLE_RECORDS
    )
    def test_validation(self, scheme, elements, data, passes_validation):
        record = RecordEntry.from_elements(*elements)
        assert record.validate(data) == passes_validation

    @pytest.mark.parametrize(
        ("scheme", "elements", "data", "passes_validation"), SAMPLE_RECORDS
    )
    def test_validate_stream(self, scheme, elements, data, passes_validation):
        record = RecordEntry.from_elements(*elements)

        assert record.validate_stream(BytesIO(data)) == passes_validation

    @pytest.mark.parametrize(
        ("scheme", "elements", "data", "passes_validation"), SAMPLE_RECORDS
    )
    def test_string_representation(self, scheme, elements, data, passes_validation):
        record = RecordEntry.from_elements(*elements)

        expected_row = tuple(
            [(str(elem) if elem is not None else "") for elem in elements]
        )
        assert record.to_row() == expected_row

    @pytest.mark.parametrize(
        ("scheme", "elements", "data", "passes_validation"), SAMPLE_RECORDS
    )
    def test_string_representation_with_prefix(
        self, scheme, elements, data, passes_validation
    ):
        record = RecordEntry.from_elements(*elements)

        expected_row = tuple(
            [
                (str(elem) if elem is not None else "")
                for elem in ("prefix/" + elements[0], elements[1], elements[2])
            ]
        )
        assert record.to_row("prefix/") == expected_row

    def test_equality(self):
        record = RecordEntry.from_elements(
            "file.py",
            "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
            "3144",
        )
        record_same = RecordEntry.from_elements(
            "file.py",
            "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
            "3144",
        )
        record_different_name = RecordEntry.from_elements(
            "file2.py",
            "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
            "3144",
        )
        record_different_hash_name = RecordEntry.from_elements(
            "file.py",
            "md5=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
            "3144",
        )
        record_different_hash_value = RecordEntry.from_elements(
            "file.py",
            "sha256=qwertyuiodfdsflkgshdlkjghrefawrwerwffsdfflk29",
            "3144",
        )
        record_different_size = RecordEntry.from_elements(
            "file.py",
            "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
            "10",
        )

        assert record == record_same

        assert record != "random string"
        assert record != record_different_name
        assert record != record_different_hash_name
        assert record != record_different_hash_value
        assert record != record_different_size

        # Ensure equality is based on current state
        record_same.hash_ = None
        assert record != record_same


class TestParseRecordFile:
    def test_accepts_empty_iterable(self):
        list(parse_record_file([]))

    @pytest.mark.parametrize(
        "record_input",
        ["record_simple_list", "record_simple_iter", "record_simple_file"],
        indirect=True,
    )
    def test_accepts_all_kinds_of_iterables(self, record_input):
        """Should accepts any iterable, e.g. container, iterator, or file object."""
        records = list(parse_record_file(record_input))
        assert len(records) == 2

        assert records == [
            (
                "file.py",
                "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
                "3144",
            ),
            ("distribution-1.0.dist-info/RECORD", "", ""),
        ]

    @pytest.mark.parametrize(
        "line, element_count",
        [
            pytest.param(
                "file.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144,",
                4,
                id="four",
            ),
            pytest.param(
                "distribution-1.0.dist-info/RECORD,,,,",
                5,
                id="five",
            ),
        ],
    )
    def test_rejects_wrong_element_count(self, line, element_count):
        with pytest.raises(InvalidRecordEntry) as exc_info:
            list(parse_record_file([line]))

        message = f"expected 3 elements, got {element_count}"
        assert message in str(exc_info.value)

    def test_shows_correct_row_number(self):
        record_lines = [
            "file1.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144",
            "file2.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144",
            "file3.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144",
            "distribution-1.0.dist-info/RECORD,,,,",
        ]
        with pytest.raises(InvalidRecordEntry) as exc_info:
            list(parse_record_file(record_lines))

        assert "Row Index 3" in str(exc_info.value)

    def test_parse_record_entry_with_comma(self):
        record_lines = [
            '"file1,file2.txt",sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144',
            "distribution-1.0.dist-info/RECORD,,",
        ]
        records = list(parse_record_file(record_lines))
        assert records == [
            (
                "file1,file2.txt",
                "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI",
                "3144",
            ),
            ("distribution-1.0.dist-info/RECORD", "", ""),
        ]

    def test_parse_record_entry_with_backslash_path(self):
        record_lines = [
            "distribution-1.0.dist-info\\RECORD,,",
        ]
        records = list(parse_record_file(record_lines))
        assert records == [
            ("distribution-1.0.dist-info/RECORD", "", ""),
        ]


class TestStructuredValidation:
    def test_blank_size_is_absent_not_zero(self):
        record = RecordEntry.from_elements("empty.bin", "", "")
        result = record.validation_result(b"")

        assert result.status is RecordStatus.ABSENT
        assert result.size_declared is False
        assert result.declared_size is None
        assert result.actual_size == 0
        assert result.size_status is RecordStatus.ABSENT
        assert result.ok

    def test_declared_zero_size_is_verified(self):
        record = RecordEntry.from_elements(
            "empty.bin", "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU", "0"
        )
        result = record.validation_result(b"")

        assert result.status is RecordStatus.VERIFIED
        assert result.size_declared is True
        assert result.declared_size == 0
        assert result.actual_size == 0
        assert result.size_status is RecordStatus.VERIFIED

    def test_declared_zero_size_mismatch(self):
        record = RecordEntry.from_elements("empty.bin", "", "0")
        result = record.validation_result(b"not empty")

        assert result.status is RecordStatus.MISMATCH
        assert result.size_status is RecordStatus.MISMATCH

    def test_zero_byte_file_with_wrong_hash_is_mismatch(self):
        record = RecordEntry.from_elements(
            "empty.bin", "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT_pNh2yI", "0"
        )
        result = record.validation_result(b"")

        assert result.status is RecordStatus.MISMATCH
        assert result.hash_status is RecordStatus.MISMATCH
        assert result.size_status is RecordStatus.VERIFIED

    def test_hash_declared_size_blank_still_verifies_hash(self):
        record = RecordEntry.from_elements(
            "file.bin", "sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4", ""
        )
        result = record.validation_result(b"test1\n")

        assert result.status is RecordStatus.ABSENT
        assert result.hash_status is RecordStatus.VERIFIED
        assert result.size_status is RecordStatus.ABSENT
        assert result.hash_declared is True
        assert result.size_declared is False

    def test_validation_result_keeps_raw_path(self):
        rows = list(
            parse_record_rows(
                [
                    "dist-info\\FILE.bin,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0"
                ]
            )
        )
        entry = rows[0].to_entry()
        result = entry.validation_result(b"")

        assert rows[0].raw_path == "dist-info\\FILE.bin"
        assert rows[0].path == "dist-info/FILE.bin"
        assert result.path == "dist-info/FILE.bin"
        assert result.raw_path == "dist-info\\FILE.bin"
        assert result.status is RecordStatus.VERIFIED


class TestRecordGroups:
    def test_identical_duplicates_keep_both_origins(self):
        rows = list(
            parse_record_rows(
                [
                    "file.bin,sha256=ABC,0",
                    "file.bin,sha256=ABC,0",
                ]
            )
        )
        groups = group_record_rows(rows)

        assert list(groups) == ["file.bin"]
        group = groups["file.bin"]
        assert group.conflicting is False
        assert len(group.occurrences) == 2
        assert [o.line_number for o in group.occurrences] == [1, 2]
        assert group.declarations == (("sha256=ABC", "0"),)

    def test_conflicting_duplicates_are_not_last_wins(self):
        rows = list(
            parse_record_rows(
                [
                    "file.bin,sha256=ABC,0",
                    "file.bin,sha256=XYZ,10",
                ]
            )
        )
        group = group_record_rows(rows)["file.bin"]

        assert group.conflicting is True
        assert len(group.declarations) == 2
        with pytest.raises(ConflictingRecordEntry) as exc_info:
            group.require_entry()
        assert exc_info.value.issues
        assert "conflicting" in str(exc_info.value)

    def test_blank_size_and_zero_size_conflict(self):
        rows = list(
            parse_record_rows(
                [
                    "file.bin,sha256=ABC,",
                    "file.bin,sha256=ABC,0",
                ]
            )
        )
        group = group_record_rows(rows)["file.bin"]

        assert group.conflicting is True
