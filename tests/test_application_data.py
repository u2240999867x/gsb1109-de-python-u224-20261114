import json
import unittest

from pyais import decode, encode_dict, ApplicationData
from pyais.messages import MessageType6Default, MessageType8Default


class TestApplicationData(unittest.TestCase):
    def test_behaves_like_plain_bytes(self):
        data = ApplicationData(b"\xeb/\x11\x8f\x7f\xf1")
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, b"\xeb/\x11\x8f\x7f\xf1")
        self.assertEqual(len(data), 6)
        # without an explicit bit count all bits are considered valid
        self.assertEqual(data.bit_count, 48)
        self.assertEqual(data.padding_bits, 0)

    def test_explicit_bit_count(self):
        data = ApplicationData(b"\x03\xac\xbcF=\xff\xc4", bit_count=53)
        self.assertEqual(data.bit_count, 53)
        self.assertEqual(data.padding_bits, 3)

    def test_invalid_bit_count_raises(self):
        with self.assertRaises(ValueError):
            ApplicationData(b"\x00", bit_count=9)
        with self.assertRaises(ValueError):
            ApplicationData(b"\x00", bit_count=-1)

    def test_record_round_trip(self):
        data = ApplicationData(b"\x03\xac\xbcF=\xff\xc4", bit_count=53)
        record = data.as_record()
        self.assertEqual(record, {"bit_count": 53, "raw": "A6y8Rj3/xA=="})
        restored = ApplicationData.from_record(record)
        self.assertEqual(restored, data)
        self.assertEqual(restored.bit_count, 53)


