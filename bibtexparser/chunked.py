"""Parsing of BibTeX text that arrives in successive chunks.

A regular :class:`~bibtexparser.splitter.Splitter` parses one complete string.
Real-world input may instead arrive in chunks (streaming uploads, paginated
API responses, large files read in fixed-size blocks). A chunk boundary can
fall in the middle of an entry, right after a backslash, inside a quoted
value, or between the code points of a multi-byte character. Naively parsing
each chunk on its own would then turn the truncated tail into a failed block
and silently swallow every block following it.

The :class:`ChunkedSplitter` in this module keeps the unparsed tail of the
input in a buffer and only ever emits blocks up to a *safe cut point*, i.e.
the end of a complete top-level block. Everything after that point is kept
verbatim and re-parsed together with the next chunk. Consequently:

* blocks emitted for chunked input are identical to the blocks obtained from
  parsing the concatenation of all chunks at once (including
  ``DuplicateBlockKeyBlock`` handling across chunk boundaries);
* every emitted block carries its position in the concatenated input, via the
  :data:`SOURCE_START_KEY` and :data:`SOURCE_END_KEY` parser metadata
  (half-open character interval, see :func:`source_span`);
* a genuinely malformed region is emitted as a
  :class:`~bibtexparser.model.ParsingFailedBlock` whose ``raw`` keeps the
  original text, while parsing resumes at the next top-level block. Recovery
  is only accepted at a point where the splitter successfully opens a new
  top-level block, which is itself emitted (with its source position) as
  evidence that the recovery point is valid.
"""

import logging

from .library import Library
from .model import Block
from .model import ImplicitComment
from .model import ParsingFailedBlock
from .splitter import Splitter

_SPLITTER_LOGGER = logging.getLogger("bibtexparser.splitter")


