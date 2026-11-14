"""Offline installation planning ("dry runs") for wheels.

This module maps every archive member of a wheel to the scheme-relative
path it would be installed to, without writing anything to the filesystem.
It also detects archive members that would occupy the same target path,
distinguishing members with identical contents from members whose contents
conflict.

The path folding rules -- in particular, whether target paths are treated
case-sensitively -- are provided by the caller, so a plan can be built for
a target platform that differs from the platform running the planner.
"""

import hashlib
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from installer._core import _determine_scheme, _process_WHEEL_file
from installer.records import RecordEntry
from installer.sources import WheelSource
from installer.utils import Scheme

__all__ = [
    "CaseSensitivity",
    "InstallPlan",
    "InstallPlanEntry",
    "TargetConflict",
    "build_install_plan",
]


class CaseSensitivity(Enum):
    """Case handling of the target platform's paths."""

    CASE_SENSITIVE = "case-sensitive"
    """Paths that differ only in case are distinct (e.g. typical Linux setups)."""

    CASE_INSENSITIVE = "case-insensitive"
    """Paths that differ only in case collide (e.g. Windows, default macOS)."""

    def fold_path(self, path: str) -> str:
        """Return the collision key for ``path`` on this platform.

        :param path: a scheme-relative, POSIX-style path
        """
        if self is CaseSensitivity.CASE_INSENSITIVE:
            return path.casefold()
        return path


@dataclass(frozen=True)
class InstallPlanEntry:
    """Planned installation location of a single archive member."""

    source_path: str
    """Path of the member within the wheel archive."""

    scheme: Scheme
    """Installation scheme that the member is expanded into."""

    destination_path: str
    """Path of the file within ``scheme`` (POSIX-style, relative)."""

    size: int
    """Size of the member's contents, in bytes."""

    digest: str
    """Hex digest of the member's contents, using the planner's hash algorithm."""

    @property
    def target_path(self) -> str:
        """Fully qualified, scheme-relative target path."""
        return posixpath.join(self.scheme, self.destination_path)


@dataclass(frozen=True)
class TargetConflict:
    """A target path that more than one archive member would occupy."""

    scheme: Scheme
    """Installation scheme containing the contested target path."""

    destination_path: str
    """Scheme-relative path that the members collide on."""

    entries: tuple[InstallPlanEntry, ...]
    """All archive members planned for the same target path."""

    identical: bool
    """Whether all colliding members carry identical contents."""

    @property
    def target_path(self) -> str:
        """Fully qualified, scheme-relative target path."""
        return posixpath.join(self.scheme, self.destination_path)


@dataclass(frozen=True)
class InstallPlan:
    """Result of planning a wheel installation for one target platform."""

    case_sensitivity: CaseSensitivity
    """Path folding rules that this plan was built with."""

    entries: tuple[InstallPlanEntry, ...]
    """One entry per planned archive member, in archive order."""

    conflicts: tuple[TargetConflict, ...]
    """Target paths occupied by more than one archive member."""

    @property
    def mapping(self) -> Mapping[str, InstallPlanEntry]:
        """Mapping of archive member paths to their planned entries."""
        return {entry.source_path: entry for entry in self.entries}

    @property
    def content_conflicts(self) -> tuple[TargetConflict, ...]:
        """Conflicts where the colliding members carry different contents."""
        return tuple(conflict for conflict in self.conflicts if not conflict.identical)


def build_install_plan(
    source: WheelSource,
    case_sensitivity: CaseSensitivity,
    *,
    hash_algorithm: str = "sha256",
) -> InstallPlan:
    """Plan the installation of ``source`` without touching the filesystem.

    The wheel root is expanded into the ``purelib`` or ``platlib`` scheme
    (based on the wheel's ``Root-Is-Purelib``) and each
    ``{distribution}-{version}.data/<scheme>`` tree is expanded into its
    corresponding scheme, mirroring the actual installation logic.

    :param source: wheel to plan the installation of
    :param case_sensitivity: path folding rules of the target platform
    :param hash_algorithm: hashing algorithm used to compare member contents

    :return: an :any:`InstallPlan` describing target paths and collisions
    """
    root_scheme = _process_WHEEL_file(source)
    record_file_path = posixpath.join(source.dist_info_dir, "RECORD")

    entries: list[InstallPlanEntry] = []
    buckets: dict[tuple[Scheme, str], list[InstallPlanEntry]] = {}

    for record_elements, stream, _is_executable in source.get_contents():
        source_path = RecordEntry.from_elements(*record_elements).path

        # RECORD is regenerated at finalization and __pycache__ members are
        # skipped during installation, so neither one occupies a target path.
        if source_path == record_file_path:
            continue
        if "__pycache__" in source_path.split("/")[:-1]:
            continue

        scheme, destination_path = _determine_scheme(
            path=source_path,
            source=source,
            root_scheme=root_scheme,
        )

        data = stream.read()
        digest = hashlib.new(hash_algorithm, data).hexdigest()
        entry = InstallPlanEntry(
            source_path=source_path,
            scheme=scheme,
            destination_path=destination_path,
            size=len(data),
            digest=digest,
        )
        entries.append(entry)

        key = (scheme, case_sensitivity.fold_path(destination_path))
        buckets.setdefault(key, []).append(entry)

    conflicts = [
        TargetConflict(
            scheme=members[0].scheme,
            destination_path=members[0].destination_path,
            entries=tuple(members),
            identical=len({member.digest for member in members}) == 1,
        )
        for members in buckets.values()
        if len(members) > 1
    ]

    return InstallPlan(
        case_sensitivity=case_sensitivity,
        entries=tuple(entries),
        conflicts=tuple(conflicts),
    )
