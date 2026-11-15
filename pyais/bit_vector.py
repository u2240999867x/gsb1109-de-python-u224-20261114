"""
This bit vector uses pre-computed lookup tables and stores the entire payload as a single
arbitrary-precision int so that every field extraction is a constant-time shift-and-mask operation.
"""
import dataclasses
import sys
import typing as t
from base64 import b64decode

# The bit vector below is fast on CPython but is slow on PyPy.
# 168 bit integers are stored as bigints and bigint arithmetic is slow in PyPy.
IS_PYPY: t.Final[bool] = sys.implementation.name == 'pypy'

SUPPORTS_FAST_PATH: t.Final[bool] = not IS_PYPY

# ASCII ordinal 6-bit AIS payload value
# Valid input range: ordinals 48 ('0') through 119 ('w').
# Everything outside that range maps to 0; upstream validation is expected.
_PAYLOAD_ARMOR: t.Final[tuple[int, ...]] = tuple(
    (c - 48 - 8 if c - 48 > 40 else c - 48) if 48 <= c <= 119 else 0 for c in range(256)
)

# AIS six-bit armoring is base64 with a different alphabet. Re-mapping the payload
# onto the standard base64 alphabet lets `base64.b64decode` do the 6-to-8 bit
# repacking in C, which is several times faster than a Python-level loop.
# Every one of the 256 input bytes maps onto a valid base64 character, so the
# decoder never sees (and never silently discards) an out-of-alphabet byte.
_B64_ALPHABET: t.Final[bytes] = b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
_ARMOR_TO_B64: t.Final[bytes] = bytes(_B64_ALPHABET[v] for v in _PAYLOAD_ARMOR)

# base64 works on groups of four characters. Pad the payload up to the next
# multiple of four with 'A' (== six zero bits) and shift the surplus back out.
_B64_PAD: t.Final[tuple[bytes, ...]] = (b'', b'AAA', b'AA', b'A')

# 6-bit value decoded AIS character (for text fields)
_SIXBIT_CHAR: t.Final[tuple[str, ...]] = tuple(
    chr(v + 64) if v < 32 else chr(v) for v in range(64)
)

# Bitmasks: _MASK[n] = (1 << n) - 1
# 257 entries covers every realistic AIS field width.
_MASK: t.Final[tuple[int, ...]] = tuple((1 << n) - 1 for n in range(257))


@dataclasses.dataclass(frozen=True)
class DecodedText:
    """Verifiable result of decoding a 6-bit ASCII text field.

    Attributes:
        text: The usable name characters. Explicit '@' terminators and the
            explicit trailing padding are removed, while valid symbols in the
            middle of the name are always preserved.
        raw_bit_length: Number of bits that actually belonged to the field and
            were present in the payload. It is the original bit length of the
            field as received (never the nominal field width).
        remainder_bits: Number of trailing bits that could not form a whole
            6-bit character (0-5). These bits are never turned into a character.
        terminated: Whether an explicit '@' terminator was encountered inside
            the field.
        raw_characters: Every 6-bit character that fully fit in the received
            field, before terminator or padding removal. This keeps the raw
            symbols (including '@' and padding) available for verification.
    """
    text: str
    raw_bit_length: int
    remainder_bits: int
    terminated: bool
    raw_characters: str


def decode_text_field(get: t.Callable[[int, int], int], start: int, width: int,
                      vector_length: int) -> DecodedText:
    """Decode a 6-bit ASCII field with exact bookkeeping.

    Only complete 6-bit characters that fit inside the received bit vector are
    decoded. A partial trailing character is reported through
    ``remainder_bits`` instead of being padded into a fabricated character.

    Args:
        get: Bound ``bit_vector.get`` callable returning the value of a bit range.
        start: Bit offset at which the field starts.
        width: Nominal field width in bits.
        vector_length: Total number of bits available in the payload.

    Returns:
        A :class:`DecodedText` with the usable text and the boundary details.
    """
    if width <= 0 or start >= vector_length:
        return DecodedText("", 0, 0, False, "")

    available = min(width, vector_length - start)
    complete_chars, remainder_bits = divmod(available, 6)

    chars = _SIXBIT_CHAR
    raw = "".join(chars[get(start + index * 6, 6)] for index in range(complete_chars))

    terminated_at = raw.find("@")
    if terminated_at != -1:
        text = raw[:terminated_at]
        terminated = True
    else:
        text = raw
        terminated = False

    # Remove only explicit trailing padding; valid symbols inside the name stay.
    # The rstrip("@")/strip() pairing matches the historical get_str() output.
    text = text.rstrip("@").strip()

    return DecodedText(
        text=text,
        raw_bit_length=available,
        remainder_bits=remainder_bits,
        terminated=terminated,
        raw_characters=raw,
    )


