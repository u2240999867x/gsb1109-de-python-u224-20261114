"""Offline reconciliation of an installed wheel against its source artifacts.

The installer is intentionally allowed to change a small number of things while
unpacking a wheel:

* files are normally copied byte-for-byte;
* files in the ``scripts`` scheme may have their ``#!python`` first line
  replaced with the selected interpreter;
* console and GUI launchers are generated from ``entry_points.txt``;
* the installed ``RECORD`` file is generated from the actual installed files.

This module builds the expected installation completely in memory, then compares
it with an existing installation.  It never executes anything from the wheel or
from the installation.  Every decision keeps the source-side and installed-side
digests so an unexpected difference can be audited later.

The important rule is that a script is not "allowed to be anything" just because
it lives in the ``scripts`` directory.  If the only permitted transformation is
the shebang replacement, the rest of the file must still match byte-for-byte.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import posixpath
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from installer._core import _determine_scheme, _process_WHEEL_file
from installer.exceptions import InstallerError
from installer.records import Hash, RecordEntry
from installer.scripts import Script
from installer.utils import (
    _WINDOWS,
    Scheme,
    construct_record_file,
    fix_shebang,
    get_launcher_kind,
    parse_entrypoints,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable, Mapping, Sequence

    from installer.scripts import LauncherKind
    from installer.sources import WheelSource

__all__ = [
    "Difference",
    "Evidence",
    "LedgerEntry",
    "Provenance",
    "ReconciliationError",
    "ReconciliationLedger",
    "Status",
    "reconcile",
    "reconcile_wheel",
]


class ReconciliationError(ValueError, InstallerError):
    """Raised when a reconciliation cannot be performed."""


class Provenance(str, Enum):
    """How an installed artifact is derived from the wheel."""

    VERBATIM = "verbatim"
    """The wheel file must be copied byte-for-byte."""

    REWRITTEN = "rewritten"
    """Only the shebang line may be replaced with the selected interpreter."""

    GENERATED = "generated"
    """The file is generated from entry point declarations or from RECORD."""

    UNKNOWN = "unknown"
    """The file exists in the installation but is not explained by the wheel."""

    def __str__(self) -> str:
        return self.value


class Status(str, Enum):
    """Result of comparing one expected or discovered artifact."""

    MATCH = "match"
    """The installed bytes are exactly the expected bytes."""

    MISSING = "missing"
    """An artifact expected from the wheel is not installed."""

    UNEXPECTED_DIFFERENCE = "unexpected-difference"
    """The installed bytes differ from the bytes allowed by the plan."""

    UNKNOWN = "unknown"
    """An installed artifact has no source in the wheel or entry points."""

    CONFLICT = "conflict"
    """More than one expected artifact targets the same installed path."""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Evidence:
    """A digest and provenance for one side of a comparison."""

    origin: str
    """Where the bytes came from, such as a wheel member or a filesystem path."""

    sha256: str
    """SHA-256 digest of the bytes, as lowercase hexadecimal."""

    size: int
    """Number of bytes."""

    detail: str | None = None
    """Optional human-readable context, such as an entry point declaration."""

    @classmethod
    def from_bytes(
        cls, origin: str, data: bytes, *, detail: str | None = None
    ) -> Evidence:
        """Build evidence from an in-memory byte string.

        :param origin: where the bytes came from
        :param data: bytes to summarise
        :param detail: optional context for the digest
        """
        return cls(
            origin=origin,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            detail=detail,
        )

    def to_dict(self) -> dict[str, object]:
        """Convert this evidence into a JSON-safe mapping."""
        result: dict[str, object] = {
            "origin": self.origin,
            "sha256": self.sha256,
            "size": self.size,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class Difference:
    """The first byte-level difference between expected and installed bytes."""

    offset: int
    """Zero-based byte offset of the first difference."""

    line: int
    """One-based line number of the first difference in the expected bytes."""

    expected_byte: int | None
    """Expected byte value, or ``None`` when the expected data ended first."""

    installed_byte: int | None
    """Installed byte value, or ``None`` when the installed data ended first."""

    expected_context: str
    """Short ASCII representation of expected bytes around the difference."""

    installed_context: str
    """Short ASCII representation of installed bytes around the difference."""

    def to_dict(self) -> dict[str, object]:
        """Convert this difference into a JSON-safe mapping."""
        return {
            "offset": self.offset,
            "line": self.line,
            "expected_byte": self.expected_byte,
            "installed_byte": self.installed_byte,
            "expected_context": self.expected_context,
            "installed_context": self.installed_context,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """One artifact in the reconciliation ledger."""

    path: str
    """Path relative to the scheme root."""

    scheme: Scheme
    """Installation scheme containing the artifact."""

    provenance: Provenance
    """How the artifact is derived from the wheel."""

    status: Status
    """Result of comparing the artifact with the installation."""

    source: Evidence | None = None
    """Evidence from the wheel source, when one exists."""

    expected: Evidence | None = None
    """Evidence for the bytes the installation plan allows."""

    installed: Evidence | None = None
    """Evidence for the bytes found in the installation."""

    difference: Difference | None = None
    """First byte-level difference, for an unexpected difference."""

    detail: str | None = None
    """Additional explanation, such as a conflict or generation source."""

    def to_dict(self) -> dict[str, object]:
        """Convert this entry into a JSON-safe mapping."""
        return {
            "path": self.path,
            "scheme": str(self.scheme),
            "provenance": self.provenance.value,
            "status": self.status.value,
            "source": self.source.to_dict() if self.source is not None else None,
            "expected": self.expected.to_dict() if self.expected is not None else None,
            "installed": (
                self.installed.to_dict() if self.installed is not None else None
            ),
            "difference": (
                self.difference.to_dict() if self.difference is not None else None
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconciliationLedger:
    """The complete result of reconciling one wheel installation."""

    entries: tuple[LedgerEntry, ...]
    wheel: str | None
    interpreter: str
    script_kind: str
    scheme_dict: Mapping[str, str]
    destdir: str | None
    scanned_schemes: tuple[Scheme, ...]

    @property
    def problems(self) -> tuple[LedgerEntry, ...]:
        """Return entries that are not exact plan matches."""
        return tuple(
            entry for entry in self.entries if entry.status is not Status.MATCH
        )

    @property
    def ok(self) -> bool:
        """Whether every expected artifact matched and no unknown files exist."""
        return not self.problems

    def counts(self) -> dict[str, dict[str, int]]:
        """Count entries by provenance and by status."""
        by_provenance = {provenance.value: 0 for provenance in Provenance}
        by_status = {status.value: 0 for status in Status}
        for entry in self.entries:
            by_provenance[entry.provenance.value] += 1
            by_status[entry.status.value] += 1
        return {"provenance": by_provenance, "status": by_status}

    def to_dict(self) -> dict[str, object]:
        """Convert the ledger into a JSON-safe mapping."""
        return {
            "wheel": self.wheel,
            "interpreter": self.interpreter,
            "script_kind": self.script_kind,
            "scheme_dict": dict(self.scheme_dict),
            "destdir": self.destdir,
            "scanned_schemes": [str(scheme) for scheme in self.scanned_schemes],
            "ok": self.ok,
            "counts": self.counts(),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialise the ledger as JSON."""
        return json.dumps(self.to_dict(), indent=indent)


