import io
import textwrap
import zipfile

from mammoth import docx, documents, zips
from ..testing import assert_equal, assert_raises, generate_test_path


class ReadTests(object):
    def test_can_read_document_with_single_paragraph_with_single_run_of_text(self):
        with open(generate_test_path("single-paragraph.docx"), "rb") as fileobj:
            result = docx.read(fileobj=fileobj)
            expected_document = documents.document([
                documents.paragraph([
                    documents.run([
                        documents.text("Walking on imported air")
                    ])
                ])
            ])
            assert_equal(expected_document, result.value)


_relationship_namespaces = {
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def test_main_document_is_found_using_package_relationships():
    fileobj = _create_zip({
        "word/document2.xml": textwrap.dedent("""\
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                <w:body>
                    <w:p>
                        <w:r>
                            <w:t>Hello.</w:t>
                        </w:r>
                    </w:p>
                </w:body>
            </w:document>
        """),
        "_rels/.rels": textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/word/document2.xml" Id="rId1"/>
            </Relationships>
        """),
    })
    result = docx.read(fileobj=fileobj)
    expected_document = documents.document([
        documents.paragraph([
            documents.run([
                documents.text("Hello.")
            ])
        ])
    ])
    assert_equal(expected_document, result.value)


def test_error_is_raised_when_main_document_part_does_not_exist():
    fileobj = _create_zip({
        "_rels/.rels": textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/word/document2.xml" Id="rId1"/>
            </Relationships>
        """),
    })
    error = assert_raises(IOError, lambda: docx.read(fileobj=fileobj))
    assert_equal(
        "Could not find main document part. Are you sure this is a valid .docx file?",
        str(error),
    )

class PartPathsTests(object):
    def test_main_document_part_is_found_using_package_relationships(self):
        fileobj = _create_zip({
            "word/document2.xml": " ",
            "_rels/.rels": textwrap.dedent("""\
                <?xml version="1.0" encoding="utf-8"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                    <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/word/document2.xml" Id="rId1"/>
                </Relationships>
            """),
        })
        part_paths = self._find_part_paths(fileobj)
        assert_equal("word/document2.xml", part_paths.main_document)

    def test_when_relationship_for_main_document_cannot_be_found_then_fallback_is_used(self):
        fileobj = _create_zip({
            "word/document.xml": " ",
        })
        part_paths = self._find_part_paths(fileobj)
        assert_equal("word/document.xml", part_paths.main_document)

    def test_comments_part_is_found_using_main_document_relationships(self):
        self._assert_path_is_found_using_main_document_relationships("comments")

    def test_when_relationship_for_comments_cannot_be_found_then_fallback_is_used(self):
        self._assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used("comments")

    def test_endnotes_part_is_found_using_main_document_relationships(self):
        self._assert_path_is_found_using_main_document_relationships("endnotes")

    def test_when_relationship_for_endnotes_cannot_be_found_then_fallback_is_used(self):
        self._assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used("endnotes")

    def test_footnotes_part_is_found_using_main_document_relationships(self):
        self._assert_path_is_found_using_main_document_relationships("footnotes")

    def test_when_relationship_for_footnotes_cannot_be_found_then_fallback_is_used(self):
        self._assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used("footnotes")

    def test_numbering_part_is_found_using_main_document_relationships(self):
        self._assert_path_is_found_using_main_document_relationships("numbering")

    def test_when_relationship_for_numbering_cannot_be_found_then_fallback_is_used(self):
        self._assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used("numbering")

    def test_styles_part_is_found_using_main_document_relationships(self):
        self._assert_path_is_found_using_main_document_relationships("styles")

    def test_when_relationship_for_styles_cannot_be_found_then_fallback_is_used(self):
        self._assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used("styles")

    def _assert_path_is_found_using_main_document_relationships(self, name):
        fileobj = _create_zip({
            "_rels/.rels": textwrap.dedent("""\
                <?xml version="1.0" encoding="utf-8"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                    <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/word/document.xml" Id="rId1"/>
                </Relationships>
            """),
            "word/document.xml": " ",
            "word/_rels/document.xml.rels": textwrap.dedent("""\
                <?xml version="1.0" encoding="utf-8"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                    <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{name}" Target="target-path.xml" Id="rId2"/>
                </Relationships>
            """.format(name=name)),
            "word/target-path.xml": " "
        })
        part_paths = self._find_part_paths(fileobj)
        assert_equal("word/target-path.xml", getattr(part_paths, name))

    def _assert_when_relationship_for_part_cannot_be_found_then_fallback_is_used(self, name):
        fileobj = _create_zip({
            "_rels/.rels": textwrap.dedent("""\
                <?xml version="1.0" encoding="utf-8"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                    <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/word/document.xml" Id="rId1"/>
                </Relationships>
            """),
            "word/document.xml": " ",
        })
        part_paths = self._find_part_paths(fileobj)
        assert_equal("word/{0}.xml".format(name), getattr(part_paths, name))


    def _find_part_paths(self, fileobj):
        return docx._find_part_paths(zips.open_zip(fileobj, "r"))