class int_bit_vector:

    __slots__ = ("_value", "_length")

    def __init__(self, data: bytes, pad: int = 0) -> None:
        # Convert bytes into single large integer.
        # The payload is re-armored onto the base64 alphabet so that the actual
        # 6-to-8 bit repacking happens inside `b64decode` rather than in Python.
        length = len(data) * 6
        remainder = len(data) & 3
        if remainder:
            value = int.from_bytes(
                b64decode(data.translate(_ARMOR_TO_B64) + _B64_PAD[remainder]), 'big'
            ) >> (6 * (4 - remainder))
        else:
            value = int.from_bytes(b64decode(data.translate(_ARMOR_TO_B64)), 'big')

        if pad:
            value >>= pad
            length -= pad

        self._value: int = value
        self._length: int = length

    @classmethod
    def from_bytes(cls, data: bytes) -> 'int_bit_vector':
        """Create a bit vector from raw bytes without AIS six-bit dearmoring."""
        bv = cls(b"", 0)
        bv._value = int.from_bytes(data, 'big')
        bv._length = len(data) * 8
        return bv

    def get_num(self, start: int, width: int, signed: bool = False) -> int:
        """Return an unsigned integer of *width* bits at bit position *start*.
        Takes an additional argument *signed* if the number is signed.
        Returns an unsigned integer by default.
        """
        if signed:
            return self.get_signed(start, width)
        return self.get(start, width)

    def get(self, start: int, width: int) -> int:
        """Return an unsigned integer of *width* bits at bit position *start*."""
        # If the requested range extends beyond the end of the vector,
        # only the available bits are returned
        if width <= 0 or start >= self._length:
            return 0
        available = min(width, self._length - start)
        shift = self._length - start - available
        return (self._value >> shift) & _MASK[available]

    def get_signed(self, start: int, width: int) -> int:
        """Return a signed (two's-complement) integer."""
        if width <= 0 or start >= self._length:
            return 0
        available = min(width, self._length - start)
        shift = self._length - start - available
        val = (self._value >> shift) & _MASK[available]
        if val & (1 << (available - 1)):
            val -= 1 << available
        return val

    def get_bool(self, start: int) -> bool:
        """Return a single bit as a boolean."""
        if start >= self._length:
            return False
        shift = self._length - start - 1
        return bool((self._value >> shift) & 1)

    def get_str(self, start: int, width: int) -> str:
        """Return a 6-bit-encoded AIS text string."""
        if width <= 0 or start >= self._length:
            return ""
        chars = _SIXBIT_CHAR
        get = self.get
        parts: list[str] = [chars[get(i, 6)] for i in range(start, start + width, 6)]
        return "".join(parts).rstrip("@").strip()

    def get_text(self, start: int, width: int) -> DecodedText:
        """Return a 6-bit text field together with its raw bit length and the
        trailing bits that could not form a complete character."""
        return decode_text_field(self.get, start, width, self._length)

    def get_bytes(self, start: int, width: int) -> bytes:
        """Return the value of *width* bits at bit position *start* as bytes."""
        if width <= 0 or start >= self._length:
            return b""

        available = min(width, self._length - start)
        shift = self._length - start - available
        # Not using _MASK because the width might be larger than 257 bits
        val = (self._value >> shift) & ((1 << available) - 1)

        # Pad to byte boundary on the right
        num_bytes = (available + 7) // 8
        pad_bits = num_bytes * 8 - available
        val <<= pad_bits

        return val.to_bytes(num_bytes, "big")

    def __len__(self) -> int:
        return self._length

    def __repr__(self) -> str:
        return f"bit_vector(length={self._length})"

    def __eq__(self, value: object) -> bool:
        try:
            if not isinstance(value, int_bit_vector):
                return False
            return self._length == value._length and self._value == value._value
        except ValueError:
            return False


# A signed 64-bit machine word (63 value bits) fits 10 six-bit characters: 10 x 6 = 60
_MAX_WORD_CHARS: t.Final[int] = 10
_MAX_WORD_BITS: t.Final[int] = 6 * _MAX_WORD_CHARS