@dataclass(frozen=True)
class _PlannedArtifact:
    scheme: Scheme
    path: str
    data: bytes
    provenance: Provenance
    source: Evidence | None
    detail: str | None = None
    comparison_data: bytes | None = None
    launcher_data: bytes | None = None
    record_payloads: tuple[tuple[Scheme, str, bytes, bytes | None], ...] | None = None
    record_file_path: str | None = None
    record_scheme: Scheme | None = None
    hash_algorithm: str = "sha256"

    @property
    def compare_bytes(self) -> bytes:
        return self.data if self.comparison_data is None else self.comparison_data


def _invalid_zip(data: bytes) -> bytes:
    return b"INVALID-ZIP\0" + data


def _canonical_zip_payload(data: bytes) -> bytes:
    """Return a deterministic, content-level representation of a ZIP archive.

    Launcher archives record the current time in each member, so a byte-exact
    comparison would report a false difference for every healthy Windows
    installation.  This projection keeps member names, compression methods,
    CRCs, sizes and decompressed contents, and rejects archives with leading or
    trailing bytes.  A changed ``__main__.py`` or an appended payload is still
    reported as a difference.
    """
    import zipfile

    if not data.startswith(b"PK\x03\x04"):
        return _invalid_zip(data)

    search_from = max(0, len(data) - 65_557)
    end_record = data.rfind(b"PK\x05\x06", search_from)
    if end_record < 0 or end_record + 22 > len(data):
        return _invalid_zip(data)

    comment_length = int.from_bytes(data[end_record + 20 : end_record + 22], "little")
    if end_record + 22 + comment_length != len(data):
        return _invalid_zip(data)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            parts = [b"ZIP2", len(members).to_bytes(4, "big")]
            for info in sorted(members, key=lambda member: member.filename):
                contents = archive.read(info)
                name = info.filename.encode("utf-8")
                parts.extend(
                    [
                        len(name).to_bytes(4, "big"),
                        name,
                        info.compress_type.to_bytes(2, "big"),
                        (info.CRC & 0xFFFFFFFF).to_bytes(4, "big"),
                        info.file_size.to_bytes(8, "big"),
                        len(contents).to_bytes(8, "big"),
                        contents,
                    ]
                )
    except (OSError, ValueError, zipfile.BadZipFile):
        return _invalid_zip(data)

    parts.append(data[end_record + 22 :])
    return b"".join(parts)


