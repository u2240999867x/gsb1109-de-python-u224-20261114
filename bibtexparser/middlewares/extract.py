from collections import deque
from collections.abc import Collection
from copy import deepcopy
from dataclasses import dataclass

from bibtexparser.library import Library
from bibtexparser.model import Entry
from bibtexparser.model import String

from .enclosing import REMOVED_ENCLOSING_KEY
from .enclosing import RemoveEnclosingMiddleware
from .middleware import LibraryMiddleware

#: Entry fields referencing a single parent entry (its key is the value).
_SINGLE_PARENT_FIELDS = ("crossref", "xref")
#: Entry fields referencing a comma-separated list of parent entries.
_LIST_PARENT_FIELDS = ("xdata",)


@dataclass(frozen=True)
class MissingDependency:
    """A dependency of the extracted entries that is not defined in the library.

    Reported by :class:`ExtractEntriesMiddleware` (see its ``missing_dependencies``
    property) for every entry key or string key that is referenced by the
    extracted fragment but cannot be found in the library.
    """

    kind: str
    """What is missing: ``"entry"`` (e.g. a ``crossref`` parent) or ``"string"``."""

    key: str
    """The missing key, as referenced (entry key or ``@string`` key)."""

    path: tuple[str, ...] = ()
    """Entry keys leading from a requested citation key to the referencing entry.

    Empty if the requested citation key itself is missing. For transitive
    parents, this is the chain of ``crossref``/``xdata``/``xref`` parents,
    e.g. ``("cited", "proceedings")`` means the gap was reached from entry
    ``proceedings``, which is a parent of the requested entry ``cited``.
    """

    field: str | None = None
    """The field of the last entry in ``path`` through which the dependency
    was referenced (e.g. ``"crossref"`` or ``"journal"``); ``None`` if the
    requested citation key itself is missing."""

    via_strings: tuple[str, ...] = ()
    """The ``@string`` keys through which a missing string was reached,
    if it was referenced indirectly from another string's value."""


def _split_concat_parts(value: str) -> list[str]:
    """Split a value expression into its ``#``-separated parts.

    The ``#`` concatenation operator only applies at the top level:
    ``#`` inside braces or quotes is part of a literal.
    """
    parts = []
    current = []
    depth = 0
    in_quotes = False
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            current.extend((char, value[i + 1]))
            i += 2
            continue
        if char == '"' and depth == 0:
            in_quotes = not in_quotes
        elif not in_quotes:
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(depth - 1, 0)
            elif char == "#" and depth == 0:
                parts.append("".join(current))
                current = []
                i += 1
                continue
        current.append(char)
        i += 1
    parts.append("".join(current))
    return parts


def _as_string_reference(part: str) -> str | None:
    """The string key referenced by a single value part, or None.

    Returns None for local literals (braced or quoted values, which shadow
    any same-named ``@string``) and for plain integers.
    """
    stripped, enclosing = RemoveEnclosingMiddleware._strip_enclosing(part)
    if enclosing != "no-enclosing" or stripped.isdigit():
        return None
    return stripped


def _parent_entry_keys(field_key: str, value: str) -> list[str]:
    """The entry keys referenced by a ``crossref``/``xref``/``xdata`` field."""
    if not isinstance(value, str):
        return []
    # A key may be given bare or enclosed; a list may additionally be
    # enclosed as a whole (e.g. `xdata = {key1, key2}`).
    stripped, _ = RemoveEnclosingMiddleware._strip_enclosing(value)
    if field_key in _LIST_PARENT_FIELDS:
        return [key.strip() for key in stripped.split(",") if key.strip()]
    return [stripped] if stripped else []


def _enclosing_demand(removed_enclosing: str | None, has_metadata: bool) -> str | None:
    """The enclosing to demand on an exported value so it round-trips.

    Values of a freshly split library are raw value expressions; writing them
    verbatim (``no-enclosing``) parses back to the same value. If enclosing
    was removed by middleware, the recorded enclosing is restored instead
    (``no-enclosing`` for values that were bare, e.g. string references).
    """
    if not has_metadata:
        return "no-enclosing"
    if removed_enclosing == "no-enclosing":
        return "no-enclosing"
    return removed_enclosing


