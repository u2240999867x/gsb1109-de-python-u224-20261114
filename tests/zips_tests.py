import io
import zipfile

from mammoth import zips
from .testing import assert_equal


def test_split_path_splits_zip_paths_on_last_forward_slash():
    assert_equal(("a", "b"), zips.split_path("a/b"))
    assert_equal(("a/b", "c"), zips.split_path("a/b/c"))
    assert_equal(("/a/b", "c"), zips.split_path("/a/b/c"))


def test_when_path_has_no_forward_slashes_then_split_path_returns_empty_dirname():
    assert_equal(("", "name"), zips.split_path("name"))


def test_join_path_joins_arguments_with_forward_slashes():
    assert_equal("a/b", zips.join_path("a", "b"))
    assert_equal("a/b/c", zips.join_path("a/b", "c"))
    assert_equal("/a/b/c", zips.join_path("/a/b", "c"))


def test_empty_parts_are_ignored_when_joining_paths():
    assert_equal("a", zips.join_path("a", ""))
    assert_equal("b", zips.join_path("", "b"))
    assert_equal("a/b", zips.join_path("a", "", "b"))


def test_when_joining_paths_then_absolute_paths_ignore_earlier_paths():
    assert_equal("/b", zips.join_path("a", "/b"))
    assert_equal("/b/c", zips.join_path("a", "/b", "c"))
    assert_equal("/b", zips.join_path("/a", "/b"))
    assert_equal("/a", zips.join_path("/a"))


def test_names_returns_every_entry_in_the_archive():
    fileobj = io.BytesIO()
    archive = zipfile.ZipFile(fileobj, "w")
    archive.writestr("word/document.xml", " ")
    archive.writestr("word/_rels/document.xml.rels", " ")
    archive.close()
    fileobj.seek(0)

    with zips.open_zip(fileobj, "r") as zip_file:
        assert_equal(
            ["word/document.xml", "word/_rels/document.xml.rels"],
            zip_file.names(),
        )
