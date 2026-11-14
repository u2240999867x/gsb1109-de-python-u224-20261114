from . import documents, raw_text


def extract_comments_from_element(element, paragraph_separator=None):
    return raw_text.extract_comments_from_element(element, paragraph_separator)


def extract_comment_ranges_from_element(element, paragraph_separator=None):
    return raw_text.extract_comment_ranges_from_element(element, paragraph_separator)


def extract_comments_and_comment_ranges_from_element(element, paragraph_separator=None):
    return (
        raw_text.extract_comments_from_element(element, paragraph_separator),
        raw_text.extract_comment_ranges_from_element(element, paragraph_separator),
    )


def extract_comment_text_from_element(comment):
    return raw_text.extract_raw_text_from_element(
        documents.document(comment.body)
    ).strip()


extract_comment_body_from_element = extract_comment_text_from_element


def extract_comments(fileobj, paragraph_separator=None):
    from . import docx

    return docx.read(fileobj).map(
        lambda document: extract_comments_from_element(document, paragraph_separator)
    )


def extract_comment_ranges(fileobj, paragraph_separator=None):
    from . import docx

    return docx.read(fileobj).map(
        lambda document: extract_comment_ranges_from_element(document, paragraph_separator)
    )
