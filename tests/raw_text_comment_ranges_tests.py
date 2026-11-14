from mammoth import documents
from mammoth.raw_text import (
    extract_comment_ranges_from_element,
    extract_comments_from_element,
    extract_raw_text_and_comment_ranges_from_element,
    extract_raw_text_from_element,
    extract_raw_text_with_comments_from_element,
)
from .testing import assert_equal


def test_comment_ranges_are_half_open_and_count_unicode_code_points():
    document = _document([
        _paragraph([
            documents.comment_range_start("1"),
            _text("Hello "),
            documents.comment_range_start("2"),
            _text("wor"),
            documents.comment_range_end("1"),
            documents.tab(),
            _text("😀ld"),
            documents.comment_range_end("2"),
        ]),
        _paragraph([_text("Tail")]),
    ], ["1", "2"])

    ranges = extract_comment_ranges_from_element(document)

    assert_equal([("1", 0, 9), ("2", 6, 13)], list(ranges))
    assert_equal("Hello wor", _slice(document, "1"))
    assert_equal("wor\t😀ld", _slice(document, "2"))


def test_overlapping_comment_ranges_are_both_preserved():
    document = _document([
        _paragraph([
            documents.comment_range_start("first"),
            _text("abc"),
            documents.comment_range_start("second"),
            _text("def"),
            documents.comment_range_end("first"),
            _text("ghi"),
            documents.comment_range_end("second"),
        ]),
    ], ["first", "second"])

    ranges = dict(
        (range_.comment_id, range_)
        for range_ in extract_comment_ranges_from_element(document)
    )

    assert_equal(("first", 0, 6), _as_tuple(ranges["first"]))
    assert_equal(("second", 3, 9), _as_tuple(ranges["second"]))


def test_comment_anchored_only_at_cursor_has_empty_range():
    document = _document([
        _paragraph([
            _text("abc"),
            documents.comment_range_start("cursor"),
            documents.comment_range_end("cursor"),
        ]),
    ], ["cursor"])

    comment_range = extract_comment_ranges_from_element(document)[0]

    assert_equal(("cursor", 3, 3), _as_tuple(comment_range))
    assert_equal("", _slice(document, "cursor"))


def test_comment_ranges_index_text_with_one_newline_between_paragraphs():
    document = _document([
        _paragraph([_text("first")]),
        _paragraph([_text("second")]),
    ], [])

    text = extract_raw_text_with_comments_from_element(document)

    assert_equal("first\nsecond\n", text.text)


def test_raw_text_and_comment_ranges_can_be_extracted_together():
    document = _document([
        _paragraph([
            documents.comment_range_start("1"),
            _text("selected"),
            documents.comment_range_end("1"),
        ]),
    ], ["1"])

    text, ranges = extract_raw_text_and_comment_ranges_from_element(document)

    assert_equal("selected\n", text)
    assert_equal([("1", 0, 8)], list(ranges))


def test_comment_body_text_is_not_included_in_selection():
    document = _document([
        _paragraph([
            documents.comment_range_start("1"),
            _text("Body"),
            documents.comment_range_end("1"),
        ]),
    ], ["1"], comment_body="Comment body words")

    text = extract_raw_text_with_comments_from_element(document)

    assert_equal("Body\n", text.text)
    assert_equal("Body", text.selected_text("1"))


def test_comments_carry_start_and_end_offsets():
    document = _document([
        _paragraph([
            documents.comment_range_start("1"),
            _text("Body"),
            documents.comment_range_end("1"),
        ]),
    ], ["1"])

    comment, = extract_comments_from_element(document)

    assert_equal("1", comment.comment_id)
    assert_equal(0, comment.start)
    assert_equal(4, comment.end)


def test_legacy_raw_text_still_uses_two_newlines_per_paragraph():
    document = _document([
        _paragraph([_text("first")]),
        _paragraph([_text("second")]),
    ], [])

    assert_equal("first\n\nsecond\n\n", extract_raw_text_from_element(document))


def _document(paragraphs, comment_ids, comment_body="Comment"):
    comments = [
        documents.comment(
            comment_id=comment_id,
            body=[_paragraph([_text(comment_body)])],
        )
        for comment_id in comment_ids
    ]
    return documents.document(paragraphs, comments=comments)


def _paragraph(children):
    return documents.paragraph(children=children)


def _text(value):
    return documents.run([documents.text(value)])


def _slice(document, comment_id):
    text = extract_raw_text_with_comments_from_element(document)
    comment = text.comment_for(comment_id)
    return text[comment.start:comment.end]


def _as_tuple(comment_range):
    return (comment_range.comment_id, comment_range.start, comment_range.end)
