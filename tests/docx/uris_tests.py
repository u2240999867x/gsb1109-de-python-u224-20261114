from mammoth.docx.uris import resolve_relative_uri, uri_to_zip_entry_name
from ..testing import assert_equal


def test_when_path_does_not_have_leading_slash_then_path_is_resolved_relative_to_base():
    assert_equal(
        "one/two/three/four",
        uri_to_zip_entry_name("one/two", "three/four"),
    )


def test_when_path_has_leading_slash_then_base_is_ignored():
    assert_equal(
        "three/four",
        uri_to_zip_entry_name("one/two", "/three/four"),
    )


def test_relative_uri_is_resolved_against_the_declaring_part_directory():
    assert_equal(
        "word/media/image1.png",
        resolve_relative_uri("word", "media/image1.png"),
    )


def test_relative_uri_parent_segments_escape_the_declaring_part_directory():
    assert_equal(
        "media/image2.png",
        resolve_relative_uri("word/_rels", "../../media/image2.png"),
    )


def test_absolute_package_uri_drops_its_leading_slash():
    assert_equal(
        "word/image3.png",
        resolve_relative_uri("word", "/word/image3.png"),
    )


def test_relative_uri_normalises_current_segments():
    assert_equal(
        "word/media/image4.png",
        resolve_relative_uri("word", "./media/./image4.png"),
    )
