"""Tracks which distributions claim which paths, across installation generations.

An :py:class:`Inventory` receives declarations from the RECORD files of
installed distributions. The same path may be claimed by more than one
distribution, so claims are reference-counted per distribution: removing or
upgrading one distribution never drops another distribution's claim on a
shared path.

Every mutation (import, removal, rollback) starts a new *generation*.
All queries accept a ``generation`` argument, making it possible to inspect
the holders, expected content and on-disk reality of each path as of any
earlier point in time. Rolling back an erroneous import appends a
compensating declaration, so historical generations remain untouched and
queries as of those generations keep returning what was true at the time.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from installer.records import RecordEntry

__all__ = [
    "AssetStatus",
    "Claim",
    "Inventory",
    "InventoryError",
]


class InventoryError(Exception):
    """Raised when an inventory operation cannot be performed."""


@dataclass(frozen=True)
class Claim:
    """A single distribution's declaration of ownership over a path."""

    distribution: str
    """Name of the distribution holding the path."""

    entry: RecordEntry
    """Expected content of the path, as declared in the distribution's RECORD."""

    generation: int
    """Generation in which this claim was declared."""


@dataclass(frozen=True)
class AssetStatus:
    """Inventory view of a single path, as of a specific generation."""

    path: str
    """The claimed path, relative to the installation root."""

    claims: tuple[Claim, ...]
    """Claims held on this path, ordered by distribution name."""

    disputed: bool
    """Whether holders declare two or more different digests for this path."""

    present: bool | None
    """Whether the path exists on disk, or None if no audit was performed."""

    mismatched: tuple[str, ...]
    """Distributions whose expected content does not match the file on disk."""

    @property
    def holders(self) -> tuple[str, ...]:
        """Names of the distributions holding this path."""
        return tuple(claim.distribution for claim in self.claims)


@dataclass(frozen=True)
class _Declaration:
    """Sets a distribution's claims to exactly ``entries`` (empty = removal)."""

    generation: int
    distribution: str
    entries: tuple[RecordEntry, ...]


def _claims_at(
    events: Iterable[_Declaration], generation: int
) -> dict[str, dict[str, Claim]]:
    """Replay ``events`` up to ``generation``, returning path -> holder -> claim."""
    claims: dict[str, dict[str, Claim]] = {}
    for event in events:
        if event.generation > generation:
            break
        stale = [p for p, holders in claims.items() if event.distribution in holders]
        for path in stale:
            del claims[path][event.distribution]
            if not claims[path]:
                del claims[path]
        for entry in event.entries:
            claims.setdefault(entry.path, {})[event.distribution] = Claim(
                distribution=event.distribution,
                entry=entry,
                generation=event.generation,
            )
    return claims


def _is_disputed(entries: Iterable[RecordEntry]) -> bool:
    digests = {str(entry.hash_) for entry in entries if entry.hash_ is not None}
    return len(digests) > 1


