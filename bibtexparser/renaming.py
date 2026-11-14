"""Grouped renaming of entry keys, keeping cross-references intact.

The logic in this module powers :meth:`bibtexparser.Library.rename_entries`.
It renames several entry keys in a single, atomic operation (which is required
to support swaps such as ``A -> B`` together with ``B -> A``), and rewrites
every value that references one of the renamed keys:

* the ``crossref`` field of any entry, and
* any additional field explicitly declared as holding citation keys
  (e.g. a BibLaTeX ``related`` list).

Values that are plain literals - titles, notes, comments, strings and any
other field that merely happens to contain the same characters as a key -
are never touched, because those fields are not declared as reference fields.

"""

import re
from collections.abc import Iterable
from collections.abc import Mapping

from bibtexparser.exceptions import RenamingFailedError
from bibtexparser.library import Library
from bibtexparser.model import Entry
from bibtexparser.model import Field

#: The BibTeX field that points from one entry to another.
CROSSREF_FIELD_KEY = "crossref"

# A maximal run of characters that are neither a concatenation operator (``#``),
# a list separator (`,`) nor whitespace. Such a run is a single citation key
# inside an unenclosed reference value, e.g. ``A`` in ``A # B`` or ``A, B``.
_REFERENCE_TOKEN = re.compile(r"[^#,\s]+")


def _remap_reference_value(value: str, key_mapping: Mapping[str, str]) -> str:
    """Rewrite every citation-key token of a reference value.

    Separators (``#`` for concatenations, ``,`` for lists and the surrounding
    whitespace) are preserved exactly. Tokens that are not keys of
    ``key_mapping`` are left unchanged, so a value that references an entry
    that is not renamed keeps pointing at the same work.

    :param value: The raw field value, e.g. ``"A # B"`` or ``"A, B"``.
    :param key_mapping: Mapping of old entry keys to their new keys.
    :return: The value with all known key tokens replaced simultaneously.
    """
    return _REFERENCE_TOKEN.sub(
        lambda match: key_mapping.get(match.group(0), match.group(0)), value
    )


def _is_reference_field(field: Field, extra_field_keys: set[str]) -> bool:
    """Whether a field holds citation keys that must follow renamed entries.

    The ``crossref`` field always references entries, and any other field is a
    reference field only if the caller explicitly declared it as one (e.g. a
    BibLaTeX ``related`` list). Fields that are not declared - titles, notes,
    comments and any other literal - never match, even if their value happens
    to contain the same characters as a renamed key.

    :param field: The field to inspect.
    :param extra_field_keys: Lower-cased keys of caller-declared reference fields.
    :return: True if references in this field are to be rewritten.
    """
    key = field.key.lower()
    return key == CROSSREF_FIELD_KEY or key in extra_field_keys


def _validate_mapping(
    current_entries: Mapping[str, Entry], key_mapping: Mapping[str, str]
) -> tuple[list[str], dict[str, Entry], list[tuple[str, str]]]:
    """Check that applying ``key_mapping`` cannot create duplicate entry keys.

    Nothing is mutated. Targets occupied by an entry that is not itself being
    renamed, missing source keys and duplicate targets are all collected.

    :param current_entries: Entries keyed by their current keys, i.e. the
        authoritative view derived from ``library.entries`` (which stays
        correct even if an entry's key was changed after it was added).
    :param key_mapping: Mapping of old entry keys to their new keys.
    :return: Tuple of (missing source keys, third-party occupiers keyed by
        target key, pairs of source keys that map to the same target).
    """
    source_keys = set(key_mapping.keys())

    missing = [key for key in key_mapping if key not in current_entries]

    occupiers: dict[str, Entry] = {}
    for target in key_mapping.values():
        occupier = current_entries.get(target)
        if occupier is not None and target not in source_keys:
            occupiers[target] = occupier

    seen_targets: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for source, target in key_mapping.items():
        previous_source = seen_targets.get(target)
        if previous_source is not None:
            collisions.append((previous_source, source))
        else:
            seen_targets[target] = source

    return missing, occupiers, collisions


def rename_entries(
    library: Library,
    key_mapping: Mapping[str, str],
    extra_reference_field_keys: Iterable[str] = (),
) -> None:
    """Rename a group of entry keys atomically, updating all references.

    The keys are renamed simultaneously, so swaps work as expected: passing
    ``{"A": "B", "B": "A"}`` exchanges the two keys rather than collapsing
    both entries onto one key.

    Along with the entry keys, every value that references a renamed entry is
    rewritten. This always includes the ``crossref`` field of every entry,
    plus any field listed in ``extra_reference_field_keys`` (e.g. a BibLaTeX
    ``related`` list). Both comma-separated lists (``"A, B"``) and
    ``#``-concatenations (``"A # B"``) are handled, and the original
    separators are preserved. Titles, notes, comments, ``@string`` blocks and
    other fields that are not declared as reference fields are left unchanged.

    The operation is atomic: if it cannot be performed, the library is not
    modified at all.

    :param library: The library whose entries are renamed (modified in place).
    :param key_mapping: Mapping of existing entry keys to their new keys.
    :param extra_reference_field_keys: Field keys (case-insensitive) that, in
        addition to ``crossref``, hold citation keys and must follow renames.
    :raises RenamingFailedError: If a source key does not exist, if two
        sources map to the same target, or if a target is already occupied by
        a third entry not part of the renaming. The error identifies the
        offending keys and the occupying entries; the library is left unchanged.
    """
    key_mapping = dict(key_mapping)
    if not key_mapping:
        return

    # `library.entries` preserves order and reflects current keys even if an
    # entry's key was changed after it was added, so it is the authoritative
    # view; the private by-key index is only rebuilt at the end.
    current_entries = {entry.key: entry for entry in library.entries}

    missing, occupiers, collisions = _validate_mapping(current_entries, key_mapping)
    if missing or occupiers or collisions:
        problems = []
        if missing:
            problems.append(f"no entry with key(s): {', '.join(sorted(missing))}")
        if collisions:
            rendered = ", ".join(f"{left!r} and {right!r}" for left, right in collisions)
            problems.append(f"the following sources map to the same target: {rendered}")
        if occupiers:
            rendered = ", ".join(
                f"target {target!r} is occupied by entry {entry.key!r}"
                for target, entry in sorted(occupiers.items())
            )
            problems.append(rendered)
        raise RenamingFailedError(
            "Cannot rename entry keys; the library was not modified. " + "; ".join(problems),
            missing=missing,
            collisions=collisions,
            occupiers=dict(occupiers),
        )

    extra_field_keys = {key.lower() for key in extra_reference_field_keys}

    entry: Entry
    for entry in library.entries:
        field: Field
        for field in entry.fields:
            if _is_reference_field(field, extra_field_keys) and isinstance(field.value, str):
                new_value = _remap_reference_value(field.value, key_mapping)
                if new_value == field.value:
                    continue
                # Assigning a new value resets the enclosing demand, so restore
                # it: we only swap key tokens and must not change how the value
                # is written (e.g. an unenclosed `A # B` must stay unenclosed).
                enclosing = field.enclosing
                field.value = new_value
                field.enclosing = enclosing

    # Validation guarantees every source exists and that the new keys are
    # unique, so assigning and rebuilding the index cannot create duplicates.
    entries_to_rename = [current_entries[source] for source in key_mapping]
    for entry, target in zip(entries_to_rename, key_mapping.values()):
        entry.key = target

    # Rebuild the by-key index from the (still order-preserving) entries list
    # so that swaps and longer renaming chains resolve to unique, new keys.
    library._entries_by_key = {entry.key: entry for entry in library.entries}