class ExtractEntriesMiddleware(LibraryMiddleware):
    """Extract a minimal, self-contained library for the given citation keys.

    The transformed library contains exactly:

    - the requested entries;
    - their parent entries, referenced (also transitively) via ``crossref``,
      ``xdata`` or ``xref`` fields (e.g. the ``@proceedings`` parent of an
      ``@inproceedings`` entry);
    - the ``@string`` definitions actually referenced by the included entries
      (bare, unenclosed field values - including parts of ``#`` concatenations),
      and the strings those strings reference in turn.

    Strings that are shadowed by a local literal (e.g. ``journal = {jacm}``
    with a ``@string{jacm = ...}`` present) are *not* referenced and thus not
    included. Comments, preambles and all other blocks are dropped.

    The exported blocks are copies; their fields and strings carry the
    enclosing demand needed to write them back verbatim, so the fragment
    round-trips: writing it with the default unparse stack and re-parsing
    with the default parse stack (starting from an empty string table)
    restores the same effective field values as in the original library.

    Dependencies that are referenced but not defined in the library (e.g. a
    ``crossref`` key without a matching entry, or a bare value without a
    matching ``@string``) are reported in ``missing_dependencies``, together
    with the path from the requested citation key to the gap.

    Note: To export string definitions (rather than already-interpolated
    values), apply this middleware before
    :class:`ResolveStringReferencesMiddleware`, e.g. on a library parsed
    with ``bibtexparser.parse_string(text, parse_stack=[])``.
    """

    def __init__(self, keys: Collection[str]):
        """

        :param keys: Citation keys of the entries to extract.
        """
        self._keys = list(keys)
        self._missing_dependencies: list[MissingDependency] = []
        # The extracted blocks are copies, the passed library is never modified.
        super().__init__(allow_inplace_modification=False)

    @property
    def missing_dependencies(self) -> list[MissingDependency]:
        """Dependencies referenced by the extracted entries, but not defined
        in the transformed library, in the order they were discovered."""
        return list(self._missing_dependencies)

    @staticmethod
    def _string_references(value: str) -> list[str]:
        """The string keys referenced by a (possibly concatenated) field value."""
        references = []
        for part in _split_concat_parts(value):
            reference = _as_string_reference(part)
            if reference is not None:
                references.append(reference)
        return references

    def _collect(self, library: Library) -> tuple[dict[str, Entry], dict[str, String]]:
        """Transitively collect the entries and strings the fragment consists of."""
        entries = library.entries_dict
        # BibTeX string keys are case-insensitive; later definitions win
        # (mirroring ResolveStringReferencesMiddleware).
        strings = {key.lower(): string for key, string in library.strings_dict.items()}

        included_entries: dict[str, Entry] = {}
        included_strings: dict[str, String] = {}
        reported: set[tuple[str, str]] = set()
        self._missing_dependencies = []

        def report(missing: MissingDependency) -> None:
            dedup_key = (
                missing.kind,
                missing.key if missing.kind == "entry" else missing.key.lower(),
            )
            if dedup_key not in reported:
                reported.add(dedup_key)
                self._missing_dependencies.append(missing)

        # Queue items: (key, path of entry keys from a requested key to the
        # referrer, field through which the key was referenced).
        entry_queue = deque((key, (), None) for key in self._keys)
        # Queue items: (lower-case string key, entry path, entry field, via strings).
        string_queue = deque()

        while entry_queue:
            key, path, field = entry_queue.popleft()
            entry = entries.get(key)
            if entry is None:
                report(MissingDependency(kind="entry", key=key, path=path, field=field))
                continue
            if key in included_entries:
                continue
            included_entries[key] = entry
            chain = path + (key,)
            for f in entry.fields:
                field_key = f.key.lower()
                if field_key in _SINGLE_PARENT_FIELDS or field_key in _LIST_PARENT_FIELDS:
                    for parent_key in _parent_entry_keys(field_key, f.value):
                        if parent_key in entries:
                            if parent_key not in included_entries:
                                entry_queue.append((parent_key, chain, f.key))
                        else:
                            report(
                                MissingDependency(
                                    kind="entry", key=parent_key, path=chain, field=f.key
                                )
                            )
                elif isinstance(f.value, str):
                    for reference in self._string_references(f.value):
                        string = strings.get(reference.lower())
                        if string is None:
                            report(
                                MissingDependency(
                                    kind="string", key=reference, path=chain, field=f.key
                                )
                            )
                        elif reference.lower() not in included_strings:
                            included_strings[reference.lower()] = string
                            string_queue.append((reference.lower(), chain, f.key, ()))

        while string_queue:
            string_key, path, field, via = string_queue.popleft()
            value = strings[string_key].value
            if not isinstance(value, str):
                continue
            for reference in self._string_references(value):
                string = strings.get(reference.lower())
                if string is None:
                    report(
                        MissingDependency(
                            kind="string",
                            key=reference,
                            path=path,
                            field=field,
                            via_strings=via + (string_key,),
                        )
                    )
                elif reference.lower() not in included_strings:
                    included_strings[reference.lower()] = string
                    string_queue.append((reference.lower(), path, field, via + (string_key,)))

        return included_entries, included_strings

    @staticmethod
    def _demand_roundtrip_enclosing(blocks: list) -> None:
        """Demand the enclosing on each value that makes it round-trip verbatim."""
        for block in blocks:
            removed = block.parser_metadata.get(REMOVED_ENCLOSING_KEY)
            if isinstance(block, Entry):
                has_metadata = isinstance(removed, dict)
                for f in block.fields:
                    if not isinstance(f.value, str):
                        continue
                    f.enclosing = _enclosing_demand(
                        removed.get(f.key) if has_metadata else None, has_metadata
                    )
            elif isinstance(block, String) and isinstance(block.value, str):
                block.enclosing = _enclosing_demand(removed, removed is not None)

    # docstr-coverage: inherited
    def transform(self, library: Library) -> Library:
        included_entries, included_strings = self._collect(library)

        # Keep the original relative order of the included blocks.
        selected = [
            block
            for block in library.blocks
            if (isinstance(block, Entry) and included_entries.get(block.key) is block)
            or (isinstance(block, String) and included_strings.get(block.key.lower()) is block)
        ]
        blocks = deepcopy(selected)
        self._demand_roundtrip_enclosing(blocks)
        return Library(blocks=blocks, fail_on_duplicate_key=False)
