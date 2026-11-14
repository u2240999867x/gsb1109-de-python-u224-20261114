"""Source of information about a wheel file."""

import posixpath
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    ClassVar,
    cast,
)

from installer.exceptions import InstallerError
from installer.records import (
    InvalidRecordEntry,
    RecordStatus,
    RecordValidationResult,
    group_record_rows,
    parse_record_rows,
    validate_record_entry,
)
from installer.utils import canonicalize_name, parse_wheel_filename

if TYPE_CHECKING:
    import os

WheelContentElement = tuple[tuple[str, str, str], BinaryIO, bool]


__all__ = ["WheelFile", "WheelSource"]


class WheelSource:
    """Represents an installable wheel.

    This is an abstract class, whose methods have to be implemented by subclasses.
    """

    validation_error: ClassVar[type[Exception]] = ValueError  #: :meta hide-value:
    """
    .. versionadded:: 0.7.0

    Exception to be raised by :py:meth:`validate_record` when validation fails.
    This is expected to be a subclass of :py:class:`ValueError`.
    """

    def __init__(self, distribution: str, version: str) -> None:
        """Initialize a WheelSource object.

        :param distribution: distribution name (like ``urllib3``)
        :param version: version associated with the wheel
        """
        super().__init__()
        self.distribution = distribution
        self.version = version

    @property
    def dist_info_dir(self) -> str:
        """Name of the dist-info directory."""
        return f"{self.distribution}-{self.version}.dist-info"

    @property
    def data_dir(self) -> str:
        """Name of the data directory."""
        return f"{self.distribution}-{self.version}.data"

    @property
    def dist_info_filenames(self) -> list[str]:
        """Get names of all files in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.dist_info_filenames
            ['METADATA', 'WHEEL']
        """
        raise NotImplementedError

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.read_dist_info("METADATA")
            ...

        :param filename: name of the file
        """
        raise NotImplementedError

    def validate_record(self) -> None:
        """Validate ``RECORD`` of the wheel.

        .. versionadded:: 0.7.0

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.
        """
        raise NotImplementedError

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This method should return an iterable. Each value from the iterable must be a
        tuple containing 3 elements:

        - record: 3-value tuple, to pass to
          :py:meth:`RecordEntry.from_elements <installer.records.RecordEntry.from_elements>`.
        - stream: An :py:class:`io.BufferedReader` object, providing the contents of the
          file at the location provided by the first element (path).
        - is_executable: A boolean, representing whether the item has an executable bit.

        All paths must be relative to the root of the wheel.

        Sample usage/behaviour::

            >>> iterable = wheel_source.get_contents()
            >>> next(iterable)
            (('pkg/__init__.py', '', '0'), <...>, False)

        This method may be called multiple times. Each iterable returned must
        provide the same content upon reading from a specific file's stream.
        """
        raise NotImplementedError


