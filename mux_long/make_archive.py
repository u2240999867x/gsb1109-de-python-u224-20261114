"""Build the sample archive frames.asc exercised by decoder.py / tests.

Frames (see field_table.csv):
  bus 1, 0x100 MotorStatus: nibble selector + two signed 12-bit branches
      sharing bits 8..19 (mux 1 -> SignedA, mux 2 -> SignedB).
  bus 2, 0x200 PressureBlock: big-endian signed 12-bit Pressure (DBC start 7)
      plus an 8-bit Flags byte.
"""

import can

FRAMES = [
    # selector = 1, SignedA raw 0xFE7 -> -25 -> -12.5 Nm (spans byte 1..2)
    (0.0, 1, 0x100, [0x01, 0xE7, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x00]),
    # selector = 2, SignedB raw 0x138 -> +312 -> +156.0 Nm (same position)
    (0.1, 1, 0x100, [0x02, 0x38, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00]),
    # selector key byte missing (DLC 0): both branches stay unknown
    (0.2, 1, 0x100, []),
    # Pressure raw 0xFE7 -> -25 -> -52.5 kPa (big-endian, spans byte 0..1)
    (0.3, 2, 0x200, [0xFE, 0x70, 0xA5, 0x00, 0x00, 0x00, 0x00, 0x00]),
]


def main() -> None:
    with can.ASCWriter("frames.asc") as writer:
        for timestamp, channel, arb_id, data in FRAMES:
            writer.on_message_received(
                can.Message(
                    timestamp=timestamp,
                    channel=channel,
                    arbitration_id=arb_id,
                    is_extended_id=False,
                    data=data,
                )
            )
    print(f"wrote {len(FRAMES)} frames -> frames.asc")


if __name__ == "__main__":
    main()
