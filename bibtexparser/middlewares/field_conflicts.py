"""Middleware for adjudicating duplicate (case-insensitively equal) field keys.

Old bibtex files sometimes contain the same field more than once, e.g. both
``pages`` and ``PAGES``. The splitter keeps every occurrence but wraps such an
entry in a :class:`~bibtexparser.model.DuplicateFieldKeyBlock`. This middleware
turns the occurrences into adjudicable data:

- purely typographical differences of a page range (``1--10`` vs. ``1-10`` vs.
  ``1 – 10``) are normalized to the same interval and merged into one value;
- genuinely contradictory values are kept as candidates, while all other
  fields of the entry stay usable;
- every decision is recorded as a :class:`FieldConflict` that remembers the
  original occurrences and their exact positions, so a single conflict (e.g.
  one edited page number) can be reverted without touching the others.
"""

import dataclasses
from copy import deepcopy
from typing import Optional
from typing import Union

from bibtexparser.library import Library
from bibtexparser.model import DuplicateFieldKeyBlock
from bibtexparser.model import Entry
from bibtexparser.model import Field
from bibtexparser.model import FieldPosition
from bibtexparser.model import ParsingFailedBlock

from .middleware import BlockMiddleware

#: Parser-metadata key under which the :class:`FieldConflict` records are stored
#: on the entries they belong to.
FIELD_CONFLICTS_METADATA_KEY = "field_conflicts"

# Range separators accepted in page fields, longest/most-specific first, so that
# ``1--10`` is split on ``--`` rather than on a single hyphen.
_RANGE_SEPARATORS = ("--", "—", "–", "-")


def _balanced_braces(text: str) -> bool:
    """Whether every opening brace in `text` is closed and vice versa."""
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _peel_enclosing(value: str) -> str:
    """Drop one pair of enclosing braces/quotes wrapping the whole value."""
    value = value.strip()
    if len(value) >= 2:
        if value[0] == "{" and value[-1] == "}" and _balanced_braces(value[1:-1]):
            return value[1:-1].strip()
        if value[0] == '"' and value[-1] == '"':
            return value[1:-1].strip()
    return value


def _canonical_endpoint(token: str) -> Optional[str]:
    # Page endpoints are compared on their meaningful characters only, so that
    # whitespace, protective braces and casing do not create false conflicts.
    cleaned = "".join(
        char for char in token if not char.isspace() and char not in '{}"'
    ).lower()
    return cleaned or None


def normalize_page_range(raw_value: str) -> Optional[str]:
    """Normalize the typography of a page range to a canonical interval.

    Returns e.g. ``"1--10"`` for ``"1-10"``, ``"1 – 10"`` or ``"1--10"``, and
    the single page ``"12"`` for ``"12"``. Returns ``None`` if the value is not
    a simple page range (string concatenations, comma-separated locators,
    unparseable endpoints, ...), in which case no automatic merge is attempted.
    """
    text = _peel_enclosing(raw_value)
    if not text or "#" in text or "," in text:
        return None

    for separator in _RANGE_SEPARATORS:
        if separator in text:
            left, _, right = text.partition(separator)
            # More than one range separator: not a single interval.
            if any(other in left or other in right for other in _RANGE_SEPARATORS):
                return None
            start = _canonical_endpoint(left)
            end = _canonical_endpoint(right)
            if start is None or end is None:
                return None
            return f"{start}--{end}"

    # No separator: a single page.
    return _canonical_endpoint(text)


def _effective_text(field: Field) -> str:
    """The comparable text backing a field, preferring the untrimmed parsed value."""
    text = field.raw_value if field.raw_value is not None else str(field.value)
    return _peel_enclosing(text)


