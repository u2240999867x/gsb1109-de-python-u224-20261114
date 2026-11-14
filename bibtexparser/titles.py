"""Collect title representations for display and retrieval.

A title in a BibTeX file is written in LaTeX, where braces serve two purposes:
they delimit arguments and they protect the case of their content from
bibliography styles.  A catalogue needs three things from such a title:

* the original BibTeX spelling, so that it can be written back unchanged,
* a human-readable display text, where LaTeX commands are decoded, but the
  case of brace-protected content (e.g. chemical formulas such as ``{H2O}``)
  is preserved, and
* a case- and accent-insensitive search key, where e.g. ``M{\\"u}ller`` and
  ``Müller`` retrieve the same entry.

The main entrypoint is :func:`collect_title_forms`, which returns a
:class:`TitleForms` instance with all three representations.  The auxiliary
:func:`normalize_search_key` returns only the search key.

Commands that cannot be decoded are reported (with their range in the original
BibTeX value) in :attr:`TitleForms.unrecognized`; they do not prevent the rest
of the title, including any later recognized accents, from being processed.
"""

import logging
import unicodedata
from dataclasses import dataclass

import pylatexenc
from pylatexenc.latex2text import LatexNodes2Text
from pylatexenc.latex2text import MacroTextSpec
from pylatexenc.latexwalker import LatexCharsNode
from pylatexenc.latexwalker import LatexCommentNode
from pylatexenc.latexwalker import LatexEnvironmentNode
from pylatexenc.latexwalker import LatexGroupNode
from pylatexenc.latexwalker import LatexMacroNode
from pylatexenc.latexwalker import LatexMathNode
from pylatexenc.latexwalker import LatexSpecialsNode
from pylatexenc.latexwalker import LatexWalker

logger = logging.getLogger(__name__)

#: Words which are lower-cased in the middle of a title-cased display value.
_SMALL_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "en",
        "for",
        "from",
        "if",
        "in",
        "into",
        "nor",
        "not",
        "of",
        "on",
        "onto",
        "or",
        "over",
        "per",
        "the",
        "to",
        "up",
        "via",
        "vs",
        "with",
        "within",
        "without",
        # Frequent non-English function words, so that title casing does not
        # unnecessarily change titles written in other languages.
        "am",
        "als",
        "auf",
        "aus",
        "bei",
        "das",
        "dem",
        "den",
        "der",
        "des",
        "die",
        "durch",
        "ein",
        "eine",
        "einem",
        "einen",
        "einer",
        "eines",
        "et",
        "für",
        "gegen",
        "im",
        "la",
        "le",
        "les",
        "los",
        "las",
        "mit",
        "nach",
        "ohne",
        "para",
        "por",
        "sur",
        "über",
        "um",
        "und",
        "unter",
        "vom",
        "von",
        "vor",
        "zu",
        "zum",
        "zur",
    }
)

#: Commands which are LaTeX bookkeeping rather than visible title content.
_SILENT_COMMANDS = frozenset({"protect", "relax"})

#: Commands whose output is case-sensitive even outside a brace-protected group.
_CASE_SENSITIVE_COMMANDS = frozenset({"ensuremath"})

#: Accent letters that do not decompose into a base letter and a combining mark.
_SEARCH_FOLDS = str.maketrans(
    {
        "æ": "ae",
        "œ": "oe",
        "ø": "o",
        "đ": "d",
        "ð": "d",
        "ł": "l",
        "ı": "i",
        "ȷ": "j",
        "þ": "th",
        "ŋ": "n",
    }
)

