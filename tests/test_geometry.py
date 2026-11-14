"""Geometry for mapping preview annotations to the stored image."""

import math

import pytest

import exifread
from exifread.geometry import (
    PixelRect,
    get_image_dimensions,
    get_image_orientation,
    image_rect_to_preview,
    map_preview_rect,
    map_preview_rect_from_tags,
    preview_transform,
)
from tests.test_process_file import RESOURCES_ROOT

ORIENTATION_ROOT = RESOURCES_ROOT / "jpg/orientation"


def _expected_region(rect, orientation, image_size, preview_size):
    """Reference mapping implemented with corner points."""

    width, height = image_size
    transform = preview_transform(image_size, preview_size, orientation)
    left, top, right, bottom = rect

    corners = []
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
        dx = (x - transform.offset_x) / transform.scale
        dy = (y - transform.offset_y) / transform.scale
        corners.append(_to_image(dx, dy, orientation, width, height))

    x0 = max(0, min(c[0] for c in corners))
    y0 = max(0, min(c[1] for c in corners))
    x1 = min(width, max(c[0] for c in corners))
    y1 = min(height, max(c[1] for c in corners))
    return (
        math.floor(x0 + 1e-9),
        math.floor(y0 + 1e-9),
        math.ceil(x1 - 1e-9),
        math.ceil(y1 - 1e-9),
    )


def _to_image(x, y, orientation, width, height):
    if orientation == 1:
        return x, y
    if orientation == 2:
        return width - x, y
    if orientation == 3:
        return width - x, height - y
    if orientation == 4:
        return x, height - y
    if orientation == 5:
        return y, x
    if orientation == 6:
        return y, height - x
    if orientation == 7:
        return width - y, height - x
    return width - y, x


@pytest.mark.parametrize("orientation", range(1, 9))
def test_full_content_maps_to_full_image(orientation):
    image_size = (640, 480)
    preview_size = (300, 500)
    transform = preview_transform(image_size, preview_size, orientation)

    content = (
        transform.offset_x,
        transform.offset_y,
        transform.offset_x + transform.content_width,
        transform.offset_y + transform.content_height,
    )
    region = map_preview_rect(content, orientation, image_size, preview_size)
    assert region == PixelRect(0, 0, *image_size)


@pytest.mark.parametrize("orientation", range(1, 9))
def test_inverse_round_trip_covers_selected_region(orientation):
    image_size = (320, 240)
    preview_size = (300, 180)
    selection = (37, 21, 301, 219)

    preview_rect = image_rect_to_preview(
        selection, orientation, image_size, preview_size
    )
    assert preview_rect is not None
    restored = map_preview_rect(preview_rect, orientation, image_size, preview_size)

    assert restored is not None
    assert restored.left <= selection[0]
    assert restored.top <= selection[1]
    assert restored.right >= selection[2]
    assert restored.bottom >= selection[3]


def test_bottom_right_boundary_does_not_grow_a_pixel():
    image_size = (400, 300)
    preview_size = (200, 200)
    for orientation in range(1, 9):
        transform = preview_transform(image_size, preview_size, orientation)
        content_right = transform.offset_x + transform.content_width
        content_bottom = transform.offset_y + transform.content_height

        region = map_preview_rect(
            (content_right - 2, content_bottom - 2, content_right, content_bottom),
            orientation,
            image_size,
            preview_size,
        )
        assert region is None or (
            region.right <= image_size[0] and region.bottom <= image_size[1]
        )


@pytest.mark.parametrize("orientation", range(1, 9))
def test_padding_only_returns_none(orientation):
    image_size = (600, 450)
    preview_size = (300, 400)
    transform = preview_transform(image_size, preview_size, orientation)

    if transform.offset_y > 10:
        rect = (0, 0, 10, transform.offset_y - 1)
    elif transform.offset_x > 10:
        rect = (0, 0, transform.offset_x - 1, 10)
    else:
        pytest.skip("preview has no padding on either axis")

    assert map_preview_rect(rect, orientation, image_size, preview_size) is None


def test_partially_padded_rect_is_clipped_to_content():
    image_size = (600, 450)
    preview_size = (300, 400)
    transform = preview_transform(image_size, preview_size, 1)

    # Starts above the image content (in the top padding).
    rect = (0, 0, 300, transform.offset_y + 50)
    region = map_preview_rect(rect, 1, image_size, preview_size)
    assert region == PixelRect(0, 0, 600, 100)


def test_mirrored_axis_swap_orientation():
    image_size = (400, 300)
    preview_size = (300, 300)

    # Orientation 5 is a transpose: display (0, 0) is stored (0, 0).
    region = map_preview_rect((0, 0, 300, 300), 5, image_size, preview_size)
    assert region == PixelRect(0, 0, 400, 300)


def test_tags_mapping_and_builtin_values_are_accepted():
    class Tag:
        def __init__(self, values):
            self.values = values

    tags = {
        "Image Orientation": Tag([6]),
        "EXIF ExifImageWidth": Tag([640]),
        "EXIF ExifImageLength": Tag([480]),
    }
    assert get_image_orientation(tags) == 6
    assert get_image_dimensions(tags) == (640, 480)

    builtin = {
        "Image Orientation": "Rotated 90 CW",
        "EXIF ExifImageWidth": 640,
        "EXIF ExifImageLength": 480,
    }
    assert get_image_orientation(builtin) == 6

    preview_size = (240, 240)
    expected = map_preview_rect((10, 20, 100, 150), 6, (640, 480), preview_size)
    assert map_preview_rect((10, 20, 100, 150), tags, preview_size) == expected
    assert (
        map_preview_rect_from_tags((10, 20, 100, 150), tags, preview_size) == expected
    )


def test_explicit_scale_and_offset():
    image_size = (500, 250)
    preview_size = (100, 100)
    rect = (10, 20, 40, 50)

    computed = map_preview_rect(rect, 1, image_size, preview_size)
    manual = map_preview_rect(
        rect,
        1,
        image_size,
        preview_size,
        scale=0.2,
        offset=(0, 25),
    )
    assert computed == manual


@pytest.mark.parametrize("orientation", range(1, 9))
def test_fixture_orientation_and_dimensions(orientation):
    path = ORIENTATION_ROOT / ("landscape_%d.jpg" % orientation)
    with open(path, "rb") as file_handle:
        tags = exifread.process_file(file_handle, details=False)

    assert get_image_orientation(tags) == orientation
    if orientation == 1:
        # This fixture has no EXIF dimension tags; the header is used instead.
        with pytest.raises(ValueError):
            get_image_dimensions(tags)
        assert get_image_dimensions(path) == (600, 450)
        return

    width = tags["EXIF ExifImageWidth"].values[0]
    height = tags["EXIF ExifImageLength"].values[0]
    assert get_image_dimensions(tags) == (width, height)


def test_path_and_file_object_inputs():
    path = ORIENTATION_ROOT / "landscape_6.jpg"
    assert get_image_orientation(path) == 6
    assert get_image_dimensions(path) == (450, 600)

    with open(path, "rb") as file_handle:
        position = file_handle.tell()
        assert get_image_orientation(file_handle) == 6
        assert get_image_dimensions(file_handle) == (450, 600)
        assert file_handle.tell() == position
        assert map_preview_rect((0, 0, 300, 400), file_handle, (300, 400)) == (
            PixelRect(0, 0, 450, 600)
        )


@pytest.mark.parametrize("orientation", range(1, 9))
def test_orientation_zero_size_rect_returns_none(orientation):
    assert (
        map_preview_rect((50, 50, 50, 60), orientation, (100, 100), (100, 100)) is None
    )
