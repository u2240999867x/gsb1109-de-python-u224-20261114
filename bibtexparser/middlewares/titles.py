"""Middleware collecting display and search forms of titles.

The title field itself is left untouched: its value remains the LaTeX/BibTeX
spelling from the source file, which can be written back unchanged.  The
collected representations are attached to the entry's ``parser_metadata``
under the key returned by :meth:`TitleFormsMiddleware.metadata_key`.
"""

import logging
from collections.abc import Iterable

from bibtexparser.library import Library
from bibtexparser.model import Block
from bibtexparser.model import Entry
from bibtexparser.titles import TitleForms
from bibtexparser.titles import collect_title_forms_for_field

from .enclosing import REMOVED_ENCLOSING_KEY
from .middleware import BlockMiddleware

logger = logging.getLogger(__name__)


class TitleFormsMiddleware(BlockMiddleware):
    """Collect raw, display and search forms of title-like fields.

    For every configured field, a :class:`~bibtexparser.titles.TitleForms`
    instance is stored in ``entry.parser_metadata["title_forms"]`` (a dict
    mapping the field key to the forms).  The field value itself is not
    modified, so parsing and writing still round-trip the original BibTeX
    spelling.

    The middleware is typically appended to the parse stack::

        library = bibtexparser.parse_string(
            bibtex,
            append_middleware=[TitleFormsMiddleware()],
        )
        forms = library.entries[0].get_parser_metadata("title_forms")["title"]
        forms.display       # human-readable, brace-protected case kept
        forms.search_key    # accent- and case-insensitive retrieval key
        forms.unrecognized  # commands that could not be decoded, with ranges
    """

    def __init__(
        self,
        fields: Iterable[str] = ("title",),
        title_case: bool = True,
        allow_inplace_modification: bool = True,
    ):
        """
        :param fields: The entry fields to collect title forms for.
        :param title_case: Whether unprotected words in the display value
            should be title-cased.  Brace-protected content is never changed.
        :param allow_inplace_modification: See corresponding property.
        """
        super().__init__(
            allow_inplace_modification=allow_inplace_modification,
            allow_parallel_execution=True,
        )
        self._fields = tuple(fields)
        self._title_case = title_case

    @property
    def fields(self) -> tuple[str, ...]:
        """The entry fields considered by this middleware."""
        return self._fields

    # docstr-coverage: inherited
    @classmethod
    def metadata_key(cls) -> str:
        return "title_forms"

    # docstr-coverage: inherited
    def transform_entry(self, entry: Entry, library: Library) -> Block:
        removed_enclosing = entry.get_parser_metadata(REMOVED_ENCLOSING_KEY)
        collected: dict[str, TitleForms] = {}
        for field_key in self._fields:
            field = entry.fields_dict.get(field_key)
            if field is None:
                continue
            if not isinstance(field.value, str):
                logger.info(
                    f" [{self.metadata_key()}] Cannot collect title forms for field "
                    f"{field_key} with value type {type(field.value)}"
                )
                continue
            collected[field_key] = collect_title_forms_for_field(
                field.value,
                title_case=self._title_case,
                strip_enclosing=not _already_stripped(field_key, field.value, removed_enclosing),
                enclosing=_known_enclosing(field_key, removed_enclosing),
            )

        if collected:
            entry.parser_metadata[self.metadata_key()] = collected
        return entry


def get_title_forms(entry: Entry, field: str = "title") -> TitleForms | None:
    """Return the title forms collected for ``entry`` and ``field``, if any.

    This is a convenience accessor for the data stored by
    :class:`TitleFormsMiddleware`.  It returns ``None`` if the middleware did
    not run, the entry has no such field, or the field was skipped.
    """
    collected = entry.get_parser_metadata(TitleFormsMiddleware.metadata_key())
    if not isinstance(collected, dict):
        return None
    forms = collected.get(field)
    return forms if isinstance(forms, TitleForms) else None


def _already_stripped(field_key: str, value: str, removed_enclosing: dict | None) -> bool:
    """Whether a field value no longer has the value enclosing from the source.

    The default parse stack removes the enclosing of values before middleware
    appended to it runs.  In that case, a remaining outer pair is a group the
    author wrote inside the value (i.e. deliberate case protection), and must
    not be stripped as value enclosing again.
    """
    if not isinstance(removed_enclosing, dict):
        return False
    removed = removed_enclosing.get(field_key)
    if removed in ("{", '"'):
        return True
    if removed is None:
        return False
    # `no-enclosing` is recorded for unenclosed values: nothing was removed.
    return False


def _known_enclosing(field_key: str, removed_enclosing: dict | None) -> str | None:
    """The value enclosing recorded by ``RemoveEnclosingMiddleware``, if any."""
    if not isinstance(removed_enclosing, dict):
        return None
    removed = removed_enclosing.get(field_key)
    return removed if removed in ("{", '"') else None