def _launcher_projection(data: bytes, launcher: bytes | None) -> bytes:
    """Project a launcher onto bytes that do not depend on ZIP timestamps.

    Launcher archives are written by :mod:`zipfile`, which records the current
    time in each member.  Comparing those timestamps would report a false
    difference for every healthy installation, so the launcher prefix and
    shebang stay byte-exact while the archive is compared by member name,
    compression method, CRC, size and contents.
    """
    if launcher is None:
        return data
    if not data.startswith(launcher):
        return data
    rest = data[len(launcher) :]
    shebang, separator, archive = rest.partition(b"\n")
    if not separator:
        return data
    canonical = _canonical_zip_payload(archive)
    if canonical.startswith(b"INVALID-ZIP\0"):
        return data
    return launcher + shebang + b"\n" + canonical


def _record_hash(data: bytes, algorithm: str) -> str:
    digest = hashlib.new(algorithm, data).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _record_prefix(
    record_scheme: Scheme,
    file_scheme: Scheme,
    scheme_dict: Mapping[str, str],
) -> str | None:
    if file_scheme == record_scheme:
        return None
    if _WINDOWS:  # pragma: no cover
        path = os.path.abspath(scheme_dict[file_scheme])  # noqa: PTH100
    else:
        path = os.path.relpath(
            scheme_dict[file_scheme],
            start=scheme_dict[record_scheme],
        )
    return path + "/"


def _required_scheme(scheme: str, scheme_dict: Mapping[str, str]) -> Scheme:
    if scheme not in scheme_dict:
        message = f"Installation scheme {scheme!r} is not present in scheme_dict"
        raise ReconciliationError(message)
    return Scheme(scheme)


