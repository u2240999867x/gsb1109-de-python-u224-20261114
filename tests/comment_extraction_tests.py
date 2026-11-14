import io

import mammoth
from .testing import assert_equal


_DOCUMENT_XML = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Hello </w:t></w:r>
      <w:commentRangeStart w:id="2"/>
      <w:r><w:t>wor</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:tab/></w:r>
      <w:r><w:t>😀ld</w:t></w:r>
      <w:commentRangeEnd w:id="2"/>
      <w:r><w:commentReference w:id="1"/></w:r>
      <w:r><w:commentReference w:id="2"/></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Tail</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_COMMENTS_XML = """\
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="1" w:author="Editor A" w:initials="EA">
    <w:p><w:r><w:t>First comment body</w:t></w:r></w:p>
  </w:comment>
  <w:comment w:id="2" w:author="Editor B" w:initials="EB">
    <w:p><w:r><w:t>Second comment body</w:t></w:r></w:p>
  </w:comment>
</w:comments>
"""


def test_public_api_preserves_overlapping_comment_ranges():
    result = mammoth.extract_raw_text_with_comments(_docx())

    text = result.value.text
    assert_equal("Hello wor\t😀ld\nTail\n", text)
    assert_equal(
        [("1", 0, 9), ("2", 6, 13)],
        [(range_.comment_id, range_.start, range_.end) for range_ in result.value.comment_ranges],
    )
    assert_equal("Hello wor", text[0:9])
    assert_equal("wor\t😀ld", text[6:13])


def test_public_api_assigns_offsets_to_comments_not_comment_text():
    result = mammoth.extract_raw_text_with_comments(_docx())

    comments = dict((comment.comment_id, comment) for comment in result.comments)

    assert_equal(("Editor A", 0, 9), (
        comments["1"].author_name,
        comments["1"].start,
        comments["1"].end,
    ))
    assert_equal("First comment body", comments["1"].text)


def test_reading_document_exposes_ranges_without_separate_extraction():
    document = mammoth.docx.read(_docx()).value

    ranges = document.comment_ranges

    assert_equal(
        [("1", 0, 9), ("2", 6, 13)],
        [(range_.comment_id, range_.start, range_.end) for range_ in ranges],
    )


def test_legacy_extract_raw_text_is_unchanged_and_still_sliceable():
    result = mammoth.extract_raw_text(_docx())

    assert_equal("Hello wor\t😀ld\n\nTail\n\n", result.value)
    assert_equal("Hello wor", result.value[0:9])


def _docx():
    return _create_zip({
        "_rels/.rels": """\
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        "word/document.xml": _DOCUMENT_XML,
        "word/comments.xml": _COMMENTS_XML,
        "word/_rels/document.xml.rels": """\
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
""",
        "[Content_Types].xml": """\
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>
""",
    })


def _create_zip(files):
    import zipfile

    fileobj = io.BytesIO()
    with zipfile.ZipFile(fileobj, "w") as zip_file:
        for name, contents in files.items():
            zip_file.writestr(name, contents)
    fileobj.seek(0)
    return fileobj
