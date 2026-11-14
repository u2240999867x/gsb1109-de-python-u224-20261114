import os

import pytest

from decoder import (
    decode_frame,
    encode_raw,
    extract_raw,
    load_table,
    to_signed,
    trace_signal,
)

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = load_table(os.path.join(HERE, "field_table.csv"))

NEG_FRAME = bytes([0x01, 0xE7, 0x0F, 0, 0, 0, 0, 0])  # selector 1, SignedA raw 0xFE7
OTHER_BRANCH = bytes([0x02, 0x38, 0x01, 0, 0, 0, 0, 0])  # selector 2, SignedB
BE_FRAME = bytes([0xFE, 0x70, 0xA5, 0, 0, 0, 0, 0])  # Pressure raw 0xFE7


def _sig(name, bus=1, frame_id=0x100):
    return next(s for s in TABLE[(bus, frame_id)] if s.name == name)


# --- DBC start-bit numbering, both byte orders ---

def test_little_endian_start_bit_is_lsb():
    assert extract_raw(bytes([0b1010_0110]), 0, 8, "little") == 0b1010_0110
    assert extract_raw(bytes([0x70, 0x0F]), 4, 8, "little") == 0xF7


def test_big_endian_dbc_start_bit_is_msb():
    # DBC start 7, len 8 -> byte 0; DBC start 15, len 8 -> byte 1
    assert extract_raw(bytes([0xAB, 0xCD]), 7, 8, "big") == 0xAB
    assert extract_raw(bytes([0xAB, 0xCD]), 15, 8, "big") == 0xCD
    # DBC start 7, len 12 -> byte 0 + high nibble of byte 1
    assert extract_raw(bytes([0xFE, 0x70]), 7, 12, "big") == 0xFE7
    # DBC start 0 -> MSB sits at bit 0 (LSB position) of byte 0
    assert extract_raw(bytes([0b0000_0001, 0x00]), 0, 1, "big") == 1


def test_negative_reading_spanning_byte_boundary_little():
    sig = _sig("SignedA")
    raw = extract_raw(NEG_FRAME, sig.start_bit, sig.length, sig.byte_order)
    assert raw == 0xFE7  # bits 8..19: byte1=0xE7, low nibble of byte2=0xF
    assert to_signed(raw, sig.length) == -25
    assert to_signed(raw, sig.length) * sig.factor + sig.offset == -12.5


def test_negative_reading_spanning_byte_boundary_big():
    sig = _sig("Pressure", bus=2, frame_id=0x200)
    raw = extract_raw(BE_FRAME, sig.start_bit, sig.length, sig.byte_order)
    assert raw == 0xFE7
    assert to_signed(raw, sig.length) == -25
    assert to_signed(raw, sig.length) * sig.factor + sig.offset == -52.5


# --- mux behaviour ---

def test_matching_branch_decoded():
    rows = decode_frame(TABLE, 1, 0x100, NEG_FRAME, 0.0)
    by_signal = {r["signal"]: r for r in rows}
    assert by_signal["SignedA"]["status"] == "ok"
    assert by_signal["SignedA"]["raw"] == -25
    assert by_signal["SignedA"]["physical"] == "-12.5"


def test_other_branch_signed_value_does_not_leak():
    rows = decode_frame(TABLE, 1, 0x100, OTHER_BRANCH, 0.1)
    signals = [r["signal"] for r in rows]
    assert "SignedB" in signals
    assert "SignedA" not in signals  # same bits, other branch: must not appear
    row = next(r for r in rows if r["signal"] == "SignedB")
    assert row["raw"] == 312
    assert row["physical"] == "156"


def test_missing_selector_byte_keeps_branches_unknown():
    rows = decode_frame(TABLE, 1, 0x100, bytes(0), 0.2)
    by_signal = {r["signal"]: r for r in rows}
    assert by_signal["Branch"]["status"] == "missing_bytes"
    for branch in ("SignedA", "SignedB"):
        assert by_signal[branch]["status"] == "selector_missing"
        assert by_signal[branch]["raw"] == ""
        assert by_signal[branch]["physical"] == ""


# --- trace back to the raw bit segment and convert back ---

def test_trace_negative_reading_roundtrip():
    sig = _sig("SignedA")
    info = trace_signal(sig, NEG_FRAME)
    assert info["byte_range"] == (1, 2)
    assert info["segment_hex"] == "E7 0F"
    assert info["raw_unsigned"] == 0xFE7
    assert info["raw_signed"] == -25
    assert info["physical"] == -12.5
    assert info["roundtrip_ok"]
    # converting the physical value back lands on the same bit segment
    rebuilt = encode_raw(sig, info["physical"], dlc=len(NEG_FRAME))
    assert extract_raw(rebuilt, sig.start_bit, sig.length, sig.byte_order) == 0xFE7


def test_trace_big_endian_roundtrip():
    sig = _sig("Pressure", bus=2, frame_id=0x200)
    info = trace_signal(sig, BE_FRAME)
    assert info["byte_range"] == (0, 1)
    assert info["segment_hex"] == "FE 70"
    assert info["physical"] == -52.5
    assert info["roundtrip_ok"]


def test_encode_rejects_nothing_but_roundtrips():
    sig = _sig("SignedB")
    for value in (-12.5, 0.0, 156.0, -2048 * 0.5, 2047 * 0.5):
        rebuilt = encode_raw(sig, value)
        raw = extract_raw(rebuilt, sig.start_bit, sig.length, sig.byte_order)
        assert to_signed(raw, sig.length) * sig.factor + sig.offset == pytest.approx(value)