def _build_plan(
    source: WheelSource,
    interpreter: str,
    script_kind: LauncherKind,
    additional_metadata: Mapping[str, bytes],
    scheme_dict: Mapping[str, str],
    hash_algorithm: str,
) -> list[_PlannedArtifact]:
    root_scheme = _process_WHEEL_file(source)
    record_file_path = posixpath.join(source.dist_info_dir, "RECORD")
    artifacts: list[_PlannedArtifact] = []

    record_source: Evidence | None = None
    if "RECORD" in source.dist_info_filenames:
        record_source = Evidence.from_bytes(
            f"wheel:{record_file_path}",
            source.read_dist_info("RECORD").encode(),
            detail=record_file_path,
        )

    if "entry_points.txt" in source.dist_info_filenames:
        entry_points_path = posixpath.join(source.dist_info_dir, "entry_points.txt")
        entry_points_bytes = source.read_dist_info("entry_points.txt").encode()
        for name, module, attr, section in parse_entrypoints(
            entry_points_bytes.decode("utf-8")
        ):
            script = Script(name, module, attr, section)
            script_name, data = script.generate(interpreter, script_kind)
            launcher = script._get_launcher_data(script_kind)
            source_evidence = Evidence.from_bytes(
                f"wheel:{entry_points_path}",
                entry_points_bytes,
                detail=f"[{section}_scripts] {name} = {module}:{attr}",
            )
            artifacts.append(
                _PlannedArtifact(
                    scheme=Scheme("scripts"),
                    path=script_name,
                    data=data,
                    provenance=Provenance.GENERATED,
                    source=source_evidence,
                    detail=f"entry point: {name}",
                    comparison_data=_launcher_projection(data, launcher),
                    launcher_data=launcher,
                )
            )

    for record_elements, stream, _is_executable in source.get_contents():
        source_record = RecordEntry.from_elements(*record_elements)
        path = source_record.path
        if path == record_file_path:
            continue
        if "__pycache__" in path.split("/")[:-1]:
            continue

        scheme, destination_path = _determine_scheme(
            path=path,
            source=source,
            root_scheme=root_scheme,
        )
        data = stream.read()
        source_evidence = Evidence.from_bytes(f"wheel:{path}", data, detail=path)

        if scheme == "scripts":
            with fix_shebang(io.BytesIO(data), interpreter) as transformed:
                expected_data = transformed.read()
            provenance = (
                Provenance.REWRITTEN
                if data.startswith(b"#!python")
                else Provenance.VERBATIM
            )
        else:
            expected_data = data
            provenance = Provenance.VERBATIM

        artifacts.append(
            _PlannedArtifact(
                scheme=scheme,
                path=destination_path,
                data=expected_data,
                provenance=provenance,
                source=source_evidence,
                detail=f"source: {path}",
            )
        )

    for filename, contents in additional_metadata.items():
        path = posixpath.join(source.dist_info_dir, filename)
        source_evidence = Evidence.from_bytes(
            f"provided:{filename}", contents, detail=filename
        )
        artifacts.append(
            _PlannedArtifact(
                scheme=root_scheme,
                path=path,
                data=contents,
                provenance=Provenance.VERBATIM,
                source=source_evidence,
                detail=f"additional metadata: {filename}",
            )
        )

    record_entries = [
        (
            artifact.scheme,
            RecordEntry(
                artifact.path,
                Hash(hash_algorithm, _record_hash(artifact.data, hash_algorithm)),
                len(artifact.data),
            ),
        )
        for artifact in artifacts
    ]
    record_entries.append((root_scheme, RecordEntry(record_file_path, None, None)))
    record_stream = construct_record_file(
        record_entries,
        lambda file_scheme: _record_prefix(root_scheme, file_scheme, scheme_dict),
    )
    try:
        expected_record = record_stream.read()
    finally:
        record_stream.close()
    artifacts.append(
        _PlannedArtifact(
            scheme=root_scheme,
            path=record_file_path,
            data=expected_record,
            provenance=Provenance.GENERATED,
            source=record_source,
            detail="installed RECORD",
            record_payloads=tuple(
                (
                    artifact.scheme,
                    artifact.path,
                    artifact.data,
                    artifact.launcher_data,
                )
                for artifact in artifacts
            ),
            record_file_path=record_file_path,
            record_scheme=root_scheme,
            hash_algorithm=hash_algorithm,
        )
    )

    return artifacts


def _installed_path(
    scheme_dict: Mapping[str, str],
    scheme: Scheme,
    path: str,
    destdir: str | None,
) -> Path:
    target_dir = Path(os.path.abspath(scheme_dict[scheme]))  # noqa: PTH100
    file = Path(os.path.abspath(target_dir / path))  # noqa: PTH100
    if not file.is_relative_to(target_dir):
        message = f"Attempting to read {path} outside of the target directory"
        raise ReconciliationError(message)
    if destdir is not None:
        rel_path = file.relative_to(file.anchor)
        return Path(destdir) / rel_path
    return file