class TestUnknownApplicationRoundTrip(unittest.TestCase):
    def test_decode_type_6_unknown_application(self):
        msg = decode(b"!AIVDM,1,1,,B,6B?n;be:cbapalgc;i6?Ow4,2*4A")
        self.assertIsInstance(msg, MessageType6Default)
        self.assertEqual(msg.dac, 669)
        self.assertEqual(msg.fid, 11)
        self.assertIsInstance(msg.data, ApplicationData)
        self.assertEqual(msg.data, b"\xeb/\x11\x8f\x7f\xf1")
        self.assertEqual(msg.data.bit_count, 48)

    def test_decode_type_8_unknown_application(self):
        msg = decode(b"!AIVDM,1,1,,A,85Mwp`1Kf3aCnsNvBWLi=wQuNhA5t43N`5nCuI=p<IBfVqnMgPGs,0*47")
        self.assertIsInstance(msg, MessageType8Default)
        self.assertEqual(msg.dac, 366)
        self.assertEqual(msg.fid, 56)
        self.assertIsInstance(msg.data, ApplicationData)
        self.assertEqual(msg.data.bit_count, 256)

    def test_bit_exact_round_trip(self):
        """Re-encoding a decoded message must reproduce the same bit string,
        even if the external fragmentation of the NMEA sentences changes."""
        sentences = [
            # unknown DAC/FID, single sentence
            (b"!AIVDM,1,1,,B,6B?n;be:cbapalgc;i6?Ow4,2*4A",),
            (b"!AIVDM,1,1,,A,85Mwp`1Kf3aCnsNvBWLi=wQuNhA5t43N`5nCuI=p<IBfVqnMgPGs,0*47",),
            # 53 bits of application data (not byte aligned)
            (b"!AIVDO,1,1,,A,85Mwp`1Kf0>dg4Huwt0,5*2D",),
            # multi-fragment message carrying 920 bits of application data
            (
                "!AIVDO,3,1,0,A,6007Ql@007V40011@T=4AD52@lA5@D93A4E1@T=4AD52@lA5@D93A4E1@T=4,0*22",
                "!AIVDO,3,2,0,A,AD52@lA5@D93A4E1@T=4AD52@lA5@D93A4E1@T=4AD52@lA5@D93A4E1@T=4,0*5D",
                "!AIVDO,3,3,0,A,AD52@lA5@D93A4E1@T=4AD52@lA5@D93A4E1@T=4AD52@lA5,0*4E",
            ),
        ]
        for raw in sentences:
            with self.subTest(raw=raw):
                msg = decode(*raw)
                reencoded = encode_dict(msg.asdict())
                decoded_again = decode(*[s.encode() for s in reencoded])
                # the armored payload and the fill bits (i.e. the bit string) must match
                self.assertEqual(msg.encode(), decoded_again.encode())
                self.assertEqual(msg.data, decoded_again.data)
                self.assertEqual(msg.data.bit_count, decoded_again.data.bit_count)

    def test_trailing_zero_bits_are_distinguished_from_padding(self):
        # identical raw bytes: in the first message the seven trailing zero
        # bits are genuine data, in the second one they are padding
        as_data = encode_dict({
            'type': 8, 'mmsi': 366999712, 'dac': 366, 'fid': 56,
            'data': ApplicationData(b"\x80", bit_count=8),
        })
        as_padding = encode_dict({
            'type': 8, 'mmsi': 366999712, 'dac': 366, 'fid': 56,
            'data': ApplicationData(b"\x80", bit_count=1),
        })
        self.assertNotEqual(as_data, as_padding)

        msg_data = decode(as_data[0].encode())
        msg_padding = decode(as_padding[0].encode())
        self.assertEqual(msg_data.data, msg_padding.data)  # same raw bytes
        self.assertEqual(msg_data.data.bit_count, 8)
        self.assertEqual(msg_padding.data.bit_count, 1)

        # both reproduce their original bit string
        self.assertEqual(encode_dict(msg_data.asdict()), as_data)
        self.assertEqual(encode_dict(msg_padding.asdict()), as_padding)

    def test_json_record_round_trip(self):
        msg = decode(b"!AIVDM,1,1,,B,6B?n;be:cbapalgc;i6?Ow4,2*4A")
        exported = json.loads(msg.to_json())
        # application identification, valid bit count and raw content are kept
        self.assertEqual(exported["dac"], 669)
        self.assertEqual(exported["fid"], 11)
        self.assertEqual(exported["data"], {"bit_count": 48, "raw": "6y8Rj3/x"})

        reencoded = encode_dict(exported)
        decoded_again = decode(*[s.encode() for s in reencoded])
        self.assertEqual(msg.encode(), decoded_again.encode())

    def test_legacy_base64_data_still_encodable(self):
        encoded = encode_dict({
            'type': 6, 'mmsi': '150834090', 'dest_mmsi': '313240222',
            'seqno': 3, 'dac': 669, 'fid': 11, 'data': '6y8Rj3/x',
        })
        msg = decode(encoded[0].encode())
        self.assertEqual(msg.data, b"\xeb/\x11\x8f\x7f\xf1")
        self.assertEqual(msg.data.bit_count, 48)

    def test_unknown_applications_participate_in_round_trip(self):
        for dac, fid in ((0, 0), (999, 63), (512, 42)):
            with self.subTest(dac=dac, fid=fid):
                # 29 valid bits; the three padding bits of the last byte are zero
                data = ApplicationData(b"\xde\xad\xbe\xe8", bit_count=29)
                encoded = encode_dict({'type': 8, 'mmsi': 123456789, 'dac': dac, 'fid': fid, 'data': data})
                msg = decode(*[s.encode() for s in encoded])
                self.assertIsInstance(msg, MessageType8Default)
                self.assertEqual((msg.dac, msg.fid), (dac, fid))
                self.assertEqual(msg.data, data)
                self.assertEqual(msg.data.bit_count, 29)
                self.assertEqual(encode_dict(msg.asdict()), encoded)

    def test_padding_bits_are_normalized_to_zero(self):
        # the three bits beyond bit_count are padding and not part of the
        # bit string; they are not preserved (and must not be)
        data = ApplicationData(b"\xde\xad\xbe\xef", bit_count=29)
        encoded = encode_dict({'type': 8, 'mmsi': 123456789, 'dac': 999, 'fid': 63, 'data': data})
        msg = decode(encoded[0].encode())
        self.assertEqual(msg.data, b"\xde\xad\xbe\xe8")
        self.assertEqual(msg.data.bit_count, 29)
        # the valid bits are unaffected, so the bit string still matches
        self.assertEqual(encode_dict(msg.asdict()), encoded)


if __name__ == '__main__':
    unittest.main()
