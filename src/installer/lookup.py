"""Offline resolution of dotted module names to wheel artifacts.

The inputs are wheels that have not been installed yet, plus the ordered
interpreter search roots that they would be installed into. The resolution
mirrors how Python's import machinery (``FileFinder`` / ``PathFinder``) picks a
file for a dotted module name:

- regular packages (a directory with an ``__init__`` module) take precedence
  over same-named module files and namespace directories,
- extension modules take precedence over source modules, which in turn take
  precedence over bytecode-only modules,
- directories without ``__init__`` act as namespace package portions that
  span search roots, until a later portion provides a regular package,
- search roots are scanned in order, so earlier roots win over later ones.

Only the ``RECORD`` manifest of each wheel is inspected; no business code is
imported or executed.
"""

import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from importlib.machinery import (
    BYTECODE_SUFFIXES,
    EXTENSION_SUFFIXES,
    SOURCE_SUFFIXES,
)
from typing import TYPE_CHECKING, Literal, NamedTuple

from installer.records import parse_record_file

if TYPE_CHECKING:
    from installer.sources import WheelSource

__all__ = [
    "LayerTrace",
    "ModuleCandidate",
    "ModuleFile",
    "ModuleResolution",
    "ModuleResolver",
    "SearchRoot",
    "WheelArtifact",
    "resolve_module",
]

ModuleFileKind = Literal["source", "extension", "bytecode"]
ModuleCandidateKind = Literal["package", "module", "namespace"]

# Lower rank wins, matching the loader ordering used by ``FileFinder``.
_SUFFIX_KIND_ORDER: tuple[tuple[Sequence[str], ModuleFileKind], ...] = (
    (EXTENSION_SUFFIXES, "extension"),
    (SOURCE_SUFFIXES, "source"),
    (BYTECODE_SUFFIXES, "bytecode"),
)
_SUFFIX_RANK: dict[str, tuple[int, ModuleFileKind]] = {
    suffix: (rank, kind)
    for rank, (suffixes, kind) in enumerate(_SUFFIX_KIND_ORDER)
    for suffix in suffixes
}


class _Portion(NamedTuple):
    """A directory scanned for the next module part."""

    root: "SearchRoot"
    rel_dir: str


@dataclass(frozen=True)
class WheelArtifact:
    """A wheel that may provide module files."""

    distribution: str
    """Distribution name, as parsed from the wheel filename."""

    version: str
    """Version associated with the wheel."""

    filename: str
    """Display name identifying the wheel (without opening it)."""


@dataclass(frozen=True)
class ModuleFile:
    """A file provided by a wheel that Python could load as a module."""

    artifact: WheelArtifact
    """Wheel that provides the file."""

    installed_path: str
    """Path of the file once installed, relative to the search root."""

    record_path: str
    """Path of the file as listed in the wheel's ``RECORD``."""

    kind: ModuleFileKind
    """How Python would load the file (extension, source or bytecode)."""


@dataclass(frozen=True)
class ModuleCandidate:
    """One candidate encountered while resolving a single module part."""

    search_root: str
    """Name of the search root that contains this candidate."""

    portion: str
    """Directory scanned for the module part, relative to the search root."""

    kind: ModuleCandidateKind
    """Whether this is a regular package, a module file or a namespace portion."""

    file: ModuleFile | None
    """The loadable file, or ``None`` for a namespace portion."""

    shadowed: bool
    """Whether Python would not actually use this candidate."""

    reason: str | None
    """Why the candidate is shadowed, when it is."""


@dataclass(frozen=True)
class LayerTrace:
    """The candidates considered for one layer of a dotted module name."""

    name: str
    """Dotted name resolved up to and including this layer."""

    portions: tuple[str, ...]
    """Directories scanned, in order, as ``"<search-root>:<relative-dir>"``."""

    candidates: tuple[ModuleCandidate, ...]
    """All candidates found in the scanned portions, in scan order."""

    selected: ModuleCandidate | None
    """Candidate selected at this layer, or ``None`` for a namespace layer."""