@dataclasses.dataclass
class FieldConflict:
    """The record of one group of duplicate fields sharing a normalized key.

    ``occurrences`` holds immutable snapshots of the fields exactly as parsed
    (including their :class:`~bibtexparser.model.FieldPosition` and untrimmed
    value). They are never mutated, which is what allows an edit to one page
    number to be undone: :meth:`revert` restores fresh copies of these snapshots
    while leaving every other field and conflict untouched.
    """

    key: str
    occurrences: list[Field]
    candidates: list[str]
    merged: bool
    canonical_value: Optional[str]
    chosen_index: Optional[int] = None
    resolved: bool = False
    #: The field objects currently representing this conflict inside the entry.
    _live: list[Field] = dataclasses.field(default_factory=list)

    @property
    def positions(self) -> list[Optional[FieldPosition]]:
        """The exact source position of every original occurrence (``None`` if not parsed)."""
        return [field.position for field in self.occurrences]

    @property
    def is_open(self) -> bool:
        """Whether all original occurrences are present and still await adjudication.

        This is also the state after :meth:`revert`, regardless of whether the
        values are merely typographical variants or genuinely contradictory.
        """
        return self.chosen_index is None

    @property
    def is_merged(self) -> bool:
        """Whether one automatically merged value currently represents the occurrences."""
        return self.merged and self.chosen_index is not None and not self.resolved

    @property
    def is_conflicting(self) -> bool:
        """Whether open occurrences hold genuinely contradictory values."""
        return self.chosen_index is None and not self.merged

    @property
    def chosen(self) -> Optional[Field]:
        """The field currently representing the conflict in the entry, if any."""
        if self.chosen_index is None:
            return None
        return self._live[0] if self._live else None

    def resolve(self, entry: Entry, choice: Union[int, str, Field]) -> Field:
        """Adjudicate this conflict by selecting one of the candidate occurrences.

        :param choice: either the index of an occurrence, one of the candidate
            value strings, or the (live) :class:`Field` instance to keep.
        :returns: the field that now represents the conflict in `entry`.
        """
        if not self.is_open:
            raise ValueError(
                f"Field '{self.key}' is already adjudicated; call revert() before resolving again."
            )
        self._rebind_if_copied(entry)

        if isinstance(choice, Field):
            try:
                index = self._live.index(choice)
            except ValueError as exc:
                raise ValueError(
                    "The chosen field is not one of the live conflict occurrences."
                ) from exc
        elif isinstance(choice, int):
            index = choice
            if not 0 <= index < len(self.occurrences):
                raise IndexError(
                    f"Invalid candidate index {index} for field '{self.key}': "
                    f"there are {len(self.occurrences)} occurrences."
                )
        else:
            try:
                index = self.candidates.index(choice)
            except ValueError as exc:
                raise ValueError(f"No candidate of field '{self.key}' matches {choice!r}.") from exc

        selected = deepcopy(self.occurrences[index])
        self._splice(entry, [selected])
        self.chosen_index = index
        self.resolved = True
        return selected

    def revert(self, entry: Entry) -> None:
        """Undo this conflict's merge/resolution, restoring both original occurrences.

        Only this conflict is touched. Fresh copies of the immutable original
        occurrences (with their original positions and values) are spliced back
        in, and other fields and conflicts remain unchanged. The record's
        adjudication (``merged``, ``canonical_value``) and the candidates are
        retained; the record simply becomes open again.
        """
        restored = deepcopy(self.occurrences)
        self._splice(entry, restored)
        self.chosen_index = None
        self.resolved = False

    def _splice(self, entry: Entry, new_live: list[Field]) -> None:
        """Replace this conflict's current fields in `entry` with `new_live`."""
        if not self._live:
            raise ValueError(f"Field '{self.key}' is not present in the entry.")
        self._rebind_if_copied(entry)
        # Locate by identity, not equality: duplicate occurrences may otherwise
        # compare equal, and equality also ignores provenance.
        target = self._live[0]
        index = next((i for i, field in enumerate(entry.fields) if field is target), None)
        if index is None:
            raise ValueError(f"Field '{self.key}' is not present in the entry.")
        entry.fields[index : index + len(self._live)] = new_live
        self._live = new_live

    def _rebind_if_copied(self, entry: Entry) -> None:
        """Re-attach ``_live`` after the entry was copied (e.g. ``deepcopy``).

        Copying an entry clones its fields, so the stored field identities no
        longer match. The immutable ``(key, position)`` pairs survive a copy and
        uniquely identify each occurrence (positions differ even for identical
        keys), which lets us re-point ``_live`` at the cloned fields.
        """
        if any(field is entry_field for entry_field in entry.fields for field in self._live):
            return
        markers = {(field.key, field.position) for field in self._live}
        rebound = [field for field in entry.fields if (field.key, field.position) in markers]
        if not rebound:
            raise ValueError(f"Field '{self.key}' is not present in the entry.")
        self._live = rebound


def get_field_conflicts(entry: Entry) -> list[FieldConflict]:
    """All :class:`FieldConflict` records attached to an entry (possibly empty)."""
    return entry.get_parser_metadata(FIELD_CONFLICTS_METADATA_KEY) or []


