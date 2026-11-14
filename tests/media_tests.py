from precisely import assert_that, contains_exactly, has_attrs

from mammoth.media import MediaStore

from .testing import assert_equal


class MediaStoreTests:
    def test_when_same_bytes_are_added_twice_then_media_is_stored_once(self):
        store = MediaStore()

        first = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text="示意图",
            data=b"image-bytes",
        )
        second = store.add_internal(
            source="word/media/image2.png",
            content_type="image/png",
            alt_text="调整后",
            data=b"image-bytes",
        )

        assert_equal(1, len(store.media))
        assert first.media is second.media

    def test_each_reference_keeps_its_own_source_alt_text_and_content_type(self):
        store = MediaStore()

        first = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text="示意图",
            data=b"image-bytes",
        )
        second = store.add_internal(
            source="word/media/image2.png",
            content_type="image/png",
            alt_text="调整后",
            data=b"image-bytes",
        )

        assert_that(first, has_attrs(
            source="word/media/image1.png",
            alt_text="示意图",
            content_type="image/png",
        ))
        assert_that(second, has_attrs(
            source="word/media/image2.png",
            alt_text="调整后",
            content_type="image/png",
        ))

    def test_when_same_bytes_are_declared_with_different_content_types_then_divergence_is_kept(self):
        store = MediaStore()

        png_reference = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text=None,
            data=b"image-bytes",
        )
        jpeg_reference = store.add_internal(
            source="word/media/image2.jpeg",
            content_type="image/jpeg",
            alt_text=None,
            data=b"image-bytes",
        )

        assert_equal(1, len(store.media))
        assert png_reference.media is jpeg_reference.media
        assert_equal("image/png", png_reference.content_type)
        assert_equal("image/jpeg", jpeg_reference.content_type)

    def test_different_bytes_are_stored_as_separate_media(self):
        store = MediaStore()

        first = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text=None,
            data=b"first-image-bytes",
        )
        second = store.add_internal(
            source="word/media/image2.png",
            content_type="image/png",
            alt_text=None,
            data=b"second-image-bytes",
        )

        assert_equal(2, len(store.media))
        assert_equal(b"first-image-bytes", first.media.data)
        assert_equal(b"second-image-bytes", second.media.data)

    def test_external_reference_records_uri_without_storing_media(self):
        store = MediaStore()

        reference = store.add_external(
            source="word/document.xml",
            content_type="image/png",
            alt_text="示意图",
            uri="https://example.com/image.png",
        )

        assert_equal([], store.media)
        assert_that(reference, has_attrs(
            source="word/document.xml",
            content_type="image/png",
            alt_text="示意图",
            media=None,
            external_uri="https://example.com/image.png",
            is_external=True,
        ))

    def test_internal_reference_is_not_external(self):
        store = MediaStore()

        reference = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text=None,
            data=b"image-bytes",
        )

        assert_equal(False, reference.is_external)
        assert_equal(None, reference.external_uri)

    def test_references_are_returned_in_order_added(self):
        store = MediaStore()
        first = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text=None,
            data=b"image-bytes",
        )
        second = store.add_external(
            source="word/document.xml",
            content_type="image/png",
            alt_text=None,
            uri="https://example.com/image.png",
        )

        assert_that(store.references, contains_exactly(first, second))

    def test_finding_by_alt_text_only_matches_occurrences_with_that_alt_text(self):
        store = MediaStore()
        schematic = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text="示意图",
            data=b"image-bytes",
        )
        store.add_internal(
            source="word/media/image2.png",
            content_type="image/png",
            alt_text="调整后",
            data=b"image-bytes",
        )

        assert_that(store.find_by_alt_text("示意图"), contains_exactly(schematic))
        assert_equal([], store.find_by_alt_text("示意"))

    def test_media_can_be_opened_to_read_stored_bytes(self):
        store = MediaStore()
        reference = store.add_internal(
            source="word/media/image1.png",
            content_type="image/png",
            alt_text=None,
            data=b"image-bytes",
        )

        with reference.media.open() as media_file:
            assert_equal(b"image-bytes", media_file.read())