def _scheme_root(
    scheme_dict: Mapping[str, str],
    scheme: Scheme,
    destdir: str | None,
) -> Path:
    root = Path(os.path.abspath(scheme_dict[scheme]))  # noqa: PTH100
    if destdir is not None:
        return Path(destdir) / root.relative_to(root.anchor)
    return root


def _first_difference(expected: bytes, installed: bytes) -> Difference:
    shared = min(len(expected), len(installed))
    offset = shared
    for index in range(shared):
        if expected[index] != installed[index]:
            offset = index
            break

    line = expected[:offset].count(b"\n") + 1
    context_start = max(0, offset - 16)
    context_end = offset + 16
    return Difference(
        offset=offset,
        line=line,
        expected_byte=expected[offset] if offset < len(expected) else None,
        installed_byte=installed[offset] if offset < len(installed) else None,
        expected_context=repr(expected[context_start:context_end]),
        installed_context=repr(installed[context_start:context_end]),
    )


def _record_matches_installed(
    artifact: _PlannedArtifact,
    installed_record: bytes,
    scheme_dict: Mapping[str, str],
    destdir: str | None,
) -> bool:
    """Check whether RECORD is consistent with the installed payload files.

    Windows launchers contain a ZIP timestamp, so regenerating one can produce
    different bytes than the copy that was installed.  In that case the RECORD
    must still be internally consistent with what is actually on disk.  The
    certificate is rebuilt from installed payload bytes and compared exactly;
    any other change, such as a dropped or added row, is still a difference.
    """
    assert artifact.record_payloads is not None
    assert artifact.record_file_path is not None
    assert artifact.record_scheme is not None

    record_scheme = artifact.record_scheme
    entries: list[tuple[Scheme, RecordEntry]] = []
    for scheme, path, expected_data, launcher in artifact.record_payloads:
        installed_path = _installed_path(scheme_dict, scheme, path, destdir)
        if not installed_path.is_file():
            return False
        installed_data = installed_path.read_bytes()
        if installed_data != expected_data and _launcher_projection(
            installed_data, launcher
        ) != _launcher_projection(expected_data, launcher):
            return False
        entries.append(
            (
                scheme,
                RecordEntry(
                    path,
                    Hash(
                        artifact.hash_algorithm,
                        _record_hash(installed_data, artifact.hash_algorithm),
                    ),
                    len(installed_data),
                ),
            )
        )

    entries.append((record_scheme, RecordEntry(artifact.record_file_path, None, None)))
    stream = construct_record_file(
        entries,
        lambda file_scheme: _record_prefix(record_scheme, file_scheme, scheme_dict),
    )
    try:
        return stream.read() == installed_record
    finally:
        stream.close()


def _compare_artifact(
    artifact: _PlannedArtifact,
    scheme_dict: Mapping[str, str],
    destdir: str | None,
) -> tuple[LedgerEntry, Path]:
    installed_path = _installed_path(
        scheme_dict, artifact.scheme, artifact.path, destdir
    )
    expected_evidence = Evidence.from_bytes(
        f"expected:{artifact.scheme}:{artifact.path}",
        artifact.data,
    )

    if not installed_path.is_file():
        return (
            LedgerEntry(
                path=artifact.path,
                scheme=artifact.scheme,
                provenance=artifact.provenance,
                status=Status.MISSING,
                source=artifact.source,
                expected=expected_evidence,
                detail=artifact.detail,
            ),
            installed_path,
        )

    installed_data = installed_path.read_bytes()
    installed_evidence = Evidence.from_bytes(
        f"file:{installed_path}",
        installed_data,
        detail=str(installed_path),
    )
    if installed_data == artifact.data:
        return (
            LedgerEntry(
                path=artifact.path,
                scheme=artifact.scheme,
                provenance=artifact.provenance,
                status=Status.MATCH,
                source=artifact.source,
                expected=expected_evidence,
                installed=installed_evidence,
                detail=artifact.detail,
            ),
            installed_path,
        )

    expected_projection = artifact.compare_bytes
    installed_projection = _launcher_projection(installed_data, artifact.launcher_data)
    if installed_projection == expected_projection:
        return (
            LedgerEntry(
                path=artifact.path,
                scheme=artifact.scheme,
                provenance=artifact.provenance,
                status=Status.MATCH,
                source=artifact.source,
                expected=expected_evidence,
                installed=installed_evidence,
                detail=(
                    f"{artifact.detail}; launcher ZIP timestamps differ but payload "
                    "matched"
                    if artifact.launcher_data is not None
                    else artifact.detail
                ),
            ),
            installed_path,
        )

    if artifact.record_payloads is not None and _record_matches_installed(
        artifact, installed_data, scheme_dict, destdir
    ):
        return (
            LedgerEntry(
                path=artifact.path,
                scheme=artifact.scheme,
                provenance=artifact.provenance,
                status=Status.MATCH,
                source=artifact.source,
                expected=expected_evidence,
                installed=installed_evidence,
                detail=(
                    f"{artifact.detail}; digest follows installed bytes whose launcher "
                    "ZIP timestamp differs"
                ),
            ),
            installed_path,
        )

    return (
        LedgerEntry(
            path=artifact.path,
            scheme=artifact.scheme,
            provenance=artifact.provenance,
            status=Status.UNEXPECTED_DIFFERENCE,
            source=artifact.source,
            expected=expected_evidence,
            installed=installed_evidence,
            difference=_first_difference(expected_projection, installed_projection),
            detail=artifact.detail,
        ),
        installed_path,
    )


