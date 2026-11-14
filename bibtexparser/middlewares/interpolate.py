import warnings
from copy import deepcopy
from typing import Any

from bibtexparser.library import Library
from bibtexparser.model import Entry
from bibtexparser.model import Field

from .enclosing import REMOVED_ENCLOSING_KEY
from .enclosing import RemoveEnclosingMiddleware
from .middleware import LibraryMiddleware
from .stringdefs import STRING_SOURCE_KEY
from .stringdefs import StringDefinitions
from .stringdefs import _Resolver

#: Parser metadata listing the references that could not be fully resolved.
#: Stored per entry field together with the resolved field keys, so that
#: partial resolutions remain inspectable after the fact.
UNRESOLVED_REFERENCES_KEY = "unresolved_string_references"


def _value_is_nonstring_or_enclosed(value: Any) -> bool:
    """Check if value is an int or a single literal enclosed in its entirety.

    A concatenation expression merely starting and ending in braces or quotes
    (e.g. ``{intro} # {outro}``) is not enclosed as a whole and must still be
    resolved, hence the precise enclosing checks of the enclosing middleware
    are reused instead of a naive start/end comparison.
    """
    if not isinstance(value, str):
        return True
    if value.startswith('"') and value.endswith('"'):
        return RemoveEnclosingMiddleware._is_enclosed_in_quotes(value)
    if value.startswith("{") and value.endswith("}"):
        return RemoveEnclosingMiddleware._is_enclosed_in_braces(value)
    return False


class ResolveStringReferencesMiddleware(LibraryMiddleware):
    """Replace string references with their values.

    Unlike a plain lookup table, the resolution is source-aware and handles the
    full bibtex value grammar:

    - ``@string`` macros are grouped by their source (see
      :data:`~bibtexparser.middlewares.stringdefs.STRING_SOURCE_KEY`). A
      reference made by a source first resolves against that source's own
      declarations, allowing a library to override a shared abbreviation
      without changing what the abbreviation means to another library;
    - concatenation expressions (``a # " text" # b``) are resolved token by
      token. If one token cannot be resolved (an undefined macro or a recursive
      expansion that loops), the already resolved fragments are kept and only
      the unresolved chain remains as bare references;
    - resolution never raises on bad input: undefined or mutually recursive
      macros leave the affected values partially resolved instead of failing the
      import of unrelated entries.

    A value that resolves to a single enclosed literal is stored as that
    literal with its original enclosing, exactly like a directly enclosed value.
    """

    # docstr-coverage: inherited
    def __init__(self, allow_inplace_modification: bool = True):
        super().__init__(allow_inplace_modification)

    # docstr-coverage: inherited
    @classmethod
    def metadata_key(cls) -> str:
        return "ResolveStringReferences"

    # docstr-coverage: inherited
    def transform(self, library: Library) -> Library:
        if not self.allow_inplace_modification:
            library = deepcopy(library)

        raised_enclosing_warning = False
        resolver = _Resolver(StringDefinitions.from_library(library))

        block: Entry
        for block in library.blocks:
            if not isinstance(block, Entry):
                # ``@string`` blocks are the vocabulary itself: they are kept
                # verbatim and only expanded at the points that reference them.
                continue
            if not raised_enclosing_warning and REMOVED_ENCLOSING_KEY in block.parser_metadata:
                raised_enclosing_warning = True
                warnings.warn(
                    (
                        "The RemoveEnclosingMiddleware must not run before "
                        "the ResolveStringReferencesMiddleware. "
                        "We continue, but string interpolation is likely to fail, "
                        "or to be too aggressive (i.e., replace too many strings)."
                    ),
                    UserWarning,
                )
            self._transform_fields(block, resolver)

        return library

    def _transform_fields(self, entry: Entry, resolver: _Resolver) -> None:
        resolved_fields: list[str] = []
        unresolved: dict[str, list[str]] = {}
        field: Field
        for field in entry.fields:
            if _value_is_nonstring_or_enclosed(field.value) or not field.value.strip():
                continue
            source = entry.get_parser_metadata(STRING_SOURCE_KEY)
            result = resolver.resolve_value(field.value, source)
            rendered = result.render()
            if rendered != field.value:
                field.value = rendered
            if result.complete:
                resolved_fields.append(field.key)
            else:
                unresolved[field.key] = list(result.unresolved)

        if resolved_fields:
            entry.parser_metadata[self.metadata_key()] = resolved_fields
        if unresolved:
            entry.parser_metadata[UNRESOLVED_REFERENCES_KEY] = unresolved


# TODO Middleware to replace field values with string references, if found

# TODO Middleware to resolve Crossref
