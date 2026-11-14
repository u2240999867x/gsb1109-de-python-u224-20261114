"""Expand archived CAN frames into a long measurement table using an offline
field table (bus / frame id / mux value / start bit / length / byte order /
offset / factor).

Start bits follow DBC numbering for BOTH byte orders:
  - little endian (Intel): start bit is the LSB of the signal, bit index =
    byte * 8 + bit_in_byte (bit 0 = LSB of byte 0).
  - big endian (Motorola): start bit is the MSB of the signal in DBC
    numbering; it is converted to the MSB-first sequential position via
    seq = 8 * (start // 8) + (7 - start % 8).

Mux rules:
  - The selector signal is decoded first. If the frame is missing the key
    bytes that carry the selector, every muxed branch stays UNKNOWN (rows
    are emitted with empty raw/physical and status 'selector_missing').
  - A signal whose mux value does not match the selector is never decoded,
    so a signed value sitting at the same position in another branch cannot
    leak into the output.

CLI:
  python decoder.py decode --table field_table.csv --archive frames.asc --out long_table.csv
  python decoder.py trace --table field_table.csv --bus 1 --frame 0x100 \
      --signal SignedA --data "01 E7 0F 00 00 00 00 00"
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from typing import Optional

import can


class InsufficientData(Exception):
    """The frame does not carry the bytes that hold this signal."""


@dataclass
class SignalDef:
    bus: int
    frame_id: int
    frame_name: str
    name: str
    mux: Optional[int]  # None = always present; otherwise branch selector value
    is_selector: bool
    start_bit: int  # DBC numbering
    length: int
    byte_order: str  # 'little' | 'big'
    is_signed: bool
    factor: float
    offset: float
    unit: str


def _parse_int(text: str) -> int:
    return int(text.strip(), 0)


def load_table(path: str) -> dict[tuple[int, int], list[SignalDef]]:
    table: dict[tuple[int, int], list[SignalDef]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sig = SignalDef(
                bus=_parse_int(row["bus"]),
                frame_id=_parse_int(row["frame_id"]),
                frame_name=row["frame_name"].strip(),
                name=row["signal"].strip(),
                mux=_parse_int(row["mux"]) if row["mux"].strip() else None,
                is_selector=row["is_selector"].strip().lower() in ("yes", "true", "1"),
                start_bit=_parse_int(row["start_bit"]),
                length=_parse_int(row["length"]),
                byte_order=row["byte_order"].strip().lower(),
                is_signed=row["is_signed"].strip().lower() in ("yes", "true", "1"),
                factor=float(row["factor"]),
                offset=float(row["offset"]),
                unit=row["unit"].strip(),
            )
            table.setdefault((sig.bus, sig.frame_id), []).append(sig)
    return table


def extract_raw(data: bytes | bytearray, start_bit: int, length: int, byte_order: str) -> int:
    """Extract the unsigned raw value; start_bit uses DBC numbering."""
    total = 8 * len(data)
    mask = (1 << length) - 1
    if byte_order == "little":
        if start_bit + length > total:
            raise InsufficientData(
                f"bits {start_bit}..{start_bit + length - 1} beyond {total} available"
            )
        return (int.from_bytes(data, "little") >> start_bit) & mask
    if byte_order == "big":
        msb_seq = 8 * (start_bit // 8) + (7 - start_bit % 8)
        if msb_seq + length > total:
            raise InsufficientData(
                f"bits {msb_seq}..{msb_seq + length - 1} (seq) beyond {total} available"
            )
        shift = total - msb_seq - length
        return (int.from_bytes(data, "big") >> shift) & mask
    raise ValueError(f"unknown byte_order: {byte_order}")


def to_signed(raw: int, length: int) -> int:
    if raw >= 1 << (length - 1):
        return raw - (1 << length)
    return raw


def decode_signal(sig: SignalDef, data: bytes | bytearray) -> tuple[int, float]:
    """Return (signed_raw, physical)."""
    raw = extract_raw(data, sig.start_bit, sig.length, sig.byte_order)
    signed_raw = to_signed(raw, sig.length) if sig.is_signed else raw
    return signed_raw, signed_raw * sig.factor + sig.offset


def encode_raw(sig: SignalDef, physical: float, dlc: int = 8) -> bytes:
    """Inverse of decode_signal: place the physical value into a fresh buffer."""
    signed_raw = round((physical - sig.offset) / sig.factor)
    raw = signed_raw & ((1 << sig.length) - 1)
    if sig.byte_order == "little":
        return (raw << sig.start_bit).to_bytes(dlc, "little")
    msb_seq = 8 * (sig.start_bit // 8) + (7 - sig.start_bit % 8)
    shift = 8 * dlc - msb_seq - sig.length
    return (raw << shift).to_bytes(dlc, "big")


def trace_signal(sig: SignalDef, data: bytes | bytearray) -> dict:
    """Follow one signal from physical value back to its raw bit segment."""
    raw = extract_raw(data, sig.start_bit, sig.length, sig.byte_order)
    signed_raw = to_signed(raw, sig.length) if sig.is_signed else raw
    physical = signed_raw * sig.factor + sig.offset
    if sig.byte_order == "little":
        first, last = sig.start_bit, sig.start_bit + sig.length - 1
        bit_span = f"bit {first}..{last} (DBC little-endian, LSB first)"
    else:
        msb_seq = 8 * (sig.start_bit // 8) + (7 - sig.start_bit % 8)
        first, last = msb_seq, msb_seq + sig.length - 1
        bit_span = (
            f"sequential bit {first}..{last} "
            f"(DBC big-endian start {sig.start_bit} = MSB)"
        )
    byte_first, byte_last = first // 8, last // 8
    segment = bytes(data[byte_first : byte_last + 1])
    rebuilt = encode_raw(sig, physical, dlc=len(data))
    roundtrip_ok = extract_raw(rebuilt, sig.start_bit, sig.length, sig.byte_order) == raw
    return {
        "signal": sig.name,
        "bit_span": bit_span,
        "byte_range": (byte_first, byte_last),
        "segment_hex": segment.hex(" ").upper(),
        "segment_bits": format(raw, f"0{sig.length}b"),
        "raw_unsigned": raw,
        "raw_signed": signed_raw,
        "physical": physical,
        "unit": sig.unit,
        "roundtrip_ok": roundtrip_ok,
    }


def decode_frame(
    table: dict[tuple[int, int], list[SignalDef]],
    bus: int,
    frame_id: int,
    data: bytes | bytearray,
    timestamp: float,
) -> list[dict]:
    defs = table.get((bus, frame_id))
    if not defs:
        return []
    rows = []
    selector = next((s for s in defs if s.is_selector), None)
    selector_value: Optional[int] = None
    selector_known = selector is None
    if selector is not None:
        try:
            selector_value, _ = decode_signal(selector, data)
            selector_known = True
            rows.append(_row(timestamp, bus, frame_id, selector, selector.mux,
                             selector_value, selector_value * selector.factor + selector.offset,
                             "ok"))
        except InsufficientData:
            rows.append(_row(timestamp, bus, frame_id, selector, selector.mux,
                             None, None, "missing_bytes"))
    for sig in defs:
        if sig.is_selector:
            continue
        if sig.mux is not None:
            if not selector_known:
                # key byte for the selector is missing: branch stays unknown
                rows.append(_row(timestamp, bus, frame_id, sig, sig.mux,
                                 None, None, "selector_missing"))
                continue
            if sig.mux != selector_value:
                # other branch at the same position must not leak in
                continue
        try:
            raw, physical = decode_signal(sig, data)
        except InsufficientData:
            rows.append(_row(timestamp, bus, frame_id, sig, sig.mux,
                             None, None, "missing_bytes"))
            continue
        rows.append(_row(timestamp, bus, frame_id, sig, sig.mux, raw, physical, "ok"))
    return rows


def _row(timestamp, bus, frame_id, sig, mux, raw, physical, status) -> dict:
    return {
        "timestamp": f"{timestamp:.6f}",
        "bus": bus,
        "frame_id": f"0x{frame_id:X}",
        "frame_name": sig.frame_name,
        "signal": sig.name,
        "mux": "" if mux is None else mux,
        "raw": "" if raw is None else raw,
        "physical": "" if physical is None else f"{physical:g}",
        "unit": sig.unit,
        "status": status,
    }


LONG_COLUMNS = ["timestamp", "bus", "frame_id", "frame_name", "signal",
                "mux", "raw", "physical", "unit", "status"]


def cmd_decode(args: argparse.Namespace) -> None:
    table = load_table(args.table)
    out_rows: list[dict] = []
    with can.ASCReader(args.archive) as reader:
        for msg in reader:
            bus = int(msg.channel) if msg.channel is not None else 1
            out_rows.extend(
                decode_frame(table, bus, msg.arbitration_id, msg.data, msg.timestamp)
            )
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LONG_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"wrote {len(out_rows)} rows -> {args.out}")


def cmd_trace(args: argparse.Namespace) -> None:
    table = load_table(args.table)
    frame_id = _parse_int(args.frame)
    data = bytes(
        int(b, 0) if b.lower().startswith(("0x", "0b")) else int(b, 16)
        for b in args.data.replace(",", " ").split()
    )
    sig = next(
        (s for s in table.get((args.bus, frame_id), []) if s.name == args.signal),
        None,
    )
    if sig is None:
        sys.exit(f"no signal {args.signal!r} for bus {args.bus} frame {args.frame}")
    info = trace_signal(sig, data)
    print(f"signal   : {info['signal']} ({sig.frame_name}, bus {args.bus}, "
          f"frame 0x{frame_id:X})")
    print(f"layout   : start {sig.start_bit}, len {sig.length}, {sig.byte_order}, "
          f"{'signed' if sig.is_signed else 'unsigned'}")
    print(f"bit span : {info['bit_span']}")
    lo, hi = info["byte_range"]
    print(f"bytes    : {lo}..{hi} = {info['segment_hex']}")
    print(f"raw bits : {info['segment_bits']} = 0x{info['raw_unsigned']:X} "
          f"({info['raw_unsigned']})")
    if sig.is_signed:
        print(f"signed   : {info['raw_signed']} (two's complement, {sig.length} bit)")
    print(f"physical : {info['raw_signed']} * {sig.factor:g} + {sig.offset:g} "
          f"= {info['physical']:g} {info['unit']}")
    print(f"re-encode: physical -> raw 0x{info['raw_unsigned']:X} -> bits "
          f"{'match original segment' if info['roundtrip_ok'] else 'MISMATCH'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dec = sub.add_parser("decode", help="expand an ASC archive into a long table")
    p_dec.add_argument("--table", required=True)
    p_dec.add_argument("--archive", required=True)
    p_dec.add_argument("--out", required=True)
    p_dec.set_defaults(func=cmd_decode)
    p_tr = sub.add_parser("trace", help="trace one signal back to its raw bits")
    p_tr.add_argument("--table", required=True)
    p_tr.add_argument("--bus", type=int, required=True)
    p_tr.add_argument("--frame", required=True)
    p_tr.add_argument("--signal", required=True)
    p_tr.add_argument("--data", required=True, help="frame bytes, e.g. '01 E7 0F'")
    p_tr.set_defaults(func=cmd_trace)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
