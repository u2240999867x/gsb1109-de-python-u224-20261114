"""
Map preview annotations back to the stored image coordinate system.

An image preview is normally shown after applying the EXIF orientation and
scaling the oriented image down to fit a preview box.  The remaining area is
padding ("letterboxing").  An annotation made on such a preview therefore has
to be transformed twice to obtain the corresponding region in the original,
stored image:

1. remove the preview scale and padding;
2. undo the EXIF orientation.

Coordinates are treated as continuous, half-open rectangular regions.  A
rectangle ``(left, top, right, bottom)`` covers ``[left, right)`` on the x axis
and ``[top, bottom)`` on the y axis.  The result is the smallest integer-pixel
region that covers the transformed rectangle.  If the annotation lies entirely
in the padding, ``None`` is returned.
"""

import math
import os
from typing import (
    Any,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

Number = Union[int, float]
Size = Tuple[Number, Number]
RectLike = Sequence[Number]

# EXIF orientation values in the stored-image to display-image direction.
ORIENTATION_MIN = 1
ORIENTATION_MAX = 8

_ORIENTATION_NAMES = {
    1: ("horizontal (normal)", "normal", "top-left"),
    2: ("mirrored horizontal", "mirror horizontal", "top-right"),
    3: ("rotated 180", "rotate 180", "bottom-right"),
    4: ("mirrored vertical", "mirror vertical", "bottom-left"),
    5: (
        "mirrored horizontal then rotated 90 ccw",
        "mirror horizontal then rotate 90 ccw",
        "transpose",
        "left-top",
    ),
    6: ("rotated 90 cw", "rotate 90 cw", "right-top"),
    7: (
        "mirrored horizontal then rotated 90 cw",
        "mirror horizontal then rotate 90 cw",
        "transverse",
        "right-bottom",
    ),
    8: ("rotated 90 ccw", "rotate 90 ccw", "left-bottom"),
}


class PixelRect(NamedTuple):
    """A half-open integer-pixel rectangle.

    The named fields use ``left/top/right/bottom``; the ``x0/y0/x1/y1``
    properties are provided for callers that prefer coordinate naming.
    ``right`` and ``bottom`` are exclusive.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def x0(self) -> int:
        """Left edge (inclusive)."""
        return self.left

    @property
    def y0(self) -> int:
        """Top edge (inclusive)."""
        return self.top

    @property
    def x1(self) -> int:
        """Right edge (exclusive)."""
        return self.right

    @property
    def y1(self) -> int:
        """Bottom edge (exclusive)."""
        return self.bottom

    @property
    def width(self) -> int:
        """Width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Height in pixels."""
        return self.bottom - self.top

    def as_tuple(self) -> Tuple[int, int, int, int]:
        """Return the rectangle as ``(left, top, right, bottom)``."""
        return self.left, self.top, self.right, self.bottom

    def to_tuple(self) -> Tuple[int, int, int, int]:
        """Alias for :meth:`as_tuple`."""
        return self.as_tuple()


class PreviewTransform(NamedTuple):
    """Geometry of an auto-oriented image inside a preview box."""

    scale: float
    offset_x: float
    offset_y: float
    display_width: float
    display_height: float

    @property
    def offset(self) -> Tuple[float, float]:
        """Return the preview offset as ``(x, y)``."""
        return self.offset_x, self.offset_y

    @property
    def content_width(self) -> float:
        """Scaled width of the image content in preview pixels."""
        return self.display_width * self.scale

    @property
    def content_height(self) -> float:
        """Scaled height of the image content in preview pixels."""
        return self.display_height * self.scale


def get_image_orientation(tags: Any) -> int:
    """Return the EXIF orientation (1-8) from tags or a single tag value.

    ``tags`` may be the mapping returned by :func:`exifread.process_file`, an
    :class:`~exifread.core.ifd_tag.IfdTag`, an integer, a one-element sequence,
    or one of the orientation names used by ExifRead.  A missing orientation is
    treated as the EXIF default, 1.  A path or binary file object is also
    accepted and read with :func:`exifread.process_file`.
    """

    if isinstance(tags, Mapping):
        value = tags.get("Image Orientation")
        if value is None:
            value = tags.get("Orientation")
        if value is None:
            return 1
    elif isinstance(tags, (str, os.PathLike)) or hasattr(tags, "read"):
        value = _read_tags(tags).get("Image Orientation", 1)
    else:
        value = tags

    return _coerce_orientation(value)


def get_image_dimensions(tags: Any) -> Size:
    """Return the stored (pre-orientation) main-image size.

    The EXIF image dimensions are preferred over the IFD0 dimensions because
    they describe the main image.  Both ``IfdTag`` values (as returned by
    ``process_file``) and built-in values are accepted.

    When the mapping does not carry dimensions, a path or binary file object is
    accepted and the size is read from the image header.  This keeps images
    whose EXIF only records the orientation usable.
    """

    if isinstance(tags, Mapping):
        candidates = (
            ("EXIF ExifImageWidth", "EXIF ExifImageLength"),
            ("Image ImageWidth", "Image ImageLength"),
            ("EXIF PixelXDimension", "EXIF PixelYDimension"),
            ("Image PixelXDimension", "Image PixelYDimension"),
        )
        for width_tag, height_tag in candidates:
            width = _tag_scalar(tags.get(width_tag))
            height = _tag_scalar(tags.get(height_tag))
            if width is not None and height is not None:
                return _coerce_size((width, height), "image size")

    else:
        return _source_size(tags)

    raise ValueError("EXIF tags do not contain the main image dimensions")


def preview_transform(
    image_size: Size,
    preview_size: Optional[Size],
    orientation: Any = 1,
    scale: Optional[Number] = None,
    offset: Optional[Size] = None,
) -> PreviewTransform:
    """Return the fit/letterbox geometry for an oriented image preview.

    The oriented image is scaled by the largest factor that fits in the preview
    box and is centred unless an explicit ``offset`` is supplied.  ``scale`` may
    be used when the caller already knows the effective preview scale.  When
    both ``scale`` and ``offset`` are supplied, ``preview_size`` is optional.
    """

    orientation = _coerce_orientation(orientation)
    image_width, image_height = _coerce_size(image_size, "image size")
    if preview_size is None:
        if scale is None or offset is None:
            raise TypeError(
                "preview_size is required unless scale and offset are given"
            )
        preview_width = preview_height = 0.0
    else:
        preview_width, preview_height = _coerce_size(preview_size, "preview size")

    if orientation >= 5:
        display_width, display_height = image_height, image_width
    else:
        display_width, display_height = image_width, image_height

    if scale is None:
        scale_value = min(
            preview_width / float(display_width),
            preview_height / float(display_height),
        )
    else:
        scale_value = float(scale)

    if not math.isfinite(scale_value) or scale_value <= 0:
        raise ValueError("preview scale must be a positive finite number")

    content_width = display_width * scale_value
    content_height = display_height * scale_value

    if offset is None:
        offset_x = (preview_width - content_width) / 2.0
        offset_y = (preview_height - content_height) / 2.0
    else:
        offset_x, offset_y = _coerce_pair(offset, "preview offset")

    return PreviewTransform(
        scale_value,
        float(offset_x),
        float(offset_y),
        float(display_width),
        float(display_height),
    )


def map_preview_rect(
    rect: Union[RectLike, "PixelRect", Any],
    orientation: Any = 1,
    image_size: Optional[Size] = None,
    preview_size: Optional[Size] = None,
    scale: Optional[Number] = None,
    offset: Optional[Size] = None,
    integer: bool = True,
    preview: Optional[Size] = None,
) -> Optional[Union[PixelRect, Tuple[float, float, float, float]]]:
    """Map a rectangle drawn on a preview to the stored image.

    Parameters may be passed in any of these convenient forms::

        map_preview_rect(rect, 6, image_size, preview_size)
        map_preview_rect(rect, tags, preview_size)
        map_preview_rect(rect, orientation=6, image_size=(w, h),
                         preview_size=(pw, ph))

    ``orientation`` may also be the dictionary returned by
    :func:`exifread.process_file`, in which case the image dimensions are read
    from it.  ``scale`` and ``offset`` override the automatically computed fit
    geometry.

    By default the result is a :class:`PixelRect` with integer, half-open
    coordinates, or ``None`` when the rectangle lies entirely in the padding.
    With ``integer=False`` a half-open float rectangle is returned instead.
    ``preview`` is an accepted alias for ``preview_size``.
    """

    if preview is not None:
        if preview_size is not None and tuple(preview_size) != tuple(preview):
            raise ValueError("preview and preview_size disagree")
        preview_size = preview

    orientation, image_size, preview_size = _normalise_public_args(
        orientation, image_size, preview_size
    )
    orientation, image_size, preview_size = _resolve_arguments(
        orientation, image_size, preview_size, scale=scale, offset=offset
    )

    transform = preview_transform(
        image_size, preview_size, orientation, scale=scale, offset=offset
    )
    rectangle = _normalise_rect(rect)

    # Clip the annotation to the image content, discarding letterbox padding.
    content_left = transform.offset_x
    content_top = transform.offset_y
    content_right = content_left + transform.content_width
    content_bottom = content_top + transform.content_height

    left = max(rectangle[0], content_left)
    top = max(rectangle[1], content_top)
    right = min(rectangle[2], content_right)
    bottom = min(rectangle[3], content_bottom)
    if right <= left or bottom <= top:
        return None

    display_left = (left - content_left) / transform.scale
    display_top = (top - content_top) / transform.scale
    display_right = (right - content_left) / transform.scale
    display_bottom = (bottom - content_top) / transform.scale

    display_left = _clamp(display_left, 0.0, transform.display_width)
    display_right = _clamp(display_right, 0.0, transform.display_width)
    display_top = _clamp(display_top, 0.0, transform.display_height)
    display_bottom = _clamp(display_bottom, 0.0, transform.display_height)

    first = _display_to_image(
        display_left,
        display_top,
        _coerce_orientation(orientation),
        _coerce_size(image_size, "image size"),
    )
    second = _display_to_image(
        display_right,
        display_bottom,
        _coerce_orientation(orientation),
        _coerce_size(image_size, "image size"),
    )

    image_width, image_height = _coerce_size(image_size, "image size")
    image_left = _clamp(min(first[0], second[0]), 0.0, float(image_width))
    image_right = _clamp(max(first[0], second[0]), 0.0, float(image_width))
    image_top = _clamp(min(first[1], second[1]), 0.0, float(image_height))
    image_bottom = _clamp(max(first[1], second[1]), 0.0, float(image_height))

    if not integer:
        return (image_left, image_top, image_right, image_bottom)

    return _covering_pixels(
        image_left,
        image_top,
        image_right,
        image_bottom,
        image_width,
        image_height,
    )


def image_rect_to_preview(
    rect: RectLike,
    orientation: Any = 1,
    image_size: Optional[Size] = None,
    preview_size: Optional[Size] = None,
    scale: Optional[Number] = None,
    offset: Optional[Size] = None,
    integer: bool = False,
) -> Optional[Union[PixelRect, Tuple[float, float, float, float]]]:
    """Map a rectangle in the stored image to preview coordinates.

    This is the inverse of :func:`map_preview_rect` and is useful for checking
    that a projection round-trips.  The result is a half-open float rectangle
    by default; pass ``integer=True`` for the smallest covering pixel region in
    the preview.
    """

    orientation, image_size, preview_size = _resolve_arguments(
        orientation, image_size, preview_size, scale=scale, offset=offset
    )
    orientation_value = _coerce_orientation(orientation)
    image_width, image_height = _coerce_size(image_size, "image size")
    transform = preview_transform(
        image_size, preview_size, orientation_value, scale=scale, offset=offset
    )

    rectangle = _normalise_rect(rect)
    left = _clamp(rectangle[0], 0.0, float(image_width))
    top = _clamp(rectangle[1], 0.0, float(image_height))
    right = _clamp(rectangle[2], 0.0, float(image_width))
    bottom = _clamp(rectangle[3], 0.0, float(image_height))
    if right <= left or bottom <= top:
        return None

    first = _image_to_display(left, top, orientation_value, image_width, image_height)
    second = _image_to_display(
        right, bottom, orientation_value, image_width, image_height
    )

    display_left = min(first[0], second[0])
    display_right = max(first[0], second[0])
    display_top = min(first[1], second[1])
    display_bottom = max(first[1], second[1])

    preview_left = transform.offset_x + display_left * transform.scale
    preview_top = transform.offset_y + display_top * transform.scale
    preview_right = transform.offset_x + display_right * transform.scale
    preview_bottom = transform.offset_y + display_bottom * transform.scale

    if not integer:
        return (preview_left, preview_top, preview_right, preview_bottom)

    if preview_size is None:
        raise TypeError("preview_size is required for integer output")
    preview_width, preview_height = _coerce_size(preview_size, "preview size")
    preview_left = _clamp(preview_left, 0.0, float(preview_width))
    preview_right = _clamp(preview_right, 0.0, float(preview_width))
    preview_top = _clamp(preview_top, 0.0, float(preview_height))
    preview_bottom = _clamp(preview_bottom, 0.0, float(preview_height))
    return _covering_pixels(
        preview_left,
        preview_top,
        preview_right,
        preview_bottom,
        preview_width,
        preview_height,
    )


def map_preview_rect_from_tags(
    rect: RectLike,
    tags: Mapping[str, Any],
    preview_size: Size,
    scale: Optional[Number] = None,
    offset: Optional[Size] = None,
    integer: bool = True,
) -> Optional[Union[PixelRect, Tuple[float, float, float, float]]]:
    """Map a preview rectangle using orientation and sizes from EXIF tags."""

    return map_preview_rect(
        rect,
        get_image_orientation(tags),
        get_image_dimensions(tags),
        preview_size,
        scale=scale,
        offset=offset,
        integer=integer,
    )


# Descriptive aliases.  Keeping these thin wrappers makes the operation easy
# to discover regardless of whether a caller thinks in terms of previews,
# annotations, or image rectangles.
preview_rect_to_image = map_preview_rect
preview_rect_to_image_rect = map_preview_rect
map_preview_rect_to_image = map_preview_rect
annotation_to_image_rect = map_preview_rect
restore_annotation = map_preview_rect
image_rect_to_preview_rect = image_rect_to_preview
map_image_rect_to_preview = image_rect_to_preview


def _coerce_orientation(value: Any) -> int:
    """Convert common EXIF orientation representations to an integer."""

    if hasattr(value, "values"):
        value = value.values

    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError("orientation must contain exactly one value")
        value = value[0]

    if isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            value = int(text)
        else:
            for orientation, names in _ORIENTATION_NAMES.items():
                if text in names:
                    return orientation
            raise ValueError("unknown EXIF orientation: %r" % value)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("orientation must be an integer from 1 to 8")

    if not ORIENTATION_MIN <= value <= ORIENTATION_MAX:
        raise ValueError("orientation must be an integer from 1 to 8")
    return value


def _normalise_public_args(
    orientation: Any,
    image_size: Optional[Size],
    preview_size: Optional[Size],
) -> Tuple[Any, Optional[Size], Optional[Size]]:
    """Accept either ``orientation, image, preview`` or ``image, preview[, orientation]``."""

    if not _looks_like_size(orientation):
        return orientation, image_size, preview_size

    if preview_size is not None and not _looks_like_size(preview_size):
        try:
            source_orientation = _coerce_orientation(preview_size)
        except (TypeError, ValueError):
            raise TypeError(
                "call as map_preview_rect(rect, orientation, image_size, "
                "preview_size) or map_preview_rect(rect, image_size, "
                "preview_size, orientation)"
            )
        return source_orientation, orientation, image_size

    return 1, orientation, image_size


def _looks_like_size(value: Any) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    if len(value) != 2:
        return False
    return all(
        not isinstance(item, bool) and isinstance(item, (int, float)) for item in value
    )


def _resolve_arguments(
    orientation: Any,
    image_size: Optional[Size],
    preview_size: Optional[Size],
    scale: Optional[Number] = None,
    offset: Optional[Size] = None,
) -> Tuple[Any, Size, Optional[Size]]:
    """Resolve the flexible public call forms.

    When ``orientation`` is a tag mapping or an image path/file object, the
    image dimensions (and, for a path, the orientation) are read from it.  The
    second positional argument is then interpreted as the preview size.
    """

    if isinstance(orientation, Mapping):
        tags = orientation
        if image_size is not None and preview_size is None:
            preview_size = image_size
        if preview_size is None and not (scale is not None and offset is not None):
            raise TypeError("preview_size is required")
        return get_image_orientation(tags), get_image_dimensions(tags), preview_size

    if (
        isinstance(orientation, (str, bytes, os.PathLike))
        or hasattr(orientation, "read")
    ) and not _is_orientation_name(orientation):
        tags = _read_tags(orientation)
        if image_size is not None and preview_size is None:
            preview_size = image_size
        if preview_size is None and not (scale is not None and offset is not None):
            raise TypeError("preview_size is required")
        try:
            image_size = get_image_dimensions(tags)
        except ValueError:
            image_size = _source_size(orientation)
        return get_image_orientation(tags), image_size, preview_size

    if image_size is None:
        raise TypeError("image_size and preview_size are required")
    if preview_size is None and not (scale is not None and offset is not None):
        raise TypeError("image_size and preview_size are required")

    return orientation, image_size, preview_size


def _is_orientation_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return text.isdigit() or any(text in names for names in _ORIENTATION_NAMES.values())


def _read_tags(path_or_file: Any) -> Mapping[str, Any]:
    """Read Exif tags lazily to avoid a circular import at module import time."""

    from exifread import process_file

    if hasattr(path_or_file, "read"):
        position = None
        try:
            position = path_or_file.tell()
        except (AttributeError, OSError):
            pass
        try:
            return process_file(path_or_file)
        finally:
            if position is not None:
                try:
                    path_or_file.seek(position)
                except (AttributeError, OSError):
                    pass

    with open(os.fspath(path_or_file), "rb") as file_handle:
        return process_file(file_handle)


def _read_bytes(path_or_file: Any) -> bytes:
    if hasattr(path_or_file, "read"):
        position = None
        try:
            position = path_or_file.tell()
        except (AttributeError, OSError):
            pass
        try:
            return path_or_file.read()
        finally:
            if position is not None:
                try:
                    path_or_file.seek(position)
                except (AttributeError, OSError):
                    pass

    with open(os.fspath(path_or_file), "rb") as file_handle:
        return file_handle.read()


def _source_size(path_or_file: Any) -> Tuple[float, float]:
    """Read the stored image size from a JPEG, PNG, or TIFF header."""

    data = _read_bytes(path_or_file)

    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_size(data)
        if width and height:
            return float(width), float(height)

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        if data[12:16] == b"IHDR":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            if width and height:
                return float(width), float(height)

    # TIFF stores ImageWidth (0x0100) and ImageLength (0x0101) in IFD0.  Walk
    # IFD0 only; thumbnails and sub-IFDs are deliberately ignored.
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        byte_order: Literal["little", "big"] = "little" if data[:2] == b"II" else "big"

        def read_int(offset: int, length: int) -> int:
            return int.from_bytes(data[offset : offset + length], byte_order)

        ifd = read_int(4, 4)
        count = read_int(ifd, 2)
        width = height = None
        for index in range(count):
            entry = ifd + 2 + index * 12
            tag = read_int(entry, 2)
            if tag not in (0x0100, 0x0101):
                continue
            field_type = read_int(entry + 2, 2)
            value_count = read_int(entry + 4, 4)
            if value_count != 1:
                continue
            length = 4 if field_type == 4 else 2
            value = read_int(entry + 8, length)
            if tag == 0x0100:
                width = value
            else:
                height = value
        if width and height:
            return float(width), float(height)

    raise ValueError("could not determine the image size from the image header")


def _jpeg_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2:
            break
        if marker in (
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        ):
            if index + 7 <= len(data):
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return width, height
            break
        index += length
    return None, None


def _tag_scalar(value: Any) -> Optional[Number]:
    if value is None:
        return None
    if hasattr(value, "values"):
        value = value.values
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        if len(value) != 1:
            raise ValueError("expected a single numeric tag value")
        value = value[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("expected a numeric tag value")
    return value


def _coerce_size(value: Any, name: str) -> Tuple[float, float]:
    width, height = _coerce_pair(value, name)
    if width <= 0 or height <= 0:
        raise ValueError("%s must be positive" % name)
    return width, height


def _coerce_pair(value: Any, name: str) -> Tuple[float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("%s must be a (width, height) pair" % name)
    if len(value) != 2:
        raise ValueError("%s must contain exactly two numbers" % name)
    first, second = value
    if isinstance(first, bool) or isinstance(second, bool):
        raise TypeError("%s must contain numbers" % name)
    if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
        raise TypeError("%s must contain numbers" % name)
    return float(first), float(second)


def _normalise_rect(rect: Any) -> Tuple[float, float, float, float]:
    if isinstance(rect, PixelRect):
        rect = rect.as_tuple()
    elif not isinstance(rect, (str, bytes, Sequence)) and hasattr(rect, "__iter__"):
        rect = tuple(rect)
    if isinstance(rect, (str, bytes)) or not isinstance(rect, Sequence):
        raise TypeError("rect must be a (left, top, right, bottom) sequence")
    if len(rect) != 4:
        raise ValueError("rect must have four values")
    values = []
    for value in rect:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("rect values must be numbers")
        if not math.isfinite(float(value)):
            raise ValueError("rect values must be finite")
        values.append(float(value))

    left, top, right, bottom = values
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return left, top, right, bottom


def _display_to_image(
    x: float, y: float, orientation: int, image_size: Size
) -> Tuple[float, float]:
    """Undo EXIF orientation: display coordinates -> stored coordinates."""

    image_width, image_height = image_size
    if orientation == 1:
        return x, y
    if orientation == 2:
        return image_width - x, y
    if orientation == 3:
        return image_width - x, image_height - y
    if orientation == 4:
        return x, image_height - y
    if orientation == 5:
        return y, x
    if orientation == 6:
        return y, image_height - x
    if orientation == 7:
        return image_width - y, image_height - x
    # orientation == 8
    return image_width - y, x


def _image_to_display(
    x: float, y: float, orientation: int, image_width: float, image_height: float
) -> Tuple[float, float]:
    """Apply EXIF orientation: stored coordinates -> display coordinates."""

    if orientation == 1:
        return x, y
    if orientation == 2:
        return image_width - x, y
    if orientation == 3:
        return image_width - x, image_height - y
    if orientation == 4:
        return x, image_height - y
    if orientation == 5:
        return y, x
    if orientation == 6:
        return image_height - y, x
    if orientation == 7:
        return image_height - y, image_width - x
    # orientation == 8
    return y, image_width - x


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _snap_to_integer(value: float) -> float:
    """Remove tiny floating-point errors at an integer boundary.

    Without this, a rectangle that ends exactly on the right or bottom image
    edge can be pushed one whole pixel outward by a value such as
    ``400.00000000000006``.
    """

    nearest = round(value)
    if abs(value - nearest) <= 1e-9:
        return float(nearest)
    return value


def _covering_pixels(
    left: float,
    top: float,
    right: float,
    bottom: float,
    image_width: Number,
    image_height: Number,
) -> Optional[PixelRect]:
    left = _snap_to_integer(_clamp(left, 0.0, float(image_width)))
    right = _snap_to_integer(_clamp(right, 0.0, float(image_width)))
    top = _snap_to_integer(_clamp(top, 0.0, float(image_height)))
    bottom = _snap_to_integer(_clamp(bottom, 0.0, float(image_height)))

    pixel_left = max(0, int(math.floor(left)))
    pixel_top = max(0, int(math.floor(top)))
    pixel_right = min(int(image_width), int(math.ceil(right)))
    pixel_bottom = min(int(image_height), int(math.ceil(bottom)))

    if pixel_right <= pixel_left or pixel_bottom <= pixel_top:
        return None
    return PixelRect(pixel_left, pixel_top, pixel_right, pixel_bottom)