class _NoSpeculativeWarnings(logging.Filter):
    """Suppresses splitter warnings emitted while parsing a tentative tail."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


#: Parser-metadata key under which the index of the first character of a block
#: in the concatenated input stream is stored (inclusive, 0-based).
SOURCE_START_KEY = "source_start"

#: Parser-metadata key under which the index just past the last character of a
#: block in the concatenated input stream is stored (exclusive, 0-based).
SOURCE_END_KEY = "source_end"


def source_span(block: Block) -> tuple[int, int] | None:
    """The ``(start, end)`` position of ``block`` in the parsed input stream.

    :returns: The half-open character interval stored under
        :data:`SOURCE_START_KEY`/ :data:`SOURCE_END_KEY`, or ``None`` if the
        block was not produced by a :class:`ChunkedSplitter`.
    """
    start = block.get_parser_metadata(SOURCE_START_KEY)
    end = block.get_parser_metadata(SOURCE_END_KEY)
    if start is None or end is None:
        return None
    return start, end


class ChunkedSplitter:
    """Split BibTeX text arriving in chunks into blocks, tolerating split blocks.

    Feed each chunk to :meth:`feed`; it returns the blocks that are complete
    given the chunks seen so far. The incomplete tail (if any) is buffered and
    prepended to the next chunk. Call :meth:`close` once the stream is
    exhausted to flush the remainder, including any genuinely malformed
    region as a :class:`~bibtexparser.model.ParsingFailedBlock`.

    A cut is only made at the end of a complete top-level (``@``-)block, so
    arbitrary chunk boundaries - including boundaries next to a backslash,
    a closing bracket or a multi-byte character - yield the same successful
    blocks and the same failed regions as parsing the whole input at once.
    """

    def __init__(self):
        # Text not yet confirmed to end on a complete block boundary.
        self._buffer = ""
        # Number of characters of the input stream already dropped from
        #   `_buffer`; added to buffer-local positions to get stream positions.
        self._offset = 0
        # Persists across drains so that duplicate keys in different chunks
        #   are treated exactly like duplicates in one string.
        self._library = Library()

    def feed(self, chunk: str) -> list[Block]:
        """Process ``chunk`` and return the newly complete blocks.

        The blocks are in input order. Their source position in the
        concatenated stream is available via :func:`source_span`.
        Blocks that cannot yet be proven complete (the buffered tail) are not
        returned; they are returned by a later :meth:`feed` or by
        :meth:`close`.
        """
        self._buffer += chunk
        return self._drain(final=False)

    def close(self) -> list[Block]:
        """Flush the buffered tail after the last chunk was fed.

        Unlike :meth:`feed`, end of input is known, so every remaining block -
        including a :class:`~bibtexparser.model.ParsingFailedBlock` covering a
        genuinely malformed tail - is emitted.
        """
        blocks = self._drain(final=True)
        if self._buffer:
            # `_drain(final=True)` must consume the whole buffer.
            raise RuntimeError("Internal error: chunked splitter did not flush its buffer.")
        return blocks

    def _drain(self, final: bool) -> list[Block]:
        if not self._buffer:
            return []

        # A non-final parse is speculative: its truncated tail looks malformed
        #   only because the input continues later, so silence the splitter's
        #   abort warnings. A malformed region that persists is re-parsed and
        #   warned about definitively when the stream is closed.
        if not final:
            warning_filter = _NoSpeculativeWarnings()
            _SPLITTER_LOGGER.addFilter(warning_filter)
            try:
                parsed = Splitter(self._buffer).split()
            finally:
                _SPLITTER_LOGGER.removeFilter(warning_filter)
        else:
            parsed = Splitter(self._buffer).split()
        spans = _locate_spans(parsed.blocks, self._buffer)

        if final:
            cut_index = len(self._buffer)
        else:
            # A cut is safe at the end of a complete top-level (`@`-)block.
            #   - A successful block at the buffer end is already provably
            #     complete: later chunks can only start a new region.
            #   - A failed block at the buffer end is withheld: it might only
            #     look malformed because its closing delimiter is in the
            #     next chunk. A failed block followed by a complete block in
            #     the same parse is still emitted (recovery was evidenced).
            #   - Implicit comments never anchor a cut: continued text would
            #     belong to the same comment in a single-string parse.
            cut_index = 0
            for block, _start, end in spans:
                if isinstance(block, (ImplicitComment, ParsingFailedBlock)):
                    continue
                cut_index = end

        candidates = []
        for block, start, end in spans:
            if end > cut_index:
                break
            candidates.append((block, start, end))

        stream_offset = self._offset
        self._buffer = self._buffer[cut_index:]
        self._offset += cut_index
        return self._commit(candidates, stream_offset)

    def _commit(
        self, candidates: list[tuple[Block, int, int]], stream_offset: int
    ) -> list[Block]:
        """Hand the cut blocks to the persistent library and tag positions.

        Adding through the library reproduces single-string semantics, e.g.
        wrapping a block whose key was already seen (possibly in an earlier
        chunk) in a ``DuplicateBlockKeyBlock``.
        """
        emitted: list[Block] = []
        for block, start, end in candidates:
            self._library.add(block, fail_on_duplicate_key=False)
            appended = self._library.blocks[-1]
            appended.set_parser_metadata(SOURCE_START_KEY, stream_offset + start)
            appended.set_parser_metadata(SOURCE_END_KEY, stream_offset + end)
            emitted.append(appended)
        return emitted

    @property
    def buffered_text(self) -> str:
        """The input currently held back because no safe cut point exists yet."""
        return self._buffer

    @property
    def library(self) -> Library:
        """All blocks emitted so far, in input order."""
        return self._library


def _locate_spans(blocks: list[Block], text: str) -> list[tuple[Block, int, int]]:
    """Locate each block's raw text within ``text`` as a half-open interval.

    The search cursor only moves forward, so repeated or identical block
    contents are matched in order. For implicit comments, the (whitespace-
    stripped) comment text is located, as blank lines around it are not part
    of the block's ``raw``; cut points are always placed at ``@``-block ends,
    so the uncovered whitespace stays with an adjacent block span.
    """
    spans = []
    cursor = 0
    for block in blocks:
        needle = block.comment if isinstance(block, ImplicitComment) else block.raw
        start = text.index(needle, cursor)
        end = start + len(needle)
        spans.append((block, start, end))
        cursor = end
    return spans