class word_bit_vector:
    __slots__ = ("_words", "_length")

    def __init__(self, data: bytes, pad: int = 0) -> None:
        words: t.List[int] = []
        cur_word = 0
        n_cur_chars = 0
        for c in data:
            cur_word = (cur_word << 6) | _PAYLOAD_ARMOR[c]
            n_cur_chars += 1
            if n_cur_chars == _MAX_WORD_CHARS:
                words.append(cur_word)
                cur_word = 0
                n_cur_chars = 0
        if n_cur_chars:
            # left-align the last word
            # before: 000000000000000000000000000000010010001100000100100001100001
            # after : 010010001100000100100001100001000000000000000000000000000000
            words.append(cur_word << (6 * (_MAX_WORD_CHARS - n_cur_chars)))
        self._words: t.List[int] = words
        self._length: int = len(data) * 6 - pad

    @property
    def _value(self) -> int:
        return self.get(0, len(self))

    @classmethod
    def from_bytes(cls, data: bytes) -> 'word_bit_vector':
        """Create a bit vector from raw bytes without AIS six-bit dearmoring."""
        words, cur, k = [], 0, 0

        for byte in data:
            avail = _MAX_WORD_BITS - k
            if avail > 8:
                # Whole byte fits into the current word
                cur = (cur << 8) | byte
                k += 8
            else:
                # Byte crosses a word boundary
                k = 8 - avail  # rest
                words.append((cur << avail) | (byte >> k))
                cur = byte & _MASK[k]
        if k:
            # left-align the last word
            words.append(cur << (_MAX_WORD_BITS - k))

        bv = cls(b"", 0)
        bv._words = words
        bv._length = len(data) * 8
        return bv

    def get_num(self, start: int, width: int, signed: bool = False) -> int:
        """Return an unsigned integer of *width* bits at bit position *start*.
        Takes an additional argument *signed* if the number is signed.
        Returns an unsigned integer by default.
        """
        if signed:
            return self.get_signed(start, width)
        return self.get(start, width)

    def get(self, start: int, width: int) -> int:
        """Return an unsigned integer of *width* bits at bit position *start*."""
        # If the requested range extends beyond the end of the vector,
        # only the available bits are returned
        if width <= 0 or start >= self._length:
            return 0

        # Limit the requested width to the maximum number of bits between `start` and `length`.
        # Example: width is 250 bits, but between start and end are only 163 bits available -> max width is 163.
        width = min(width, self._length - start)

        first_word, off = divmod(start, _MAX_WORD_BITS)
        first_w_avail = _MAX_WORD_BITS - off
        if width <= first_w_avail:
            # The field lies within a single word.
            return (self._words[first_word] >> (first_w_avail - width)) & _MASK[width]

        # The field spans multiple words.
        val = self._words[first_word] & _MASK[first_w_avail]

        num_words, last_word_tail = divmod((width - first_w_avail), _MAX_WORD_BITS)
        last_word = first_word + num_words
        for cur_word in range(first_word, first_word + num_words):
            val = (val << _MAX_WORD_BITS) | self._words[cur_word + 1]
        if last_word_tail:
            last_word += 1
        return (val << last_word_tail) | (self._words[last_word] >> (_MAX_WORD_BITS - last_word_tail))

    def get_signed(self, start: int, width: int) -> int:
        """Return a signed (two's-complement) integer."""
        if width <= 0 or start >= self._length:
            return 0

        available = min(width, self._length - start)
        val = self.get(start, available)
        if val & (1 << (available - 1)):
            val -= 1 << available
        return val

    def get_bool(self, start: int) -> bool:
        """Return a single bit as a boolean."""
        if start >= self._length:
            return False
        i, off = divmod(start, _MAX_WORD_BITS)
        return bool((self._words[i] >> (_MAX_WORD_BITS - 1 - off)) & 1)

    def get_str(self, start: int, width: int) -> str:
        """Return a 6-bit-encoded AIS text string."""
        if width <= 0 or start >= self._length:
            return ""
        chars = _SIXBIT_CHAR
        parts: t.List[str] = [chars[self.get(i, 6)] for i in range(start, start + width, 6)]
        return "".join(parts).rstrip("@").strip()

    def get_text(self, start: int, width: int) -> DecodedText:
        """Return a 6-bit text field together with its raw bit length and the
        trailing bits that could not form a complete character."""
        return decode_text_field(self.get, start, width, self._length)

    def get_bytes(self, start: int, width: int) -> bytes:
        """Return the value of *width* bits at bit position *start* as bytes."""
        if width <= 0 or start >= self._length:
            return b""

        available = min(width, self._length - start)
        num_bytes = (available + 7) // 8
        val = self.get(start, available) << (num_bytes * 8 - available)
        return val.to_bytes(num_bytes, "big")

    def __len__(self) -> int:
        return self._length

    def __repr__(self) -> str:
        return f"word_bit_vector(length={self._length})"

    def __eq__(self, value: object) -> bool:
        try:
            if not isinstance(value, word_bit_vector):
                return False
            return self._length == value._length and self._value == value._value
        except ValueError:
            return False


bit_vector = int_bit_vector
if IS_PYPY:
    bit_vector = word_bit_vector  # type: ignore[assignment,misc] # noqa: F811