@dataclass(frozen=True)
class ModuleResolution:
    """Result of resolving a dotted module name against ordered search roots."""

    module: str
    """Dotted module name that was resolved."""

    found: bool
    """Whether the name can be imported from the described installation."""

    kind: ModuleCandidateKind | None
    """Kind of the resolved name, or ``None`` when it cannot be found."""

    origin: ModuleFile | None
    """File that would actually be loaded, except for namespace packages."""

    search_root: str | None
    """Search root providing the selected file, when there is one."""

    trace: tuple[LayerTrace, ...]
    """Per-layer record of the scan, for step-by-step comparison."""

    shadowed: tuple[ModuleCandidate, ...]
    """Candidates that lose to the selected one at some point of the scan."""


def _split_module_name(module: str) -> tuple[str, ...]:
    """Validate a dotted module name and split it into its parts."""
    if not module:
        raise ValueError("module name must not be empty")
    parts = tuple(module.split("."))
    if any(not part.isidentifier() for part in parts):
        raise ValueError(f"not a valid dotted module name: {module!r}")
    return parts


def _classify_record_path(record_path: str, data_dir: str) -> str | None:
    """Map a RECORD path to its installed path inside a search root.

    Returns ``None`` for files that never become importable modules.
    """
    parts = record_path.split("/")
    if "__pycache__" in parts[:-1]:
        return None

    top_level = parts[0]
    if top_level.endswith(".dist-info"):
        return None

    if top_level == data_dir:
        if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
            return None
        return posixpath.join(*parts[2:])

    return record_path


def _as_module_file(
    artifact: WheelArtifact, installed_path: str, record_path: str
) -> ModuleFile | None:
    """Build a ``ModuleFile`` when the path has an importable suffix."""
    ranked_matches = [
        (rank, kind, suffix)
        for suffix, (rank, kind) in _SUFFIX_RANK.items()
        if installed_path.endswith(suffix)
    ]
    if not ranked_matches:
        return None
    kind = min(ranked_matches, key=lambda item: item[0])[1]
    return ModuleFile(
        artifact=artifact,
        installed_path=installed_path,
        record_path=record_path,
        kind=kind,
    )


class SearchRoot:
    """The wheels installed into one interpreter search root, in install order.

    Wheels added later overwrite files from earlier wheels at the same
    installed path, matching the behaviour of an actual installation.
    """

    def __init__(self, name: str, wheels: Iterable["WheelSource"] = ()) -> None:
        """Initialize a search root and index the given wheels.

        :param name: identifier used in traces and in resolver root orders
        :param wheels: wheels that would be installed into this root, in order
        """
        self.name = name
        self._artifacts: list[WheelArtifact] = []
        self._files: dict[str, ModuleFile] = {}
        self._directories: set[str] = set()
        self._overwrites: dict[str, list[ModuleFile]] = {}
        for wheel in wheels:
            self.add_wheel(wheel)

    @property
    def artifacts(self) -> tuple[WheelArtifact, ...]:
        """Wheels indexed by this root, in installation order."""
        return tuple(self._artifacts)

    def add_wheel(self, source: "WheelSource") -> WheelArtifact:
        """Index one wheel from its ``RECORD`` manifest.

        :param source: wheel to index; only manifest metadata is read
        :return: the artifact descriptor used for files from this wheel
        """
        artifact = WheelArtifact(
            distribution=source.distribution,
            version=source.version,
            filename=f"{source.distribution}-{source.version}.whl",
        )
        self._artifacts.append(artifact)

        record_lines = source.read_dist_info("RECORD").splitlines()
        for record in parse_record_file(record_lines):
            record_path = record[0]
            installed_path = _classify_record_path(record_path, source.data_dir)
            if installed_path is None:
                continue

            parts = installed_path.split("/")
            for index in range(1, len(parts)):
                self._directories.add(posixpath.join(*parts[:index]))

            module_file = _as_module_file(
                artifact, installed_path=installed_path, record_path=record_path
            )
            if module_file is None:
                continue

            previous = self._files.get(installed_path)
            if previous is not None:
                self._overwrites.setdefault(installed_path, []).append(previous)
            self._files[installed_path] = module_file

        return artifact

    def _has_directory(self, rel_dir: str) -> bool:
        return rel_dir in self._directories

    def _lookup_files(
        self, base: str, *, init: bool
    ) -> tuple[ModuleFile | None, tuple[ModuleFile, ...]]:
        """Find loadable files for a module or an ``__init__`` at ``base``."""
        prefix = posixpath.join(base, "__init__") if init else base
        matches = []
        for suffix, (rank, _kind) in _SUFFIX_RANK.items():
            module_file = self._files.get(prefix + suffix)
            if module_file is not None:
                matches.append((rank, module_file))
        matches.sort(key=lambda item: item[0])
        if not matches:
            return None, ()
        best = matches[0][1]
        alternatives = tuple(module_file for _rank, module_file in matches[1:])
        return best, alternatives

    def _overwritten_files(self, installed_path: str) -> tuple[ModuleFile, ...]:
        return tuple(self._overwrites.get(installed_path, ()))


