from . import docx, conversion, options, images, transforms, underline, raw_text, results
from . import comments
from .raw_text import extract_raw_text_from_element
from .docx.style_map import write_style_map, read_style_map

__all__ = [
    "convert_to_html",
    "convert_to_markdown",
    "extract_raw_text",
    "extract_raw_text_with_comments",
    "extract_raw_text_with_comment_ranges",
    "extract_raw_text_with_annotations",
    "extract_annotated_raw_text",
    "extract_raw_text_and_comment_ranges",
    "extract_raw_text_and_comments",
    "extract_comment_ranges",
    "extract_comments",
    "extract_comments_and_comment_ranges",
    "extract_annotations",
    "extract_comment_annotations",
    "images",
    "transforms",
    "underline",
]


_undefined = object()


def convert_to_html(*args, **kwargs):
    return convert(*args, output_format="html", **kwargs)


def convert_to_markdown(*args, **kwargs):
    return convert(*args, output_format="markdown", **kwargs)


def convert(
    fileobj,
    transform_document=None,
    id_prefix=None,
    include_embedded_style_map=_undefined,
    external_file_access=_undefined,
    **kwargs
):
    if include_embedded_style_map is _undefined:
        include_embedded_style_map = True

    if transform_document is None:
        transform_document = lambda x: x

    if include_embedded_style_map:
        kwargs["embedded_style_map"] = read_style_map(fileobj)

    if external_file_access is _undefined:
        external_file_access = False

    return options.read_options(kwargs).bind(lambda convert_options:
        docx.read(fileobj, external_file_access=external_file_access).map(transform_document).bind(lambda document:
            conversion.convert_document_element_to_html(
                document,
                id_prefix=id_prefix,
                **convert_options
            )
        )
    )


def extract_raw_text(fileobj):
    return _with_comment_access(
        docx.read(fileobj).map(extract_raw_text_from_element)
    )


def extract_raw_text_with_comments(fileobj):
    return _with_comment_access(
        docx.read(fileobj).map(raw_text.extract_raw_text_with_comments_from_element)
    )


def extract_raw_text_with_comment_ranges(fileobj):
    return extract_raw_text_with_comments(fileobj)


def extract_raw_text_with_annotations(fileobj):
    return extract_raw_text_with_comments(fileobj)


def extract_annotated_raw_text(fileobj):
    return extract_raw_text_with_comments(fileobj)


def extract_raw_text_and_comment_ranges(fileobj):
    return docx.read(fileobj).map(raw_text.extract_raw_text_and_comment_ranges_from_element)


def extract_raw_text_and_comments(fileobj):
    return docx.read(fileobj).map(raw_text.extract_raw_text_and_comments_from_element)


def extract_comment_ranges(fileobj):
    return comments.extract_comment_ranges(fileobj)


def extract_comments(fileobj):
    return comments.extract_comments(fileobj)


def extract_comments_and_comment_ranges(fileobj):
    return docx.read(fileobj).map(comments.extract_comments_and_comment_ranges_from_element)


def extract_annotations(fileobj):
    return extract_comment_ranges(fileobj)


extract_comment_annotations = extract_annotations


class _ResultWithComments(results.Result):
    @property
    def comments(self):
        return getattr(self.value, "comments", [])

    @property
    def comment_ranges(self):
        return getattr(self.value, "comment_ranges", [])

    @property
    def annotations(self):
        return self.comment_ranges


def _with_comment_access(result):
    return _ResultWithComments(result.value, result.messages)


def embed_style_map(fileobj, style_map):
    write_style_map(fileobj, style_map)

def read_embedded_style_map(fileobj):
    return read_style_map(fileobj)
