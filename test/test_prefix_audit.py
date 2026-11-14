#!/usr/bin/env python

"""Regression tests for the offline prefix-only capture audit.

The business rule under test: a capture that only retains the first
``PREFIX_BYTES`` bytes of every frame must not be declared "equal" to the full
capture just because the retained prefixes match. Three outcomes are possible:

* fully evidenced equality            -> ``IDENTICAL``
* a difference inside the evidence    -> ``CONFLICT`` (with byte position)
* equality only observable up to the
  cut-off point                       -> ``NO_EVIDENCE``

A difference located behind the cut-off point must never be reported as a
discovered conflict.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import can
from can.prefix_audit import (
    PREFIX_BYTES,
    Verdict,
    audit_files,
    audit_frame,
    audit_messages,
    explain_dlc,
    explain_length,
    format_frame_audit,
    frame_key,
    main,
    raw_dlc_of,
    report_as_json,
    sample_message,
    write_prefix_sample,
)

DATA_DIR = Path(__file__).parent / "data"


def make_fd_message(byte_count: int, *, arbitration_id: int = 0x100) -> can.Message:
    return can.Message(
        arbitration_id=arbitration_id,
        is_extended_id=False,
        is_fd=True,
        dlc=byte_count,
        data=bytes((index * 7) & 0xFF for index in range(byte_count)),
    )


class TestDlcSemantics:
    """The raw 4-bit DLC and the declared byte count are different things."""

    def test_fd_code_nine_encodes_twelve_bytes(self):
        assert explain_dlc(9, is_fd=True) == ("raw DLC 9 (FD length code) encodes 12 payload bytes")

    def test_classic_code_eight_encodes_eight_bytes(self):
        assert explain_dlc(8, is_fd=False) == ("raw DLC 8 encodes 8 payload bytes")

    def test_nine_declared_bytes_is_not_code_nine(self):
        text = explain_length(9)
        assert "9 declared payload bytes" in text
        assert "raw DLC code 9" in text
        assert "not a valid CAN FD length" in text

    def test_twelve_declared_bytes_map_to_code_nine(self):
        assert explain_length(12) == (
            "12 declared payload bytes are encoded on the wire by raw DLC code 9"
        )

    def test_raw_dlc_of_translates_byte_count_to_wire_code(self):
        assert raw_dlc_of(can.Message(dlc=8, data=range(8))) == 8
        assert raw_dlc_of(make_fd_message(12)) == 9
        assert raw_dlc_of(make_fd_message(64)) == 15

    def test_explain_rejects_out_of_range_values(self):
        with pytest.raises(ValueError):
            explain_dlc(16, is_fd=True)
        with pytest.raises(ValueError):
            explain_length(65)


class TestSampling:
    def test_retains_only_prefix_but_keeps_declared_byte_count(self):
        original = make_fd_message(12)
        sampled = sample_message(original)
        assert len(sampled.data) == PREFIX_BYTES
        assert bytes(sampled.data) == bytes(original.data[:PREFIX_BYTES])
        # the declared length survives even though the payload is gone
        assert sampled.dlc == 12
        assert sampled.is_fd == original.is_fd
        assert sampled.arbitration_id == original.arbitration_id

    def test_custom_prefix_and_zero_prefix(self):
        original = make_fd_message(64)
        assert len(sample_message(original, prefix_bytes=4).data) == 4
        empty = sample_message(original, prefix_bytes=0)
        assert len(empty.data) == 0
        assert empty.dlc == 64

    def test_short_frame_is_kept_whole(self):
        original = make_fd_message(4)
        sampled = sample_message(original)
        assert len(sampled.data) == 4
        assert sampled.dlc == 4


class TestFrameVerdicts:
    def test_identical_when_sample_covers_full_payload(self):
        reference = can.Message(arbitration_id=0x200, is_extended_id=False, dlc=8, data=range(8))
        audit = audit_frame(sample_message(reference), reference)
        assert audit.verdict is Verdict.IDENTICAL
        assert audit.conflict is None
        assert audit.unverified_bytes == 0

    def test_identical_for_short_fd_frame(self):
        reference = make_fd_message(4)
        audit = audit_frame(sample_message(reference), reference)
        assert audit.verdict is Verdict.IDENTICAL

    def test_conflict_reports_position_inside_prefix(self):
        reference = make_fd_message(12)
        sampled = sample_message(reference)
        sampled.data[3] ^= 0xFF
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.CONFLICT
        assert audit.first_conflict_byte == 3
        assert audit.conflict is not None
        assert audit.conflict.reason == "payload_byte"

    def test_difference_at_eighth_byte_is_still_evidenced(self):
        # byte indices 0..7 are inside the 8-byte evidence window
        reference = make_fd_message(12)
        sampled = sample_message(reference)
        sampled.data[7] ^= 0x01
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.CONFLICT
        assert audit.first_conflict_byte == 7

    def test_tail_difference_behind_cutoff_is_no_evidence(self):
        # The reference changes at byte 10, but the sample ends after byte 7:
        # there is no evidence either way, so it must NOT be a conflict.
        sampled = sample_message(make_fd_message(12))
        diverging_reference = make_fd_message(12)
        diverging_reference.data[10] ^= 0xAA
        assert bytes(sampled.data) == bytes(diverging_reference.data[:8])

        audit = audit_frame(sampled, diverging_reference)
        assert audit.verdict is Verdict.NO_EVIDENCE
        assert audit.conflict is None
        assert audit.unverified_bytes == 4

    def test_sixty_four_byte_frame_leaves_fifty_six_unverified(self):
        reference = make_fd_message(64)
        audit = audit_frame(sample_message(reference), reference)
        assert audit.verdict is Verdict.NO_EVIDENCE
        assert audit.retained_bytes == 8
        assert audit.unverified_bytes == 56

    def test_difference_at_cutoff_plus_one_is_not_a_conflict(self):
        reference = make_fd_message(16)
        sampled = sample_message(reference)
        reference.data[8] ^= 0x55
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.NO_EVIDENCE
        assert audit.conflict is None

    def test_prefix_conflict_wins_even_when_tail_also_differs(self):
        reference = make_fd_message(16)
        sampled = sample_message(reference)
        sampled.data[2] = 0x77
        reference.data[15] = 0x33
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.CONFLICT
        assert audit.first_conflict_byte == 2

    def test_declared_byte_count_mismatch_is_a_conflict(self):
        reference = make_fd_message(12)
        sampled = sample_message(reference)
        sampled.dlc = 16
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.CONFLICT
        assert audit.conflict is not None
        assert audit.conflict.reason == "declared_byte_count"

    def test_identity_mismatch_is_a_conflict(self):
        reference = make_fd_message(12)
        sampled = sample_message(reference)
        sampled.arbitration_id = 0x101
        audit = audit_frame(sampled, reference)
        assert audit.verdict is Verdict.CONFLICT
        assert audit.conflict is not None
        assert audit.conflict.reason == "arbitration_id"

    def test_remote_frame_without_payload(self):
        reference = can.Message(
            arbitration_id=0x300,
            is_extended_id=False,
            is_remote_frame=True,
            dlc=8,
        )
        sampled = sample_message(reference)
        assert audit_frame(sampled, reference).verdict is Verdict.IDENTICAL

    def test_frame_key_carries_identity(self):
        message = make_fd_message(12, arbitration_id=0x42)
        key = frame_key(7, message)
        assert key.index == 7
        assert key.arbitration_id == 0x42
        assert key.is_fd is True


class TestMessageSequenceAudit:
    def test_counts_three_verdicts(self):
        identical = can.Message(
            arbitration_id=0x1, is_extended_id=False, dlc=4, data=b"\x01\x02\x03\x04"
        )
        truncated = make_fd_message(12, arbitration_id=0x2)
        conflicting = can.Message(arbitration_id=0x3, is_extended_id=False, dlc=8, data=range(8))
        references = [identical, truncated, conflicting]
        samples = [
            sample_message(identical),
            sample_message(truncated),
            sample_message(conflicting),
        ]
        samples[2].data[0] ^= 0xFF

        report = audit_messages(samples, references)
        assert report.counts == {
            Verdict.IDENTICAL: 1,
            Verdict.NO_EVIDENCE: 1,
            Verdict.CONFLICT: 1,
        }
        assert len(report.conflicts) == 1
        assert len(report.no_evidence) == 1
        assert "no evidence  : 1" in report.summary()

    def test_extra_and_missing_frames_are_evidenced_conflicts(self):
        reference = make_fd_message(12)
        report = audit_messages([sample_message(reference), reference], [reference])
        assert len(report.frame_audits) == 2
        assert report.frame_audits[1].verdict is Verdict.CONFLICT
        assert report.frame_audits[1].conflict.reason == "extra_frame"

        report = audit_messages([], [reference])
        assert report.frame_audits[0].verdict is Verdict.CONFLICT
        assert report.frame_audits[0].conflict.reason == "missing_frame"

    def test_json_serialization_shape(self):
        report = audit_messages([sample_message(make_fd_message(12))], [make_fd_message(12)])
        payload = report_as_json(report)
        assert payload["prefix_bytes"] == PREFIX_BYTES
        assert payload["counts"]["no_evidence"] == 1
        frame = payload["frames"][0]
        assert frame["verdict"] == "no_evidence"
        assert frame["raw_dlc"] == 9
        assert frame["declared_byte_count"] == 12
        json.dumps(payload)  # must be JSON serializable

    def test_format_frame_audit_mentions_verdict_and_cutoff(self):
        report = audit_messages([sample_message(make_fd_message(12))], [make_fd_message(12)])
        text = format_frame_audit(report.frame_audits[0])
        assert "verdict=no_evidence" in text
        assert "raw_dlc=9" in text


class TestFileLevelAudit:
    def _write_full_capture(self, path: Path) -> None:
        messages = [
            make_fd_message(12, arbitration_id=0x100),
            can.Message(arbitration_id=0x200, is_extended_id=False, dlc=8, data=range(8)),
            make_fd_message(64, arbitration_id=0x300),
        ]
        with can.BLFWriter(path) as writer:
            for message in messages:
                writer.on_message_received(message)

    def test_sample_file_preserves_declared_length(self, tmp_path):
        full = tmp_path / "full.blf"
        sample = tmp_path / "sample.blf"
        self._write_full_capture(full)

        written = write_prefix_sample(full, sample)
        assert written == 3

        with can.BLFReader(sample) as reader:
            readback = list(reader)
        assert [message.dlc for message in readback] == [12, 8, 64]
        assert [len(message.data) for message in readback] == [8, 8, 8]
        assert raw_dlc_of(readback[0]) == 9

    def test_audit_files_classifies_each_frame(self, tmp_path):
        full = tmp_path / "full.blf"
        sample = tmp_path / "sample.blf"
        self._write_full_capture(full)
        write_prefix_sample(full, sample)

        report = audit_files(sample, full)
        verdicts = [audit.verdict for audit in report.frame_audits]
        assert verdicts == [
            Verdict.NO_EVIDENCE,
            Verdict.IDENTICAL,
            Verdict.NO_EVIDENCE,
        ]

    def test_audit_files_detects_evidenced_conflict(self, tmp_path):
        full = tmp_path / "full.blf"
        sample = tmp_path / "sample.blf"
        self._write_full_capture(full)
        write_prefix_sample(full, sample)

        conflict_capture = tmp_path / "conflict.blf"
        conflicting = make_fd_message(12, arbitration_id=0x100)
        conflicting.data[1] ^= 0x0F
        with can.BLFWriter(conflict_capture) as writer:
            writer.on_message_received(conflicting)

        report = audit_files(sample, conflict_capture)
        assert report.frame_audits[0].verdict is Verdict.CONFLICT
        assert report.frame_audits[0].first_conflict_byte == 1

    def test_real_fd_capture_is_no_evidence_after_sampling(self, tmp_path):
        source = DATA_DIR / "test_CanFdMessage.blf"
        sample = tmp_path / "sampled.blf"
        write_prefix_sample(source, sample)
        report = audit_files(sample, source)
        assert report.frame_audits, "expected frames from the real capture"
        assert all(
            audit.verdict in (Verdict.NO_EVIDENCE, Verdict.IDENTICAL)
            for audit in report.frame_audits
        )
        assert any(audit.verdict is Verdict.NO_EVIDENCE for audit in report.frame_audits)


class TestCommandLine:
    def test_module_help_is_callable(self):
        result = subprocess.run(
            [sys.executable, "-m", "can.prefix_audit", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "audit" in result.stdout

    def test_explain_subcommand(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["explain", "9", "--fd"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "raw DLC 9" in out
        assert "12 payload bytes" in out

    def test_explain_length_subcommand(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["explain", "12", "--as-length"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "raw DLC code 9" in out

    def _capture_paths(self, tmp_path):
        full = tmp_path / "full.blf"
        sample = tmp_path / "sample.blf"
        with can.BLFWriter(full) as writer:
            writer.on_message_received(make_fd_message(12))
        write_prefix_sample(full, sample)
        return full, sample

    def test_clean_audit_exits_zero(self, tmp_path, capsys):
        full, sample = self._capture_paths(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["audit", str(sample), str(full)])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "no evidence  : 1" in out

    def test_conflict_audit_exits_nonzero(self, tmp_path):
        _full, sample = self._capture_paths(tmp_path)
        conflict = tmp_path / "conflict.blf"
        message = make_fd_message(12)
        message.data[0] ^= 0xFF
        with can.BLFWriter(conflict) as writer:
            writer.on_message_received(message)
        with pytest.raises(SystemExit) as exc_info:
            main(["audit", str(sample), str(conflict)])
        assert exc_info.value.code == 1

    def test_require_identical_fails_on_no_evidence(self, tmp_path):
        full, sample = self._capture_paths(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "audit",
                    str(sample),
                    str(full),
                    "--require-identical",
                ]
            )
        assert exc_info.value.code == 1

    def test_sample_then_audit_subcommands(self, tmp_path, capsys):
        full = tmp_path / "full.blf"
        sample = tmp_path / "sample.blf"
        with can.BLFWriter(full) as writer:
            writer.on_message_received(make_fd_message(12))
        with pytest.raises(SystemExit) as exc_info:
            main(["sample", str(full), str(sample)])
        assert exc_info.value.code == 0
        assert sample.exists()
        capsys.readouterr()  # discard the sample command's output
        with pytest.raises(SystemExit) as exc_info:
            main(["audit", str(sample), str(full), "--json"])
        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["frames"][0]["verdict"] == "no_evidence"
        assert payload["frames"][0]["conflict"] is None
