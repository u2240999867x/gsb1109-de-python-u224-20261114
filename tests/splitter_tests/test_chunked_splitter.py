"""Tests for chunk-wise splitting (`bibtexparser.chunked.ChunkedSplitter`).

The central guarantee is partition invariance: feeding a text in arbitrarily
small chunks must produce the exact same blocks (and failed regions) as
splitting the concatenated text at once, including when chunk boundaries fall
next to backslashes, closing brackets or multi-byte characters.
"""

import logging
from textwrap import dedent

import pytest

from bibtexparser.chunked import SOURCE_END_KEY
from bibtexparser.chunked import SOURCE_START_KEY
from bibtexparser.chunked import ChunkedSplitter
from bibtexparser.chunked import source_span
from bibtexparser.model import DuplicateBlockKeyBlock
from bibtexparser.model import Entry
from bibtexparser.model import ImplicitComment
from bibtexparser.model import ParsingFailedBlock
from bibtexparser.splitter import Splitter

# A corpus covering: strings, healthy entries, a recoverable malformed region,
# @-signs in values, quotes/braces/parentheses, backslashes, CJK text and
# duplicate keys.
CORPUS = [
    pytest.param(
        dedent("""\
        @string{acme = "Acme Corp"}

        @article{first,
          author = {Doe, John},
          title = {LeQua @ {CLEF} 2022: {A} Shared Task},
          note = "BibTeX entries start with @article{",
          year = {2021}
        }

        @article{broken, title = {Unclosed
        @article{second, author = {Roe}, title = {Second}, year = {2022}}
        % trailing comment
        """),
        id="mixed_with_recovery",
    ),
    pytest.param(
        '@article{k, title = {a\\b \\\\end}, note = "quote \\" inside"}\n'
        "@misc{k2, x={y}}\n",
        id="backslashes_and_escaped_quote",
    ),
    pytest.param(
        "@article{k(1), note = {f(x)}}\n"
        '@article(p, title = "a ) b")\n'
        '@string(s = "x ) y")\n'
        "@comment(unbalanced {braces and \"quotes)\n",
        id="parenthesis_blocks",
    ),
    pytest.param(
        "前置中文注释\n"
        "@article{中文key, title = {凯撒 测试期刊 {嵌套} 内容}, year = {2013}}\n"
        "尾部文本\n"
        "@book{b, v = {\n多\n行\n}}\n",
        id="cjk_text",
    ),
    pytest.param(
        "@article{dup, t={1}}\n@article{dup, t={2}}\n",
        id="duplicate_keys",
    ),
    pytest.param(
        "@preamble{\"a\" # b}\n@string{x = {v}}\n@article{a, title={t},}\n",
        id="preamble_string_entry",
    ),
    pytest.param("% only comments and whitespace\n\n   \n", id="comments_only"),
    pytest.param("", id="empty"),
]


def _feed(splitter: ChunkedSplitter, text: str, cut: int) -> list:
    blocks = []
    for index in range(0, len(text), cut):
        blocks.extend(splitter.feed(text[index : index + cut]))
    blocks.extend(splitter.close())
    return blocks


def _signature(blocks: list) -> list:
    return [
        (
            type(block).__name__,
            getattr(block, "key", None),
            block.comment if isinstance(block, ImplicitComment) else block.raw,
        )
        for block in blocks
    ]


def _full_parse_signature_and_spans(text: str):
    blocks = Splitter(text).split().blocks
    spans = []
    cursor = 0
    for block in blocks:
        needle = block.comment if isinstance(block, ImplicitComment) else block.raw
        start = text.index(needle, cursor)
        end = start + len(needle)
        cursor = end
        spans.append((start, end))
    return _signature(blocks), spans


@pytest.mark.parametrize("text", CORPUS)
def test_every_cut_point_matches_full_file_parse(text: str):
    """Partition invariance: blocks, raws, order and source spans are cut-independent."""
    expected_signature, expected_spans = _full_parse_signature_and_spans(text)

    for cut in range(1, max(2, len(text))):
        splitter = ChunkedSplitter()
        blocks = _feed(splitter, text, cut)

        assert splitter.buffered_text == ""
        assert _signature(blocks) == expected_signature
        assert [source_span(block) for block in blocks] == expected_spans


@pytest.mark.parametrize("text", CORPUS)
def test_source_spans_point_at_the_original_text(text: str):
    if not text:
        return
    splitter = ChunkedSplitter()
    blocks = _feed(splitter, text, cut=3)

    previous_end = 0
    for block in blocks:
        start, end = source_span(block)
        needle = block.comment if isinstance(block, ImplicitComment) else block.raw
        assert text[start:end] == needle
        assert start >= previous_end
        previous_end = end
        assert block.get_parser_metadata(SOURCE_START_KEY) == start
        assert block.get_parser_metadata(SOURCE_END_KEY) == end