def _scan_unknowns(
    scheme_dict: Mapping[str, str],
    expected_keys: set[tuple[Scheme, str]],
    scanned_schemes: Sequence[Scheme],
    destdir: str | None,
    ignore_bytecode: bool,
) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    seen_paths: set[Path] = set()
    for scheme in scanned_schemes:
        root = _scheme_root(scheme_dict, scheme, destdir)
        if not root.is_dir():
            continue
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            if (
                ignore_bytecode
                and scheme in ("purelib", "platlib")
                and (file_path.suffix == ".pyc" or "__pycache__" in file_path.parts)
            ):
                continue
            resolved = file_path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            relative_path = file_path.relative_to(root).as_posix()
            if (scheme, relative_path) in expected_keys:
                continue
            data = file_path.read_bytes()
            entries.append(
                LedgerEntry(
                    path=relative_path,
                    scheme=scheme,
                    provenance=Provenance.UNKNOWN,
                    status=Status.UNKNOWN,
                    installed=Evidence.from_bytes(
                        f"file:{file_path}", data, detail=str(file_path)
                    ),
                    detail="no wheel file or entry point declaration produces this path",
                )
            )
    return entries


def reconcile(
    source: WheelSource,
    *,
    scheme_dict: Mapping[str, str],
    interpreter: str,
    script_kind: LauncherKind | None = None,
    destdir: str | os.PathLike[str] | None = None,
    additional_metadata: Mapping[str, bytes] | None = None,
    scan_schemes: Iterable[str] = ("scripts",),
    ignore_bytecode: bool = True,
    hash_algorithm: str = "sha256",
    wheel: str | None = None,
) -> ReconciliationLedger:
    """Reconcile an existing installation with the plan for ``source``.

    The expected installation is constructed in memory using the same rules as
    :class:`installer.destinations.SchemeDictionaryDestination`: non-script
    files must be verbatim copies, script shebangs may be rewritten, and entry
    point scripts plus the installed ``RECORD`` file are generated.

    Files in ``scan_schemes`` are enumerated recursively so an extra script is
    reported as :attr:`Provenance.UNKNOWN` instead of being accepted merely
    because it is in a scripts directory.  Other schemes are checked only for
    the files expected from this wheel, which avoids reporting unrelated
    packages in shared ``purelib``/``platlib`` directories.

    :param source: wheel source to derive the expected installation from
    :param scheme_dict: mapping of scheme name to installation directory
    :param interpreter: interpreter path used for shebangs and launchers
    :param script_kind: launcher kind, defaulting to the current platform
    :param destdir: optional staging root, handled like ``--destdir``
    :param additional_metadata: extra metadata files written during installation
    :param scan_schemes: schemes whose directories are scanned for unknown files
    :param ignore_bytecode: skip ``.pyc`` files and ``__pycache__`` directories
        inside ``purelib`` and ``platlib``; bytecode in other schemes is always
        reported because the installer does not produce it there
    :param hash_algorithm: algorithm used in the generated ``RECORD`` file
    :param wheel: optional wheel name recorded in the ledger
    :return: reconciliation ledger
    :raises ReconciliationError: if the plan cannot be constructed or read
    """
    if script_kind is None:
        script_kind = get_launcher_kind()
    if additional_metadata is None:
        additional_metadata = {}

    normalized_destdir = os.fspath(destdir) if destdir is not None else None
    artifacts = _build_plan(
        source,
        interpreter,
        script_kind,
        additional_metadata,
        scheme_dict,
        hash_algorithm,
    )
    if not artifacts:
        message = "The wheel produced no installation artifacts"
        raise ReconciliationError(message)

    grouped: dict[tuple[Scheme, str], list[_PlannedArtifact]] = defaultdict(list)
    for artifact in artifacts:
        _required_scheme(str(artifact.scheme), scheme_dict)
        grouped[(artifact.scheme, artifact.path)].append(artifact)

    scanned = tuple(
        _required_scheme(str(scheme), scheme_dict) for scheme in scan_schemes
    )

    entries: list[LedgerEntry] = []
    for (scheme, path), matching in sorted(
        grouped.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        primary = matching[-1]
        entry, installed_path = _compare_artifact(
            primary, scheme_dict, normalized_destdir
        )
        entries.append(entry)
        if len(matching) > 1:
            installed_evidence = None
            if installed_path.is_file():
                installed_evidence = Evidence.from_bytes(
                    f"file:{installed_path}",
                    installed_path.read_bytes(),
                    detail=str(installed_path),
                )
            for shadowed in matching[:-1]:
                entries.append(
                    LedgerEntry(
                        path=path,
                        scheme=scheme,
                        provenance=shadowed.provenance,
                        status=Status.CONFLICT,
                        source=shadowed.source,
                        expected=Evidence.from_bytes(
                            f"expected:{scheme}:{path}", shadowed.data
                        ),
                        installed=installed_evidence,
                        detail=(
                            "another wheel member or entry point later writes this "
                            "same path; the final writer is the compared artifact"
                        ),
                    )
                )

    expected_keys = set(grouped)
    entries.extend(
        _scan_unknowns(
            scheme_dict,
            expected_keys,
            scanned,
            normalized_destdir,
            ignore_bytecode,
        )
    )
    entries.sort(key=lambda entry: (str(entry.scheme), entry.path, entry.status.value))

    return ReconciliationLedger(
        entries=tuple(entries),
        wheel=wheel,
        interpreter=interpreter,
        script_kind=script_kind,
        scheme_dict=dict(scheme_dict),
        destdir=normalized_destdir,
        scanned_schemes=scanned,
    )


def reconcile_wheel(
    wheel_path: os.PathLike[str],
    *,
    scheme_dict: Mapping[str, str],
    interpreter: str,
    script_kind: LauncherKind | None = None,
    destdir: str | os.PathLike[str] | None = None,
    additional_metadata: Mapping[str, bytes] | None = None,
    scan_schemes: Iterable[str] = ("scripts",),
    ignore_bytecode: bool = True,
    hash_algorithm: str = "sha256",
) -> ReconciliationLedger:
    """Reconcile an installation with a wheel on disk.

    This is a convenience wrapper around :func:`reconcile` that opens and closes
    the wheel for the caller.
    """
    from installer.sources import WheelFile

    with WheelFile.open(wheel_path) as source:
        return reconcile(
            source,
            scheme_dict=scheme_dict,
            interpreter=interpreter,
            script_kind=script_kind,
            destdir=destdir,
            additional_metadata=additional_metadata,
            scan_schemes=scan_schemes,
            ignore_bytecode=ignore_bytecode,
            hash_algorithm=hash_algorithm,
            wheel=os.fspath(wheel_path),
        )


def _parse_scheme(value: str) -> tuple[str, str]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        message = f"expected NAME=PATH, got {value!r}"
        raise ValueError(message)
    return name, path


def _get_main_parser() -> argparse.ArgumentParser:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m installer.reconcile",
        description=(
            "Reconcile an installed wheel tree against an offline installation "
            "plan without executing any installed code."
        ),
    )
    parser.add_argument("wheel", type=str, help="wheel file that was installed")
    parser.add_argument(
        "--scheme",
        action="append",
        default=[],
        metavar="NAME=PATH",
        type=_parse_scheme,
        help=(
            "installation scheme root; repeat for each scheme "
            "(default: the current interpreter's sysconfig paths)"
        ),
    )
    parser.add_argument(
        "--interpreter",
        default=sys.executable,
        help="interpreter path used for shebangs and launchers",
    )
    parser.add_argument(
        "--script-kind",
        default=None,
        choices=["posix", "win-ia32", "win-amd64", "win-arm", "win-arm64"],
        help="launcher kind (default: current platform)",
    )
    parser.add_argument(
        "--destdir",
        default=None,
        help="staging root used when the wheel was installed",
    )
    parser.add_argument(
        "--scan-scheme",
        action="append",
        dest="scan_schemes",
        metavar="NAME",
        help="scheme whose directory is scanned for unknown files (default: scripts)",
    )
    parser.add_argument(
        "--include-bytecode",
        action="store_true",
        help="report .pyc files and __pycache__ contents as unknown files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the complete ledger as JSON",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="write the ledger to this file instead of standard output",
    )
    return parser


