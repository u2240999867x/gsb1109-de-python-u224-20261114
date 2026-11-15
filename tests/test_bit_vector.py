import random
import unittest

from pyais.bit_vector import int_bit_vector, word_bit_vector, DecodedText
from pyais.util import SIX_BIT_ENCODING


class BitVectorEqualityTestCase(unittest.TestCase):
    def test_simple_six_bit_ascii(self):
        """Ensure that for 6bit ASCII both bit vector implementations behave identically"""
        x = int_bit_vector(b"81mg=5@0GE=85<<?dPG?B<4QQ")
        y = word_bit_vector(b"81mg=5@0GE=85<<?dPG?B<4QQ")

        self.assertEqual(len(x), len(y))
        self.assertEqual(x._value, y._value)
        self.assertEqual(x.get(0, 250), y.get(0, 250))
        self.assertEqual(x.get_str(0, 250), y.get_str(0, 250))
        self.assertEqual(x.get_bytes(0, 250), y.get_bytes(0, 250))
        self.assertEqual(x.get_bool(27), y.get_bool(27))

    def test_simple_bytes(self):
        """Ensure that for random bytes both bit vector implementations behave identically"""
        x = int_bit_vector.from_bytes(b'!\x90PB\x05\x96\x8b\xe3\xd7\xab@1\xe0\x000c\xc0@\x0f\xa0\x00\x00\x00\x0cx\x00\x00\x00')
        y = word_bit_vector.from_bytes(b'!\x90PB\x05\x96\x8b\xe3\xd7\xab@1\xe0\x000c\xc0@\x0f\xa0\x00\x00\x00\x0cx\x00\x00\x00')

        self.assertEqual(len(x), len(y))
        self.assertEqual(x._value, y._value)
        self.assertEqual(x.get(0, 250), y.get(0, 250))
        self.assertEqual(x.get_str(0, 250), y.get_str(0, 250))
        self.assertEqual(x.get_bytes(0, 250), y.get_bytes(0, 250))
        self.assertEqual(x.get_bool(27), y.get_bool(27))

    def test_fuzzy_bytes(self):
        """Ensure both bit vector implementations behave identically for random bytes of arbitrary length"""
        for k in range(25):
            data = random.randbytes(k)
            x = int_bit_vector.from_bytes(data)
            y = word_bit_vector.from_bytes(data)
            length = len(x)
            self.assertEqual(len(x), len(y))
            for i in range(length):
                for width in range(-2, length - i + 6):  # negative, zero, normal, too wide
                    assert x.get(i, width) == y.get(i, width)
                    assert x.get_signed(i, width) == y.get_signed(i, width)
                    assert x.get_str(i, width) == y.get_str(i, width)
                    assert x.get_bytes(i, width) == y.get_bytes(i, width)
                    assert x.get_bool(i) == y.get_bool(i)

    def test_fuzzy_bytes_with_padding(self):
        """Ensure both bit vector implementations behave identically for random bytes of arbitrary length and padding"""
        for k in range(25):
            data = random.randbytes(k)
            pad = random.randint(0, min(k, 7))
            x = int_bit_vector(data, pad=pad)
            y = word_bit_vector(data, pad=pad)
            length = len(x)
            self.assertEqual(len(x), len(y))
            for i in range(length):
                for width in range(-2, length - i + 6):  # negative, zero, normal, too wide
                    assert x.get(i, width) == y.get(i, width)
                    assert x.get_signed(i, width) == y.get_signed(i, width)
                    assert x.get_str(i, width) == y.get_str(i, width)
                    assert x.get_bytes(i, width) == y.get_bytes(i, width)
                    assert x.get_bool(i) == y.get_bool(i)

    def test_eq(self):
        self.assertEqual(int_bit_vector(b'0', 2), int_bit_vector(b'1', 2))
        self.assertEqual(word_bit_vector(b'0', 2), word_bit_vector(b'1', 2))


class DecodedTextFieldTestCase(unittest.TestCase):
    @staticmethod
    def _payload(text):
        from pyais.util import PAYLOAD_ARMOR
        return ''.join(PAYLOAD_ARMOR[SIX_BIT_ENCODING[c]] for c in text).encode()

    def test_complete_characters_with_bookkeeping(self):
        payload = self._payload('ABCDEFGHIJKLMNOPQRSTUVWXYZ@')  # 27 chars -> 162 bits
        for cls in (int_bit_vector, word_bit_vector):
            bv = cls(payload, 1)  # 161 bits -> 26 complete chars + a 5-bit remainder
            self.assertEqual(len(bv), 161)
            result = bv.get_text(0, 167)

            self.assertIsInstance(result, DecodedText)
            self.assertEqual(result.text, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            self.assertEqual(len(result.raw_characters), 26)
            self.assertEqual(result.raw_bit_length, 161)
            self.assertEqual(result.remainder_bits, 5)
            self.assertFalse(result.terminated)

    def test_partial_character_is_not_fabricated(self):
        # 26 complete characters followed by exactly 5 non-zero trailing bits.
        payload = self._payload('ABCDEFGHIJKLMNOPQRSTUVWXYZ@')
        for cls in (int_bit_vector, word_bit_vector):
            bv = cls(payload, 1)
            result = bv.get_text(0, 167)
            self.assertEqual(result.remainder_bits, 5)
            self.assertNotIn('@', result.text)
            self.assertEqual(result.text, result.raw_characters)

    def test_terminator_stops_text_but_keeps_raw_symbols(self):
        payload = self._payload('SHORT@@@@@@@@@@')
        result = int_bit_vector(payload, 0).get_text(0, 90)
        self.assertEqual(result.text, 'SHORT')
        self.assertTrue(result.terminated)
        self.assertEqual(result.raw_characters, 'SHORT@@@@@@@@@@')
        self.assertEqual(result.raw_bit_length, 90)
        self.assertEqual(result.remainder_bits, 0)

    def test_trailing_padding_removed_middle_symbols_kept(self):
        payload = self._payload('A B  @@@@@@@@@@')
        result = int_bit_vector(payload, 0).get_text(0, 90)
        self.assertEqual(result.text, 'A B')
        self.assertTrue(result.raw_characters.startswith('A B  @'))

    def test_field_outside_vector_is_empty(self):
        bv = int_bit_vector(self._payload('ABC'), 0)
        result = bv.get_text(100, 120)
        self.assertEqual(result, DecodedText('', 0, 0, False, ''))


if __name__ == '__main__':
    unittest.main()
