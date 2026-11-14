"""Offline audit of prefix-only ("first N bytes") CAN captures against full captures.

A sampling device may keep only the first :data:`PREFIX_BYTES` bytes of every
frame instead of the complete payload. Comparing such a sample against the full
reference file by *prefix equality* is not sufficient: identical prefixes do not
prove that the truncated bytes are identical.

This module performs an offline, frame-by-frame audit and classifies every
frame into one of three verdicts:

``IDENTICAL``
    The frames are fully equal, including payload, DLC and frame identity. The
    sample holds enough evidence to confirm equality.

``CONFLICT``
    A difference is visible *inside* the evidence window (the prefix the sample
    actually retains), or the frame identity / DLC already disagree. The
    position of the first differing byte is reported.

``NO_EVIDENCE``
    Everything that can be compared is equal, but the sample truncates the
    payload, so the bytes after the cut-off point cannot be verified. A
    difference behind the cut-off point is deliberately *not* reported as a
    discovered conflict, because the sample contains no evidence for it.

The module also keeps two related but distinct concepts apart:

* the raw DLC, i.e. the 4-bit length code carried on the wire
  (``0``-``15``; for CAN FD, codes ``9``-``15`` encode ``12``-``64`` bytes);
* the declared byte count, i.e. how many payload bytes the frame actually
  carries.

For example, FD length code ``9`` encodes 12 bytes, not 9. Use
:func:`explain_dlc` / :func:`explain_length` to render either value in a
human-readable form.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .io import BLFWriter, LogReader
from .message import Message
from .util import CAN_FD_DLC, dlc2len, len2dlc

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .io.generic import MessageReader

PREFIX_BYTES: Final[int] = 8
"""Number of payload bytes retained per frame by the sampling device."""


class Verdict(StrEnum):
    """Result of auditing one frame pair."""

    IDENTICAL = "identical"
    CONFLICT = "conflict"
    NO_EVIDENCE = "no_evidence"


@dataclass(frozen=True)
class FrameKey:
    """Identity used to pair a sample frame with its reference frame.

    Frames are paired by their position in the capture first (see
    :func:`frame_key`); the captured fields document which frame was involved.
    """

    index: int
    arbitration_id: int
    is_extended_id: bool
    is_fd: bool
    is_remote_frame: bool


@dataclass(frozen=True)
class Conflict:
    """A confirmed, evidenced disagreement between sample and reference.

    Only differences reachable with the bytes the sample actually retains are
    reported here. ``byte_index`` is ``None`` when the mismatch is not a single
    payload byte (frame identity or declared byte count).
    """

    reason: str
    byte_index: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class FrameAudit:
    """Verdict for one frame position."""

    key: FrameKey
    verdict: Verdict
    sample_prefix: bytes
    reference_data: bytes
    declared_byte_count: int
    raw_dlc: int
    retained_bytes: int
    conflict: Conflict | None = None
    unverified_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def first_conflict_byte(self) -> int | None:
        """Zero-based index of the first evidenced differing byte, if any."""
        if self.conflict is not None:
            return self.conflict.byte_index
        return None


def frame_key(index: int, message: Message) -> FrameKey:
    """Build the :class:`FrameKey` describing ``message`` at position ``index``."""
    return FrameKey(
        index=index,
        arbitration_id=message.arbitration_id,
        is_extended_id=message.is_extended_id,
        is_fd=message.is_fd,
        is_remote_frame=message.is_remote_frame,
    )


def raw_dlc_of(message: Message) -> int:
    """Return the raw 4-bit DLC that encodes the message's declared byte count.

    ``Message.dlc`` stores the byte count; the wire DLC is recovered with
    :func:`can.util.len2dlc`. For classic CAN the two values coincide for
    lengths up to 8; for CAN FD they diverge for payloads longer than 8 bytes.
    """
    return len2dlc(message.dlc)


def explain_dlc(raw_dlc: int, *, is_fd: bool) -> str:
    """Explain a raw DLC value (the 4-bit length code on the wire).

    >>> explain_dlc(9, is_fd=True)
    'raw DLC 9 (FD length code) encodes 12 payload bytes'
    >>> explain_dlc(8, is_fd=False)
    'raw DLC 8 encodes 8 payload bytes'
    """
    if not 0 <= raw_dlc <= 15:
        raise ValueError(f"raw DLC must be in 0..15, got {raw_dlc}")
    encoded = dlc2len(raw_dlc)
    if is_fd:
        return f"raw DLC {raw_dlc} (FD length code) encodes {encoded} payload bytes"
    if raw_dlc > 8:
        return f"raw DLC {raw_dlc} is reserved on classic CAN; treated as {encoded} payload bytes"
    return f"raw DLC {raw_dlc} encodes {encoded} payload bytes"


def explain_length(byte_count: int) -> str:
    """Explain a declared payload byte count and its raw DLC encoding.

    >>> explain_length(12)
    '12 declared payload bytes are encoded on the wire by raw DLC code 9'
    """
    if not 0 <= byte_count <= 64:
        raise ValueError(f"byte count must be in 0..64, got {byte_count}")
    code = len2dlc(byte_count)
    if byte_count <= 8:
        return (
            f"{byte_count} declared payload bytes are encoded on the wire "
            f"by raw DLC code {code} (code and byte count coincide)"
        )
    if byte_count not in CAN_FD_DLC:
        return (
            f"{byte_count} declared payload bytes are not a valid CAN FD "
            f"length; the smallest encasing payload is "
            f"{dlc2len(code)} bytes (raw DLC code {code})"
        )
    return f"{byte_count} declared payload bytes are encoded on the wire by raw DLC code {code}"


def sample_message(message: Message, prefix_bytes: int = PREFIX_BYTES) -> Message:
    """Return a copy of ``message`` retaining only its first ``prefix_bytes``.

    The declared DLC (:attr:`~can.Message.dlc`, a byte count) is preserved so
    that the sample still records how long the original frame claimed to be,
    even though most of its payload is gone.
    """
    if prefix_bytes < 0:
        raise ValueError("prefix_bytes must be non-negative")
    retained = bytes(message.data[:prefix_bytes])
    return Message(
        timestamp=message.timestamp,
        arbitration_id=message.arbitration_id,
        is_extended_id=message.is_extended_id,
        is_remote_frame=message.is_remote_frame,
        is_error_frame=message.is_error_frame,
        channel=message.channel,
        dlc=message.dlc,
        data=retained,
        is_fd=message.is_fd,
        is_rx=message.is_rx,
        bitrate_switch=message.bitrate_switch,
        error_state_indicator=message.error_state_indicator,
        check=False,
    )


def _identity_conflict(sample: Message, reference: Message) -> Conflict | None:
    if sample.arbitration_id != reference.arbitration_id:
        return Conflict(
            reason="arbitration_id",
            detail=(
                f"sample ID {sample.arbitration_id:#x} != "
                f"reference ID {reference.arbitration_id:#x}"
            ),
        )
    if sample.is_extended_id != reference.is_extended_id:
        return Conflict(
            reason="id_flag",
            detail="extended-identifier flag differs",
        )
    if sample.is_remote_frame != reference.is_remote_frame:
        return Conflict(
            reason="remote_flag",
            detail="remote-frame flag differs",
        )
    if sample.is_fd != reference.is_fd:
        return Conflict(reason="fd_flag", detail="CAN FD flag differs")
    return None


def audit_frame(
    sample: Message,
    reference: Message,
    index: int = 0,
    prefix_bytes: int = PREFIX_BYTES,
) -> FrameAudit:
    """Audit one sample/reference frame pair.

    The cut-off point is ``min(prefix_bytes, len(sample.data))``: only bytes the
    sample actually retains count as evidence. Anything differing behind that
    point yields :attr:`Verdict.NO_EVIDENCE`, never a conflict.
    """
    key = frame_key(index, reference)
    retained = bytes(sample.data[:prefix_bytes])
    reference_data = bytes(reference.data)
    raw_dlc = raw_dlc_of(reference)
    notes: list[str] = []

    identity = _identity_conflict(sample, reference)
    if identity is not None:
        return FrameAudit(
            key=key,
            verdict=Verdict.CONFLICT,
            sample_prefix=retained,
            reference_data=reference_data,
            declared_byte_count=reference.dlc,
            raw_dlc=raw_dlc,
            retained_bytes=len(retained),
            conflict=identity,
            notes=tuple(notes),
        )

    if sample.dlc != reference.dlc:
        return FrameAudit(
            key=key,
            verdict=Verdict.CONFLICT,
            sample_prefix=retained,
            reference_data=reference_data,
            declared_byte_count=reference.dlc,
            raw_dlc=raw_dlc,
            retained_bytes=len(retained),
            conflict=Conflict(
                reason="declared_byte_count",
                detail=(
                    f"sample declares {sample.dlc} byte(s), reference declares {reference.dlc}"
                ),
            ),
            notes=tuple(notes),
        )

    window = min(prefix_bytes, len(retained), len(reference_data))
    for byte_index in range(window):
        if retained[byte_index] != reference_data[byte_index]:
            return FrameAudit(
                key=key,
                verdict=Verdict.CONFLICT,
                sample_prefix=retained,
                reference_data=reference_data,
                declared_byte_count=reference.dlc,
                raw_dlc=raw_dlc,
                retained_bytes=len(retained),
                conflict=Conflict(
                    reason="payload_byte",
                    byte_index=byte_index,
                    detail=(
                        f"byte {byte_index} differs within the retained "
                        f"prefix: sample {retained[byte_index]:#04x} != "
                        f"reference {reference_data[byte_index]:#04x}"
                    ),
                ),
                notes=tuple(notes),
            )

    if len(retained) < len(reference_data):
        unverified = len(reference_data) - len(retained)
        notes.append(
            f"first {len(retained)} byte(s) match; {unverified} byte(s) "
            f"after the cut-off point are not covered by the sample"
        )
        return FrameAudit(
            key=key,
            verdict=Verdict.NO_EVIDENCE,
            sample_prefix=retained,
            reference_data=reference_data,
            declared_byte_count=reference.dlc,
            raw_dlc=raw_dlc,
            retained_bytes=len(retained),
            unverified_bytes=unverified,
            notes=tuple(notes),
        )

    if len(retained) != len(reference_data):
        return FrameAudit(
            key=key,
            verdict=Verdict.CONFLICT,
            sample_prefix=retained,
            reference_data=reference_data,
            declared_byte_count=reference.dlc,
            raw_dlc=raw_dlc,
            retained_bytes=len(retained),
            conflict=Conflict(
                reason="payload_length",
                detail=(
                    f"sample retains {len(retained)} byte(s) but reference "
                    f"holds {len(reference_data)}"
                ),
            ),
            notes=tuple(notes),
        )

    return FrameAudit(
        key=key,
        verdict=Verdict.IDENTICAL,
        sample_prefix=retained,
        reference_data=reference_data,
        declared_byte_count=reference.dlc,
        raw_dlc=raw_dlc,
        retained_bytes=len(retained),
        notes=tuple(notes),
    )


@dataclass
class AuditReport:
    """Aggregate result of auditing two captures."""

    prefix_bytes: int
    frame_audits: list[FrameAudit] = field(default_factory=list)

    @property
    def counts(self) -> dict[Verdict, int]:
        """Number of frames per verdict."""
        result = dict.fromkeys(Verdict, 0)
        for audit in self.frame_audits:
            result[audit.verdict] += 1
        return result

    @property
    def identical(self) -> list[FrameAudit]:
        return [a for a in self.frame_audits if a.verdict is Verdict.IDENTICAL]

    @property
    def conflicts(self) -> list[FrameAudit]:
        return [a for a in self.frame_audits if a.verdict is Verdict.CONFLICT]

    @property
    def no_evidence(self) -> list[FrameAudit]:
        return [a for a in self.frame_audits if a.verdict is Verdict.NO_EVIDENCE]

    def summary(self) -> str:
        """Render a compact, human-readable summary."""
        counts = self.counts
        lines = [
            f"prefix audit (retains first {self.prefix_bytes} byte(s) per frame)",
            f"  total frames : {len(self.frame_audits)}",
            f"  identical    : {counts[Verdict.IDENTICAL]}",
            f"  conflict     : {counts[Verdict.CONFLICT]}",
            f"  no evidence  : {counts[Verdict.NO_EVIDENCE]}",
        ]
        return "\n".join(lines)


def audit_messages(
    samples: Sequence[Message],
    references: Sequence[Message],
    prefix_bytes: int = PREFIX_BYTES,
) -> AuditReport:
    """Audit two in-memory frame sequences, paired by position.

    Frames present on only one side are reported as evidenced conflicts because
    the presence or absence of a frame is itself observable, not hidden behind
    the payload cut-off point.
    """
    report = AuditReport(prefix_bytes=prefix_bytes)
    common = min(len(samples), len(references))
    for index in range(common):
        report.frame_audits.append(
            audit_frame(samples[index], references[index], index, prefix_bytes)
        )

    for index in range(common, len(samples)):
        message = samples[index]
        report.frame_audits.append(
            FrameAudit(
                key=frame_key(index, message),
                verdict=Verdict.CONFLICT,
                sample_prefix=bytes(message.data[:prefix_bytes]),
                reference_data=b"",
                declared_byte_count=message.dlc,
                raw_dlc=raw_dlc_of(message),
                retained_bytes=min(len(message.data), prefix_bytes),
                conflict=Conflict(
                    reason="extra_frame",
                    detail="frame present in sample but absent in reference",
                ),
            )
        )

    for index in range(common, len(references)):
        message = references[index]
        report.frame_audits.append(
            FrameAudit(
                key=frame_key(index, message),
                verdict=Verdict.CONFLICT,
                sample_prefix=b"",
                reference_data=bytes(message.data),
                declared_byte_count=message.dlc,
                raw_dlc=raw_dlc_of(message),
                retained_bytes=0,
                conflict=Conflict(
                    reason="missing_frame",
                    detail="frame present in reference but absent in sample",
                ),
            )
        )

    return report


def _read_messages(reader: MessageReader) -> list[Message]:
    return list(reader)


def audit_files(
    sample_path: str | Path,
    reference_path: str | Path,
    prefix_bytes: int = PREFIX_BYTES,
) -> AuditReport:
    """Audit a sampled capture file against a full reference capture file.

    Both files may be in any format :class:`~can.io.LogReader` supports.
    """
    with LogReader(str(reference_path)) as reference_reader:
        references = _read_messages(reference_reader)
    with LogReader(str(sample_path)) as sample_reader:
        samples = _read_messages(sample_reader)
    return audit_messages(samples, references, prefix_bytes)


def write_prefix_sample(
    source_path: str | Path,
    output_path: str | Path,
    prefix_bytes: int = PREFIX_BYTES,
) -> int:
    """Create a prefix-only capture from a full capture.

    Returns the number of frames written. The output is always a BLF file:
    its CAN FD object stores the declared byte count (as the raw DLC) and the
    actually retained bytes (as ``valid_bytes``) separately, so a truncated
    frame still remembers how long the original frame claimed to be.
    """
    source_path = Path(source_path)
    output_path = Path(output_path)
    count = 0
    with LogReader(str(source_path)) as reader, BLFWriter(str(output_path)) as writer:
        for message in reader:
            writer.on_message_received(sample_message(message, prefix_bytes))
            count += 1
    return count


def iter_audits(report: AuditReport) -> Iterable[FrameAudit]:
    """Iterate over the per-frame audits in ``report``."""
    return iter(report.frame_audits)


def _audit_to_dict(audit: FrameAudit) -> dict[str, object]:
    return {
        "index": audit.key.index,
        "arbitration_id": audit.key.arbitration_id,
        "is_fd": audit.key.is_fd,
        "verdict": audit.verdict.value,
        "declared_byte_count": audit.declared_byte_count,
        "raw_dlc": audit.raw_dlc,
        "retained_bytes": audit.retained_bytes,
        "unverified_bytes": audit.unverified_bytes,
        "conflict": (
            None
            if audit.conflict is None
            else {
                "reason": audit.conflict.reason,
                "byte_index": audit.conflict.byte_index,
                "detail": audit.conflict.detail,
            }
        ),
        "notes": list(audit.notes),
    }


def report_as_json(report: AuditReport) -> dict[str, object]:
    """Serialize an :class:`AuditReport` to plain JSON-compatible data."""
    return {
        "prefix_bytes": report.prefix_bytes,
        "counts": {verdict.value: count for verdict, count in report.counts.items()},
        "frames": [_audit_to_dict(audit) for audit in report.frame_audits],
    }


def format_frame_audit(audit: FrameAudit) -> str:
    """Render one frame audit as human-readable text."""
    key = audit.key
    line = (
        f"frame {key.index:>4d}  id={key.arbitration_id:#010x} "
        f"{'FD' if key.is_fd else 'CC'}  raw_dlc={audit.raw_dlc} "
        f"declared={audit.declared_byte_count}B "
        f"retained={audit.retained_bytes}B  "
        f"verdict={audit.verdict.value}"
    )
    details: list[str] = []
    if audit.conflict is not None:
        location = (
            f" at byte {audit.conflict.byte_index}" if audit.conflict.byte_index is not None else ""
        )
        details.append(f"  conflict[{audit.conflict.reason}]{location}: {audit.conflict.detail}")
    for note in audit.notes:
        details.append(f"  note: {note}")
    return "\n".join([line, *details]) if details else line


def run_sample(args: argparse.Namespace) -> int:
    count = write_prefix_sample(args.input, args.output, args.prefix)
    print(
        f"wrote {count} prefix-only frame(s) retaining the first "
        f"{args.prefix} byte(s) each to {args.output}"
    )
    return 0


def run_audit(args: argparse.Namespace) -> int:
    report = audit_files(args.sample, args.reference, args.prefix)
    if args.json:
        print(json.dumps(report_as_json(report), indent=2, sort_keys=True))
    else:
        for audit in report.frame_audits:
            print(format_frame_audit(audit))
        print()
        print(report.summary())

    if report.counts[Verdict.CONFLICT]:
        return 1
    if args.require_identical and report.counts[Verdict.NO_EVIDENCE]:
        return 1
    return 0


def run_explain(args: argparse.Namespace) -> int:
    if args.as_length:
        print(explain_length(args.value))
    else:
        print(explain_dlc(args.value, is_fd=args.fd))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="can.prefix_audit",
        description=(
            "Offline audit of captures that retain only the first N bytes "
            "of each frame, distinguishing identical frames, evidenced "
            "conflicts and frames for which the truncated bytes give no "
            "evidence."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser(
        "sample", help="create a prefix-only capture from a full capture"
    )
    sample_parser.add_argument("input", help="full reference capture")
    sample_parser.add_argument("output", help="destination BLF file")
    sample_parser.add_argument(
        "--prefix", type=int, default=PREFIX_BYTES, help="bytes retained per frame"
    )
    sample_parser.set_defaults(handler=run_sample)

    audit_parser = subparsers.add_parser(
        "audit", help="audit a prefix-only capture against a full capture"
    )
    audit_parser.add_argument("sample", help="prefix-only capture")
    audit_parser.add_argument("reference", help="full reference capture")
    audit_parser.add_argument(
        "--prefix", type=int, default=PREFIX_BYTES, help="bytes retained per frame"
    )
    audit_parser.add_argument("--json", action="store_true", help="emit JSON")
    audit_parser.add_argument(
        "--require-identical",
        action="store_true",
        help="also fail when frames can only be classified as no_evidence",
    )
    audit_parser.set_defaults(handler=run_audit)

    explain_parser = subparsers.add_parser(
        "explain", help="explain a raw DLC code or a declared byte count"
    )
    explain_parser.add_argument("value", type=int, help="raw DLC code or byte count")
    explain_parser.add_argument(
        "--fd", action="store_true", help="interpret the value as a CAN FD DLC code"
    )
    explain_parser.add_argument(
        "--as-length",
        action="store_true",
        help="interpret the value as a declared byte count instead of a DLC code",
    )
    explain_parser.set_defaults(handler=run_explain)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point. Exits non-zero when evidenced conflicts are found."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