class _WheelFileValidationError(ValueError, InstallerError):
    """Raised when a wheel file fails validation."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__(repr(issues))
        self.issues = issues

    def __repr__(self) -> str:
        return f"WheelFileValidationError(issues={self.issues!r})"


class _WheelFileBadDistInfo(ValueError, InstallerError):
    """Raised when a wheel file has issues around `.dist-info`."""

    def __init__(self, *, reason: str, filename: str | None, dist_info: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.filename = filename
        self.dist_info = dist_info

    def __str__(self) -> str:
        return (
            f"{self.reason} (filename={self.filename!r}, dist_info={self.dist_info!r})"
        )


class WheelFile(WheelSource):
    """Implements `WheelSource`, for an existing file from the filesystem.

    Example usage::

        >>> with WheelFile.open("sampleproject-2.0.0-py3-none-any.whl") as source:
        ...     installer.install(source, destination)
    """

    validation_error = _WheelFileValidationError

    def __init__(self, f: zipfile.ZipFile) -> None:
        """Initialize a WheelFile object.

        :param f: An open zipfile, which will stay open as long as this object is used.
        """
        self._zipfile = f
        assert f.filename

        basename = Path(f.filename).name
        parsed_name = parse_wheel_filename(basename)
        super().__init__(
            version=parsed_name.version,
            distribution=parsed_name.distribution,
        )

    @classmethod
    @contextmanager
    def open(cls, path: "os.PathLike[str]") -> Iterator["WheelFile"]:
        """Create a wheelfile from a given path."""
        with zipfile.ZipFile(path) as f:
            yield cls(f)

    @cached_property
    def dist_info_dir(self) -> str:
        """Name of the dist-info directory."""
        top_level_directories = {
            path.split("/", 1)[0] for path in self._zipfile.namelist()
        }
        dist_infos = [
            name for name in top_level_directories if name.endswith(".dist-info")
        ]

        try:
            (dist_info_dir,) = dist_infos
        except ValueError:
            raise _WheelFileBadDistInfo(
                reason="Wheel doesn't contain exactly one .dist-info directory",
                filename=self._zipfile.filename,
                dist_info=str(sorted(dist_infos)),
            ) from None

        # NAME-VER.dist-info
        di_dname = dist_info_dir.rsplit("-", 2)[0]
        norm_di_dname = canonicalize_name(di_dname)
        norm_file_dname = canonicalize_name(self.distribution)

        if norm_di_dname != norm_file_dname:
            raise _WheelFileBadDistInfo(
                reason="Wheel .dist-info directory doesn't match wheel filename",
                filename=self._zipfile.filename,
                dist_info=dist_info_dir,
            )

        return dist_info_dir

    @property
    def dist_info_filenames(self) -> list[str]:
        """Get names of all files in the dist-info directory."""
        base = self.dist_info_dir
        return [
            name[len(base) + 1 :]
            for name in self._zipfile.namelist()
            if name[-1:] != "/"
            if base == posixpath.commonpath([name, base])
        ]

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory."""
        path = posixpath.join(self.dist_info_dir, filename)
        return self._zipfile.read(path).decode("utf-8")

    def validate_record(self, *, validate_contents: bool = True) -> None:
        """Validate ``RECORD`` of the wheel.

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.

        File names will always be validated against ``RECORD``.

        If ``validate_contents`` is true, sizes and hashes of files
        will also be validated against ``RECORD``.

        :param validate_contents: Whether to validate content integrity.
        """
        issues = [
            issue
            for result in self.iter_record_validation(
                validate_contents=validate_contents
            )
            for issue in result.issues
        ]
        if issues:
            raise _WheelFileValidationError(issues)

    def iter_record_validation(
        self, *, validate_contents: bool = True
    ) -> Iterator[RecordValidationResult]:
        """Validate each file in the wheel, yielding structured results.

        Each result reports one file with a :class:`RecordStatus`:

        - ``ABSENT``: RECORD does not declare the hash and/or size (a blank
          cell), or the file has no RECORD entry at all.
        - ``VERIFIED``: every declaration present in RECORD matches the file.
        - ``MISMATCH``: a present declaration does not match, declarations
          conflict, or the entry itself is invalid.

        The original path spelling and every declaration's origin are retained
        in each result. Duplicate rows with the same declaration are reported as
        multiple origins; duplicate rows with different declarations are a
        mismatch and never resolved by taking the last row.

        :param validate_contents: Whether to read file contents for validation.
        """
        try:
            record_lines = self.read_dist_info("RECORD").splitlines()
            groups = group_record_rows(parse_record_rows(record_lines))
        except Exception as exc:
            raise _WheelFileValidationError(
                [f"Unable to retrieve `RECORD` from {self._zipfile.filename}: {exc!r}"]
            ) from exc

        record_path = f"{self.dist_info_dir}/RECORD"
        signature_names = ("RECORD.p7s", "RECORD.jws")
        seen_paths: set[str] = set()

        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue

            group = groups.get(item.filename)
            seen_paths.add(item.filename)

            if (
                self.dist_info_dir
                == posixpath.commonpath([self.dist_info_dir, item.filename])
                and item.filename.split("/")[-1] in signature_names
            ):
                # Both are for digital signatures, and not mentioned in RECORD.
                if group is not None:
                    yield RecordValidationResult(
                        path=item.filename,
                        raw_path=group.raw_paths[0],
                        status=RecordStatus.MISMATCH,
                        occurrences=group.occurrences,
                        issues=(
                            f"In {self._zipfile.filename}, digital signature file "
                            f"{item.filename} is incorrectly contained in RECORD.",
                        ),
                    )
                else:
                    yield RecordValidationResult(
                        path=item.filename,
                        raw_path=item.filename,
                        status=RecordStatus.ABSENT,
                    )
                continue

            if group is None:
                yield RecordValidationResult(
                    path=item.filename,
                    raw_path=item.filename,
                    status=RecordStatus.ABSENT,
                    issues=(
                        f"In {self._zipfile.filename}, {item.filename} is not "
                        "mentioned in RECORD",
                    ),
                )
                continue

            if group.conflicting:
                declarations = "; ".join(
                    f"{occurrence.line_number}:{occurrence.declaration!r}"
                    for occurrence in group.occurrences
                )
                yield RecordValidationResult(
                    path=item.filename,
                    raw_path=group.raw_paths[0],
                    status=RecordStatus.MISMATCH,
                    occurrences=group.occurrences,
                    issues=(
                        f"In {self._zipfile.filename}, conflicting RECORD entries "
                        f"for {item.filename}: {declarations}",
                    ),
                )
                continue

            try:
                record = group.require_entry()
            except InvalidRecordEntry as exc:
                yield RecordValidationResult(
                    path=item.filename,
                    raw_path=group.raw_paths[0],
                    status=RecordStatus.MISMATCH,
                    occurrences=group.occurrences,
                    issues=tuple(
                        f"In {self._zipfile.filename}, entry in RECORD file for "
                        f"{item.filename} is invalid: {issue}"
                        for issue in exc.issues
                    ),
                )
                continue

            if item.filename == record_path:
                # RECORD must not contain its own hash / size.
                if record.hash_ is not None or record.size is not None:
                    yield RecordValidationResult(
                        path=item.filename,
                        raw_path=group.raw_paths[0],
                        status=RecordStatus.MISMATCH,
                        entry=record,
                        occurrences=group.occurrences,
                        issues=(
                            f"In {self._zipfile.filename}, RECORD file incorrectly "
                            "contains hash / size.",
                        ),
                    )
                else:
                    yield RecordValidationResult(
                        path=item.filename,
                        raw_path=group.raw_paths[0],
                        status=RecordStatus.ABSENT,
                        entry=record,
                        occurrences=group.occurrences,
                    )
                continue

            declared_absent = record.hash_ is None or record.size is None
            if validate_contents:
                with self._zipfile.open(item, "r") as stream:
                    result = validate_record_entry(
                        record,
                        cast("BinaryIO", stream),
                        raw_path=group.raw_paths[0],
                        occurrences=group.occurrences,
                    )
                issues = []
                if declared_absent:
                    issues.append(
                        f"In {self._zipfile.filename}, hash / size of "
                        f"{item.filename} is not included in RECORD"
                    )
                if result.mismatched:
                    issues.append(
                        f"In {self._zipfile.filename}, hash / size of "
                        f"{item.filename} didn't match RECORD"
                    )
                if issues:
                    yield RecordValidationResult(
                        path=result.path,
                        raw_path=result.raw_path,
                        status=(
                            RecordStatus.MISMATCH
                            if result.mismatched
                            else result.status
                        ),
                        entry=result.entry,
                        occurrences=result.occurrences,
                        actual_hash=result.actual_hash,
                        actual_size=result.actual_size,
                        hash_status=result.hash_status,
                        size_status=result.size_status,
                        issues=tuple(issues),
                    )
                else:
                    yield result
            else:
                yield RecordValidationResult(
                    path=record.path,
                    raw_path=group.raw_paths[0],
                    status=(
                        RecordStatus.ABSENT
                        if declared_absent
                        else RecordStatus.VERIFIED
                    ),
                    entry=record,
                    occurrences=group.occurrences,
                    issues=(
                        (
                            f"In {self._zipfile.filename}, hash / size of "
                            f"{item.filename} is not included in RECORD",
                        )
                        if declared_absent
                        else ()
                    ),
                )

        for path, group in groups.items():
            if path in seen_paths:
                continue
            yield RecordValidationResult(
                path=path,
                raw_path=group.raw_paths[0],
                status=RecordStatus.MISMATCH,
                occurrences=group.occurrences,
                issues=(
                    f"In {self._zipfile.filename}, {path} is declared in RECORD "
                    "but is not present in the wheel",
                ),
            )

    def validate_record_entries(
        self, *, validate_contents: bool = True
    ) -> list[RecordValidationResult]:
        """Return structured validation results for every RECORD entry.

        Unlike :py:meth:`validate_record`, this does not raise on problems; the
        caller can inspect each result's status and issues. The results are a
        list snapshot of :py:meth:`iter_record_validation`.
        """
        return list(self.iter_record_validation(validate_contents=validate_contents))

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This implementation requires that every file that is a part of the wheel
        archive has a corresponding entry in RECORD. If they are not, an
        :any:`AssertionError` will be raised.
        """
        # Group rows by normalized path so duplicates are not resolved by the
        # last row. Identical duplicates are equivalent; conflicting ones raise.
        record_lines = self.read_dist_info("RECORD").splitlines()
        record_groups = group_record_rows(parse_record_rows(record_lines))

        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue

            # Default to an empty declaration, because validation is handled by
            # `validate_record`.
            group = record_groups.get(item.filename)
            if group is None:
                record = (item.filename, "", "")
            else:
                entry = group.require_entry()
                record = (
                    entry.path,
                    str(entry.hash_ or ""),
                    str(entry.size) if entry.size is not None else "",
                )

            # Borrowed from:
            # https://github.com/pypa/pip/blob/0f21fb92/src/pip/_internal/utils/unpacking.py#L96-L100
            mode = item.external_attr >> 16
            is_executable = bool(mode and stat.S_ISREG(mode) and mode & 0o111)

            with self._zipfile.open(item) as stream:
                stream_casted = cast("BinaryIO", stream)
                yield record, stream_casted, is_executable