class ResolveFieldConflictsMiddleware(BlockMiddleware):
    """Resolve entries containing duplicate, case-insensitively equal field keys.

    Duplicate occurrences of the same normalized key are either merged (when
    their values are equivalent, which for the configured page fields includes
    typographical variants of the same page range) or retained as adjudicable
    :class:`FieldConflict` candidates. The entry always remains usable: even
    unresolved conflicts only flag the offending fields, never the whole entry.

    A :class:`~bibtexparser.model.DuplicateFieldKeyBlock` produced by the
    splitter is recovered into its underlying entry; other kinds of failed
    blocks are passed through unchanged.

    Stack ordering: place this middleware *before*
    :class:`~bibtexparser.middlewares.RemoveEnclosingMiddleware` (i.e. at the
    start of a custom parse stack). A recovered entry would otherwise skip the
    enclosing-removal step, so its still-enclosed values would get a second
    pair of braces when written. Entries that were never failed blocks are
    unaffected by this ordering.
    """

    def __init__(
        self,
        page_fields: tuple[str, ...] = ("pages",),
        allow_inplace_modification: bool = True,
    ):
        """
        :param page_fields: Field keys (matched case-insensitively) for which
            typographical page-range differences are normalized before merging.
        """
        super().__init__(
            allow_inplace_modification=allow_inplace_modification,
            allow_parallel_execution=True,
        )
        self._page_fields = tuple(key.lower() for key in page_fields)

    @classmethod
    def metadata_key(cls) -> str:
        return FIELD_CONFLICTS_METADATA_KEY

    @property
    def page_fields(self) -> tuple[str, ...]:
        """The keys treated as page fields when normalizing ranges."""
        return self._page_fields

    # docstr-coverage: inherited
    def transform_failed_block(
        self, failed_block: ParsingFailedBlock, library: Library
    ) -> ParsingFailedBlock | Entry:
        if isinstance(failed_block, DuplicateFieldKeyBlock):
            return self._resolve_entry(failed_block.ignore_error_block)
        return failed_block

    # docstr-coverage: inherited
    def transform_entry(self, entry: Entry, library: Library) -> Entry:
        if not self._has_duplicate_keys(entry):
            return entry
        return self._resolve_entry(entry)

    @staticmethod
    def _has_duplicate_keys(entry: Entry) -> bool:
        """Whether two fields share a key after lower-casing."""
        seen: set[str] = set()
        for field in entry.fields:
            normalized = field.key.lower()
            if normalized in seen:
                return True
            seen.add(normalized)
        return False

    def _resolve_entry(self, entry: Entry) -> Entry:
        """Adjudicate every duplicate-key group in `entry` and rebuild its fields."""
        # Immutable snapshots first: a revert must restore exact occurrences,
        # values and positions even after fields have been merged or edited.
        snapshots = [deepcopy(field) for field in entry.fields]

        groups: dict[str, list[int]] = {}
        order: list[str] = []
        for index, field in enumerate(entry.fields):
            normalized = field.key.lower()
            if normalized not in groups:
                groups[normalized] = []
                order.append(normalized)
            groups[normalized].append(index)

        conflicts: list[FieldConflict] = []
        new_fields: list[Field] = []
        for normalized in order:
            indices = groups[normalized]
            if len(indices) == 1:
                new_fields.append(entry.fields[indices[0]])
                continue

            occurrences = [snapshots[index] for index in indices]
            conflict = self._adjudicate(normalized, occurrences)
            conflicts.append(conflict)
            new_fields.extend(conflict._live)

        entry.fields = new_fields
        if conflicts:
            entry.set_parser_metadata(FIELD_CONFLICTS_METADATA_KEY, conflicts)
        return entry

    def _adjudicate(
        self, normalized_key: str, occurrences: list[Field]
    ) -> FieldConflict:
        """Build the :class:`FieldConflict` for one duplicate-key group."""
        candidates = [_effective_text(field) for field in occurrences]

        canonical: Optional[str] = None
        if normalized_key in self._page_fields:
            normalized_ranges = [normalize_page_range(text) for text in candidates]
            if all(value is not None for value in normalized_ranges):
                first = normalized_ranges[0]
                if all(value == first for value in normalized_ranges[1:]):
                    canonical = first

        if canonical is not None:
            # Same interval, different typography: keep one working field. The
            # first occurrence's formatting and exact position are preserved.
            live = [deepcopy(occurrences[0])]
            return FieldConflict(
                key=normalized_key,
                occurrences=occurrences,
                candidates=candidates,
                merged=True,
                canonical_value=canonical,
                chosen_index=0,
                _live=live,
            )

        if len(set(candidates)) == 1:
            # Identical values under keys differing only in case.
            live = [deepcopy(occurrences[0])]
            return FieldConflict(
                key=normalized_key,
                occurrences=occurrences,
                candidates=candidates,
                merged=True,
                canonical_value=None,
                chosen_index=0,
                _live=live,
            )

        # Genuinely contradictory: keep every candidate so the caller can decide,
        # while the rest of the entry remains fully usable.
        live = deepcopy(occurrences)
        return FieldConflict(
            key=normalized_key,
            occurrences=occurrences,
            candidates=candidates,
            merged=False,
            canonical_value=None,
            chosen_index=None,
            _live=live,
        )