def test_feed_withholds_the_incomplete_tail():
    first_entry = "@article{first, title = {One}}"
    text = first_entry + "\n@article{second, title = {Two}}"
    cut = text.index("@article{second") + 4  # ends inside the second entry
    splitter = ChunkedSplitter()

    first_batch = splitter.feed(text[:cut])

    assert [block.key for block in first_batch] == ["first"]
    # The cut point is the end of the first entry; everything after it
    # (including the leading newline) stays buffered verbatim.
    assert splitter.buffered_text == text[len(first_entry) : cut]
    assert all(source_span(block)[1] <= len(first_entry) for block in first_batch)

    second_batch = splitter.feed(text[cut:])
    # The second entry is now complete, so it is emitted without waiting for
    # end-of-input (its closed delimiter proves no later chunk can extend it).
    assert splitter.close() == []
    assert [block.key for block in second_batch] == ["second"]
    assert source_span(second_batch[0]) == (text.index("@article{second"), len(text))


def test_close_reports_genuinely_malformed_tail_with_verbatim_raw():
    text = "@article{broken, title = {never closed"
    splitter = ChunkedSplitter()

    assert splitter.feed(text[:10]) == []
    failed = splitter.feed(text[10:])
    assert failed == []  # without end-of-input, the tail might still be completed
    flushed = splitter.close()

    assert len(flushed) == 1
    assert isinstance(flushed[0], ParsingFailedBlock)
    assert flushed[0].raw == text
    assert source_span(flushed[0]) == (0, len(text))


def test_bad_block_does_not_swallow_the_following_volume():
    text = dedent("""\
        @article{broken, title = {Unclosed
        @article{good, title = {Fine}}
        @book{also_good, year = {2024}}
    """)
    splitter = ChunkedSplitter()
    # Cut right inside the malformed region on every single-character chunk.
    blocks = _feed(splitter, text, cut=1)

    failed = [block for block in blocks if isinstance(block, ParsingFailedBlock)]
    entries = [block for block in blocks if isinstance(block, Entry)]
    assert len(failed) == 1
    assert "broken" in failed[0].raw and "Unclosed" in failed[0].raw
    assert [entry.key for entry in entries] == ["good", "also_good"]


def test_recovery_point_is_evidenced_by_a_new_top_level_block():
    text = dedent("""\
        @article{broken, title = {Unclosed
        @article{good, title = {Fine}}
    """)
    blocks = _feed(ChunkedSplitter(), text, cut=2)

    failed, good = blocks
    assert isinstance(failed, ParsingFailedBlock) and isinstance(good, Entry)
    assert good.key == "good" and good.raw == "@article{good, title = {Fine}}"
    # The evidence (a fully parseable block) starts where recovery began,
    # i.e. after the verbatim malformed region.
    assert source_span(good)[0] >= source_span(failed)[1] - 1
    assert text[source_span(good)[0] : source_span(good)[1]] == good.raw


def test_at_sign_and_line_starts_inside_values_keep_full_file_semantics():
    # A mid-line @ is content; a line-start @ in an unclosed block triggers
    # recovery. Whatever the splitter decides on the full text must hold for
    # every chunking of it.
    texts = [
        "@article{k, title = {LeQua @ {CLEF} 2022}}\n@misc{next, x={y}}",
        '@article{k, email = "john.doe@example.com", t={x}}',
        dedent("""\
            @article{k, title = {unclosed
            @misc{recovered, x={y}}"""),
        "@article{k, title={unclosed @misc{fake}",
    ]
    for text in texts:
        expected, expected_spans = _full_parse_signature_and_spans(text)
        for cut in range(1, len(text)):
            blocks = _feed(ChunkedSplitter(), text, cut)
            assert _signature(blocks) == expected
            assert [source_span(block) for block in blocks] == expected_spans


def test_cuts_adjacent_to_backslash_bracket_and_han_character():
    text = '@article{k, t = {a\\\\b}}\n@article{汉, t = {字}}\n'
    expected, expected_spans = _full_parse_signature_and_spans(text)
    # Split into exactly two chunks at every boundary next to a backslash,
    # closing bracket or Han character (and at every other boundary as well).
    for boundary in range(1, len(text)):
        splitter = ChunkedSplitter()
        blocks = splitter.feed(text[:boundary])
        blocks.extend(splitter.feed(text[boundary:]))
        blocks.extend(splitter.close())
        assert _signature(blocks) == expected
        assert [source_span(block) for block in blocks] == expected_spans


def test_duplicate_keys_spanning_chunks_become_a_duplicate_block():
    first = "@article{dup, t={1}}\n"
    second = "@article{dup, t={2}}"
    splitter = ChunkedSplitter()

    splitter.feed(first)
    # The second entry is complete at the buffer end, so it is emitted at
    # once even though the library wraps it as a duplicate-key failure.
    blocks = splitter.feed(second)
    assert splitter.close() == []

    assert isinstance(blocks[0], DuplicateBlockKeyBlock)
    assert blocks[0].key == "dup"
    # Same outcome, same types, as a single-string parse.
    full = Splitter(first + second).split().blocks
    assert _signature(splitter.library.blocks) == _signature(full)


def test_speculative_parses_do_not_warn_but_final_failure_does(caplog):
    with caplog.at_level(logging.WARNING, logger="bibtexparser.splitter"):
        splitter = ChunkedSplitter()
        splitter.feed("@article{broken, title = {unclosed")
        assert caplog.records == []  # tentative tail, not declared malformed yet

        splitter.close()
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_empty_chunks_and_empty_stream():
    splitter = ChunkedSplitter()
    assert splitter.feed("") == []
    assert splitter.feed("") == []
    assert splitter.close() == []
    assert splitter.buffered_text == ""
    assert source_span(Entry("article", "k", [])) is None
