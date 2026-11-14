"""
Reassemble striped compressed observation volumes using an XOR parity volume.

A handover manifest describes how fixed-size records were interleaved into
several compressed data volumes and, optionally, one volume that holds the
byte-wise XOR of the blocks in every stripe.  :func:`recover` decompresses the
volumes with :func:`xopen.xopen`, rebuilds the original stream and labels every
output segment with one of three provenance values:

``direct``
    The bytes were read from a volume block that is not contradicted by any
    available check.
``parity``
    The bytes were rebuilt from the XOR parity volume.
``unknown``
    The bytes cannot be proven: two or more blocks of a stripe are damaged or
    missing, no usable parity block exists, or an XOR mismatch cannot be
    attributed to a single damaged volume.

Fault attribution is driven by checksums.  If all volumes look complete but an
XOR check fails, parity is **not** assumed to be the damaged one.  Data that is
individually verified stays ``direct``.  A whole-volume checksum failure makes
that volume a suspect, and the XOR volume is used to localize the failing block
inside it; the replacement is accepted only if it matches the declared block or
whole-volume checksum.  A bad parity volume therefore cannot silently corrupt
verified data, and a bad data volume cannot be hidden by blaming parity.

Stripes are resolved independently.  If one stripe has two damaged blocks and
must be reported ``unknown``, later stripes with complete material are still
recovered normally.

Manifest
--------
The manifest is JSON.  ``stripe_length`` is the only required geometry, and
each data volume needs a ``name``.  Relative names are resolved against the
manifest's directory.

============================  ===============================================
``stripe_length``             Bytes in one stripe (required)
``record_length``             Output alignment, defaults to 1
``volumes``                   List or object of volume entries
``volume_order``              Optional list/object overriding list order
``parity``                    Parity-volume entry, or use ``"role": "parity"``
``last_volume_length``        True decoded length of the final volume
``total_length``              Expected length of the reconstructed stream
============================  ===============================================

A volume entry accepts ``name``, ``length`` (decoded bytes), ``sha256``
(checksum of the whole decoded volume), ``block_sha256`` (a list or indexed
object of checksums in stripe order), and ``damaged``.

Command line::

    python -m xopen.recovery handover.json -o original.bin \\
        --report report.json --segments segments.tsv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import xopen

__all__ = [
    "DIRECT",
    "PARITY",
    "UNKNOWN",
    "HandoverError",
    "Layout",
    "Recovery",
    "Segment",
    "Volume",
    "load_layout",
    "recover",
]

DIRECT = "direct"
PARITY = "parity"
UNKNOWN = "unknown"

LABELS = {DIRECT: "直接读取", PARITY: "校验恢复", UNKNOWN: "未知"}

# Per-block evidence.
GOOD = "good"
BAD = "bad"
SUSPECT = "suspect"
UNCHECKED = "unchecked"


class HandoverError(ValueError):
    """Raised when a handover manifest or its volumes are inconsistent."""


@dataclass(frozen=True)
class Volume:
    """A volume named by the manifest."""

    name: str
    path: Path
    order: int
    length: int | None = None
    sha256: str | None = None
    block_sha256: tuple[str | None, ...] = ()
    damaged: bool = False
    role: str = "data"


@dataclass(frozen=True)
class Layout:
    """Geometry of the striped stream."""

    stripe_length: int
    record_length: int
    volumes: tuple[Volume, ...]
    parity: Volume | None = None
    last_volume_length: int | None = None
    total_length: int | None = None


@dataclass
class Segment:
    """A contiguous range of output bytes with one provenance."""

    start: int
    end: int
    source: str
    stripe: int
    volume: str = ""
    reason: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "source": self.source,
            "source_label": LABELS[self.source],
            "stripe": self.stripe,
            "volume": self.volume,
            "reason": self.reason,
        }


@dataclass
class Recovery:
    """Reconstructed bytes plus the evidence for every segment."""

    data: bytes
    segments: list[Segment] = field(default_factory=list)
    stripes: list[dict[str, Any]] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def summary(self) -> dict[str, int]:
        totals = {DIRECT: 0, PARITY: 0, UNKNOWN: 0}
        for segment in self.segments:
            totals[segment.source] += segment.length
        return totals

    def as_dict(self) -> dict[str, Any]:
        return {
            "length": self.length,
            "summary": self.summary,
            "segments": [segment.as_dict() for segment in self.segments],
            "stripes": self.stripes,
            "volumes": self.volumes,
        }


_NAME_KEYS = ("name", "file", "filename", "path", "volume", "id")
_ORDER_KEYS = ("order", "index", "position", "sequence", "seq", "ordinal")
_LENGTH_KEYS = ("length", "size", "true_length", "real_length", "decoded_length")
_HASH_KEYS = ("sha256", "checksum", "digest", "hash")
_BLOCK_HASH_KEYS = ("block_sha256", "block_checksums", "block_hashes", "blocks_sha256")
_DAMAGED_KEYS = ("damaged", "missing", "corrupt", "lost", "bad", "unreadable")
_ROLE_KEYS = ("role", "kind", "type")
_PARITY_ROLES = ("parity", "xor", "checksum")


def _lookup(
    mapping: Mapping[str, Any], keys: Iterable[str], default: Any = None
) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in lowered and lowered[key] is not None:
            return lowered[key]
    return default


def _integer(value: Any, what: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise HandoverError(f"{what} must be an integer, not a boolean")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise HandoverError(f"{what} must be an integer, got {value!r}") from error
    if number < 0:
        raise HandoverError(f"{what} must not be negative, got {number}")
    if positive and number == 0:
        raise HandoverError(f"{what} must be positive")
    return number


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    for prefix in ("sha256:", "sha256-", "0x"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text or None


def _block_digests(value: Any, name: str) -> tuple[str | None, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        indexed: dict[int, str | None] = {}
        for key, digest in value.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as error:
                raise HandoverError(
                    f"block checksums for {name!r} must use integer indices"
                ) from error
            indexed[index] = _digest(digest)
        if not indexed:
            return ()
        result: list[str | None] = [None] * (max(indexed) + 1)
        for index, digest in indexed.items():
            result[index] = digest
        return tuple(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_digest(digest) for digest in value)
    raise HandoverError(f"block checksums for {name!r} must be a list or an object")


def _entry_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        value = _lookup(entry, _NAME_KEYS)
        if value is not None:
            return str(value)
    return None


def _is_parity(entry: Mapping[str, Any]) -> bool:
    return str(_lookup(entry, _ROLE_KEYS, "")).lower() in _PARITY_ROLES


def _parse_volume(
    base: Path, entry: Mapping[str, Any], position: int, *, role: str = "data"
) -> Volume:
    name = _entry_name(entry)
    if name is None:
        raise HandoverError(f"volume entry {position} does not name a file")
    length = _lookup(entry, _LENGTH_KEYS)
    length = None if length is None else _integer(length, f"volume {name!r} length")
    path = Path(name)
    if not path.is_absolute():
        path = base / path
    order = _lookup(entry, _ORDER_KEYS)
    order = position if order is None else _integer(order, f"volume {name!r} order")
    return Volume(
        name=name,
        path=path,
        order=order,
        length=length,
        sha256=_digest(_lookup(entry, _HASH_KEYS)),
        block_sha256=_block_digests(_lookup(entry, _BLOCK_HASH_KEYS), name),
        damaged=any(bool(entry.get(key)) for key in _DAMAGED_KEYS),
        role=role,
    )


def _volume_entries(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = _lookup(document, ("volumes", "parts", "files", "slots", "entries"))
    order = _lookup(document, ("volume_order", "orders", "volume_sequence", "sequence"))
    if raw is None and order is None:
        raise HandoverError("manifest does not list any volumes")

    if isinstance(raw, list):
        listed = [
            dict(entry) if isinstance(entry, Mapping) else {"name": str(entry)}
            for entry in raw
        ]
    elif isinstance(raw, Mapping):
        listed = [
            {
                "name": str(name),
                **(
                    dict(details)
                    if isinstance(details, Mapping)
                    else {"order": details}
                ),
            }
            for name, details in raw.items()
        ]
    elif raw is None:
        listed = []
    else:
        raise HandoverError("'volumes' must be a list or an object")

    if order is None:
        return listed

    by_name = {
        name: entry for entry in listed if (name := _entry_name(entry)) is not None
    }
    names: list[str] = []
    if isinstance(order, Mapping):
        ordered = sorted(order, key=lambda key: _integer(order[key], "volume order"))
        for position, raw_name in enumerate(ordered):
            name = str(raw_name)
            names.append(name)
            entry = dict(by_name.get(name, {"name": name}))
            entry["order"] = position
            by_name[name] = entry
    elif isinstance(order, Sequence) and not isinstance(order, (str, bytes, bytearray)):
        for position, item in enumerate(order):
            name = _entry_name(item)
            if name is None:
                raise HandoverError(
                    f"volume_order entry {position} does not name a volume"
                )
            names.append(name)
            entry = dict(by_name.get(name, {"name": name}))
            if isinstance(item, Mapping):
                entry.update(item)
            entry["order"] = position
            by_name[name] = entry
    else:
        raise HandoverError("'volume_order' must be a list or an object")

    result = [by_name[name] for name in names if name in by_name]
    seen = {_entry_name(entry) for entry in result}
    result.extend(entry for entry in listed if _entry_name(entry) not in seen)
    return result


def load_layout(manifest: str | Path) -> Layout:
    """Parse a handover manifest and resolve the volume paths it names."""

    path = Path(manifest)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HandoverError(f"manifest not found: {path}") from error
    except UnicodeDecodeError as error:
        raise HandoverError(f"manifest is not UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise HandoverError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(document, Mapping):
        raise HandoverError("manifest must be a JSON object")

    geometry: Mapping[str, Any] = document
    for key in ("layout", "geometry", "stripes", "stream"):
        nested = _lookup(document, (key,))
        if isinstance(nested, Mapping):
            geometry = nested
            break

    stripe = _lookup(
        geometry, ("stripe_length", "stripe_size", "stripe", "block_size", "chunk_size")
    )
    if stripe is None:
        stripe = _lookup(
            document,
            ("stripe_length", "stripe_size", "stripe", "block_size", "chunk_size"),
        )
    if stripe is None:
        raise HandoverError("manifest does not specify 'stripe_length'")
    stripe = _integer(stripe, "stripe length", positive=True)

    record = _lookup(
        geometry, ("record_length", "record_size", "unit_size", "sample_size")
    )
    if record is None:
        record = _lookup(
            document, ("record_length", "record_size", "unit_size", "sample_size")
        )
    record = 1 if record is None else _integer(record, "record length", positive=True)
    if stripe % record:
        raise HandoverError("record length must divide the stripe length")

    last_keys = (
        "last_volume_length",
        "last_length",
        "final_length",
        "tail_length",
        "true_last_length",
    )
    last = _lookup(geometry, last_keys)
    if last is None:
        last = _lookup(document, last_keys)
    last = None if last is None else _integer(last, "last volume length")

    total_keys = ("total_length", "original_length", "plain_length")
    total = _lookup(geometry, total_keys)
    if total is None:
        total = _lookup(document, total_keys)
    total = None if total is None else _integer(total, "total length")

    entries = _volume_entries(geometry)
    data_entries = [entry for entry in entries if not _is_parity(entry)]
    if not data_entries:
        raise HandoverError("manifest has no data volumes")
    base = path.parent
    volumes = [
        _parse_volume(base, entry, position)
        for position, entry in enumerate(data_entries)
    ]
    orders = [volume.order for volume in volumes]
    if len(set(orders)) != len(orders):
        raise HandoverError("volume order is not unique")
    volumes.sort(key=lambda volume: volume.order)

    parity_entries = [entry for entry in entries if _is_parity(entry)]
    for key in (
        "parity",
        "xor",
        "parity_volume",
        "checksum_volume",
        "check_volume",
        "xor_volume",
    ):
        value = _lookup(document, (key,))
        if isinstance(value, Mapping):
            parity_entries.append(value)
        elif isinstance(value, str):
            parity_entries.append({"name": value})
    if len(parity_entries) > 1:
        raise HandoverError("manifest names more than one parity volume")
    parity = None
    if parity_entries:
        parity = _parse_volume(base, parity_entries[0], len(volumes), role="parity")
        if parity.name in {volume.name for volume in volumes}:
            raise HandoverError("the parity volume is also listed as a data volume")

    if last is not None and volumes[-1].length is None:
        tail = volumes[-1]
        volumes[-1] = Volume(
            name=tail.name,
            path=tail.path,
            order=tail.order,
            length=last,
            sha256=tail.sha256,
            block_sha256=tail.block_sha256,
            damaged=tail.damaged,
        )

    return Layout(
        stripe_length=stripe,
        record_length=record,
        volumes=tuple(volumes),
        parity=parity,
        last_volume_length=last,
        total_length=total,
    )


def _read(path: Path) -> tuple[bytes | None, str]:
    try:
        with xopen(path, "rb", threads=0) as stream:
            return stream.read(), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, EOFError, ValueError, ImportError) as error:
        return None, f"unreadable: {error}"


def _sha256(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _xor(blocks: Iterable[bytes], length: int) -> bytes:
    result = bytearray(length)
    for block in blocks:
        for index, byte in enumerate(block[:length]):
            result[index] ^= byte
    return bytes(result)


def _width(volume_length: int, stripe: int, stripe_length: int) -> int:
    return max(0, min(volume_length - stripe * stripe_length, stripe_length))


class _VolumeState:
    """Decoded volume and the evidence available for its blocks."""

    def __init__(self, volume: Volume) -> None:
        self.volume = volume
        self.data, self.read_state = _read(volume.path)
        self.length = self._length()
        self.whole_ok = self._whole_ok()
        self.block_ok: dict[int, bool] = {}
        self.stripe_length = 1

    def _length(self) -> int:
        observed = 0 if self.data is None else len(self.data)
        declared = self.volume.length
        if declared is None:
            return observed
        if self.data is None:
            return declared
        if declared > observed:
            self.read_state = "short"
            return observed
        return declared

    def _whole_ok(self) -> bool | None:
        if self.data is None:
            return False
        if self.volume.sha256 is None:
            return None
        return _sha256(self.data[: self.length]) == self.volume.sha256

    def prime(self, stripe_length: int) -> None:
        self.stripe_length = stripe_length
        for stripe, digest in enumerate(self.volume.block_sha256):
            if digest is None:
                continue
            block = _block(self, stripe)
            self.block_ok[stripe] = block is not None and _sha256(block) == digest

    def state(self, stripe: int) -> str:
        if self.data is None or self.read_state != "ok" or self.volume.damaged:
            return BAD
        if stripe in self.block_ok:
            return GOOD if self.block_ok[stripe] else BAD
        if self.whole_ok is True:
            return GOOD
        if self.whole_ok is False:
            return SUSPECT
        return UNCHECKED

    def bits(self, stripe: int) -> bytes | None:
        width = _width(self.length, stripe, self.stripe_length)
        if width == 0:
            return b""
        if self.data is None:
            return None
        start = stripe * self.stripe_length
        chunk = self.data[start : start + width]
        return chunk if len(chunk) == width else None

    def replace(self, data: bytes) -> None:
        self.data = data
        self.read_state = "ok"
        self.whole_ok = self._whole_ok()


def _block(state: _VolumeState, stripe: int) -> bytes | None:
    return state.bits(stripe)


def _resolve_lengths(layout: Layout, states: list[_VolumeState]) -> list[int]:
    lengths: list[int | None] = [
        state.length if state.data is not None else state.volume.length
        for state in states
    ]

    if layout.total_length is not None:
        missing = [index for index, value in enumerate(lengths) if value is None]
        known = sum(value for value in lengths if value is not None)
        if len(missing) == 1:
            lengths[missing[0]] = layout.total_length - known
        elif not missing and known != layout.total_length:
            raise HandoverError(
                f"volume lengths add up to {known}, not {layout.total_length}"
            )

    for index, value in enumerate(lengths):
        if value is not None:
            continue
        volume = layout.volumes[index]
        if index == len(lengths) - 1 and layout.last_volume_length is not None:
            lengths[index] = layout.last_volume_length
            continue
        known = [item for item in lengths if item is not None]
        if not known:
            raise HandoverError(
                f"cannot infer the length of missing volume {volume.name!r}"
            )
        stripes = max(
            (item + layout.stripe_length - 1) // layout.stripe_length for item in known
        )
        later = lengths[index + 1 :]
        if all(
            item is not None and item == layout.stripe_length * stripes
            for item in later
        ):
            lengths[index] = layout.stripe_length * stripes
        else:
            raise HandoverError(
                f"cannot infer the length of missing volume {volume.name!r}; "
                "add 'length' to the manifest"
            )

    result = [int(value) for value in lengths]  # type: ignore[arg-type]
    if layout.last_volume_length is not None:
        result[-1] = layout.last_volume_length
    if layout.total_length is not None and sum(result) != layout.total_length:
        raise HandoverError(
            f"volume lengths add up to {sum(result)}, not {layout.total_length}"
        )
    return result


def _stripe_count(lengths: Sequence[int], stripe_length: int) -> int:
    return max(
        ((length + stripe_length - 1) // stripe_length for length in lengths), default=0
    )


def _rebased(layout: Layout, base: Path) -> Layout:
    def move(volume: Volume) -> Volume:
        return Volume(
            name=volume.name,
            path=base / volume.path.name,
            order=volume.order,
            length=volume.length,
            sha256=volume.sha256,
            block_sha256=volume.block_sha256,
            damaged=volume.damaged,
            role=volume.role,
        )

    return Layout(
        stripe_length=layout.stripe_length,
        record_length=layout.record_length,
        volumes=tuple(move(volume) for volume in layout.volumes),
        parity=None if layout.parity is None else move(layout.parity),
        last_volume_length=layout.last_volume_length,
        total_length=layout.total_length,
    )


def _parity_block(
    parity: _VolumeState | None, stripe: int, width: int
) -> tuple[str, bytes | None]:
    """Return the parity evidence and its block for one stripe."""

    if parity is None:
        return BAD, None
    verdict = parity.state(stripe)
    if verdict in (BAD, SUSPECT):
        return verdict, None
    block = parity.bits(stripe)
    if block is None or len(block) < width:
        return BAD, None
    return verdict, block[:width]


def _others(
    states: Sequence[_VolumeState], stripe: int, skip: int, widths: Sequence[int]
) -> list[bytes] | None:
    blocks: list[bytes] = []
    for index in range(len(states)):
        if index == skip or widths[index] == 0:
            continue
        block = states[index].bits(stripe)
        if block is None:
            return None
        blocks.append(block)
    return blocks


def _rebuild(
    stripe: int,
    target: int,
    states: Sequence[_VolumeState],
    widths: Sequence[int],
    parity_block: bytes,
) -> bytes | None:
    others = _others(states, stripe, target, widths)
    if others is None:
        return None
    width = widths[target]
    return _xor([parity_block, *others], max(len(parity_block), width))[:width]


def _resolve_stripe(
    stripe: int,
    states: Sequence[_VolumeState],
    parity: _VolumeState | None,
    lengths: Sequence[int],
    layout: Layout,
    blocks: list[list[bytes | None]],
    sources: list[list[str]],
    notes: list[list[str]],
) -> None:
    """Resolve one stripe, independently of every other stripe.

    Evidence levels are ``GOOD`` (a matching checksum), ``BAD`` (missing,
    unreadable or a failing checksum), ``SUSPECT`` (the whole-volume checksum
    failed, but the exact block is not yet localized), and ``UNCHECKED`` (no
    checksum contradicts the bytes).  The XOR volume never silently overrides
    a positive check: a mismatch is attributed only when exactly one piece of
    evidence is missing.
    """

    widths = [
        _width(lengths[index], stripe, layout.stripe_length)
        for index in range(len(states))
    ]
    evidence = [states[index].state(stripe) for index in range(len(states))]
    parity_width = max(widths, default=0)
    parity_verdict, parity_block = _parity_block(parity, stripe, parity_width)
    parity_ok = parity_block is not None

    # Initially only blocks with no contradicting evidence are direct.
    for index, verdict in enumerate(evidence):
        if widths[index] == 0:
            continue
        if verdict in (GOOD, UNCHECKED):
            blocks[stripe][index] = states[index].bits(stripe)
            sources[stripe][index] = DIRECT
        else:
            sources[stripe][index] = UNKNOWN

    bad = [
        index
        for index in range(len(states))
        if widths[index] and evidence[index] == BAD
    ]
    suspect = [
        index
        for index in range(len(states))
        if widths[index] and evidence[index] == SUSPECT
    ]
    unchecked = [
        index
        for index in range(len(states))
        if widths[index] and evidence[index] == UNCHECKED
    ]

    # Two known losses in one stripe cannot be recovered from a single XOR
    # block, and a known loss together with a suspect volume is also two
    # unknowns: the suspect bytes feed the reconstruction and could corrupt it.
    if len(bad) >= 2 or (bad and suspect):
        for index in bad:
            notes[stripe][index] = "multiple-missing"
        for index in suspect:
            notes[stripe][index] = "xor-mismatch-unattributed"
        return

    if len(bad) == 1:
        target = bad[0]
        if not parity_ok:
            notes[stripe][target] = (
                "no-parity-volume" if parity is None else "parity-unavailable"
            )
            return
        recovered = _rebuild(stripe, target, states, widths, parity_block)
        if recovered is None:
            notes[stripe][target] = "stripe-incomplete"
            return
        digest = _block_digest(layout.volumes[target], stripe)
        if digest is not None and _sha256(recovered) != digest:
            notes[stripe][target] = "recovered-block-checksum-mismatch"
            return
        blocks[stripe][target] = recovered
        sources[stripe][target] = PARITY
        notes[stripe][target] = f"recovered {layout.volumes[target].name}"
        return

    # No known loss.  Compare the data blocks with parity.  This is the only
    # way to localize a whole-volume checksum failure without assuming which
    # side is wrong.
    if suspect:
        data_blocks = [states[index].bits(stripe) for index in range(len(states))]
        if any(
            block is None for index, block in enumerate(data_blocks) if widths[index]
        ):
            for index in suspect:
                notes[stripe][index] = "stripe-incomplete"
            return
        if not parity_ok or parity_block is None:
            for index in suspect:
                notes[stripe][index] = (
                    "no-parity-volume" if parity is None else "parity-unavailable"
                )
            return

        actual = _xor(
            [block for block in data_blocks if block is not None], parity_width
        )
        if actual == parity_block:
            # The suspect bytes agree with parity, so the failure is elsewhere
            # in that volume.  This stripe is corroborated and stays direct.
            for index in suspect:
                notes[stripe][index] = "xor-corroborated"
            return

        if len(suspect) == 1 and parity_verdict == GOOD:
            target = suspect[0]
            recovered = _rebuild(stripe, target, states, widths, parity_block)
            if recovered is None:
                notes[stripe][target] = "stripe-incomplete"
                return
            if recovered == states[target].bits(stripe):
                # Parity cannot explain the failure: the block already agrees
                # with XOR, so the damaged bytes lie outside this stripe or in
                # a volume we cannot identify.
                notes[stripe][target] = "xor-mismatch-unattributed"
                return
            digest = _block_digest(layout.volumes[target], stripe)
            if digest is not None and _sha256(recovered) != digest:
                notes[stripe][target] = "recovered-block-checksum-mismatch"
                return
            blocks[stripe][target] = recovered
            sources[stripe][target] = PARITY
            notes[stripe][target] = f"recovered {layout.volumes[target].name}"
            return

        # More than one suspect, or parity itself is not positively verified:
        # the mismatch cannot be pinned on one volume.
        for index in suspect:
            notes[stripe][index] = "xor-mismatch-unattributed"
        return

    # Only GOOD and UNCHECKED blocks remain.
    if not parity_ok or parity_block is None:
        return

    present = [
        blocks[stripe][index] for index in range(len(states)) if widths[index] > 0
    ]
    if (
        _xor([block for block in present if block is not None], parity_width)
        == parity_block
    ):
        for index in unchecked:
            notes[stripe][index] = "xor-corroborated"
        return

    if len(unchecked) == 1 and parity_verdict == GOOD:
        target = unchecked[0]
        recovered = _rebuild(stripe, target, states, widths, parity_block)
        if recovered is None:
            notes[stripe][target] = "stripe-incomplete"
            return
        digest = _block_digest(layout.volumes[target], stripe)
        if digest is not None and _sha256(recovered) != digest:
            notes[stripe][target] = "xor-mismatch-unattributed"
            return
        blocks[stripe][target] = recovered
        sources[stripe][target] = PARITY
        notes[stripe][target] = f"recovered {layout.volumes[target].name}"
        return

    # The checked data agrees with each other, so if every data block carries
    # a positive check the disagreement is on the parity side and must not
    # downgrade verified data.
    if all(
        evidence[index] == GOOD for index in range(len(states)) if widths[index] > 0
    ):
        return
    for index in unchecked:
        notes[stripe][index] = "xor-mismatch-unattributed"


def _block_digest(volume: Volume, stripe: int) -> str | None:
    return volume.block_sha256[stripe] if stripe < len(volume.block_sha256) else None


def recover(manifest: str | Path, *, base_dir: str | Path | None = None) -> Recovery:
    """Reconstruct the stream described by ``manifest``.

    Relative volume names resolve against the manifest's directory, or
    against ``base_dir`` when it is given.
    """

    layout = load_layout(manifest)
    if base_dir is not None:
        layout = _rebased(layout, Path(base_dir))

    states = [_VolumeState(volume) for volume in layout.volumes]
    parity = None if layout.parity is None else _VolumeState(layout.parity)
    for state in states:
        state.prime(layout.stripe_length)
    if parity is not None:
        parity.prime(layout.stripe_length)
    lengths = _resolve_lengths(layout, states)

    stripes = _stripe_count(lengths, layout.stripe_length)
    blocks: list[list[bytes | None]] = [[None] * len(states) for _ in range(stripes)]
    sources: list[list[str]] = [[UNKNOWN] * len(states) for _ in range(stripes)]
    notes: list[list[str]] = [[""] * len(states) for _ in range(stripes)]

    for stripe in range(stripes):
        _resolve_stripe(stripe, states, parity, lengths, layout, blocks, sources, notes)

    # A volume rebuilt from parity must also satisfy its whole-volume checksum.
    # A per-block pass alone only proves the blocks that had their own digests.
    for index, state in enumerate(states):
        if state.volume.sha256 is None or state.whole_ok is True:
            continue
        recovered = [
            stripe for stripe in range(stripes) if sources[stripe][index] == PARITY
        ]
        if not recovered:
            continue
        assembled = _assemble(index, states, lengths, blocks)
        if assembled is None or _sha256(assembled) != state.volume.sha256:
            for stripe in recovered:
                sources[stripe][index] = UNKNOWN
                notes[stripe][index] = "recovered-volume-checksum-mismatch"

    output = bytearray()
    segments: list[Segment] = []
    stripe_reports: list[dict[str, Any]] = []
    offset = 0
    for stripe in range(stripes):
        widths = [
            _width(lengths[index], stripe, layout.stripe_length)
            for index in range(len(states))
        ]
        stripe_start = offset
        for index, volume in enumerate(layout.volumes):
            width = widths[index]
            if width == 0:
                continue
            block = blocks[stripe][index]
            output.extend(bytes(width) if block is None else block[:width])
            _add_segment(
                segments,
                offset,
                offset + width,
                sources[stripe][index],
                stripe,
                volume.name,
                notes[stripe][index],
            )
            offset += width
        stripe_reports.append(
            {
                "index": stripe,
                "offset": stripe_start,
                "length": offset - stripe_start,
                "source": _stripe_source(sources[stripe], widths),
                "blocks": [
                    {
                        "volume": layout.volumes[index].name,
                        "source": sources[stripe][index],
                        "source_label": LABELS[sources[stripe][index]],
                        "reason": notes[stripe][index],
                    }
                    for index in range(len(states))
                    if widths[index] > 0
                ],
            }
        )

    if layout.total_length is not None:
        del output[layout.total_length :]

    return Recovery(
        data=bytes(output),
        segments=segments,
        stripes=stripe_reports,
        volumes=_volume_reports(layout, states, parity, lengths, blocks),
    )


def _stripe_source(sources: Sequence[str], widths: Sequence[int]) -> str:
    values = [sources[index] for index in range(len(sources)) if widths[index] > 0]
    if UNKNOWN in values:
        return UNKNOWN
    if PARITY in values:
        return PARITY
    return DIRECT


def _add_segment(
    segments: list[Segment],
    start: int,
    end: int,
    source: str,
    stripe: int,
    volume: str,
    reason: str,
) -> None:
    if (
        segments
        and segments[-1].end == start
        and segments[-1].source == source
        and segments[-1].reason == reason
    ):
        segments[-1].end = end
        return
    segments.append(
        Segment(
            start=start,
            end=end,
            source=source,
            stripe=stripe,
            volume=volume,
            reason=reason,
        )
    )


def _verified(data: bytes | None, volume: Volume) -> bool:
    if data is None:
        return False
    return volume.sha256 is None or _sha256(data) == volume.sha256


def _assemble(
    index: int,
    states: Sequence[_VolumeState],
    lengths: Sequence[int],
    blocks: Sequence[Sequence[bytes | None]],
) -> bytes | None:
    pieces: list[bytes] = []
    for stripe in range(len(blocks)):
        width = _width(lengths[index], stripe, states[index].stripe_length)
        if width == 0:
            continue
        block = blocks[stripe][index]
        if block is None:
            return None
        pieces.append(block[:width])
    return b"".join(pieces)


def _volume_reports(
    layout: Layout,
    states: Sequence[_VolumeState],
    parity: _VolumeState | None,
    lengths: Sequence[int],
    blocks: Sequence[Sequence[bytes | None]],
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for index, state in enumerate(states):
        assembled = _assemble(index, states, lengths, blocks) or b""
        reports.append(
            {
                "name": state.volume.name,
                "role": "data",
                "order": state.volume.order,
                "length": lengths[index],
                "state": state.read_state,
                "declared_sha256": state.volume.sha256,
                "actual_sha256": _sha256(assembled),
                "verified": _verified(assembled, state.volume),
                "damaged": state.volume.damaged,
            }
        )
    if parity is not None:
        reports.append(
            {
                "name": parity.volume.name,
                "role": "parity",
                "order": parity.volume.order,
                "length": None if parity.data is None else len(parity.data),
                "state": parity.read_state,
                "declared_sha256": parity.volume.sha256,
                "actual_sha256": _sha256(parity.data),
                "verified": _verified(parity.data, parity.volume),
                "damaged": parity.volume.damaged,
            }
        )
    return reports


def _write_tsv(path: Path, recovery: Recovery) -> None:
    lines = ["start\tend\tlength\tsource\tsource_label\tstripe\tvolume\treason"]
    for segment in recovery.segments:
        lines.append(
            "\t".join(
                str(value)
                for value in (
                    segment.start,
                    segment.end,
                    segment.length,
                    segment.source,
                    LABELS[segment.source],
                    segment.stripe,
                    segment.volume,
                    segment.reason,
                )
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_summary(recovery: Recovery, stream: Any = None) -> None:
    stream = sys.stdout if stream is None else stream
    print(f"total: {recovery.length} bytes", file=stream)
    for source in (DIRECT, PARITY, UNKNOWN):
        print(
            f"{source:<8} {LABELS[source]}: {recovery.summary[source]} bytes",
            file=stream,
        )
    for segment in recovery.segments:
        note = f" ({segment.reason})" if segment.reason else ""
        print(
            f"[{segment.start}:{segment.end}) {segment.length:>8} "
            f"{segment.source:<8} {LABELS[segment.source]} stripe={segment.stripe} "
            f"volume={segment.volume}{note}",
            file=stream,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reassemble striped compressed volumes using a handover manifest."
    )
    parser.add_argument("manifest", type=Path, help="handover manifest (JSON)")
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="reconstructed stream"
    )
    parser.add_argument(
        "--report", type=Path, help="write the full recovery report as JSON"
    )
    parser.add_argument(
        "--segments", type=Path, help="write per-segment provenance as TSV"
    )
    parser.add_argument(
        "--base-dir", type=Path, help="directory containing the volume files"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="do not print the segment table"
    )
    parser.add_argument(
        "--fail-on-unknown",
        action="store_true",
        help="exit with status 2 when some output bytes cannot be proven",
    )
    args = parser.parse_args(argv)

    try:
        recovery = recover(args.manifest, base_dir=args.base_dir)
    except HandoverError as error:
        parser.error(str(error))
        raise AssertionError("unreachable") from error
    args.output.write_bytes(recovery.data)
    if args.report is not None:
        args.report.write_text(
            json.dumps(recovery.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.segments is not None:
        _write_tsv(args.segments, recovery)
    if not args.quiet:
        _print_summary(recovery)
    if args.fail_on_unknown and recovery.summary[UNKNOWN]:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