#: Spoken names of Greek letters, used to fold them into the search key.
_GREEK_NAMES = {
    "α": " alpha ",
    "β": " beta ",
    "γ": " gamma ",
    "δ": " delta ",
    "ε": " epsilon ",
    "ζ": " zeta ",
    "η": " eta ",
    "θ": " theta ",
    "ι": " iota ",
    "κ": " kappa ",
    "λ": " lambda ",
    "μ": " mu ",
    "ν": " nu ",
    "ξ": " xi ",
    "ο": " omicron ",
    "π": " pi ",
    "ρ": " rho ",
    "σ": " sigma ",
    "ς": " sigma ",
    "τ": " tau ",
    "υ": " upsilon ",
    "φ": " phi ",
    "χ": " chi ",
    "ψ": " psi ",
    "ω": " omega ",
}


@dataclass(frozen=True)
class UnrecognizedCommand:
    """A LaTeX command that could not be decoded.

    The ``start`` and ``end`` offsets are a half-open range into the original
    BibTeX value (see :attr:`TitleForms.raw`), i.e. ``raw[start:end]`` is the
    command exactly as written, including its leading backslash but excluding
    any arguments.  Arguments following the command are still rendered, so an
    unknown wrapper such as ``\\ce{H2O}`` does not hide the formula; their
    range, if any, is available via :attr:`argument_span`.
    """

    name: str
    """The command name without the leading backslash, e.g. ``ce``."""

    start: int
    """Start offset (inclusive) of the command in the original value."""

    end: int
    """End offset (exclusive) of the command in the original value."""

    raw: str
    """The command exactly as written in the original value."""

    argument_span: tuple[int, int] | None = None
    """Range of the brace arguments attached to the command, if any."""

    @property
    def span(self) -> tuple[int, int]:
        """The ``(start, end)`` range of the command in the original value."""
        return self.start, self.end

    @property
    def full_span(self) -> tuple[int, int]:
        """Range covering the command and its attached arguments, if any."""
        if self.argument_span is None:
            return self.span
        return self.start, self.argument_span[1]


@dataclass(frozen=True)
class TitleForms:
    """The raw, display and search representations of one title."""

    raw: str
    """The analyzed title string (LaTeX commands and braces kept).

    This is the field value as stored in the library: for a parsed entry, the
    value enclosing has already been removed by the default parse stack.  It is
    never modified.  Command ranges refer to this string; use :attr:`bibtex`
    for a spelling that restores the enclosing."""

    display: str
    """Human-readable title.

    LaTeX commands are decoded and braces are removed; the wording itself is
    kept as written unless ``title_case=True`` was requested.  Content of
    brace-protected groups is always kept verbatim, in particular its case
    (e.g. ``{H2O}`` stays ``H2O``)."""

    search_key: str
    """Normalized key for retrieval.

    The key is accent-folded and case-folded, LaTeX markup is removed, and
    non-alphanumeric characters are replaced by spaces.  Thus ``M{\\"u}ller``,
    ``Müller`` and ``muller`` all yield ``muller``."""

    unrecognized: tuple[UnrecognizedCommand, ...] = ()
    """Commands that could not be decoded, in source order."""

    protected_spans: tuple[tuple[int, int], ...] = ()
    """Half-open ranges of case-protected content in :attr:`display`.

    The ranges cover the text that originated from brace-protected groups, so
    consumers can distinguish protected from unprotected characters."""

    enclosing: str | None = None
    """The value enclosing of the source field (``'{'`` or ``'"'``), if known.

    ``None`` means unknown, e.g. for a standalone :func:`collect_title_forms`
    call.  See :attr:`bibtex`."""

    @property
    def has_unrecognized(self) -> bool:
        """Whether at least one command could not be decoded."""
        return len(self.unrecognized) > 0

    @property
    def bibtex(self) -> str:
        """A restorable BibTeX spelling of the title.

        This is :attr:`raw` with its value enclosing restored.  If the input
        already carried a value enclosing (standalone call), it is returned
        unchanged.  Otherwise curly braces are used (quotes if
        :attr:`enclosing` says so).
        """
        if self.enclosing in ("{", '"'):
            # `raw` is the stripped field value, so the enclosing is added back.
            return f'"{self.raw}"' if self.enclosing == '"' else "{" + self.raw + "}"

        _, offset = _strip_outer_enclosing(self.raw)
        if offset:
            return self.raw
        if self.raw != self.raw.strip():
            stripped = self.raw.strip()
            return "{" + stripped + "}"
        return "{" + self.raw + "}"

    @property
    def protected_display(self) -> str:
        """The display text with case-protected ranges wrapped in braces.

        This is useful when the display form is passed on to a system that
        applies its own case conversion: wrapping the protected ranges again
        preserves the protection.  For titles without protected ranges, this
        equals :attr:`display`.
        """
        if not self.protected_spans:
            return self.display

        parts = []
        previous_end = 0
        for start, end in self.protected_spans:
            parts.append(self.display[previous_end:start])
            parts.append("{")
            parts.append(self.display[start:end])
            parts.append("}")
            previous_end = end
        parts.append(self.display[previous_end:])
        return "".join(parts)


