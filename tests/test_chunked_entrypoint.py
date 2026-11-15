"""Tests for the chunk-wise entrypoints `parse_stream` and `parse_chunks`."""

from textwrap import dedent

import bibtexparser
from bibtexparser.chunked import SOURCE_END_KEY
from bibtexparser.chunked import SOURCE_START_KEY
from bibtexparser.chunked import source_span
from bibtexparser.model import DuplicateBlockKeyBlock
from bibtexparser.model import ImplicitComment
from bibtexparser.model import ParsingFailedBlock


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def test_parse_chunks_matches_parse_string_for_every_cut_size():
    text = dedent("""\
        @string{me = "My Name"}
        @article{first, author = me, title = {First title}, year = {2023}}
        @article{second, title = {Second title}}
    """)
    reference = bibtexparser.parse_string(text)

    for size in (1, 2, 5, 13, 1000):
        library = bibtexparser.parse_chunks(_chunk(text, size))

        assert [entry.key for entry in library.entries] == [
            entry.key for entry in reference.entries
        ]
        assert library.entries_dict["first"]["author"] == "My Name"
        assert bibtexparser.write_string(library) == bibtexparser.write_string(reference)
        assert all(source_span(block) is not None for block in library.blocks)


def test_parse_stream_accepts_a_generator_and_applies_middleware():
    def generate():
        yield '@article{k, title = {Title}'
        yield ', year = {2024}}'

    library = bibtexparser.parse_stream(generate())

    assert len(library.entries) == 1
    entry = library.entries[0]
    # The default parse stack strips the value enclosings.
    assert entry["title"] == "Title"
    assert entry["year"] == "2024"
    start, end = source_span(entry)
    assert (start, end) == (0, len('@article{k, title = {Title}, year = {2024}}'))


def test_source_positions_survive_the_parse_stack_and_point_to_raw_text():
    text = "@string{s = {v}}\n@article{k, title = {T}}\n% a comment\n"
    library = bibtexparser.parse_stream(iter(_chunk(text, 4)))

    for block in library.blocks:
        start, end = source_span(block)
        needle = block.comment if isinstance(block, ImplicitComment) else block.raw
        assert text[start:end] == needle
        assert block.get_parser_metadata(SOURCE_START_KEY) == start
        assert block.get_parser_metadata(SOURCE_END_KEY) == end


def test_cross_chunk_string_reference_resolves_against_previous_chunk():
    chunks = ['@string{me = "My Name"}\n@article{', "first, author = me}"]
    library = bibtexparser.parse_stream(chunks)

    assert library.failed_blocks == []
    assert library.entries_dict["first"]["author"] == "My Name"


def test_parse_stream_into_existing_library_returns_and_extends_it():
    base = bibtexparser.parse_string("@article{old, title = {Old}}")
    library = bibtexparser.parse_stream(["@article{new, title = {", "New}}"], library=base)

    assert library is base
    assert [entry.key for entry in library.entries] == ["old", "new"]
    assert base.entries_dict["new"]["title"] == "New"


def test_duplicate_key_across_chunks_is_a_failed_block():
    first = "@article{dup, title = {First}}"
    second = "@article{dup, title = {Second}}"
    library = bibtexparser.parse_stream([first, second])

    assert len(library.failed_blocks) == 1
    duplicate = library.failed_blocks[0]
    assert isinstance(duplicate, DuplicateBlockKeyBlock)
    assert library.entries_dict["dup"]["title"] == "First"
    # The wrapper keeps the source position of the block it represents.
    assert source_span(duplicate) == (len(first), len(first) + len(second))


def test_duplicate_against_preexisting_library_keeps_source_span():
    base = bibtexparser.parse_string("@article{dup, title = {Old}}")
    text = "@article{dup, title = {New}}"
    library = bibtexparser.parse_stream([text[:12], text[12:]], library=base)

    duplicate = library.failed_blocks[0]
    assert isinstance(duplicate, DuplicateBlockKeyBlock)
    assert source_span(duplicate) == (0, len(text))


def test_malformed_region_is_kept_verbatim_and_parsing_resumes():
    text = dedent("""\
        @article{broken, title = {Unclosed
        @article{good, title = {Fine}}
    """)
    library = bibtexparser.parse_stream(iter(_chunk(text, 3)))

    failed = [block for block in library.blocks if isinstance(block, ParsingFailedBlock)]
    assert len(failed) == 1
    assert failed[0].raw == "@article{broken, title = {Unclosed"
    assert [entry.key for entry in library.entries] == ["good"]

    good = library.entries[0]
    failed_span = source_span(failed[0])
    good_span = source_span(good)
    assert text[good_span[0] : good_span[1]] == good.raw
    assert good_span[0] >= failed_span[1] - 1


def test_blocks_split_at_arbitrary_characters_reassemble_to_full_parse():
    text = (
        '@article{k, title = "BibTeX entries start with @article{", '
        "note = {LeQua @ {CLEF}}}\n"
        "@book{汉, publisher = {出版社}}\n"
    )
    reference = bibtexparser.parse_string(text)

    for boundary in range(1, len(text)):
        library = bibtexparser.parse_stream([text[:boundary], text[boundary:]])
        assert [type(block).__name__ for block in library.blocks] == [
            type(block).__name__ for block in reference.blocks
        ]
        assert bibtexparser.write_string(library) == bibtexparser.write_string(reference)


def test_empty_stream_returns_empty_library():
    library = bibtexparser.parse_stream(iter(()))

    assert library.blocks == []
    assert library.entries == []
    assert isinstance(library, bibtexparser.Library)


def test_custom_parse_stack_is_used():
    from bibtexparser.middlewares import RemoveEnclosingMiddleware

    text = '@article{k, title = {Title}}'
    library = bibtexparser.parse_stream(
        _chunk(text, 7),
        parse_stack=[RemoveEnclosingMiddleware(allow_inplace_modification=True)],
    )

    assert library.entries[0]["title"] == "Title"
    # Block source metadata is independent of the chosen parse stack.
    assert source_span(library.entries[0]) == (0, len(text))
