"""Group wheel artifacts into project versions.

A wheel is represented here as an :class:`Artifact`. An artifact keeps the
metadata that belongs to that individual file: its filename, build tag, wheel
tags, ``WHEEL`` metadata, and the dependency conditions declared in
``METADATA``.

Artifacts that share a normalized project name and version are grouped into a
:class:`VersionView`. The view has two layers:

* The version layer contains project metadata that every artifact agrees on.
  Each field is compared using its own semantics: multi-use fields are treated
  as sets, versions and ``Requires-Python`` are canonicalized first, and a
  value that is only present on some artifacts is still a disagreement.
* The artifact layer keeps the filename, build tag, wheel tags, ``WHEEL``
  metadata, and dependency conditions for each wheel.

Fields that disagree are never resolved by picking a value (or a majority).
They are exposed through :attr:`VersionView.divergences`, with every distinct
value mapped to the artifacts that declared it.
"""

import re
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import os

from installer.exceptions import InstallerError
from installer.records import parse_record_file
from installer.sources import WheelSource
from installer.utils import canonicalize_name, parse_metadata_file, parse_wheel_filename

__all__ = [
    "ARTIFACT_ONLY_FIELDS",
    "MULTI_VALUE_FIELDS",
    "Artifact",
    "Field",
    "FieldGroup",
    "FieldValue",
    "InvalidArtifactMetadata",
    "Inventory",
    "Metadata",
    "ProjectVersion",
    "ReleaseView",
    "VersionView",
    "WheelArtifact",
    "WheelInventory",
    "build_inventory",
    "canonicalize_specifier_set",
    "canonicalize_version",
    "group_artifacts",
    "group_wheels",
    "inventory_from_wheels",
]

# Metadata fields that stay on the individual wheel. Dependency conditions can
# contain environment markers or platform-specific alternatives, and ``WHEEL``
# metadata describes the build of one artifact.
ARTIFACT_ONLY_FIELDS = frozenset(
    name.lower()
    for name in (
        "Requires-Dist",
        "Requires-External",
    )
)

# Metadata fields that may occur more than once. They are compared as sets, so
# order and repetition do not create a difference.
MULTI_VALUE_FIELDS = frozenset(
    name.lower()
    for name in (
        "Classifier",
        "Dynamic",
        "License-File",
        "Obsoletes-Dist",
        "Platform",
        "Project-URL",
        "Provides-Dist",
        "Provides-Extra",
        "Requires-Dist",
        "Requires-External",
        "Supported-Platform",
    )
)

# Fields whose values have a syntax worth canonicalizing before comparing.
_VERSION_FIELD = "version"
_NAME_FIELD = "name"
_REQUIRES_PYTHON_FIELD = "requires-python"