def _build_decoder() -> LatexNodes2Text:
    """Create the LaTeX-to-text decoder used for titles."""
    context = pylatexenc.latex2text.get_default_latex_context_db()
    context.add_context_category(
        "bibtexparser-title-context",
        prepend=True,
        macros=[
            # As in LatexDecodingMiddleware: do not wrap URLs in '< ... >'.
            MacroTextSpec("url", simplify_repl="%s"),
        ],
    )
    return LatexNodes2Text(latex_context=context, math_mode="text")


_DECODER = _build_decoder()

#: Text-formatting commands that only wrap their argument.  The walker parses
#: their arguments as separate group nodes, so the command itself is dropped
#: and the group is rendered normally.  They are not reported as unrecognized.
_WRAPPER_COMMANDS = frozenset(
    {
        "textsuperscript",
        "textsubscript",
        "textnormal",
        "textup",
        "textmd",
        "textsf",
        "texttt",
        "operatorname",
        "mbox",
    }
)


def collect_title_forms(
    value: str, *, title_case: bool = True, strip_enclosing: bool | None = None
) -> TitleForms:
    """Collect the raw, display and search representations of a title.

    :param value: The title as written in BibTeX (LaTeX commands and braces
        included).  Reported command ranges always refer to the passed
        ``value``.
    :param title_case: Whether to title-case unprotected words in
        :attr:`TitleForms.display`.  Default ``True``.  Content of
        brace-protected groups is never case-changed, regardless of this
        setting; unprotected words with intentional inner capitals (acronyms,
        ``McDonald``) are left alone as well.
    :param strip_enclosing: Whether a single pair enclosing the whole value
        (``{...}`` or ``"..."``) is value enclosing rather than case protection.
        ``None`` (default) detects such a pair heuristically.  Pass ``False``
        if the value is already stripped (e.g. the field value of a parsed
        library, where the enclosing was removed by the parse stack).
    :return: The three representations and any unrecognized commands.
    :raises TypeError: If ``value`` is not a string.
    """
    return collect_title_forms_for_field(
        value,
        title_case=title_case,
        strip_enclosing=strip_enclosing,
        enclosing=None,
    )


def collect_title_forms_for_field(
    value: str,
    *,
    title_case: bool = True,
    strip_enclosing: bool | None = None,
    enclosing: str | None = None,
) -> TitleForms:
    """Like :func:`collect_title_forms`, for a title value of an entry field.

    :param enclosing: The value enclosing known from the field
        (``'{'``, ``'"'`` or ``None``), used for :attr:`TitleForms.bibtex`.
    """
    if not isinstance(value, str):
        raise TypeError(f"title value must be a string, got {type(value).__name__}")

    if strip_enclosing is False:
        content, offset = value, 0
    else:
        content, offset = _strip_outer_enclosing(value)

    forms = _TitleRenderer(content, title_case=title_case, offset=offset).render()
    if enclosing is None and offset:
        # A standalone call whose value was enclosed as a whole: `raw` keeps
        # that enclosing, so no further enclosing needs to be restored.
        return TitleForms(
            raw=value,
            display=forms.display,
            search_key=forms.search_key,
            unrecognized=forms.unrecognized,
            protected_spans=forms.protected_spans,
        )

    return TitleForms(
        raw=value,
        display=forms.display,
        search_key=forms.search_key,
        unrecognized=forms.unrecognized,
        protected_spans=forms.protected_spans,
        enclosing=enclosing,
    )


