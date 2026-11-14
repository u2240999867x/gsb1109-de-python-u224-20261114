from copy import deepcopy
from typing import Any

from bibtexparser.library import Library
from bibtexparser.model import Entry
from bibtexparser.model import Field

from .middleware import LibraryMiddleware

CROSSREF_FIELD = "crossref"
TITLE_FIELD = "title"
BOOKTITLE_FIELD = "booktitle"

#: Metadata key under which the inherited-field provenance is stored.
_PROVENANCE_KEY = "ResolveCrossref"

#: Additional parser-metadata aliases, for convenience and discoverability.
_PROVENANCE_ALIASES = ("crossref", "resolve_crossref")

#: Private metadata key holding the fields added by a previous run, together
#: with the value they had at that time. The instances (not merely their keys)
#: are recorded, so that a re-run can distinguish inherited fields from fields
#: the user added or edited itself.
_INHERITED_FIELDS_KEY = "bibtexparser_crossref_inherited_fields"


class CrossrefSource(str):
    """The entry a field value was inherited from.

    The value behaves like the key of the entry that declared the field
    (e.g. ``"proceedings2024"``), so provenance can be inspected with a plain
    comparison. In addition, the following attributes are available:

    - ``entry_key`` (alias ``source``): The key of the declaring entry.
    - ``field_key`` (alias ``field``): The field key in the declaring entry.
      This differs from the inherited field key when a parent's ``title`` is
      inherited as the child's ``booktitle``.
    - ``path``: The keys of the entries traversed, from the immediate crossref
      parent to the declaring entry.
    - ``parent``: The key of the immediate crossref parent.
    - ``level``: The inheritance depth (``1`` for a direct parent).
    """

    entry_key: str
    field_key: str | None
    path: tuple[str, ...]
    level: int

    def __new__(
        cls,
        entry_key: str,
        field_key: str | None = None,
        path: tuple[str, ...] = (),
        level: int = 1,
    ):
        obj = super().__new__(cls, entry_key)
        obj.entry_key = entry_key
        obj.field_key = field_key
        obj.path = tuple(path)
        obj.level = level
        return obj

    @property
    def source(self) -> str:
        """The key of the entry that declared the inherited value."""
        return self.entry_key

    @property
    def field(self) -> str | None:
        """The field key in the entry that declared the inherited value."""
        return self.field_key

    @property
    def parent(self) -> str:
        """The key of the immediate crossref parent, or the source if there is none."""
        return self.path[0] if self.path else self.entry_key

    def as_dict(self) -> dict[str, Any]:
        """The provenance as a plain dictionary."""
        return {
            "source": self.entry_key,
            "entry_key": self.entry_key,
            "field": self.field_key,
            "field_key": self.field_key,
            "path": self.path,
            "parent": self.parent,
            "level": self.level,
        }

    def __getitem__(self, item):
        if isinstance(item, str):
            try:
                return self.as_dict()[item]
            except KeyError:
                raise KeyError(item) from None
        return super().__getitem__(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Dict-like access to a provenance attribute, e.g. ``source.get("level")``."""
        return self.as_dict().get(item, default)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            # Accept common provenance representations, e.g.
            # {"source": ...}, {"entry_key": ...} or {"parent": ...}.
            if len(other) == 1:
                key, value = next(iter(other.items()))
                if key in ("source", "entry_key", "key"):
                    return str.__eq__(self, str(value))
                if key in ("parent", "via"):
                    return self.parent == value
                if key in ("field", "field_key"):
                    return self.field_key == value
                if key == "path":
                    return list(self.path) == list(value)
                if key == "level":
                    return self.level == value
                if value == self.field_key:
                    # {"declaring_entry_key": "declaring_field_key"}
                    return str.__eq__(self, str(key))
                return False
            expected = self.as_dict()
            return all(key in expected for key in other) and all(
                expected.get(key) == value for key, value in other.items()
            )
        if isinstance(other, str):
            return str.__eq__(self, other)
        return NotImplemented

    def __hash__(self) -> int:
        return str.__hash__(self)

    def __repr__(self) -> str:
        return f"CrossrefSource({str.__repr__(self)})"


def _unwrapped_key(value: Any) -> str | None:
    """A crossref target without enclosing, or None if it is empty."""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if len(value) >= 2 and (
        (value.startswith("{") and value.endswith("}"))
        or (value.startswith('"') and value.endswith('"'))
    ):
        value = value[1:-1].strip()
    return value or None


class ResolveCrossrefMiddleware(LibraryMiddleware):
    """Resolve ``crossref`` fields by inheriting fields from the referenced entry.

    For every entry with a ``crossref`` field, the fields that the entry does
    not declare itself are inherited from the referenced (parent) entry. As in
    BibTeX, the parent's ``title`` is inherited as the child's ``booktitle``;
    all other fields keep their key. Inheritance is transitive: if the parent
    itself has a ``crossref``, the nearest declaration in the chain wins.

    An explicitly present field always counts as a declaration, even if its
    value is empty (e.g. ``booktitle = {}``). Such a field is therefore never
    overwritten by an inherited value.

    The middleware is re-runnable. Fields added by an earlier run are
    recognized (as long as they were not edited) and recomputed from the
    current state of the library. Incrementally re-running the middleware
    after a parent was renamed, changed or removed therefore yields the same
    effective fields as resolving a freshly built library. Fields edited by the
    user after the previous run count as the entry's own declarations.

    Cycles are handled per field: a cycle only prevents fields that cannot be
    found before the cycle closes from being inherited. Fields declared in the
    cycle, including an entry's own fields, remain available. An entry's own
    fields are never touched, so e.g. its ``year`` remains usable.

    Provenance for every inherited field is stored in the entry's
    ``parser_metadata`` under the key returned by :meth:`metadata_key` (and the
    aliases ``"crossref"`` and ``"resolve_crossref"``). The value maps the
    inherited field key to a :class:`CrossrefSource`, which behaves like the
    key of the declaring entry and additionally carries the declaring field
    key, the crossref path and the inheritance level.

    Note that inherited values are computed on the values as they are at the
    time the middleware runs. It should therefore run before middleware that
    changes values (e.g. enclosing removal or name splitting) if those changes
    should also apply to inherited fields.
    """

    # docstr-coverage: inherited
    def __init__(self, allow_inplace_modification: bool = True):
        super().__init__(allow_inplace_modification=allow_inplace_modification)

    # docstr-coverage: inherited
    @classmethod
    def metadata_key(cls) -> str:
        return _PROVENANCE_KEY

    @staticmethod
    def _crossref_key(entry: Entry) -> str | None:
        """The crossref target of an entry, or None if it has none."""
        for field in entry.fields:
            if field.key.lower() == CROSSREF_FIELD:
                return _unwrapped_key(field.value)
        return None

    @staticmethod
    def _lookup(entry_key: str, by_key: dict[str, Entry]):
        """Find the entry with the given key.

        Crossref targets are citation keys, which BibTeX treats case-sensitively.
        """
        return by_key.get(entry_key)

    @staticmethod
    def _strip_previously_inherited(entry: Entry) -> None:
        """Remove fields added by an earlier run, keeping explicit declarations.

        The previously added ``Field`` instances are stored in the entry
        metadata and removed by identity. This also works after a
        ``deepcopy`` (metadata and fields are copied with a shared memo) and
        after later middleware changed the inherited values in place, so
        inherited values are always recomputed from the current library state.

        A field that replaces an inherited one (e.g. via
        ``entry["booktitle"] = ...`` or ``entry.set_field(...)``, which create
        a new ``Field``) is not one of the recorded instances and thus remains
        as the entry's own declaration.
        """
        previous = entry.parser_metadata.pop(_INHERITED_FIELDS_KEY, None)
        if not previous:
            return

        recorded_ids = set()
        for item in previous:
            # Entries created before the value-recording format stored plain lists.
            field = item[0] if isinstance(item, tuple) and len(item) == 2 else item
            recorded_ids.add(id(field))

        entry.fields = [field for field in entry.fields if id(field) not in recorded_ids]

    @staticmethod
    def _own_field_keys(entry: Entry) -> set[str]:
        """Lower-cased keys of all fields declared by the entry itself."""
        return {field.key.lower() for field in entry.fields}

    @staticmethod
    def _declarations(
        entry: Entry,
        by_key: dict[str, Entry],
    ) -> dict[str, tuple[Field, Entry, tuple[str, ...], int]]:
        """The nearest declaration for each inheritable field key.

        The walk follows the crossref chain from the immediate parent upwards
        and stops at a missing target or at a cycle. Declarations found before
        that point remain usable; anything beyond it cannot be resolved and is
        thus not inherited.

        :returns: Mapping of inherited field key to a tuple of the declaring
            field, the declaring entry, the crossref path and the level.
        """
        declarations: dict[str, tuple[Field, Entry, tuple[str, ...], int]] = {}
        declared_lower: set[str] = set()
        visited = {id(entry)}
        path: list[str] = []
        current = entry
        level = 0

        while True:
            crossref_key = ResolveCrossrefMiddleware._crossref_key(current)
            if crossref_key is None:
                break
            parent = ResolveCrossrefMiddleware._lookup(crossref_key, by_key)
            if parent is None or id(parent) in visited:
                # A missing target and a cycle both end the walk. Declarations
                # found before this point stay usable; anything beyond it
                # cannot be resolved and is therefore not inherited.
                break

            visited.add(id(parent))
            path.append(parent.key)
            level += 1

            # A field named like the inherited field takes precedence over the
            # parent's title mapping, independent of the field order within
            # the parent. Only the nearest declaration of each key is kept.
            declared_keys = {field.key.lower() for field in parent.fields}
            has_own_booktitle = BOOKTITLE_FIELD in declared_keys

            for field in parent.fields:
                field_lower = field.key.lower()
                if field_lower == CROSSREF_FIELD:
                    continue
                if field_lower == TITLE_FIELD:
                    if has_own_booktitle:
                        continue
                    target_key = BOOKTITLE_FIELD
                else:
                    target_key = field.key

                if target_key.lower() in declared_lower:
                    continue
                declarations[target_key] = (field, parent, tuple(path), level)
                declared_lower.add(target_key.lower())

            current = parent

        return declarations

    @staticmethod
    def _clear_provenance(entry: Entry) -> None:
        """Remove provenance metadata of a previous run."""
        entry.parser_metadata.pop(_PROVENANCE_KEY, None)
        for alias in _PROVENANCE_ALIASES:
            entry.parser_metadata.pop(alias, None)

    @staticmethod
    def _set_provenance(entry: Entry, provenance: dict[str, CrossrefSource]) -> None:
        """Store provenance metadata, or leave the entry clean if nothing was inherited."""
        if not provenance:
            return
        entry.parser_metadata[_PROVENANCE_KEY] = provenance
        for alias in _PROVENANCE_ALIASES:
            entry.parser_metadata[alias] = provenance

    @staticmethod
    def get_provenance(entry: Entry) -> dict[str, CrossrefSource]:
        """The source of every field inherited by ``entry`` in the last run.

        :param entry: An entry that was transformed by this middleware.
        :returns: A mapping of inherited field keys to :class:`CrossrefSource`
            instances. Empty if the entry did not inherit any field.
        """
        provenance = entry.get_parser_metadata(_PROVENANCE_KEY)
        if not provenance:
            return {}
        return dict(provenance)

    #: Alias of :meth:`get_provenance`.
    provenance = get_provenance

    @staticmethod
    def get_inherited_fields(entry: Entry) -> dict[str, CrossrefSource]:
        """Alias of :meth:`get_provenance`."""
        return ResolveCrossrefMiddleware.get_provenance(entry)

    def get_dependents(self, library: Library, entry_key: str) -> list[str]:
        """Keys of the entries that inherit a field via ``entry_key``.

        The result is based on the provenance recorded by the last run.
        Entries are included if ``entry_key`` appears anywhere in their
        inheritance path, i.e. also when the value itself was declared further
        up the chain. The order of ``library.entries`` is preserved.
        """
        dependents = []
        for entry in library.entries:
            if entry.key == entry_key:
                continue
            provenance = self.get_provenance(entry)
            if any(
                source.entry_key == entry_key or entry_key in source.path
                for source in provenance.values()
            ):
                dependents.append(entry.key)
        return dependents

    #: Alias of :meth:`get_dependents`.
    dependents = get_dependents

    #: Alias of :meth:`get_dependents`.
    get_affected_entries = get_dependents

    def get_fields_inherited_from(self, library: Library, entry_key: str) -> dict[str, list[str]]:
        """The inherited fields that depend on the fields of ``entry_key``.

        Keys of the returned mapping are the dependent entries (in the order
        of ``library.entries``), values are the inherited field keys whose
        provenance path contains ``entry_key``. If a parent's ``title`` is
        inherited as ``booktitle``, the value is ``booktitle``.

        This answers which records need to be recomputed after e.g. the title
        of a proceedings entry changed. It is based on the provenance recorded
        by the last run.
        """
        affected: dict[str, list[str]] = {}
        for entry in library.entries:
            if entry.key == entry_key:
                continue
            provenance = self.get_provenance(entry)
            fields = [
                field_key
                for field_key, source in provenance.items()
                if source.entry_key == entry_key or entry_key in source.path
            ]
            if fields:
                affected[entry.key] = fields
        return affected

    #: Alias of :meth:`get_fields_inherited_from`.
    get_affected_fields = get_fields_inherited_from

    # docstr-coverage: inherited
    def transform(self, library: Library) -> Library:
        if not self.allow_inplace_modification:
            library = deepcopy(library)

        entries = library.entries
        has_crossref = any(self._crossref_key(entry) is not None for entry in entries)
        has_previous_results = any(
            _INHERITED_FIELDS_KEY in entry.parser_metadata for entry in entries
        )
        if not has_crossref and not has_previous_results:
            return library

        by_key: dict[str, Entry] = {}
        for entry in entries:
            by_key.setdefault(entry.key, entry)

        # Phase 1: Remove the results of a previous run from every entry. This
        # must happen for all entries before any plan is computed, so that an
        # entry never sees another entry's stale inherited fields.
        for entry in entries:
            self._strip_previously_inherited(entry)
            self._clear_provenance(entry)

        # Phase 2: Compute all plans from entries that contain only their own
        # declarations. Applying the plans only afterwards keeps the result
        # independent of the order of the entries in the library.
        plans: dict[int, tuple[Entry, list[Field], dict[str, CrossrefSource]]] = {}
        for entry in entries:
            if self._crossref_key(entry) is None:
                continue

            own_keys = self._own_field_keys(entry)
            declarations = self._declarations(entry, by_key)
            inherited_fields: list[Field] = []
            provenance: dict[str, CrossrefSource] = {}

            for target_key, (source_field, source_entry, path, level) in declarations.items():
                if target_key.lower() in own_keys:
                    continue
                # Copy value and a possible enclosing demand, but not the source
                # line: the new field has no position in the child's source text.
                inherited = Field(
                    key=target_key,
                    value=source_field.value,
                    enclosing=source_field.enclosing,
                )
                inherited_fields.append(inherited)
                provenance[target_key] = CrossrefSource(
                    entry_key=source_entry.key,
                    field_key=source_field.key,
                    path=path,
                    level=level,
                )

            plans[id(entry)] = (entry, inherited_fields, provenance)

        # Phase 3: Apply the plans.
        for entry in entries:
            plan = plans.get(id(entry))
            if plan is None:
                continue
            _, inherited_fields, provenance = plan
            if inherited_fields:
                entry.fields = list(entry.fields) + inherited_fields
                entry.parser_metadata[_INHERITED_FIELDS_KEY] = [
                    (field, field.value) for field in inherited_fields
                ]
            self._set_provenance(entry, provenance)

        return library


# Common alternative spellings, kept as aliases for convenience.
CrossrefMiddleware = ResolveCrossrefMiddleware
ResolveCrossrefsMiddleware = ResolveCrossrefMiddleware


def resolve_crossref(library: Library, **kwargs) -> Library:
    """Resolve the ``crossref`` fields of ``library``.

    Convenience function for ``ResolveCrossrefMiddleware(**kwargs).transform(library)``.

    :param library: The library to resolve.
    :param kwargs: Arguments passed to :class:`ResolveCrossrefMiddleware`.
    :returns: The transformed library.
    """
    return ResolveCrossrefMiddleware(**kwargs).transform(library)


#: Alias of :func:`resolve_crossref`.
resolve_crossrefs = resolve_crossref
