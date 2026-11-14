"""Source-aware ``@string`` definitions and their (partial) resolution.

BibTeX ``@string`` macros are case-insensitive and can be redefined. When the
bib files of several institutions are merged, the same abbreviation (e.g.
``@string{j = ...}``) may therefore denote different journals to different
sources. Collapsing all definitions into a single mapping makes the
abbreviation resolve to the value of the last source that happened to define
it.

The classes in this module instead keep every declaration, grouped by the
*source* it came from and remembering the declaration order:

- a macro referenced from a source first resolves against that source's own
  declarations, so a local override wins for the local library only;
- if the source does not define it, the declarations of the other sources are
  consulted, most recently added first, which allows referencing a shared
  vocabulary;
- a source's override never mutates another source's declaration.

Resolution is total (it never raises): an undefined macro is kept as an
unresolved reference, and a recursive expansion that loops (two macros
referencing each other) stops at the repeated macro instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from bibtexparser.library import Library
from bibtexparser.model import String

#: Parser metadata holding the source a ``@string`` block was imported from.
#: Absence of the metadata (i.e. ``None``) marks the definitions of the
#: currently parsed library, which take precedence over any imported source.
STRING_SOURCE_KEY = "bibtexparser_string_source"

#: Source label for definitions seeded from a previously parsed library when
#: those do not carry an explicit source label themselves.
IMPORTED_STRING_SOURCE = "imported"

# A token of a bibtex value, before resolution:
#   - ``("lit", inner_text, enclosing)``: a quoted/braced literal, with the
#     enclosing character (``"{"`` or ``'"'``) stripped from ``inner_text``;
#   - ``("num", digits, None)``: a bare number, a literal that needs no lookup;
#   - ``("ref", macro_name, None)``: a bare macro reference.
Token = tuple[str, str, str | None]


@dataclass(frozen=True)
class StringDeclaration:
    """One ``@string`` definition, as seen in a source."""

    key: str
    value: str
    source: str | None


@dataclass
class StringDefinitions:
    """The ``@string`` macros visible while resolving references.

    Declarations are grouped by ``source`` and remember the order in which
    sources were added. Within a source, redeclaring a macro overrides the
    previous declaration; across sources, the source a lookup originates from
    always wins, and the remaining sources are consulted from the most recently
    added to the oldest, which models a shared vocabulary with local overrides.
    """

    _by_source: dict[str | None, dict[str, StringDeclaration]] = field(default_factory=dict)
    _source_order: list[str | None] = field(default_factory=list)

    @classmethod
    def from_library(cls, library: Library) -> StringDefinitions:
        """Collect the active ``@string`` blocks of ``library``.

        Only active ``String`` blocks are collected: duplicate definitions are
        already surfaced as ``DuplicateBlockKeyBlock`` (see
        ``library.failed_blocks``) and are not valid declarations, matching the
        behavior of ``library.strings_dict``. The source of each block is read
        from its :data:`STRING_SOURCE_KEY` parser metadata (``None`` for the
        library's own definitions).
        """
        definitions = cls()
        for block in library.blocks:
            if not isinstance(block, String):
                continue
            definitions.add(
                key=block.key,
                value=block.value,
                source=block.get_parser_metadata(STRING_SOURCE_KEY),
            )
        return definitions

    def add(self, key: str, value: str, source: str | None = None) -> StringDeclaration:
        """Add a declaration, overriding an earlier declaration of ``key``
        in the same source."""
        lower_key = key.lower()
        declaration = StringDeclaration(key=lower_key, value=value, source=source)
        if source not in self._by_source:
            self._by_source[source] = {}
            self._source_order.append(source)
        self._by_source[source][lower_key] = declaration
        return declaration

    @property
    def sources(self) -> list[str | None]:
        """The sources holding declarations, in the order they were added."""
        return list(self._source_order)

    def declaration(self, key: str, source: str | None = None) -> StringDeclaration | None:
        """The declaration of ``key`` visible from ``source``, if any."""
        lower_key = key.lower()
        own = self._by_source.get(source)
        if own is not None and lower_key in own:
            return own[lower_key]
        for other_source in reversed(self._source_order):
            if other_source == source:
                continue
            other = self._by_source[other_source]
            if lower_key in other:
                return other[lower_key]
        return None


def _split_concatenation(value: str) -> list[Token]:
    """Split an unenclosed bibtex value on its top-level ``#`` operators.

    Braced and quoted segments (including nested braces and ``#`` inside such
    segments) are kept whole, mirroring the escaping rules of the splitter:
    a backslash escapes the character right after it.
    """
    tokens: list[Token] = []
    segment_start = 0
    depth = 0
    in_quotes = False
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if depth == 0 and char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char == "{":
            depth += 1
        elif not in_quotes and char == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and not in_quotes and char == "#":
            segment = value[segment_start:index].strip()
            if segment:
                tokens.append(_classify_segment(segment))
            segment_start = index + 1
        index += 1
    tail = value[segment_start:].strip()
    if tail:
        tokens.append(_classify_segment(tail))
    return tokens


def _classify_segment(segment: str) -> Token:
    if len(segment) >= 2:
        if segment[0] == "{" and segment[-1] == "}":
            return "lit", segment[1:-1], "{"
        if segment[0] == '"' and segment[-1] == '"':
            return "lit", segment[1:-1], '"'
    if segment.isdigit():
        # A bare number (e.g. `year = 2023`) is a literal, not a macro lookup.
        return "num", segment, None
    return "ref", segment, None


def _render_token(token: Token) -> str:
    kind, text, enclosing = token
    if kind == "lit":
        if enclosing == '"':
            return f'"{text}"'
        return f"{{{text}}}"
    return text


@dataclass(frozen=True)
class ResolveResult:
    """The outcome of resolving a bibtex value."""

    tokens: tuple[Token, ...]
    complete: bool
    unresolved: tuple[str, ...]

    def render(self) -> str:
        """Render the (partially) resolved tokens back to a bibtex value.

        A single, fully resolved literal is rendered with its original
        enclosing (so the following enclosing middleware treats it exactly like
        a plain enclosed value); otherwise the tokens are joined with `` # ``,
        keeping unresolved references bare and resolved fragments enclosed.
        """
        if self.complete and len(self.tokens) == 1 and self.tokens[0][0] == "lit":
            return _render_token(self.tokens[0])
        return " # ".join(_render_token(token) for token in self.tokens)


class _Resolver:
    """Resolves bibtex values against :class:`StringDefinitions`.

    The expansion of a fully resolved macro is memoized. Partial or cyclic
    expansions are never cached, as they depend on the references that are
    still open along the current expansion path.
    """

    def __init__(self, definitions: StringDefinitions):
        self._definitions = definitions
        self._cache: dict[tuple[str, str | None], ResolveResult] = {}

    def resolve_value(self, value: str, source: str | None = None) -> ResolveResult:
        return self._resolve_expression(value, source, frozenset())

    def _resolve_expression(
        self, value: str, source: str | None, seen: frozenset[str]
    ) -> ResolveResult:
        tokens = _split_concatenation(value)
        resolved_tokens: list[Token] = []
        unresolved: list[str] = []
        complete = True
        for token in tokens:
            kind, text, enclosing = token
            if kind in ("lit", "num"):
                resolved_tokens.append(token)
                continue
            result = self._resolve_reference(text, source, seen)
            resolved_tokens.extend(result.tokens)
            if not result.complete:
                complete = False
                for name in result.unresolved:
                    if name not in unresolved:
                        unresolved.append(name)
        return ResolveResult(
            tokens=tuple(resolved_tokens),
            complete=complete,
            unresolved=tuple(unresolved),
        )

    def _resolve_reference(
        self, name: str, source: str | None, seen: frozenset[str]
    ) -> ResolveResult:
        lower_name = name.lower()
        if lower_name in seen:
            # The macro is already being expanded further up the chain:
            # keep it as the open, unresolved link instead of looping.
            return ResolveResult(
                tokens=(("ref", name, None),),
                complete=False,
                unresolved=(name,),
            )

        cache_key = (lower_name, source)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        declaration = self._definitions.declaration(lower_name, source)
        if declaration is None:
            return ResolveResult(
                tokens=(("ref", name, None),),
                complete=False,
                unresolved=(name,),
            )

        result = self._resolve_expression(
            declaration.value, declaration.source, seen | {lower_name}
        )
        if result.complete:
            self._cache[cache_key] = result
        return result