def normalize_search_key(value: str) -> str:
    """Return the search key of a BibTeX/LaTeX title.

    This is a convenience wrapper around :func:`collect_title_forms`; it
    decodes LaTeX markup before normalizing, so accent commands and their
    Unicode spellings produce the same key.
    """
    return collect_title_forms(value).search_key


def display_title(value: str, *, title_case: bool = True) -> str:
    """Return the human-readable display text of a BibTeX/LaTeX title."""
    return collect_title_forms(value, title_case=title_case).display


def title_search_key(value: str) -> str:
    """Alias of :func:`normalize_search_key`, for readability at call sites."""
    return normalize_search_key(value)


def build_title_index(entries, field: str = "title") -> dict[str, list]:
    """Group entries by the search key of one of their fields.

    Uses the forms collected by ``TitleFormsMiddleware`` when available and
    falls back to collecting them on the fly, so it works on libraries parsed
    with and without the middleware.

    :param entries: Iterable of entries (e.g. ``library.entries``).
    :param field: The field to index, ``"title"`` by default.
    :return: A dict mapping search keys to the entries having that key.
    """
    index: dict[str, list] = {}
    for entry in entries:
        forms = entry.get_parser_metadata("title_forms")
        forms = forms.get(field) if isinstance(forms, dict) else None
        if not isinstance(forms, TitleForms):
            value = entry.fields_dict.get(field)
            if value is None or not isinstance(value.value, str):
                continue
            forms = collect_title_forms(value.value)
        if not forms.search_key:
            continue
        index.setdefault(forms.search_key, []).append(entry)
    return index


def _strip_outer_enclosing(value: str) -> tuple[str, int]:
    """Strip the value enclosing of a raw page value.

    The splitter keeps ``{...}`` and ``"..."`` around field values.  These
    braces enclose the value, they do not protect its case: treating them as
    a group would mark the whole title as protected.  Only a pair enclosing
    the *whole* value is removed; inner braces are left alone.

    :return: The stripped content and the offset at which it starts in
        ``value`` (so command ranges can still refer to the original value).
    """
    stripped = value.strip()
    leading = len(value) - len(value.lstrip())
    if len(stripped) < 2:
        return value, 0
    if stripped[0] == '"' and stripped[-1] == '"':
        return stripped[1:-1], leading + 1
    if stripped[0] != "{" or stripped[-1] != "}":
        return value, 0

    depth = 0
    for index, char in enumerate(stripped):
        if char == "{" and not _is_escaped(stripped, index):
            depth += 1
        elif char == "}" and not _is_escaped(stripped, index):
            depth -= 1
            if depth == 0 and index != len(stripped) - 1:
                # The first brace is closed before the end of the value,
                # hence the outer braces do not enclose the whole value.
                return value, 0
    if depth != 0:
        return value, 0
    return stripped[1:-1], leading + 1


def _is_escaped(value: str, index: int) -> bool:
    """Whether ``value[index]`` is preceded by an odd number of backslashes."""
    backslashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