class Inventory:
    """An inventory of paths claimed by installed distributions.

    Declarations are ingested per distribution with :py:meth:`import_record`
    and withdrawn with :py:meth:`remove_distribution` (for example when the
    distribution is upgraded or uninstalled). Each operation starts a new
    generation, and :py:meth:`rollback` withdraws an entire earlier import
    without rewriting history.
    """

    def __init__(self) -> None:
        """Create an empty inventory, at generation 0."""
        self._events: list[_Declaration] = []
        self._generation = 0

    @property
    def generation(self) -> int:
        """The current (latest) generation; 0 before any import."""
        return self._generation

    def _declare(self, distribution: str, entries: Iterable[RecordEntry]) -> int:
        self._generation += 1
        self._events.append(
            _Declaration(
                generation=self._generation,
                distribution=distribution,
                entries=tuple(entries),
            )
        )
        return self._generation

    def import_record(self, distribution: str, entries: Iterable[RecordEntry]) -> int:
        """Ingest the RECORD declarations of an installed distribution.

        The distribution's claims become exactly ``entries``, replacing any
        claims it previously held. Claims held by other distributions --
        including on the same paths -- are left untouched.

        :param distribution: name of the distribution owning the RECORD
        :param entries: entries parsed from the distribution's RECORD
        :return: the generation started by this import
        """
        return self._declare(distribution, entries)

    def remove_distribution(self, distribution: str) -> int:
        """Withdraw every claim held by a distribution.

        Only the named distribution's claims are dropped; other distributions
        keep their claims on paths they share with it.

        :param distribution: name of the distribution being removed
        :return: the generation started by this removal
        """
        return self._declare(distribution, ())

    def rollback(self, generation: int) -> int:
        """Withdraw an entire earlier import (or removal).

        Appends a compensating declaration that restores the affected
        distribution's claims to what they were just before ``generation``.
        History is append-only, so queries as of earlier generations still
        return exactly what was true at that time.

        :param generation: the generation whose effect should be undone
        :return: the generation started by this rollback
        :raises InventoryError: if ``generation`` does not exist
        """
        event = next(
            (event for event in self._events if event.generation == generation), None
        )
        if event is None:
            raise InventoryError(f"no such generation: {generation}")
        restored = [
            claim.entry
            for holders in _claims_at(self._events, generation - 1).values()
            for claim in holders.values()
            if claim.distribution == event.distribution
        ]
        return self._declare(event.distribution, restored)

    def snapshot(self, generation: int | None = None) -> dict[str, AssetStatus]:
        """Describe every claimed path as of ``generation``.

        :param generation: generation to inspect; defaults to the current one
        :return: mapping of path to :py:class:`AssetStatus`, with
            ``present`` set to None since no audit is performed
        :raises InventoryError: if ``generation`` is in the future
        """
        claims = self._claims(generation)
        return {
            path: AssetStatus(
                path=path,
                claims=tuple(
                    sorted(holders.values(), key=lambda claim: claim.distribution)
                ),
                disputed=_is_disputed(claim.entry for claim in holders.values()),
                present=None,
                mismatched=(),
            )
            for path, holders in sorted(claims.items())
        }

    def holders(self, path: str, generation: int | None = None) -> tuple[str, ...]:
        """Return the distributions holding ``path`` as of ``generation``.

        :param path: the path to inspect
        :param generation: generation to inspect; defaults to the current one
        :return: distribution names, sorted
        """
        return tuple(sorted(self._claims(generation).get(path, ())))

    def disputes(
        self, generation: int | None = None
    ) -> dict[str, tuple[RecordEntry, ...]]:
        """Return paths whose holders declare different digests.

        :param generation: generation to inspect; defaults to the current one
        :return: mapping of each disputed path to the distinct expected
            entries declared for it
        """
        result = {}
        for path, holders in self._claims(generation).items():
            entries = [claim.entry for claim in holders.values()]
            if _is_disputed(entries):
                distinct: list[RecordEntry] = []
                for entry in entries:
                    if entry not in distinct:
                        distinct.append(entry)
                result[path] = tuple(
                    sorted(
                        distinct,
                        key=lambda entry: (str(entry.hash_ or ""), entry.size or -1),
                    )
                )
        return dict(sorted(result.items()))

    def audit(
        self, root: str | Path, generation: int | None = None
    ) -> dict[str, AssetStatus]:
        """Compare the claims as of ``generation`` with the files under ``root``.

        :param root: installation root that RECORD paths are relative to
        :param generation: generation to inspect; defaults to the current one
        :return: mapping of path to :py:class:`AssetStatus`, with ``present``
            and ``mismatched`` reflecting the on-disk reality
        """
        root_path = Path(root)
        report = {}
        for path, status in self.snapshot(generation).items():
            target = root_path / path
            present = target.is_file()
            mismatched = []
            if present:
                mismatched = [
                    claim.distribution
                    for claim in status.claims
                    if not self._matches(target, claim.entry)
                ]
            report[path] = AssetStatus(
                path=path,
                claims=status.claims,
                disputed=status.disputed,
                present=present,
                mismatched=tuple(mismatched),
            )
        return report

    def missing(
        self, root: str | Path, generation: int | None = None
    ) -> tuple[str, ...]:
        """Return claimed paths that do not exist under ``root``.

        :param root: installation root that RECORD paths are relative to
        :param generation: generation to inspect; defaults to the current one
        :return: missing paths, sorted
        """
        return tuple(
            path
            for path, status in self.audit(root, generation).items()
            if not status.present
        )

    @staticmethod
    def _matches(target: Path, entry: RecordEntry) -> bool:
        with target.open("rb") as stream:
            return entry.validate_stream(stream)

    def _claims(self, generation: int | None) -> dict[str, dict[str, Claim]]:
        if generation is None:
            generation = self._generation
        if generation > self._generation or generation < 0:
            raise InventoryError(f"no such generation: {generation}")
        return _claims_at(self._events, generation)