def test_alternate_content_can_use_choice_when_namespace_is_supported():
    fileobj = _create_docx_with_body("""
        <mc:AlternateContent>
            <mc:Choice Requires="wps">
                <w:p><w:r><w:t>choice</w:t></w:r></w:p>
            </mc:Choice>
            <mc:Fallback>
                <w:p><w:r><w:t>fallback</w:t></w:r></w:p>
            </mc:Fallback>
        </mc:AlternateContent>
    """)

    result = docx.read(
        fileobj,
        supported_namespaces=["http://schemas.microsoft.com/office/word/2010/wordprocessingShape"],
    )

    assert_equal(
        documents.document([
            documents.paragraph([
                documents.run([documents.text("choice")])
            ])
        ]),
        result.value,
    )


def test_alternate_content_uses_fallback_by_default():
    fileobj = _create_docx_with_body("""
        <mc:AlternateContent>
            <mc:Choice Requires="wps">
                <w:p><w:r><w:t>choice</w:t></w:r></w:p>
            </mc:Choice>
            <mc:Fallback>
                <w:p><w:r><w:t>fallback</w:t></w:r></w:p>
            </mc:Fallback>
        </mc:AlternateContent>
    """)

    result = docx.read(fileobj)

    assert_equal(
        documents.document([
            documents.paragraph([
                documents.run([documents.text("fallback")])
            ])
        ]),
        result.value,
    )


def test_alternate_content_without_choice_or_fallback_leaves_a_gap():
    fileobj = _create_docx_with_body("""
        <w:p><w:r><w:t>before</w:t></w:r></w:p>
        <mc:AlternateContent>
            <mc:Choice Requires="wps">
                <w:p><w:r><w:t>first</w:t></w:r></w:p>
            </mc:Choice>
            <mc:Choice Requires="wpg">
                <w:p><w:r><w:t>second</w:t></w:r></w:p>
            </mc:Choice>
        </mc:AlternateContent>
        <w:p><w:r><w:t>after</w:t></w:r></w:p>
    """)

    result = docx.read(fileobj, supported_namespaces=[])

    assert_equal(
        documents.document([
            documents.paragraph([documents.run([documents.text("before")])]),
            documents.paragraph([documents.run([documents.text("after")])]),
        ]),
        result.value,
    )


def _create_zip(files):
    fileobj = io.BytesIO()

    zip_file = zipfile.ZipFile(fileobj, "w")
    try:
        for name, contents in files.items():
            zip_file.writestr(name, contents)
    finally:
        zip_file.close()

    fileobj.seek(0)
    return fileobj


def _create_docx_with_body(body):
    return _create_zip({
        "word/document.xml": textwrap.dedent("""\
            <w:document
                xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
                xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
                xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup">
                <w:body>{0}</w:body>
            </w:document>
        """.format(textwrap.dedent(body))),
        "_rels/.rels": textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml" Id="rId1"/>
            </Relationships>
        """),
    })