class _TitleRenderer:
    """Render one title from parsed LaTeX nodes."""

    def __init__(self, source: str, *, title_case: bool, offset: int = 0):
        self._source = source
        self._offset = offset
        self._title_case = title_case
        self._chars: list[tuple[str, bool]] = []
        self._unrecognized: list[UnrecognizedCommand] = []

    def render(self) -> TitleForms:
        try:
            nodes, _, _ = LatexWalker(self._source).get_latex_nodes(pos=0)
        except Exception as e:
            # Be forgiving: a single malformed title must not break collection.
            logger.warning(f"Could not parse title as LaTeX, using decoded fallback: {e}")
            self._append_decoded_text(self._source, protected=False)
        else:
            self._render_nodes(nodes, protected=False)

        chars = _normalize_whitespace(self._chars)
        plain_text = "".join(char for char, _ in chars)
        display, protected_spans = _render_display(chars, title_case=self._title_case)
        return TitleForms(
            raw=self._source,
            display=display,
            search_key=_normalize_text_for_search(plain_text),
            unrecognized=tuple(self._unrecognized),
            protected_spans=protected_spans,
        )

    def _render_nodes(self, nodes, protected: bool) -> None:
        index = 0
        while index < len(nodes):
            node = nodes[index]
            next_node = nodes[index + 1] if index + 1 < len(nodes) else None
            consumed_next = False
            if isinstance(node, LatexCharsNode):
                self._append_text(node.chars, protected)
            elif isinstance(node, LatexGroupNode):
                # A group protects the case of everything it contains,
                # in addition to delimiting macro arguments.
                self._render_nodes(node.nodelist, protected=True)
            elif isinstance(node, LatexMacroNode):
                consumed_next = self._render_macro(node, protected, next_node)
            elif isinstance(node, LatexSpecialsNode):
                self._append_decoded_nodes([node], protected)
            elif isinstance(node, LatexMathNode):
                # Math variables and formulas are case-sensitive.
                self._append_decoded_nodes([node], protected=True)
            elif isinstance(node, LatexEnvironmentNode):
                self._append_decoded_nodes([node], protected)
            elif isinstance(node, LatexCommentNode):
                # LaTeX comments are not part of the visible title.
                pass
            else:
                logger.info(f"Ignoring unsupported title node type {type(node).__name__}")
            index += 2 if consumed_next else 1

    def _render_macro(
        self, node: LatexMacroNode, protected: bool, next_node=None
    ) -> bool:
        """Render a macro node.  Returns whether ``next_node`` was consumed."""
        name = node.macroname
        arguments = (
            [arg for arg in node.nodeargd.argnlist if arg is not None]
            if node.nodeargd is not None and node.nodeargd.argnlist
            else []
        )
        if name in _SILENT_COMMANDS:
            return False
        if name in _WRAPPER_COMMANDS:
            # ``\textsuperscript{14}``: keep the argument, drop the command.
            if arguments:
                self._render_argument_nodes(arguments, protected)
                return False
            # The walker did not attach the argument (command unknown to it),
            # so the argument follows as a separate group node.
            if isinstance(next_node, LatexGroupNode):
                self._render_argument_nodes([next_node], protected)
                return True
            return False

        spec = _DECODER.latex_context.get_macro_spec(name)
        if spec is None:
            self._record_unrecognized(node, next_node)
            # Arguments are separate nodes and are rendered normally below,
            # so text after (and inside) an unknown command is not lost.
            return False

        if (
            spec.simplify_repl is None
            and not spec.discard
            and node.nodeargd is not None
            and node.nodeargd.argnlist
        ):
            # A formatting wrapper such as \textbf{...}: render its arguments
            # node by node, so nested brace protection is preserved.
            # The argument group itself delimits the argument; it is not case
            # protection (``\textit{the ...}`` must still be title-cased).
            self._render_argument_nodes(node.nodeargd.argnlist, protected)
            return

        self._append_decoded_nodes(
            [node],
            protected=protected or name in _CASE_SENSITIVE_COMMANDS,
        )

    def _render_argument_nodes(self, arguments, protected: bool) -> None:
        """Render macro arguments.

        In BibTeX, braces protect the case of whatever they enclose, and this
        includes the braces delimiting a macro argument: ``\\textbf{nasa}``
        keeps its lower-case spelling when a bibliography style changes case.
        Therefore the argument content is always treated as protected.
        """
        for argument in arguments:
            if argument is None:
                continue
            if isinstance(argument, LatexGroupNode):
                self._render_nodes(argument.nodelist, protected=True)
            else:
                self._render_nodes([argument], protected)

    def _record_unrecognized(self, node: LatexMacroNode, next_node=None) -> None:
        token = self._source[node.pos : node.pos + node.len]
        # `LatexMacroNode.len` includes whitespace consumed after the command.
        token = token.rstrip()
        end = node.pos + len(token)
        argument_span = self._argument_span(node, next_node)
        self._unrecognized.append(
            UnrecognizedCommand(
                name=node.macroname,
                start=self._offset + node.pos,
                end=self._offset + end,
                raw=self._source[node.pos:end],
                argument_span=(
                    None
                    if argument_span is None
                    else (self._offset + argument_span[0], self._offset + argument_span[1])
                ),
            )
        )

    @staticmethod
    def _argument_span(
        node: LatexMacroNode, next_node=None
    ) -> tuple[int, int] | None:
        """Range of the brace arguments immediately following a macro node.

        An unknown macro (e.g. ``\\ce``) is not parsed with an argument list,
        so its arguments follow as separate group nodes.  A command such as
        ``\\frac`` keeps its arguments in ``nodeargd`` instead.
        """
        if node.nodeargd is not None and node.nodeargd.argnlist:
            arguments = [arg for arg in node.nodeargd.argnlist if arg is not None]
            if arguments:
                return arguments[0].pos, max(a.pos + a.len for a in arguments)

        # Only the directly following group is attached to the command;
        # everything after it is ordinary text.
        if isinstance(next_node, LatexGroupNode):
            return next_node.pos, next_node.pos + next_node.len
        return None

    def _append_decoded_nodes(self, nodes, protected: bool) -> None:
        try:
            text = _DECODER.nodelist_to_text(nodes)
        except Exception as e:
            logger.warning(f"Could not decode LaTeX title fragment: {e}")
            text = "".join(self._source[node.pos : node.pos + node.len] for node in nodes)
        self._append_text(text, protected)

    def _append_decoded_text(self, text: str, protected: bool) -> None:
        try:
            text = _DECODER.latex_to_text(text)
        except Exception:
            # Last resort: keep the source text, which is still better than
            # dropping the title entirely.
            pass
        self._append_text(text, protected)

    def _append_text(self, text: str, protected: bool) -> None:
        self._chars.extend((char, protected) for char in text)


