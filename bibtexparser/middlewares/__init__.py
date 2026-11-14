from bibtexparser.middlewares.enclosing import AddEnclosingMiddleware
from bibtexparser.middlewares.enclosing import RemoveEnclosingMiddleware
from bibtexparser.middlewares.field_conflicts import FIELD_CONFLICTS_METADATA_KEY
from bibtexparser.middlewares.field_conflicts import FieldConflict
from bibtexparser.middlewares.field_conflicts import ResolveFieldConflictsMiddleware
from bibtexparser.middlewares.field_conflicts import get_field_conflicts
from bibtexparser.middlewares.field_conflicts import normalize_page_range
from bibtexparser.middlewares.fieldkeys import NormalizeFieldKeys
from bibtexparser.middlewares.interpolate import ResolveStringReferencesMiddleware
from bibtexparser.middlewares.latex_encoding import LatexDecodingMiddleware
from bibtexparser.middlewares.latex_encoding import LatexEncodingMiddleware
from bibtexparser.middlewares.middleware import BlockMiddleware
from bibtexparser.middlewares.middleware import LibraryMiddleware
from bibtexparser.middlewares.month import MonthAbbreviationMiddleware
from bibtexparser.middlewares.month import MonthIntMiddleware
from bibtexparser.middlewares.month import MonthLongStringMiddleware
from bibtexparser.middlewares.names import MergeCoAuthors
from bibtexparser.middlewares.names import MergeNameParts
from bibtexparser.middlewares.names import NameParts
from bibtexparser.middlewares.names import SeparateCoAuthors
from bibtexparser.middlewares.names import SplitNameParts
from bibtexparser.middlewares.sorting_blocks import SortBlocksByTypeAndKeyMiddleware
from bibtexparser.middlewares.sorting_blocks import SortBlocksMiddleware
from bibtexparser.middlewares.sorting_entry_fields import SortFieldsAlphabeticallyMiddleware
from bibtexparser.middlewares.sorting_entry_fields import SortFieldsCustomMiddleware

from .parsestack import default_parse_stack
from .parsestack import default_unparse_stack

# Declared explicitly so that sphinx autodoc documents these re-exports;
# without __all__, `automodule:: bibtexparser.middlewares` skips them all
# as imported members.
__all__ = [
    "AddEnclosingMiddleware",
    "BlockMiddleware",
    "FIELD_CONFLICTS_METADATA_KEY",
    "FieldConflict",
    "LatexDecodingMiddleware",
    "LatexEncodingMiddleware",
    "LibraryMiddleware",
    "MergeCoAuthors",
    "MergeNameParts",
    "MonthAbbreviationMiddleware",
    "MonthIntMiddleware",
    "MonthLongStringMiddleware",
    "NameParts",
    "NormalizeFieldKeys",
    "RemoveEnclosingMiddleware",
    "ResolveFieldConflictsMiddleware",
    "ResolveStringReferencesMiddleware",
    "SeparateCoAuthors",
    "SortBlocksByTypeAndKeyMiddleware",
    "SortBlocksMiddleware",
    "SortFieldsAlphabeticallyMiddleware",
    "SortFieldsCustomMiddleware",
    "SplitNameParts",
    "default_parse_stack",
    "default_unparse_stack",
    "get_field_conflicts",
    "normalize_page_range",
]