def _format_entry(entry: LedgerEntry) -> str:
    parts = [
        f"{entry.status.value:<22}",
        f"{entry.provenance.value:<10}",
        f"{entry.scheme}:{entry.path}",
    ]
    if entry.installed is not None:
        parts.append(f"installed={entry.installed.sha256}")
    if entry.expected is not None:
        parts.append(f"expected={entry.expected.sha256}")
    if entry.source is not None:
        parts.append(f"source={entry.source.sha256}")
    if entry.difference is not None:
        difference = entry.difference
        parts.append(f"offset={difference.offset}")
        parts.append(f"line={difference.line}")
    return " ".join(parts)


def _format_ledger(ledger: ReconciliationLedger) -> str:
    counts = ledger.counts()
    lines = [
        f"wheel: {ledger.wheel}",
        f"interpreter: {ledger.interpreter}",
        f"script_kind: {ledger.script_kind}",
        (
            "provenance: "
            + ", ".join(
                f"{name}={value}" for name, value in counts["provenance"].items()
            )
        ),
        (
            "status: "
            + ", ".join(f"{name}={value}" for name, value in counts["status"].items())
        ),
    ]
    for entry in ledger.entries:
        lines.append(_format_entry(entry))
        if entry.detail is not None:
            lines.append(f"  detail: {entry.detail}")
        if entry.difference is not None:
            difference = entry.difference
            lines.append(f"  expected: {difference.expected_context}")
            lines.append(f"  installed: {difference.installed_context}")
    lines.append("result: OK" if ledger.ok else "result: DIFFERENCES FOUND")
    return "\n".join(lines) + "\n"


def _main(cli_args: Sequence[str] | None = None) -> int:
    from installer.__main__ import _get_scheme_dict
    from installer.utils import parse_wheel_filename

    parser = _get_main_parser()
    args = parser.parse_args(cli_args)

    wheel_path = Path(args.wheel)
    distribution = parse_wheel_filename(wheel_path.name).distribution
    scheme_dict = dict(args.scheme) if args.scheme else _get_scheme_dict(distribution)
    scan_schemes = args.scan_schemes if args.scan_schemes else ["scripts"]

    try:
        ledger = reconcile_wheel(
            wheel_path,
            scheme_dict=scheme_dict,
            interpreter=args.interpreter,
            script_kind=args.script_kind,
            destdir=args.destdir,
            scan_schemes=scan_schemes,
            ignore_bytecode=not args.include_bytecode,
        )
    except (ReconciliationError, OSError, ValueError) as exc:
        parser.error(str(exc))
        raise AssertionError("argparse.error always exits") from exc

    output = ledger.to_json() + "\n" if args.json else _format_ledger(ledger)
    if args.output is not None:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0 if ledger.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