def _normalize_whitespace(chars: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Collapse whitespace runs to a single space and strip the ends."""
    normalized: list[tuple[str, bool]] = []
    for char, protected in chars:
        if char.isspace():
            if normalized and normalized[-1][0] != " ":
                normalized.append((" ", protected))
        else:
            normalized.append((char, protected))
    while normalized and normalized[-1][0] == " ":
        normalized.pop()
    return normalized


def _render_display(
    chars: list[tuple[str, bool]], *, title_case: bool
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Apply title casing outside protected content and collect protected spans."""
    actions = _case_actions(chars) if title_case else ["keep"] * len(chars)

    pieces = []
    spans: list[tuple[int, int]] = []
    span_start: int | None = None
    offset = 0
    for (char, protected), action in zip(chars, actions):
        emitted = _apply_case(char, action)
        if protected:
            if span_start is None:
                span_start = offset
            span_end = offset + len(emitted)
        elif span_start is not None:
            spans.append((span_start, span_end))
            span_start = None
        pieces.append(emitted)
        offset += len(emitted)
    if span_start is not None:
        spans.append((span_start, span_end))

    display = "".join(pieces)
    trimmed_spans = []
    for start, end in spans:
        start, end = _trim_span(display, start, end)
        if start < end:
            trimmed_spans.append((start, end))
    return display, tuple(trimmed_spans)


def _case_actions(chars: list[tuple[str, bool]]) -> list[str]:
    """Per-character case actions for title casing (``upper``/``lower``/``keep``)."""
    actions = ["keep"] * len(chars)
    words = _word_tokens(chars)
    for word_index, components in enumerate(words):
        follows_sentence_mark = _follows_sentence_mark(chars, components[0][0])
        for component_index, (start, end) in enumerate(components):
            cased = [
                i
                for i in range(start, end)
                if chars[i][0].isalpha() and chars[i][0] not in _GREEK_NAMES
            ]
            if not cased:
                continue

            letters = "".join(chars[i][0] for i in cased)
            has_protected_letters = any(chars[i][1] for i in cased)
            is_first = word_index == 0 and component_index == 0
            is_last = word_index == len(words) - 1 and component_index == len(components) - 1
            is_small_word = (
                not is_first
                and not is_last
                and not has_protected_letters
                and not follows_sentence_mark
                and letters.casefold() in _SMALL_WORDS
            )
            if _preserve_word_case(letters):
                # Acronyms (``DNA``) and words with meaningful inner capitals
                # (``McDonald``, ``iPhone``) must not be flattened by casing.
                continue

            first_cased = cased[0]
            for index in cased:
                if chars[index][1]:
                    continue
                if is_small_word:
                    actions[index] = "lower"
                elif index == first_cased:
                    actions[index] = "upper"
                else:
                    actions[index] = "lower"
    return actions


def _follows_sentence_mark(chars: list[tuple[str, bool]], start: int) -> bool:
    """Whether a token starts a new sentence/clause (after ``:``, ``?``, ...)."""
    for index in range(start - 1, -1, -1):
        char = chars[index][0]
        if char.isspace():
            continue
        # A period usually ends an abbreviation (``Proc. of ...``) rather than
        # a sentence, so small words after it stay lower-cased.
        return char in ":;!?—–-("
    return False


def _preserve_word_case(letters: str) -> bool:
    """Whether a word's casing looks intentional and should be kept."""
    if len(letters) < 2:
        return False
    if letters.isupper():
        return True
    # An inner capital (not the first letter) signals CamelCase.
    return any(char.isupper() for char in letters[1:])


def _word_tokens(chars: list[tuple[str, bool]]) -> list[list[tuple[int, int]]]:
    """Words as lists of half-open index ranges of their components.

    Words are separated by whitespace; hyphenated compounds are split into
    components, such that each part is cased individually
    (``state-of-the-art`` becomes ``State-of-the-Art``).
    """
    words: list[list[tuple[int, int]]] = []
    components: list[tuple[int, int]] = []
    start = None
    for index, (char, _) in enumerate(chars):
        if char.isspace():
            if start is not None:
                components.append((start, index))
                start = None
            if components:
                words.append(components)
                components = []
        elif char in "-‐‑‒–—":
            if start is not None:
                components.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        components.append((start, len(chars)))
    if components:
        words.append(components)
    return words


def _apply_case(char: str, action: str) -> str:
    if action == "upper":
        return char.upper()
    if action == "lower":
        return char.lower()
    return char


def _trim_span(display: str, start: int, end: int) -> tuple[int, int]:
    while start < end and display[start].isspace():
        start += 1
    while end > start and display[end - 1].isspace():
        end -= 1
    return start, end


def _normalize_text_for_search(text: str) -> str:
    """Normalize already-decoded text into a search key."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = without_marks.casefold().translate(_SEARCH_FOLDS)

    # Greek letters used in titles (e.g. ``$\alpha$``) are spelled out, so that
    # a search for the letter itself and for its name both find the entry.
    words = "".join(_GREEK_NAMES.get(char, char) for char in folded)

    characters = []
    for char in words:
        if char in "'’":
            # Apostrophes are better removed than turned into a word break.
            continue
        if char in "_^":
            # Math sub-/superscripts belong to the term they annotate:
            # ``H_2O`` and ``H2O`` must yield the same key.
            continue
        characters.append(char if char.isalnum() else " ")
    return " ".join("".join(characters).split())