class ModuleResolver:
    """Resolve dotted module names across ordered :class:`SearchRoot` objects."""

    def __init__(self, search_roots: Sequence[SearchRoot]) -> None:
        """Initialize the resolver.

        :param search_roots: search roots in interpreter priority order
        :raises ValueError: if two search roots share a name
        """
        self._roots = list(search_roots)
        self._roots_by_name = {root.name: root for root in self._roots}
        if len(self._roots_by_name) != len(self._roots):
            raise ValueError("search roots must have unique names")

    def resolve(
        self,
        module: str,
        root_order: Sequence[str] | None = None,
    ) -> ModuleResolution:
        """Resolve one dotted module name.

        :param module: dotted module name to look up
        :param root_order: optional permutation of search root names, used to
                           re-run the resolution as if two roots were swapped
        :return: the selected origin plus every shadowed candidate
        """
        parts = _split_module_name(module)
        roots = self._ordered_roots(root_order)

        portions = [_Portion(root=root, rel_dir="") for root in roots]
        traces: list[LayerTrace] = []
        shadowed: list[ModuleCandidate] = []

        for depth, part in enumerate(parts, start=1):
            layer_name = ".".join(parts[:depth])
            candidates, selected, next_portions = self._scan_layer(portions, part)
            traces.append(
                LayerTrace(
                    name=layer_name,
                    portions=tuple(
                        f"{root.name}:{rel_dir or '.'}" for root, rel_dir in portions
                    ),
                    candidates=tuple(candidates),
                    selected=selected,
                )
            )
            shadowed.extend(candidate for candidate in candidates if candidate.shadowed)

            if selected is None:
                if not next_portions:
                    return self._resolution(
                        module, False, None, None, None, traces, shadowed
                    )
                if depth == len(parts):
                    return self._resolution(
                        module, True, "namespace", None, None, traces, shadowed
                    )
                portions = next_portions
                continue

            if depth == len(parts):
                assert selected.file is not None
                origin_root = self._roots_by_name[selected.search_root]
                for overwritten in origin_root._overwritten_files(
                    selected.file.installed_path
                ):
                    shadowed.append(
                        ModuleCandidate(
                            search_root=selected.search_root,
                            portion=selected.portion,
                            kind=selected.kind,
                            file=overwritten,
                            shadowed=True,
                            reason="overwritten-by-later-wheel",
                        )
                    )
                return self._resolution(
                    module,
                    True,
                    selected.kind,
                    selected.file,
                    selected.search_root,
                    traces,
                    shadowed,
                )

            if selected.kind != "package":
                # A module file cannot contain submodules, so the scan stops here.
                return self._resolution(
                    module, False, None, None, None, traces, shadowed
                )

            portions = [
                _Portion(
                    root=self._roots_by_name[selected.search_root],
                    rel_dir=posixpath.join(selected.portion, part)
                    if selected.portion
                    else part,
                )
            ]

        # Unreachable: a non-empty validated name always returns inside the loop.
        raise AssertionError  # pragma: no cover

    def _scan_layer(
        self, portions: Sequence[_Portion], part: str
    ) -> tuple[list[ModuleCandidate], ModuleCandidate | None, list[_Portion]]:
        """Scan every portion for one module part."""
        candidates: list[ModuleCandidate] = []
        next_portions: list[_Portion] = []
        decisive: ModuleCandidate | None = None

        for root, rel_dir in portions:
            base = posixpath.join(rel_dir, part) if rel_dir else part
            package_file, package_alternatives = root._lookup_files(base, init=True)
            module_file, module_alternatives = root._lookup_files(base, init=False)
            has_directory = root._has_directory(base)

            portion_candidates: list[ModuleCandidate] = []
            if package_file is not None:
                portion_candidates.append(
                    ModuleCandidate(
                        root.name, rel_dir, "package", package_file, False, None
                    )
                )
                portion_candidates.extend(
                    ModuleCandidate(
                        root.name, rel_dir, "package", alternative, False, None
                    )
                    for alternative in package_alternatives
                )
            if module_file is not None:
                portion_candidates.append(
                    ModuleCandidate(
                        root.name, rel_dir, "module", module_file, False, None
                    )
                )
                portion_candidates.extend(
                    ModuleCandidate(
                        root.name, rel_dir, "module", alternative, False, None
                    )
                    for alternative in module_alternatives
                )
            if has_directory and package_file is None:
                portion_candidates.append(
                    ModuleCandidate(root.name, rel_dir, "namespace", None, False, None)
                )

            if not portion_candidates:
                continue

            if decisive is not None:
                reason = (
                    "shadowed-by-regular-package"
                    if decisive.kind == "package"
                    else "shadowed-by-module"
                )
                candidates.extend(
                    replace(candidate, shadowed=True, reason=reason)
                    for candidate in portion_candidates
                )
                continue

            # Candidates are ordered best-package, best-module, then namespace,
            # so the first one is what Python's local finder would select.
            winner = portion_candidates[0]
            if winner.kind == "namespace":
                candidates.extend(portion_candidates)
                next_portions.append(_Portion(root=root, rel_dir=base))
                continue

            decisive = winner
            for candidate in portion_candidates:
                if candidate is winner:
                    candidates.append(candidate)
                else:
                    candidates.append(
                        replace(
                            candidate,
                            shadowed=True,
                            reason=self._local_shadow_reason(winner, candidate),
                        )
                    )

        if decisive is not None:
            # Namespace portions accumulated from earlier roots lose once a
            # later root provides a regular package or a module file.
            reason = (
                "shadowed-by-regular-package"
                if decisive.kind == "package"
                else "shadowed-by-module"
            )
            for index, candidate in enumerate(candidates):
                if not candidate.shadowed and candidate.kind == "namespace":
                    candidates[index] = replace(candidate, shadowed=True, reason=reason)
            next_portions = []

        return candidates, decisive, next_portions

    @staticmethod
    def _local_shadow_reason(winner: ModuleCandidate, loser: ModuleCandidate) -> str:
        """Why ``loser`` loses to ``winner`` inside the same portion."""
        if loser.kind == winner.kind:
            return "suffix-precedence"
        if winner.kind == "package":
            return "package-precedence"
        return "module-precedence"

    def _ordered_roots(self, root_order: Sequence[str] | None) -> Sequence[SearchRoot]:
        """Return roots in the requested order, validating the permutation."""
        if root_order is None:
            return self._roots
        if set(root_order) != set(self._roots_by_name):
            raise ValueError(
                "root_order must name every search root exactly once: "
                f"got {list(root_order)!r}"
            )
        return [self._roots_by_name[name] for name in root_order]

    @staticmethod
    def _resolution(
        module: str,
        found: bool,
        kind: ModuleCandidateKind | None,
        origin: ModuleFile | None,
        search_root: str | None,
        traces: list[LayerTrace],
        shadowed: list[ModuleCandidate],
    ) -> ModuleResolution:
        return ModuleResolution(
            module=module,
            found=found,
            kind=kind,
            origin=origin,
            search_root=search_root,
            trace=tuple(traces),
            shadowed=tuple(shadowed),
        )


def resolve_module(module: str, search_roots: Sequence[SearchRoot]) -> ModuleResolution:
    """Resolve a module name against wheels grouped by ordered search roots.

    :param module: dotted module name to look up
    :param search_roots: search roots in interpreter priority order
    :return: the selected origin plus every shadowed candidate
    """
    return ModuleResolver(search_roots).resolve(module)