_VERSION_PATTERN = re.compile(
    r"""
    ^\s*
    v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [-_.]?
        (?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)
        [-_.]?
        (?P<pre_n>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)
    )?
    (?P<dev>
        [-_.]?
        (?P<dev_l>dev)
        [-_.]?
        (?P<dev_n>[0-9]+)?
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

_SPECIFIER_PATTERN = re.compile(r"^(===|==|!=|~=|<=|>=|<|>)\s*(.+)$")
_WILDCARD_PATTERN = re.compile(r"^v?([0-9]+(?:\.[0-9]+)*)\s*\.\s*\*$", re.IGNORECASE)

_PRE_LABELS = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "preview": "rc",
    "pre": "rc",
    "c": "rc",
    "rc": "rc",
}


class InvalidArtifactMetadata(InstallerError, ValueError):
    """Raised when an artifact's metadata cannot be read or understood."""


def _read_record_paths(source: WheelSource) -> tuple[str, ...]:
    """Return file paths from a source's ``RECORD`` when it is readable."""
    try:
        record_text = source.read_dist_info("RECORD")
    except (KeyError, NotImplementedError):
        return ()
    return tuple(row[0] for row in parse_record_file(record_text.splitlines()))


@dataclass(frozen=True)
class _VersionParts:
    """Parsed :pep:`440` version components."""

    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[str, ...] | None

    @property
    def canonical(self) -> str:
        """Return the canonical string representation."""
        prefix = f"{self.epoch}!" if self.epoch else ""
        result = prefix + ".".join(str(part) for part in self.release)
        if self.pre is not None:
            result += f"{self.pre[0]}{self.pre[1]}"
        if self.post is not None:
            result += f".post{self.post}"
        if self.dev is not None:
            result += f".dev{self.dev}"
        if self.local is not None:
            result += "+" + ".".join(self.local)
        return result


def _parse_version(value: str) -> _VersionParts | None:
    """Parse ``value`` into version parts, or return ``None`` if invalid."""
    match = _VERSION_PATTERN.match(value)
    if match is None:
        return None

    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()

    pre: tuple[str, int] | None = None
    if match.group("pre_l") is not None:
        label = _PRE_LABELS[match.group("pre_l").lower()]
        pre = (label, int(match.group("pre_n") or 0))

    post: int | None = None
    if match.group("post_n1") is not None:
        post = int(match.group("post_n1"))
    elif match.group("post_l") is not None:
        post = int(match.group("post_n2") or 0)

    dev: int | None = None
    if match.group("dev_l") is not None:
        dev = int(match.group("dev_n") or 0)

    local: tuple[str, ...] | None = None
    if match.group("local") is not None:
        local = tuple(
            str(int(part)) if part.isdigit() else part.lower()
            for part in re.split(r"[-_.]", match.group("local"))
        )

    return _VersionParts(
        epoch=int(match.group("epoch") or 0),
        release=tuple(release),
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


def canonicalize_version(value: str) -> str:
    """Canonicalize a version according to :pep:`440`.

    Trailing zeroes in the release segment are removed, pre-release labels are
    shortened, and local version segments are lowercased. Values that do not
    parse as PEP 440 versions are stripped and lowercased, which still gives
    stable grouping for otherwise identical strings.

    :param value: version string to canonicalize
    """
    parts = _parse_version(value)
    if parts is None:
        return value.strip().lower()
    return parts.canonical


def _canonicalize_release_prefix(value: str) -> str:
    """Canonicalize the release part of an ``==1.0.*`` style wildcard."""
    match = _WILDCARD_PATTERN.match(value)
    if match is None:
        return canonicalize_version(value)
    release = [str(int(part)) for part in match.group(1).split(".")]
    return ".".join(release) + ".*"


def _canonicalize_compatible_release(value: str) -> str:
    """Canonicalize a ``~=`` clause without changing its segment count."""
    parts = _parse_version(value)
    match = _VERSION_PATTERN.match(value)
    if parts is None or match is None:
        return value.strip()
    prefix = f"{parts.epoch}!" if parts.epoch else ""
    result = prefix + ".".join(
        str(int(part)) for part in match.group("release").split(".")
    )
    if parts.pre is not None:
        result += f"{parts.pre[0]}{parts.pre[1]}"
    if parts.post is not None:
        result += f".post{parts.post}"
    if parts.dev is not None:
        result += f".dev{parts.dev}"
    if parts.local is not None:
        result += "+" + ".".join(parts.local)
    return result


def canonicalize_specifier_set(value: str) -> str:
    """Canonicalize a :pep:`440` specifier set for equality comparison.

    Clause order and insignificant whitespace are removed. Compatible-release
    clauses and release wildcards retain their segment count, because
    ``~=1.4`` is not ``~=1.4.0`` and ``==1.0.*`` is not ``==1.0.0.*``.

    :param value: specifier set, such as ``>=3.8,<4``
    """
    clauses: set[str] = set()
    for raw_clause in value.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        match = _SPECIFIER_PATTERN.match(clause)
        if match is None:
            return " ".join(value.split())
        operator, version = match.groups()
        version = version.strip()
        if operator == "===":
            clauses.add(f"{operator}{version}")
        elif operator in ("==", "!=") and version.endswith(".*"):
            clauses.add(f"{operator}{_canonicalize_release_prefix(version)}")
        elif operator == "~=":
            clauses.add(f"{operator}{_canonicalize_compatible_release(version)}")
        else:
            clauses.add(f"{operator}{canonicalize_version(version)}")
    return ",".join(sorted(clauses))


def _normalize_text(name: str, value: str) -> str:
    """Normalize one metadata value according to the field's semantics."""
    key = name.lower()
    if key == _NAME_FIELD:
        return canonicalize_name(value)
    if key == _VERSION_FIELD:
        return canonicalize_version(value)
    if key == _REQUIRES_PYTHON_FIELD:
        return canonicalize_specifier_set(value)
    if key in ("author-email", "maintainer-email"):
        return ",".join(part.strip() for part in value.split(","))
    if key == "project-url":
        label, separator, url = value.partition(",")
        if separator:
            return f"{label.strip()}, {url.strip()}"
    if key == "description-content-type":
        return " ".join(value.split()).lower()
    if key == "license-file":
        return value.strip().replace("\\", "/")
    if key == "description":
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return " ".join(value.split())


class Metadata:
    """An immutable, case-insensitive view of ``METADATA``-style headers.

    Repeated headers remain separate values, preserving multi-value semantics.
    Iteration yields header names using the casing first seen in the source.
    """

    def __init__(self, values: "Mapping[str, str | Iterable[str]]") -> None:
        """Initialize the metadata mapping.

        :param values: mapping of header names to one value or several values
        """
        internal: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for name, raw_values in values.items():
            key = name.lower()
            if key not in internal:
                internal[key] = []
                names[key] = name
            if isinstance(raw_values, str):
                internal[key].append(raw_values)
            else:
                internal[key].extend(raw_values)

        self._values = {key: tuple(items) for key, items in internal.items()}
        self._names = names

    @classmethod
    def from_message(cls, message: Any, *, include_payload: bool = False) -> "Metadata":
        """Build metadata from a parsed :class:`email.message.Message`.

        :param message: parsed ``METADATA`` or ``WHEEL`` message
        :param include_payload: expose a non-empty body as ``Description`` when
            no ``Description`` header is present
        """
        values: dict[str, list[str]] = {}
        for name, value in message.items():
            values.setdefault(name, []).append(value)

        if include_payload:
            payload = message.get_payload()
            if isinstance(payload, str) and payload.strip():
                body = payload.strip("\r\n")
                if body and not any(name.lower() == "description" for name in values):
                    values["Description"] = [body]

        return cls(values)

    def get_all(self, name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Return every value declared for ``name``, preserving order."""
        return self._values.get(name.lower(), default)

    def get(self, name: str, default: str | None = None) -> str | None:
        """Return the first value declared for ``name``."""
        values = self.get_all(name)
        return values[0] if values else default

    def items(self) -> Iterator[tuple[str, tuple[str, ...]]]:
        """Yield ``(header name, values)`` pairs."""
        for key, values in self._values.items():
            yield self._names[key], values

    def keys(self) -> Iterator[str]:
        """Yield header names using their original casing."""
        return iter(self._names.values())

    def values(self) -> Iterator[tuple[str, ...]]:
        """Yield value tuples."""
        return iter(self._values.values())

    def as_dict(self) -> dict[str, str | tuple[str, ...]]:
        """Return a plain dict, using a scalar for single values."""
        return {
            self._names[key]: values[0] if len(values) == 1 else values
            for key, values in self._values.items()
        }

    def __getitem__(self, name: str) -> tuple[str, ...]:
        key = name.lower()
        if key not in self._values:
            raise KeyError(name)
        return self._values[key]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._names.values())

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Metadata({self.as_dict()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Metadata):
            return NotImplemented
        return self._values == other._values

    def __hash__(self) -> int:
        return hash(frozenset(self._values.items()))


@dataclass(frozen=True)
class Artifact:
    """One wheel file and the metadata it declares.

    This is the artifact layer. The filename, build tag, tags, ``WHEEL``
    metadata, dependency conditions, and file list stay on the artifact even
    when they happen to agree across all artifacts.
    """

    filename: str
    """Wheel filename, or an empty string when unknown."""

    distribution: str
    """Project name as declared by the artifact's ``METADATA``."""

    version: str
    """Project version as declared by the artifact's ``METADATA``."""

    metadata: Metadata
    """Project metadata headers declared by this artifact."""

    wheel_metadata: Metadata
    """``WHEEL`` metadata declared by this artifact."""

    path: Path | None = None
    """Path to the wheel, when this artifact came from a file."""

    build_tag: str | None = None
    """Build tag from the wheel filename."""

    tag: str | None = None
    """Wheel tag string from the wheel filename."""

    size: int | None = None
    """Size of the wheel file in bytes, when known."""

    files: tuple[str, ...] = ()
    """File paths listed in this artifact's ``RECORD``."""

    @classmethod
    def from_path(cls, path: "str | os.PathLike[str]") -> "Artifact":
        """Read one wheel from the filesystem.

        :param path: path to a ``.whl`` file
        """
        wheel_path = Path(path)
        try:
            from installer.sources import WheelFile

            with WheelFile.open(wheel_path) as source:
                return cls.from_source(
                    source,
                    filename=wheel_path.name,
                    path=wheel_path,
                    size=wheel_path.stat().st_size,
                )
        except (KeyError, ValueError) as exc:
            raise InvalidArtifactMetadata(
                f"{wheel_path.name} does not contain readable METADATA/WHEEL files"
            ) from exc
        except zipfile.BadZipFile as exc:
            raise InvalidArtifactMetadata(
                f"{wheel_path.name} is not a valid wheel archive"
            ) from exc

    @classmethod
    def from_source(
        cls,
        source: WheelSource,
        *,
        filename: str | None = None,
        path: Path | None = None,
        size: int | None = None,
    ) -> "Artifact":
        """Read one artifact from an existing :class:`WheelSource`.

        :param source: wheel source to read
        :param filename: wheel filename, if known
        :param path: filesystem path, if known
        :param size: wheel size in bytes, if known
        """
        try:
            metadata_text = source.read_dist_info("METADATA")
            wheel_text = source.read_dist_info("WHEEL")
        except KeyError as exc:
            raise InvalidArtifactMetadata(
                f"{filename or source.distribution} does not contain METADATA/WHEEL files"
            ) from exc

        metadata = Metadata.from_message(
            parse_metadata_file(metadata_text), include_payload=True
        )
        wheel_metadata = Metadata.from_message(parse_metadata_file(wheel_text))

        parsed = parse_wheel_filename(filename) if filename else None
        distribution = metadata.get("Name") or (
            parsed.distribution if parsed is not None else source.distribution
        )
        version = metadata.get("Version") or (
            parsed.version if parsed is not None else source.version
        )
        assert distribution is not None
        assert version is not None

        return cls(
            filename=filename or "",
            distribution=distribution,
            version=version,
            metadata=metadata,
            wheel_metadata=wheel_metadata,
            path=path,
            build_tag=parsed.build_tag if parsed is not None else None,
            tag=parsed.tag if parsed is not None else None,
            size=size,
            files=_read_record_paths(source),
        )

    @property
    def canonical_name(self) -> str:
        """PEP 503 normalized project name."""
        return canonicalize_name(self.distribution)

    @property
    def canonical_version(self) -> str:
        """Canonicalized project version."""
        return canonicalize_version(self.version)

    @property
    def display_name(self) -> str:
        """Filename, or a stable label when no filename is known."""
        return self.filename or f"{self.distribution}-{self.version}"

    @property
    def requires_python(self) -> str | None:
        """Raw ``Requires-Python`` value, if declared."""
        return self.metadata.get("Requires-Python")

    @property
    def requires_dist(self) -> tuple[str, ...]:
        """Raw ``Requires-Dist`` values, if declared."""
        return self.metadata.get_all("Requires-Dist")

    @property
    def requires_external(self) -> tuple[str, ...]:
        """Raw ``Requires-External`` values, if declared."""
        return self.metadata.get_all("Requires-External")

    @property
    def provides_extra(self) -> tuple[str, ...]:
        """Raw ``Provides-Extra`` values, if declared."""
        return self.metadata.get_all("Provides-Extra")

    @property
    def tags(self) -> tuple[str, ...]:
        """Wheel tags, preferring ``WHEEL`` metadata over the filename."""
        declared = self.wheel_metadata.get_all("Tag")
        if declared:
            return declared
        return (self.tag,) if self.tag else ()

    @property
    def is_platform_specific(self) -> bool:
        """Whether at least one wheel tag targets a specific platform."""
        return any(not tag.endswith("-none-any") for tag in self.tags)

    @property
    def platform_metadata(self) -> dict[str, str | tuple[str, ...]]:
        """Artifact-layer fields that can vary by platform."""
        values: dict[str, str | tuple[str, ...]] = {
            "Filename": self.filename,
            "Build": self.build_tag or self.wheel_metadata.get("Build") or "",
            "Tag": self.tags,
        }
        for name, value in self.wheel_metadata.as_dict().items():
            values.setdefault(name, value)
        return values

    @property
    def dependency_metadata(self) -> dict[str, tuple[str, ...]]:
        """Dependency fields kept on the artifact layer."""
        return {
            name: self.metadata.get_all(name)
            for name in (
                "Requires-Dist",
                "Requires-External",
                "Provides-Dist",
                "Obsoletes-Dist",
            )
            if self.metadata.get_all(name)
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of this artifact."""
        return {
            "filename": self.filename,
            "distribution": self.distribution,
            "version": self.version,
            "canonical_name": self.canonical_name,
            "canonical_version": self.canonical_version,
            "build_tag": self.build_tag,
            "tags": list(self.tags),
            "platform_specific": self.is_platform_specific,
            "size": self.size,
            "metadata": self.metadata.as_dict(),
            "wheel_metadata": self.wheel_metadata.as_dict(),
            "requires_dist": list(self.requires_dist),
            "files": list(self.files),
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()

    def __repr__(self) -> str:
        return (
            f"Artifact(filename={self.filename!r}, "
            f"distribution={self.distribution!r}, version={self.version!r})"
        )


@dataclass(frozen=True)
class FieldValue:
    """One artifact's value for one metadata field."""

    artifact: Artifact
    raw: "str | tuple[str, ...] | None"
    """Raw value as declared; tuples preserve repeated fields."""

    value: Any
    """Normalized value used for comparison."""


@dataclass(frozen=True)
class FieldGroup:
    """A distinct normalized value and the artifacts that declared it."""

    value: Any
    """Normalized value shared by :attr:`artifacts`."""

    raw: "str | tuple[str, ...] | None"
    """Raw representation from the first artifact in this group."""

    artifacts: tuple[Artifact, ...]
    """Artifacts that declared this value."""

    @property
    def filenames(self) -> tuple[str, ...]:
        """Display names of the artifacts in this group."""
        return tuple(artifact.display_name for artifact in self.artifacts)


@dataclass(frozen=True)
class Field:
    """A metadata field compared across all artifacts in a version view."""

    name: str
    """Header name, using the casing first seen in the metadata."""

    values: tuple[FieldValue, ...]
    """One value per artifact, in artifact order."""

    @property
    def consistent(self) -> bool:
        """Whether every artifact declared the same normalized value."""
        if not self.values:
            return True
        first = self.values[0].value
        return all(item.value == first for item in self.values[1:])

    @property
    def value(self) -> "str | tuple[str, ...] | None":
        """The agreed raw value, or ``None`` when artifacts disagree."""
        if not self.consistent or not self.values:
            return None
        return self.values[0].raw

    @property
    def normalized_value(self) -> Any:
        """The agreed normalized value, or ``None`` when artifacts disagree."""
        if not self.consistent:
            return None
        return self.values[0].value

    @property
    def divergent(self) -> bool:
        """Whether artifacts disagree on this field."""
        return not self.consistent

    @property
    def provenance(self) -> dict[Artifact, "str | tuple[str, ...] | None"]:
        """Map each artifact to the raw value it declared."""
        return {item.artifact: item.raw for item in self.values}

    @property
    def groups(self) -> tuple[FieldGroup, ...]:
        """Distinct values in first-seen order, each with its artifacts."""
        grouped: dict[Any, list[FieldValue]] = {}
        for item in self.values:
            grouped.setdefault(item.value, []).append(item)
        return tuple(
            FieldGroup(
                value=value,
                raw=items[0].raw,
                artifacts=tuple(item.artifact for item in items),
            )
            for value, items in grouped.items()
        )

    def artifacts_for(self, value: Any) -> tuple[Artifact, ...]:
        """Return artifacts that declared one normalized value."""
        return tuple(item.artifact for item in self.values if item.value == value)

    def describe(self) -> str:
        """Return a human-readable description with artifact provenance."""
        if self.consistent:
            return f"{self.name}: {self.value!r}"
        groups = "; ".join(
            f"{group.raw!r} from {', '.join(group.filenames)}" for group in self.groups
        )
        return f"{self.name}: {groups}"


def _raw_value(metadata: Metadata, name: str) -> "str | tuple[str, ...] | None":
    values = metadata.get_all(name)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return values


def _normalized_value(name: str, raw: "str | tuple[str, ...] | None") -> Any:
    if raw is None:
        return None
    key = name.lower()
    if key in MULTI_VALUE_FIELDS:
        values = raw if isinstance(raw, tuple) else (raw,)
        return frozenset(_normalize_text(name, value) for value in values)
    if isinstance(raw, tuple):
        return tuple(sorted({_normalize_text(name, value) for value in raw}))
    return _normalize_text(name, raw)


class VersionView:
    """The project-version layer for a group of artifacts.

    A view is built from artifacts that share a canonical project name and
    version. Only fields agreed on by every artifact are exposed through
    :attr:`metadata`; disagreements are exposed through :attr:`divergences`.
    """

    def __init__(self, artifacts: Iterable[Artifact]) -> None:
        """Build a view from at least one artifact.

        :param artifacts: artifacts belonging to one project version
        """
        self.artifacts = tuple(artifacts)
        if not self.artifacts:
            raise ValueError("a version view requires at least one artifact")

        first = self.artifacts[0]
        self.distribution = first.distribution
        self.version = first.version
        self.canonical_name = first.canonical_name
        self.canonical_version = first.canonical_version

        field_names: dict[str, str] = {}
        for artifact in self.artifacts:
            for name in artifact.metadata:
                key = name.lower()
                if key not in ARTIFACT_ONLY_FIELDS:
                    field_names.setdefault(key, name)

        fields: dict[str, Field] = {}
        for name in field_names.values():
            raw_values = tuple(
                _raw_value(artifact.metadata, name) for artifact in self.artifacts
            )
            fields[name] = Field(
                name=name,
                values=tuple(
                    FieldValue(
                        artifact=artifact,
                        raw=raw,
                        value=_normalized_value(name, raw),
                    )
                    for artifact, raw in zip(self.artifacts, raw_values, strict=True)
                ),
            )

        self.fields = fields
        self.metadata = Metadata(
            {
                name: field.value
                for name, field in fields.items()
                if field.consistent and field.value is not None
            }
        )

    @property
    def project_metadata(self) -> dict[str, str | tuple[str, ...]]:
        """Metadata agreed on by every artifact."""
        return self.metadata.as_dict()

    @property
    def consistent_fields(self) -> dict[str, Field]:
        """Fields on which every artifact agrees."""
        return {name: field for name, field in self.fields.items() if field.consistent}

    @property
    def divergences(self) -> dict[str, Field]:
        """Fields on which artifacts disagree, with all values preserved."""
        return {name: field for name, field in self.fields.items() if field.divergent}

    @property
    def has_divergences(self) -> bool:
        """Whether any project field differs between artifacts."""
        return bool(self.divergences)

    @property
    def artifact_metadata(self) -> dict[str, Metadata]:
        """Per-artifact project metadata, keyed by display name."""
        return {artifact.display_name: artifact.metadata for artifact in self.artifacts}

    @property
    def platform_metadata(self) -> dict[str, dict[str, str | tuple[str, ...]]]:
        """Per-artifact platform metadata, keyed by display name."""
        return {
            artifact.display_name: artifact.platform_metadata
            for artifact in self.artifacts
        }

    @property
    def files(self) -> dict[str, tuple[str, ...]]:
        """Per-artifact file paths from ``RECORD``."""
        return {artifact.display_name: artifact.files for artifact in self.artifacts}

    @property
    def tags(self) -> dict[str, tuple[str, ...]]:
        """Per-artifact wheel tags."""
        return {artifact.display_name: artifact.tags for artifact in self.artifacts}

    @property
    def build_tags(self) -> dict[str, str | None]:
        """Per-artifact build tags."""
        return {
            artifact.display_name: artifact.build_tag for artifact in self.artifacts
        }

    @property
    def dependencies(self) -> dict[str, tuple[str, ...]]:
        """Per-artifact ``Requires-Dist`` conditions, keyed by display name."""
        return {
            artifact.display_name: artifact.requires_dist for artifact in self.artifacts
        }

    @property
    def dependency_metadata(self) -> dict[str, dict[str, tuple[str, ...]]]:
        """Per-artifact dependency fields."""
        return {
            artifact.display_name: artifact.dependency_metadata
            for artifact in self.artifacts
        }

    @property
    def requires_python(self) -> str | None:
        """``Requires-Python`` when shared, or ``None`` when it diverges."""
        field = self.field("Requires-Python")
        return None if field is None else field.value  # type: ignore[return-value]

    @property
    def requires_python_by_artifact(self) -> dict[str, str | None]:
        """Raw ``Requires-Python`` declared by each artifact."""
        return {
            artifact.display_name: artifact.requires_python
            for artifact in self.artifacts
        }

    def field(self, name: str) -> Field | None:
        """Return the comparison for ``name``, case-insensitively."""
        for field_name, field in self.fields.items():
            if field_name.lower() == name.lower():
                return field
        return None

    def get(self, name: str, default: Any = None) -> Any:
        """Return the shared raw value for ``name``, or ``default``."""
        field = self.field(name)
        if field is None or not field.consistent:
            return default
        return field.value

    def artifacts_for(self, name: str, value: Any) -> tuple[Artifact, ...]:
        """Return artifacts that declared one normalized field value."""
        field = self.field(name)
        return () if field is None else field.artifacts_for(value)

    @property
    def version_layer(self) -> dict[str, str | tuple[str, ...]]:
        """Project metadata that was hoisted to the version layer."""
        return self.project_metadata

    @property
    def artifact_layer(self) -> tuple[Artifact, ...]:
        """Artifacts that make up this version."""
        return self.artifacts

    def divergence_sources(
        self, name: str
    ) -> dict[str | tuple[str, ...] | None, tuple[str, ...]]:
        """Map each distinct raw value of a divergent field to its artifacts.

        This provides direct provenance without selecting a value.

        :param name: metadata field name
        """
        field = self.field(name)
        if field is None or field.consistent:
            return {}
        return {
            group.raw: tuple(artifact.display_name for artifact in group.artifacts)
            for group in field.groups
        }

    def get_artifact(self, filename: str) -> Artifact | None:
        """Return an artifact by display name."""
        for artifact in self.artifacts:
            if artifact.display_name == filename:
                return artifact
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of this version view."""
        return {
            "name": self.distribution,
            "canonical_name": self.canonical_name,
            "version": self.version,
            "canonical_version": self.canonical_version,
            "metadata": self.project_metadata,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "divergences": {
                name: {
                    "groups": [
                        {"value": group.raw, "artifacts": list(group.filenames)}
                        for group in field.groups
                    ]
                }
                for name, field in self.divergences.items()
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()

    def __getitem__(self, name: str) -> Any:
        field = self.field(name)
        if field is None:
            raise KeyError(name)
        return field.value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.field(name) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.artifacts)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionView):
            return NotImplemented
        return (
            self.canonical_name == other.canonical_name
            and self.canonical_version == other.canonical_version
            and self.artifacts == other.artifacts
        )

    def __hash__(self) -> int:
        return hash((self.canonical_name, self.canonical_version, self.artifacts))

    def __repr__(self) -> str:
        return (
            f"VersionView(distribution={self.distribution!r}, "
            f"version={self.version!r}, artifacts={len(self.artifacts)})"
        )


class Inventory:
    """Wheel artifacts grouped by canonical project name and version.

    Artifacts are grouped only by normalized project name and version. Wheel
    filenames, build tags, and platform tags never create a new version.
    """

    def __init__(
        self, artifacts: "Iterable[Artifact | str | os.PathLike[str]]" = ()
    ) -> None:
        """Initialize an inventory.

        :param artifacts: artifacts or paths to wheel files
        """
        self._groups: dict[tuple[str, str], list[Artifact]] = {}
        for item in artifacts:
            self.add(item)

    @classmethod
    def from_paths(cls, paths: "Iterable[str | os.PathLike[str]]") -> "Inventory":
        """Build an inventory from wheel paths."""
        return cls(Artifact.from_path(path) for path in paths)

    @classmethod
    def from_artifacts(cls, artifacts: Iterable[Artifact]) -> "Inventory":
        """Build an inventory from existing artifacts."""
        return cls(artifacts)

    @classmethod
    def from_sources(cls, sources: Iterable[WheelSource]) -> "Inventory":
        """Build an inventory from wheel sources without filenames."""
        return cls(Artifact.from_source(source) for source in sources)

    def add(self, artifact: "Artifact | str | os.PathLike[str]") -> Artifact:
        """Add one artifact or wheel path and return the artifact."""
        if not isinstance(artifact, Artifact):
            artifact = Artifact.from_path(artifact)
        key = (artifact.canonical_name, artifact.canonical_version)
        self._groups.setdefault(key, []).append(artifact)
        return artifact

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        """All artifacts in insertion order."""
        return tuple(artifact for group in self._groups.values() for artifact in group)

    @property
    def versions(self) -> tuple[VersionView, ...]:
        """All version views in insertion order."""
        return tuple(VersionView(group) for group in self._groups.values())

    def by_name(self, distribution: str) -> tuple[VersionView, ...]:
        """Return all versions of ``distribution``."""
        name = canonicalize_name(distribution)
        return tuple(
            VersionView(group)
            for (group_name, _), group in self._groups.items()
            if group_name == name
        )

    def get(self, distribution: str, version: str | None = None) -> VersionView | None:
        """Return a version view by canonical name and optional version.

        :param distribution: project name, normalized before lookup
        :param version: project version, canonicalized before lookup
        """
        name = canonicalize_name(distribution)
        if version is not None:
            group = self._groups.get((name, canonicalize_version(version)))
            return None if group is None else VersionView(group)

        matches = self.by_name(name)
        if not matches:
            return None
        if len(matches) > 1:
            raise LookupError(
                f"{distribution!r} has multiple versions; specify a version"
            )
        return matches[0]

    @classmethod
    def from_wheels(cls, paths: "Iterable[str | os.PathLike[str]]") -> "Inventory":
        """Alias for :meth:`from_paths`."""
        return cls.from_paths(paths)

    def add_wheel(self, path: "str | os.PathLike[str]") -> Artifact:
        """Alias for :meth:`add` for a wheel path."""
        return self.add(path)

    @property
    def version_views(self) -> tuple[VersionView, ...]:
        """Alias for :attr:`versions`."""
        return self.versions

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of the inventory."""
        return {"versions": [view.as_dict() for view in self.versions]}

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()

    def __iter__(self) -> Iterator[VersionView]:
        return iter(self.versions)

    def __len__(self) -> int:
        return len(self._groups)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, tuple) and len(item) == 2:
            distribution, version = item
            return (
                isinstance(distribution, str)
                and isinstance(version, str)
                and (canonicalize_name(distribution), canonicalize_version(version))
                in self._groups
            )
        if isinstance(item, str):
            name = canonicalize_name(item)
            return any(key[0] == name for key in self._groups)
        return False

    def __getitem__(self, item: "str | tuple[str, str]") -> VersionView:
        view = self.get(item[0], item[1]) if isinstance(item, tuple) else self.get(item)
        if view is None:
            raise KeyError(item)
        return view

    def __repr__(self) -> str:
        return f"Inventory(versions={len(self)}, artifacts={len(self.artifacts)})"


def build_inventory(
    paths: "Iterable[str | os.PathLike[str]]",
) -> Inventory:
    """Build an inventory from wheel paths."""
    return Inventory.from_paths(paths)


def group_artifacts(artifacts: Iterable[Artifact]) -> tuple[VersionView, ...]:
    """Group existing artifacts into version views."""
    return Inventory.from_artifacts(artifacts).versions


def group_wheels(
    paths: "Iterable[str | os.PathLike[str]]",
) -> tuple[VersionView, ...]:
    """Group wheel paths into version views."""
    return Inventory.from_paths(paths).versions


def inventory_from_wheels(
    paths: "Iterable[str | os.PathLike[str]]",
) -> Inventory:
    """Alias for :func:`build_inventory`."""
    return Inventory.from_paths(paths)


# Explicit names for callers that prefer to state the layer they mean.
WheelArtifact = Artifact
WheelInventory = Inventory
ReleaseView = VersionView
ProjectVersion = VersionView
