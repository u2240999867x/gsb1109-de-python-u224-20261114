"""
Tests for xopen.recovery, using small synthetic handover volumes.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from xopen import xopen
from xopen.recovery import DIRECT, PARITY, UNKNOWN, HandoverError, load_layout, recover


def _xor(*chunks: bytes, width: int) -> bytes:
    result = bytearray(width)
    for chunk in chunks:
        for index, byte in enumerate(chunk):
            result[index] ^= byte
    return bytes(result)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build(
    tmp_path: Path,
    *,
    volumes: int = 3,
    stripes: int = 3,
    stripe_length: int = 16,
    record_length: int = 4,
    last_short: int = 0,
    compress: bool = True,
    drop: tuple[int, ...] = (),
    corrupt_byte: tuple[tuple[int, int], ...] = (),
    corrupt_parity: bool = False,
    include_block_hash: bool = True,
    parity_role: bool = False,
) -> tuple[dict[str, Any], bytes]:
    """Write synthetic volumes and a manifest, returning both plus plaintext."""

    total = volumes * stripes * stripe_length - last_short
    plain = bytes((index * 7 + 3) % 251 for index in range(total))

    data = [bytearray() for _ in range(volumes)]
    parity = bytearray()
    for stripe in range(stripes):
        blocks: list[bytes] = []
        for index in range(volumes):
            start = (stripe * volumes + index) * stripe_length
            block = plain[start : start + stripe_length]
            blocks.append(block)
            data[index].extend(block)
        width = max((len(block) for block in blocks), default=0)
        parity.extend(_xor(*blocks, width=width))

    entries = []
    for index, payload in enumerate(data):
        raw = bytes(payload)
        entries.append(
            {
                "name": f"volume{index + 1}",
                "length": len(raw),
                "sha256": _sha(raw),
            }
        )
        if include_block_hash:
            entries[-1]["block_sha256"] = [
                _sha(raw[stripe * stripe_length : (stripe + 1) * stripe_length])
                for stripe in range(stripes)
            ]

    parity_bytes = bytes(parity)
    manifest: dict[str, Any] = {
        "stripe_length": stripe_length,
        "record_length": record_length,
        "volumes": entries,
    }
    if parity_role:
        manifest["volumes"].append(
            {
                "name": "parity",
                "role": "parity",
                "length": len(parity_bytes),
                "sha256": _sha(parity_bytes),
            }
        )
    else:
        manifest["parity"] = {
            "name": "parity",
            "length": len(parity_bytes),
            "sha256": _sha(parity_bytes),
        }

    # Write files, then apply corruption.  Checksums must describe the data as
    # it was at write time, otherwise they cannot be used to detect damage.
    for index, payload in enumerate(data):
        name = f"volume{index + 1}"
        path = tmp_path / (name + ".xz" if compress else name)
        if compress:
            with xopen(path, "wb") as stream:
                stream.write(bytes(payload))
        else:
            path.write_bytes(bytes(payload))
        manifest["volumes"][index]["name"] = path.name
    parity_path = tmp_path / ("parity.xz" if compress else "parity")
    if compress:
        with xopen(parity_path, "wb") as stream:
            stream.write(parity_bytes)
    else:
        parity_path.write_bytes(parity_bytes)
    if parity_role:
        manifest["volumes"][-1]["name"] = parity_path.name
    else:
        manifest["parity"]["name"] = parity_path.name

    for index, offset in corrupt_byte:
        payload = data[index]
        payload[offset] ^= 0xFF
        path = tmp_path / manifest["volumes"][index]["name"]
        if compress:
            with xopen(path, "wb") as stream:
                stream.write(bytes(payload))
        else:
            path.write_bytes(bytes(payload))
    if corrupt_parity:
        corrupted = bytearray(parity_bytes)
        corrupted[0] ^= 0xFF
        if compress:
            with xopen(parity_path, "wb") as stream:
                stream.write(bytes(corrupted))
        else:
            parity_path.write_bytes(bytes(corrupted))

    for index in drop:
        (tmp_path / manifest["volumes"][index]["name"]).unlink()

    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, plain


def test_direct_reassembly(tmp_path):
    _, plain = _build(tmp_path)
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary == {DIRECT: len(plain), PARITY: 0, UNKNOWN: 0}
    assert len(result.segments) == 1
    assert result.segments[0].start == 0
    assert result.segments[0].end == len(plain)
    assert result.segments[0].source == DIRECT


def test_one_missing_volume_is_recovered(tmp_path):
    manifest, plain = _build(tmp_path, drop=(1,))
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary == {DIRECT: len(plain) - 3 * 16, PARITY: 3 * 16, UNKNOWN: 0}
    recovered = [segment for segment in result.segments if segment.source == PARITY]
    assert recovered
    assert {segment.volume for segment in recovered} == {manifest["volumes"][1]["name"]}


def test_double_loss_is_unknown_but_later_stripes_recover(tmp_path):
    # Break the first block of two volumes.  Only the first stripe is affected.
    manifest, _ = _build(tmp_path, corrupt_byte=((0, 0), (1, 0)))
    result = recover(tmp_path / "handover.json")
    assert result.summary[UNKNOWN] == 2 * 16
    assert result.summary[PARITY] == 0
    assert result.summary[DIRECT] == 3 * 3 * 16 - 2 * 16
    volume3 = manifest["volumes"][2]["name"]
    assert all(
        segment.source == DIRECT
        for segment in result.segments
        if segment.volume == volume3
    )
    assert result.stripes[0]["source"] == UNKNOWN
    assert result.stripes[1]["source"] == DIRECT
    assert result.stripes[2]["source"] == DIRECT


def test_parity_mismatch_does_not_blame_verified_data(tmp_path):
    # A single corrupt data block is detected by its block checksum and repaired.
    manifest, plain = _build(tmp_path, corrupt_byte=((0, 5),))
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary == {DIRECT: 3 * 3 * 16 - 16, PARITY: 16, UNKNOWN: 0}


def test_bad_parity_is_blamed_when_data_verifies(tmp_path):
    _, plain = _build(tmp_path, corrupt_parity=True)
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary == {DIRECT: len(plain), PARITY: 0, UNKNOWN: 0}
    parity_report = next(v for v in result.volumes if v["role"] == "parity")
    assert parity_report["verified"] is False


def test_whole_volume_mismatch_with_bad_parity_is_not_parity_blame(tmp_path):
    # Volume 2's whole checksum and parity both fail, but no block checksums
    # exist.  There is no evidence that identifies a single damaged volume.
    manifest, _ = _build(
        tmp_path,
        corrupt_byte=((1, 5),),
        corrupt_parity=True,
        include_block_hash=False,
    )
    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = recover(tmp_path / "handover.json")
    assert result.summary[UNKNOWN] > 0


def test_declared_damaged_volume_is_recovered(tmp_path):
    manifest, plain = _build(tmp_path, drop=(1,))
    manifest["volumes"][1]["damaged"] = True
    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary[UNKNOWN] == 0


def test_corruption_spread_over_stripes_uses_parity(tmp_path):
    # Each stripe loses one block of volume 2, so all three stripes need parity.
    corrupt = tuple((1, stripe * 16 + 4) for stripe in range(3))
    manifest, plain = _build(tmp_path, corrupt_byte=corrupt)
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary == {DIRECT: 3 * 3 * 16 - 3 * 16, PARITY: 3 * 16, UNKNOWN: 0}


def test_missing_volume_without_declared_length_is_inferred(tmp_path):
    manifest, plain = _build(tmp_path, drop=(1,))
    del manifest["volumes"][1]["length"]
    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = recover(tmp_path / "handover.json")
    assert result.data == plain


def test_cannot_infer_short_middle_volume_length(tmp_path):
    manifest, _ = _build(tmp_path, drop=(1,), volumes=3, stripes=2, last_short=5)
    del manifest["volumes"][1]["length"]
    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HandoverError, match="length"):
        recover(tmp_path / "handover.json")


def test_later_stripe_recovers_after_double_loss(tmp_path):
    _, plain = _build(tmp_path, corrupt_byte=((0, 0), (1, 0)))
    result = recover(tmp_path / "handover.json")
    assert result.stripes[0]["source"] == UNKNOWN
    assert result.stripes[1]["source"] == DIRECT
    assert result.stripes[2]["source"] == DIRECT
    assert result.data[48:144] == plain[48:144]


def test_missing_volume_length_comes_from_manifest(tmp_path):
    manifest, plain = _build(tmp_path, drop=(1,))
    assert manifest["volumes"][1]["length"] == 3 * 16
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.summary[UNKNOWN] == 0


def test_short_final_volume(tmp_path):
    _, plain = _build(tmp_path, volumes=3, stripes=2, stripe_length=16, last_short=5)
    assert len(plain) == 3 * 2 * 16 - 5
    result = recover(tmp_path / "handover.json")
    assert result.data == plain
    assert result.stripes[-1]["length"] == 3 * 16 - 5
    assert result.summary[UNKNOWN] == 0


def test_parity_role_entry_is_supported(tmp_path):
    _, plain = _build(tmp_path, parity_role=True)
    result = recover(tmp_path / "handover.json")
    assert result.data == plain


def test_manifest_requires_stripe_length(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"volumes": ["a"]}), encoding="utf-8")
    with pytest.raises(HandoverError, match="stripe_length"):
        load_layout(path)


def test_duplicate_order_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "stripe_length": 4,
                "volumes": [
                    {"name": "a", "order": 1},
                    {"name": "b", "order": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HandoverError, match="order is not unique"):
        load_layout(path)


def test_volume_order_list_is_parsed(tmp_path):
    manifest, _ = _build(tmp_path, volumes=2, stripes=2)
    manifest["volume_order"] = [
        manifest["volumes"][1]["name"],
        manifest["volumes"][0]["name"],
    ]
    (tmp_path / "handover.json").write_text(json.dumps(manifest), encoding="utf-8")
    layout = load_layout(tmp_path / "handover.json")
    assert [volume.name for volume in layout.volumes] == manifest["volume_order"]


def test_cli_writes_report_and_segments(tmp_path):
    _, plain = _build(tmp_path)
    out = tmp_path / "original.bin"
    report = tmp_path / "report.json"
    segments = tmp_path / "segments.tsv"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "xopen.recovery",
            str(tmp_path / "handover.json"),
            "-o",
            str(out),
            "--report",
            str(report),
            "--segments",
            str(segments),
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert out.read_bytes() == plain
    document = json.loads(report.read_text())
    assert document["summary"][DIRECT] == len(plain)
    assert segments.read_text().startswith("start\tend\tlength")


def test_record_length_must_divide_stripe(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"stripe_length": 10, "record_length": 4, "volumes": ["a"]}),
        encoding="utf-8",
    )
    with pytest.raises(HandoverError, match="record length"):
        load_layout(path)
